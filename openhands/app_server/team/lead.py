"""Lead reasoning: triage and decide.

The lead's "thinking" is a single LLM call that returns a small structured JSON
object which drives a store transition. The call is injected (``think_fn``) so
tests can supply deterministic output; the default uses litellm with the
configured lead model (cheaper than the engineering model — design §8).

The lead is deliberately a *decision* maker here, not an executor: triage/decide
produce a role + rationale + risk/priority, and the sweep applies the resulting
store transitions. Actual engineering happens in a spawned member conversation
(P6). Structured output is validated before any state change, so a malformed
reply never corrupts the machine.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from openhands.app_server.team.models import Issue, Priority, Risk
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / 'prompts'
_env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)), autoescape=False)

# think_fn(prompt) -> raw model text (expected to contain a JSON object).
ThinkFn = Callable[[str], str]


def _default_think(model: str) -> ThinkFn:
    def think(prompt: str) -> str:
        import litellm

        resp = litellm.completion(
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=1024,
            temperature=0.0,
        )
        return resp.choices[0].message.content or ''

    return think


def _parse_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from model output; raise on failure."""
    text = text.strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end < start:
        raise ValueError(f'no JSON object in lead output: {text[:200]!r}')
    return json.loads(text[start : end + 1])


class LeadBrain:
    def __init__(
        self,
        store: TeamStore,
        *,
        model: str,
        round_cap: int = 3,
        think_fn: ThinkFn | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.round_cap = round_cap
        self._think = think_fn or _default_think(model)

    def _issue_number(self, issue: Issue) -> str | None:
        if issue.github_ref and '#' in issue.github_ref:
            return issue.github_ref.split('#')[-1]
        return None

    def _members(self) -> list[dict]:
        out = []
        for a in self.store.list_agents(enabled_only=True):
            if a.role in ('grand_leader', 'lead'):
                continue
            skills = json.loads(a.skills_json) if a.skills_json else []
            out.append(
                {
                    'role': a.role,
                    'agent_kind': a.agent_kind.value if a.agent_kind else 'openhands',
                    'skills': skills,
                }
            )
        return out

    # -- triage ------------------------------------------------------------
    def triage(self, issue: Issue) -> dict[str, Any]:
        """Return a validated triage decision:
        {assignee_role, rationale, risk: Risk|None, priority: Priority}.
        Raises ValueError on malformed output or an unknown assignee."""
        members = self._members()
        member_roles = {m['role'] for m in members}
        prompt = _env.get_template('triage.j2').render(
            repo=issue.repo,
            number=self._issue_number(issue),
            title=issue.title,
            body=issue.body,
            members=members,
        )
        raw = self._think(prompt)
        data = _parse_json(raw)

        assignee = data.get('assignee_role')
        if assignee not in member_roles:
            raise ValueError(
                f'lead triaged to unknown role {assignee!r} '
                f'(available: {sorted(member_roles)})'
            )
        priority = Priority(data.get('priority', 'normal'))
        risk = Risk.HIGH if data.get('risk') == 'high' else None
        return {
            'assignee_role': assignee,
            'rationale': data.get('rationale', ''),
            'risk': risk,
            'priority': priority,
        }

    # -- decide ------------------------------------------------------------
    def decide(self, issue: Issue) -> dict[str, Any]:
        """Return a validated decision:
        {action: 'commit'|'pushback'|'reassign', rationale, new_assignee?}.
        Forces commit/reassign at the round cap."""
        forced = issue.round_count >= self.round_cap
        comments = [
            {'author_role': c.author_role, 'body': c.body}
            for c in self.store.list_comments(issue.id)
        ]
        prompt = _env.get_template('decide.j2').render(
            repo=issue.repo,
            number=self._issue_number(issue),
            title=issue.title,
            assignee_role=issue.assignee_role,
            round_count=issue.round_count,
            round_cap=self.round_cap,
            comments=comments,
            forced=forced,
        )
        raw = self._think(prompt)
        data = _parse_json(raw)
        action = data.get('action')
        if action not in ('commit', 'pushback', 'reassign'):
            raise ValueError(f'lead returned unknown action {action!r}')
        # Enforce convergence: never allow pushback past the cap.
        if forced and action == 'pushback':
            logger.info(
                'lead pushback past round cap on %s -> forcing commit', issue.id
            )
            action = 'commit'
        return {
            'action': action,
            'rationale': data.get('rationale', ''),
            'new_assignee': data.get('new_assignee'),
        }

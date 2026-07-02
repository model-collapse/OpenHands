"""Reactive router — event-driven advancement (design §6b).

Complements the lead sweep (which triages/decides). One pass:

- ``assigned`` (waiting on the assignee) -> spawn the assignee to evaluate the
  issue *against the real repo*; accept -> ``discussing`` for the lead to commit,
  concern -> ``discussing`` with the concern recorded (the lead decides next
  sweep). The assignee's reasoning is a spawned conversation (kind-aware).
- ``committed`` -> spawn the engineer via the full-auto path (the proven
  label->conversation->PR loop) and move to ``in_progress``.
- ``in_progress`` -> poll the spawned conversation's execution_status; finished
  -> ``in_review`` (a PR is expected), error -> escalate to the grand leader.
- grand-leader guidance/halt: a ``halt`` directive stops the issue immediately;
  other guidance is left for the lead (routed by the sweep/store).

Execution and evaluation both go through the injected ``AgentSpawner`` (config
from the member's row) and an injected ``status_fn`` (conversation
execution_status), so this is unit-testable without a live backend.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from openhands.app_server.team.lead import _parse_json
from openhands.app_server.team.models import (
    ROLE_LEAD,
    Provenance,
    State,
)
from openhands.app_server.team.spawn import AgentSpawner
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / 'prompts')), autoescape=False
)

# status_fn(conversation_id) -> execution status string
# ('idle'|'running'|'finished'|'error'|None-if-unknown).
StatusFn = Callable[[str], Awaitable[str | None]]

# eval_fn(prompt) -> raw assignee model text (assignee's structured decision).
# Kept separate from the spawner: in production the assignee's evaluation IS a
# spawned member conversation, but its verdict is read back as structured text.
EvalFn = Callable[[str], Awaitable[str]]

_HALT_TOKENS = ('halt', 'stop the team', '/halt')


class ReactiveRouter:
    def __init__(
        self,
        store: TeamStore,
        spawner: AgentSpawner,
        *,
        engineer_status_fn: StatusFn,
        assignee_eval_fn: EvalFn,
        commit_instruction: str = (
            'Implement this issue end-to-end: make the change on a new branch, '
            'run the tests/build, open a pull request that closes it, and comment '
            'a summary on the issue. Use the configured GitHub credentials.'
        ),
    ) -> None:
        self.store = store
        self.spawner = spawner
        self._status = engineer_status_fn
        self._assignee_eval = assignee_eval_fn
        self.commit_instruction = commit_instruction

    async def run_once(self) -> int:
        actions = 0
        for issue in self.store.list_issues():
            try:
                if issue.state == State.ASSIGNED:
                    if await self._evaluate_assignment(issue):
                        actions += 1
                elif issue.state == State.COMMITTED:
                    if await self._execute(issue):
                        actions += 1
                elif issue.state == State.IN_PROGRESS:
                    if await self._check_progress(issue):
                        actions += 1
            except Exception as e:  # noqa: BLE001
                logger.error('reactive: error on issue %s: %s', issue.id, e)
        return actions

    # -- assignee evaluation (grounded in repo) ---------------------------
    async def _evaluate_assignment(self, issue) -> bool:
        # Only evaluate once per assignment: skip if the assignee already spoke.
        comments = self.store.list_comments(issue.id)
        if any(c.author_role == issue.assignee_role for c in comments):
            return False
        prompt = _env.get_template('assignee_eval.j2').render(
            role=issue.assignee_role,
            repo=issue.repo,
            number=(issue.github_ref.split('#')[-1] if issue.github_ref else None),
            title=issue.title,
            body=issue.body,
            lead_note=next(
                (c.body for c in reversed(comments) if c.author_role == ROLE_LEAD),
                None,
            ),
        )
        raw = await self._assignee_eval(prompt)
        data = _parse_json(raw)
        decision = data.get('decision')
        notes = data.get('notes', '')
        self.store.add_comment(
            issue_id=issue.id,
            author_role=issue.assignee_role,
            provenance=Provenance.AGENT,
            body=notes,
        )
        # Either way the issue moves to discussing; the lead decides next sweep
        # (accept -> likely commit; concern -> the lead weighs it). This keeps a
        # single convergence authority (the lead) rather than auto-committing on
        # the assignee's say-so.
        self.store.transition(
            issue.id,
            to_state=State.DISCUSSING,
            actor_role=issue.assignee_role,
            reason=f'assignee {decision}',
        )
        # Clear the stuck marker set at assignment; it's now the lead's turn.
        self.store.mark_stuck(issue.id, None, issue.assignee_role)
        logger.info(
            'reactive: assignee %s %s on %s', issue.assignee_role, decision, issue.id
        )
        return True

    # -- execution (committed -> engineer) --------------------------------
    async def _execute(self, issue) -> bool:
        cid = await self.spawner.spawn(
            role=issue.assignee_role or ROLE_LEAD,
            issue=issue,
            instruction=self.commit_instruction,
        )
        if cid is None:
            self.store.transition(
                issue.id,
                to_state=State.ASSIGNED,
                actor_role=ROLE_LEAD,
                reason='spawn failed; re-queueing',
            )
            return False
        self.store.transition(
            issue.id,
            to_state=State.IN_PROGRESS,
            actor_role=issue.assignee_role or ROLE_LEAD,
            reason='engineer started',
            conversation_id=cid,
        )
        self.store.mark_stuck(issue.id, _now_iso(), issue.assignee_role or ROLE_LEAD)
        return True

    # -- progress polling -------------------------------------------------
    async def _check_progress(self, issue) -> bool:
        cid = self.store.latest_spawn_conversation(issue.id)
        if not cid:
            return False
        status = await self._status(cid)
        if status == 'finished':
            self.store.transition(
                issue.id,
                to_state=State.IN_REVIEW,
                actor_role=issue.assignee_role or ROLE_LEAD,
                reason='engineer finished; PR expected',
                conversation_id=cid,
            )
            self.store.mark_stuck(issue.id, None, issue.assignee_role or ROLE_LEAD)
            return True
        if status == 'error':
            from openhands.app_server.team.models import ROLE_GRAND_LEADER

            self.store.inbox_add(
                ROLE_GRAND_LEADER, issue.id, 'engineer conversation errored'
            )
            self.store.record_nonstate_event(
                actor_role=issue.assignee_role or ROLE_LEAD,
                kind=self._error_event_kind(),
                issue_id=issue.id,
                conversation_id=cid,
                detail={'status': 'error'},
            )
            return True
        return False  # still running/idle

    @staticmethod
    def _error_event_kind():
        from openhands.app_server.team.models import EventKind

        return EventKind.STUCK

    # -- grand-leader halt -------------------------------------------------
    def apply_halt(self, issue_id: str, actor_role: str, reason: str) -> None:
        """Immediately halt an issue (grand-leader escape hatch, design §9)."""
        from openhands.app_server.team.models import EventKind

        self.store.transition(
            issue_id,
            to_state=State.HALTED,
            actor_role=actor_role,
            reason=reason,
        )
        self.store.record_nonstate_event(
            actor_role=actor_role,
            kind=EventKind.HALT,
            issue_id=issue_id,
            detail={'reason': reason},
        )

    @staticmethod
    def is_halt_directive(text: str | None) -> bool:
        if not text:
            return False
        low = text.lower()
        return any(tok in low for tok in _HALT_TOKENS)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

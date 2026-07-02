"""Kind-aware agent spawner for the AI team.

Turns a member's stored ``agents`` row (the single source of truth) into a
started conversation via ``POST /api/v1/app-conversations``. The member's agent
config rides the request's ``agent_settings_override`` field (added in P2a), so
one caller can spawn OpenHands *and* ACP members concurrently. Nothing is
invented at spawn time — the spawn is a pure function of the row + the issue, so
re-spawning the same row is reproducible.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from openhands.app_server.team.models import Agent, AgentKind, EventKind, Issue
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)


class AgentSpawner:
    def __init__(self, self_url: str, store: TeamStore) -> None:
        self.self_url = self_url.rstrip('/')
        self.store = store

    def _agent_settings_override(self, agent: Agent) -> dict[str, Any] | None:
        """Build the per-conversation agent-settings override from the row.

        ``launch_config_json`` is the authoritative agent config; for ACP it
        carries ``agent_kind='acp'``, ``acp_server``, ``acp_model``. For an
        OpenHands member we only need to steer the model (the default agent kind
        already applies), so a bare override is usually unnecessary.
        """
        cfg: dict[str, Any] = {}
        if agent.launch_config_json:
            try:
                cfg = json.loads(agent.launch_config_json)
            except json.JSONDecodeError:
                logger.warning(
                    'agent %s has invalid launch_config_json; ignoring', agent.role
                )
                cfg = {}
        if agent.agent_kind == AgentKind.ACP:
            cfg.setdefault('agent_kind', 'acp')
            if agent.acp_server:
                cfg.setdefault('acp_server', agent.acp_server)
            if agent.llm_model:
                cfg.setdefault('acp_model', agent.llm_model)
        return cfg or None

    async def spawn(
        self,
        *,
        role: str,
        issue: Issue,
        instruction: str,
        run: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> str | None:
        """Start a conversation for ``role`` on ``issue``. Returns conversation id.

        Config is derived solely from the member's ``agents`` row. Records a
        ``spawn`` event linking the conversation id to the issue.
        """
        agent = self.store.get_agent(role)
        if agent is None:
            logger.error('spawn: no such agent role %r', role)
            return None

        payload: dict[str, Any] = {
            'selected_repository': issue.repo,
            'initial_message': {
                'role': 'user',
                'content': [{'type': 'text', 'text': instruction}],
                'run': run,
            },
        }
        # OpenHands members: steer the model directly. ACP members: full override.
        if agent.agent_kind == AgentKind.OPENHANDS and agent.llm_model:
            payload['llm_model'] = agent.llm_model
        override = self._agent_settings_override(agent)
        if override:
            payload['agent_settings_override'] = override

        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            resp = await client.post(
                f'{self.self_url}/api/v1/app-conversations',
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            cid = data.get('app_conversation_id') or data.get('id')
        except Exception as e:  # noqa: BLE001
            logger.error('spawn failed for role %s on issue %s: %s', role, issue.id, e)
            return None
        finally:
            if owns_client:
                await client.aclose()

        self.store.record_nonstate_event(
            actor_role=role,
            kind=EventKind.SPAWN,
            issue_id=issue.id,
            conversation_id=cid,
            detail={'agent_kind': agent.agent_kind.value if agent.agent_kind else None},
        )
        logger.info('spawned %s conversation %s for issue %s', role, cid, issue.id)
        return cid

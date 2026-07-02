"""Team bootstrap (design §2b): grow the roster instead of declaring it.

On first enable the system creates exactly one agent — the Team Lead — with the
GitHub identity. The grand leader then converses with the lead (on a dedicated
``internal`` issue) and the lead forms the rest of the team by writing ``agents``
rows. This module owns lead creation and the mechanical part of member
registration; the *decision* of who to hire is the lead's (P5), driven by the
``formation.j2`` prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    ROLE_LEAD,
    ActorKind,
    Agent,
    AgentKind,
)
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)


def ensure_grand_leader(
    store: TeamStore, *, display_name: str = 'Grand Leader'
) -> Agent:
    """Ensure the human supervisor role exists (influence-only, no agent_kind)."""
    existing = store.get_agent(ROLE_GRAND_LEADER)
    if existing:
        return existing
    return store.upsert_agent(
        Agent(
            role=ROLE_GRAND_LEADER,
            display_name=display_name,
            actor_kind=ActorKind.HUMAN,
        )
    )


def ensure_lead(
    store: TeamStore,
    *,
    github_identity: str,
    llm_model: str | None = None,
    display_name: str = 'Team Lead',
) -> Agent:
    """Ensure the Team Lead exists. The lead owns the GitHub identity and is an
    OpenHands agent (it needs the full tool/skill surface to triage + manage)."""
    existing = store.get_agent(ROLE_LEAD)
    if existing:
        return existing
    logger.info('bootstrap: creating Team Lead (github=%s)', github_identity)
    return store.upsert_agent(
        Agent(
            role=ROLE_LEAD,
            display_name=display_name,
            actor_kind=ActorKind.AGENT,
            agent_kind=AgentKind.OPENHANDS,
            github_identity=github_identity,
            llm_model=llm_model,
        )
    )


def bootstrap_team(
    store: TeamStore, *, github_identity: str, lead_model: str | None = None
) -> Agent:
    """Idempotent: ensure grand_leader + lead exist. Returns the lead."""
    ensure_grand_leader(store)
    return ensure_lead(store, github_identity=github_identity, llm_model=lead_model)


def register_member(store: TeamStore, spec: dict[str, Any]) -> Agent:
    """Write one member row from a lead-produced formation spec.

    ``spec`` shape (matches ``formation.j2`` output):
        {role, agent_kind, acp_server?, llm_model?, skills?, display_name?,
         launch_config?}
    ``created_by_role='lead'`` marks it as lead-formed (audited as FORM_MEMBER).
    """
    role = spec['role']
    agent_kind = AgentKind(spec.get('agent_kind', 'openhands'))
    launch_config = spec.get('launch_config')
    # For ACP members, fold acp_server/model into launch_config so spawn.py has
    # a single authoritative blob.
    if agent_kind == AgentKind.ACP:
        lc: dict[str, Any] = dict(launch_config or {})
        lc.setdefault('agent_kind', 'acp')
        if spec.get('acp_server'):
            lc.setdefault('acp_server', spec['acp_server'])
        if spec.get('llm_model'):
            lc.setdefault('acp_model', spec['llm_model'])
        launch_config = lc

    return store.upsert_agent(
        Agent(
            role=role,
            display_name=spec.get('display_name', role),
            actor_kind=ActorKind.AGENT,
            agent_kind=agent_kind,
            acp_server=spec.get('acp_server'),
            llm_model=spec.get('llm_model'),
            launch_config_json=json.dumps(launch_config) if launch_config else None,
            skills_json=json.dumps(spec['skills']) if spec.get('skills') else None,
            created_by_role=ROLE_LEAD,
        )
    )


def register_members(store: TeamStore, specs: list[dict[str, Any]]) -> list[Agent]:
    """Register a batch of members (a full formation reply)."""
    return [register_member(store, s) for s in specs]

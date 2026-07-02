"""Read-only cockpit for the AI team (design §10).

Mounted under ``/api/v1/team``. The supervisor observes and drills into
reasoning; there are NO state-mutation endpoints (the human acts via GitHub —
design §9). The centerpiece is ``/issues/{id}``: an event timeline where every
event deep-links to the conversation that produced it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from openhands.app_server.team.config import TeamConfig
from openhands.app_server.team.models import TERMINAL_STATES, State
from openhands.app_server.team.service import get_team_service

router = APIRouter(prefix='/team', tags=['AI Team'])

# States that should surface to the grand leader by default.
_ATTENTION_STATES = {State.AWAITING_GL, State.HALTED}


def _issue_dict(issue) -> dict:
    d = asdict(issue)
    # enums -> values for JSON
    d['origin'] = issue.origin.value
    d['state'] = issue.state.value
    d['priority'] = issue.priority.value
    d['risk'] = issue.risk.value if issue.risk else None
    return d


def _is_stuck(issue, threshold: int) -> bool:
    if not issue.stuck_since:
        return False
    try:
        since = datetime.fromisoformat(issue.stuck_since)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - since).total_seconds() > threshold


@router.get('/health')
async def health() -> dict:
    svc = get_team_service()
    cfg: TeamConfig = svc.config
    agents = svc.store.list_agents()
    return {
        'status': 'ok',
        'enabled': cfg.enabled,
        'agents': len(agents),
        'lead_configured': any(a.role == 'lead' for a in agents),
    }


@router.get('/dashboard')
async def dashboard(filter: str = 'attention') -> dict:
    """Issues grouped by state. ``filter=attention`` (default) shows only what
    needs the grand leader (awaiting_gl / halted / stuck); ``filter=all`` shows
    everything grouped by state; ``filter=open`` hides terminal states.
    """
    svc = get_team_service()
    issues = svc.store.list_issues()
    threshold = svc.config.stuck_threshold

    if filter == 'attention':
        issues = [
            i for i in issues if i.state in _ATTENTION_STATES or _is_stuck(i, threshold)
        ]
    elif filter == 'open':
        issues = [i for i in issues if i.state not in TERMINAL_STATES]

    grouped: dict[str, list[dict]] = {}
    for i in issues:
        grouped.setdefault(i.state.value, []).append(_issue_dict(i))

    return {
        'filter': filter,
        'total': sum(len(v) for v in grouped.values()),
        'by_state': grouped,
    }


@router.get('/issues')
async def list_issues(state: str | None = None, assignee: str | None = None) -> dict:
    svc = get_team_service()
    states = None
    if state:
        try:
            states = [State(state)]
        except ValueError as e:
            raise HTTPException(400, f'unknown state: {state}') from e
    issues = svc.store.list_issues(states=states, assignee=assignee)
    return {'items': [_issue_dict(i) for i in issues]}


@router.get('/issues/{issue_id}')
async def get_issue(issue_id: str) -> dict:
    """Issue + comment transcript + event timeline. Each event carries its
    ``conversation_id`` so the UI can deep-link to the agent's reasoning."""
    svc = get_team_service()
    issue = svc.store.get_issue(issue_id)
    if issue is None:
        raise HTTPException(404, 'issue not found')
    comments = svc.store.list_comments(issue_id)
    events = svc.store.list_events(issue_id)
    return {
        'issue': _issue_dict(issue),
        'comments': [
            {
                'id': c.id,
                'author_role': c.author_role,
                'provenance': c.provenance.value,
                'conversation_id': c.conversation_id,
                'body': c.body,
                'created_at': c.created_at,
            }
            for c in comments
        ],
        'events': [
            {
                'id': e.id,
                'actor_role': e.actor_role,
                'kind': e.kind.value,
                'from_state': e.from_state,
                'to_state': e.to_state,
                'conversation_id': e.conversation_id,
                'detail': e.detail,
                'created_at': e.created_at,
            }
            for e in events
        ],
    }


@router.get('/agents')
async def list_agents() -> dict:
    svc = get_team_service()
    return {
        'items': [
            {
                'role': a.role,
                'display_name': a.display_name,
                'actor_kind': a.actor_kind.value,
                'agent_kind': a.agent_kind.value if a.agent_kind else None,
                'acp_server': a.acp_server,
                'llm_model': a.llm_model,
                'enabled': a.enabled,
                'created_by_role': a.created_by_role,
            }
            for a in svc.store.list_agents()
        ]
    }


@router.get('/events')
async def list_events(issue_id: str | None = None, limit: int = 200) -> dict:
    """Raw audit spine, for debugging."""
    svc = get_team_service()
    events = svc.store.list_events(issue_id, limit=limit)
    return {
        'items': [
            {
                'id': e.id,
                'issue_id': e.issue_id,
                'actor_role': e.actor_role,
                'kind': e.kind.value,
                'from_state': e.from_state,
                'to_state': e.to_state,
                'conversation_id': e.conversation_id,
                'detail': e.detail,
                'created_at': e.created_at,
            }
            for e in events
        ]
    }

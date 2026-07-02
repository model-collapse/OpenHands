"""Cockpit for the AI team (design §10).

Mounted under ``/api/v1/team``. Read-mostly: the supervisor observes and drills
into reasoning, and does NOT edit *issue state* here (issue state changes flow
through the lead / GitHub — design §9). The one write is administrative *setup*
— ``POST /bootstrap`` creates the team lead (and optionally opens a formation
issue). The centerpiece read is ``/issues/{id}``: an event timeline where every
event deep-links to the conversation that produced it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from openhands.app_server.team.config import TeamConfig
from openhands.app_server.team.models import TERMINAL_STATES, Origin, State
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


@router.post('/bootstrap')
async def bootstrap(request: Request) -> dict:
    """Create the team lead (administrative setup — see module docstring).

    Body (all optional):
      { "github_identity": "<login>",   # else uses TEAM_GITHUB_IDENTITY
        "needs": "<what the team should cover>" }

    Creates the grand_leader + lead. If ``needs`` is given, opens an ``internal``
    formation issue so the lead forms its team on the next sweep (design §2b).
    Idempotent: re-calling returns the existing lead.
    """
    from openhands.app_server.team.bootstrap import bootstrap_team
    from openhands.app_server.team.models import ROLE_LEAD

    svc = get_team_service()
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    github_identity = body.get('github_identity') or svc.config.github_identity
    if not github_identity:
        raise HTTPException(
            400, 'github_identity is required (body or TEAM_GITHUB_IDENTITY)'
        )

    lead = bootstrap_team(
        svc.store,
        github_identity=github_identity,
        lead_model=svc.config.lead_model,
    )

    formation_issue_id = None
    needs = (body.get('needs') or '').strip()
    if needs:
        issue = svc.store.create_issue(
            origin=Origin.INTERNAL,
            title='Team formation',
            body=needs,
            author_role='grand_leader',
            state=State.INTERNAL,
        )
        # Mark it a formation issue so the sweep runs form_team (not triage) on
        # it. kv keyed by issue id keeps the marker out of the issue schema.
        svc.store.kv_set(f'formation:{issue.id}', needs)
        formation_issue_id = issue.id

    return {
        'lead': {
            'role': lead.role,
            'github_identity': lead.github_identity,
            'agent_kind': lead.agent_kind.value if lead.agent_kind else None,
        },
        'lead_exists': lead.role == ROLE_LEAD,
        'formation_issue_id': formation_issue_id,
    }

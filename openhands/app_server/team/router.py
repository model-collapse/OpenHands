"""Cockpit for the AI team (design §10, v2 multi-team).

Two routers are exported:
- ``router`` (prefix ``/teams``): the v2 API — a teams collection plus
  team-scoped read routes under ``/teams/{team_id}/…``.
- ``compat_router`` (prefix ``/team``): v1 single-team routes kept as thin
  redirects to ``/teams/default/…`` so existing callers/UI don't break.

Read-mostly: the supervisor observes and drills into reasoning, and does NOT
edit *issue state* here (state changes flow through the lead — design §9). The
writes are administrative *setup*: ``POST /teams`` (create a team + spawn its
lead) and legacy ``POST /team/bootstrap``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from openhands.app_server.team.models import (
    ROLE_LEAD,
    TERMINAL_STATES,
    Origin,
    State,
)
from openhands.app_server.team.service import get_team_service

router = APIRouter(prefix='/teams', tags=['AI Team'])
compat_router = APIRouter(prefix='/team', tags=['AI Team (compat)'])

# States that should surface to the grand leader by default.
_ATTENTION_STATES = {State.AWAITING_GL, State.HALTED}


def _issue_dict(issue) -> dict:
    d = asdict(issue)
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


def _team_health(store) -> dict:
    agents = store.list_agents()
    issues = store.list_issues()
    attention = sum(
        1
        for i in issues
        if i.state in _ATTENTION_STATES
        or _is_stuck(i, get_team_service().config.stuck_threshold)
    )
    return {
        'agents': len(agents),
        'lead_configured': any(a.role == ROLE_LEAD for a in agents),
        'open_issues': sum(1 for i in issues if i.state not in TERMINAL_STATES),
        'needs_attention': attention,
    }


# =====================================================================
# Teams collection
# =====================================================================
@router.get('')
async def list_teams() -> dict:
    svc = get_team_service()
    out = []
    for t in svc.base_store.list_teams():
        store = svc.store_for(t['id'])
        out.append({**t, **_team_health(store)})
    return {'items': out}


@router.post('')
async def create_team(request: Request) -> dict:
    """Create a team + spawn its lead (+ optional formation brief).

    Body: { "id"?, "name", "github_identity"?, "repos"? [..], "needs"? }.
    Administrative setup write (design §10). Idempotent per team id.
    """
    from openhands.app_server.team.bootstrap import bootstrap_team

    svc = get_team_service()
    body = await _json(request)
    name = (body.get('name') or '').strip()
    if not name:
        raise HTTPException(400, 'name is required')
    team_id = (body.get('id') or name).strip().lower().replace(' ', '-')
    github_identity = body.get('github_identity') or svc.config.github_identity
    repos = body.get('repos') or []

    svc.base_store.create_team(
        team_id=team_id,
        name=name,
        github_identity=github_identity,
        repos=repos,
    )
    store = svc.store_for(team_id)
    if not github_identity:
        raise HTTPException(
            400, 'github_identity is required (body or TEAM_GITHUB_IDENTITY)'
        )
    lead = bootstrap_team(
        store, github_identity=github_identity, lead_model=svc.config.lead_model
    )

    formation_issue_id = None
    needs = (body.get('needs') or '').strip()
    if needs:
        issue = store.create_issue(
            origin=Origin.INTERNAL,
            title='Team formation',
            body=needs,
            author_role='grand_leader',
            state=State.INTERNAL,
        )
        store.kv_set(f'formation:{issue.id}', needs)
        formation_issue_id = issue.id

    return {
        'team': {**svc.base_store.get_team(team_id)},  # type: ignore[dict-item]
        'lead': {'role': lead.role, 'github_identity': lead.github_identity},
        'formation_issue_id': formation_issue_id,
    }


# =====================================================================
# Team-scoped reads
# =====================================================================
def _require_team(team_id: str):
    svc = get_team_service()
    if svc.base_store.get_team(team_id) is None and team_id != 'default':
        raise HTTPException(404, f'no such team: {team_id}')
    return svc.store_for(team_id)


@router.get('/{team_id}/health')
async def team_health(team_id: str) -> dict:
    return {'status': 'ok', 'team_id': team_id, **_team_health(_require_team(team_id))}


@router.get('/{team_id}/dashboard')
async def dashboard(team_id: str, filter: str = 'attention') -> dict:
    store = _require_team(team_id)
    issues = store.list_issues()
    threshold = get_team_service().config.stuck_threshold
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
        'team_id': team_id,
        'filter': filter,
        'total': sum(len(v) for v in grouped.values()),
        'by_state': grouped,
    }


@router.get('/{team_id}/issues')
async def list_issues(
    team_id: str, state: str | None = None, assignee: str | None = None
) -> dict:
    store = _require_team(team_id)
    states = None
    if state:
        try:
            states = [State(state)]
        except ValueError as e:
            raise HTTPException(400, f'unknown state: {state}') from e
    issues = store.list_issues(states=states, assignee=assignee)
    return {'items': [_issue_dict(i) for i in issues]}


@router.get('/{team_id}/issues/{issue_id}')
async def get_issue(team_id: str, issue_id: str) -> dict:
    store = _require_team(team_id)
    issue = store.get_issue(issue_id)
    if issue is None:
        raise HTTPException(404, 'issue not found')
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
            for c in store.list_comments(issue_id)
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
            for e in store.list_events(issue_id)
        ],
    }


@router.get('/{team_id}/agents')
async def list_agents(team_id: str) -> dict:
    store = _require_team(team_id)
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
            for a in store.list_agents()
        ]
    }


@router.get('/{team_id}/events')
async def list_events(
    team_id: str, issue_id: str | None = None, limit: int = 200
) -> dict:
    store = _require_team(team_id)
    events = store.list_events(issue_id, limit=limit)
    return {
        'items': [
            {
                'id': e.id,
                'issue_id': e.issue_id,
                'actor_role': e.actor_role,
                'kind': e.kind.value,
                'conversation_id': e.conversation_id,
                'detail': e.detail,
                'created_at': e.created_at,
            }
            for e in events
        ]
    }


@router.get('/{team_id}/lead-conversation')
async def lead_conversation(team_id: str) -> dict:
    """The id of the grand-leader<->lead conversation, so the UI can open it."""
    svc = get_team_service()
    team = svc.base_store.get_team(team_id)
    if team is None:
        raise HTTPException(404, f'no such team: {team_id}')
    return {'team_id': team_id, 'conversation_id': team.get('lead_conversation_id')}


# =====================================================================
# v1 compat: /api/v1/team/* -> teams/default
# =====================================================================
@compat_router.get('/health')
async def compat_health() -> dict:
    svc = get_team_service()
    return {
        'status': 'ok',
        'enabled': svc.config.enabled,
        **_team_health(svc.store_for('default')),
    }


@compat_router.post('/bootstrap')
async def compat_bootstrap(request: Request) -> dict:
    """Legacy single-team bootstrap → operates on the 'default' team."""
    from openhands.app_server.team.bootstrap import bootstrap_team

    svc = get_team_service()
    body = await _json(request)
    github_identity = body.get('github_identity') or svc.config.github_identity
    if not github_identity:
        raise HTTPException(
            400, 'github_identity is required (body or TEAM_GITHUB_IDENTITY)'
        )
    svc.base_store.create_team(
        team_id='default', name='default', github_identity=github_identity
    )
    store = svc.store_for('default')
    lead = bootstrap_team(
        store, github_identity=github_identity, lead_model=svc.config.lead_model
    )
    formation_issue_id = None
    needs = (body.get('needs') or '').strip()
    if needs:
        issue = store.create_issue(
            origin=Origin.INTERNAL,
            title='Team formation',
            body=needs,
            author_role='grand_leader',
            state=State.INTERNAL,
        )
        store.kv_set(f'formation:{issue.id}', needs)
        formation_issue_id = issue.id
    return {
        'lead': {'role': lead.role, 'github_identity': lead.github_identity},
        'formation_issue_id': formation_issue_id,
    }


@compat_router.get('/dashboard')
async def compat_dashboard(filter: str = 'attention') -> RedirectResponse:
    return RedirectResponse(url=f'/api/v1/teams/default/dashboard?filter={filter}')


@compat_router.get('/agents')
async def compat_agents() -> RedirectResponse:
    return RedirectResponse(url='/api/v1/teams/default/agents')


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


# =====================================================================
# UI board (read-only, no GitHub parity) — design §10/§15
# =====================================================================
_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>AI Teams — cockpit</title><style>
 body{font-family:system-ui,sans-serif;max-width:940px;margin:32px auto;padding:0 16px;color:#222}
 h1{font-size:20px} h2{font-size:15px;margin:18px 0 6px;color:#555}
 .card{border:1px solid #e5e5e5;border-radius:8px;padding:8px 10px;margin:4px 0;cursor:pointer}
 .muted{color:#888;font-size:12px} .tag{font-size:11px;background:#f0f0f0;border-radius:4px;padding:1px 6px}
 .risk{background:#fde2e2;color:#a00} .badge{background:#ffe9b3;border-radius:10px;padding:1px 8px;font-size:12px}
 a{color:#0366d6;text-decoration:none} code{background:#f4f4f4;padding:1px 5px;border-radius:4px}
</style></head><body>
<h1>AI Teams</h1>
<div id="teams"></div>
<div id="detail"></div>
<script>
const API='/api/v1/teams';
async function loadTeams(){
 const d = await (await fetch(API)).json();
 const el=document.getElementById('teams'); el.innerHTML='<h2>Teams</h2>';
 if(!d.items.length){ el.innerHTML+='<p class="muted">No teams yet. POST '+API+' to create one.</p>'; return; }
 for(const t of d.items){
   const c=document.createElement('div'); c.className='card';
   const badge = t.needs_attention ? ' <span class="badge">'+t.needs_attention+' need you</span>' : '';
   c.innerHTML='<b>'+t.name+'</b> <span class="muted">'+(t.github_identity||'')+' · '+
     (t.open_issues||0)+' open · lead '+(t.lead_configured?'✓':'—')+'</span>'+badge;
   c.onclick=()=>loadTeam(t.id, t.name); el.appendChild(c);
 }
}
async function loadTeam(id, name){
 const d = await (await fetch(API+'/'+id+'/dashboard?filter=all')).json();
 const el=document.getElementById('detail'); el.innerHTML='<h2>'+name+' — board</h2>';
 const states=Object.keys(d.by_state);
 if(!states.length){ el.innerHTML+='<p class="muted">No issues.</p>'; }
 for(const st of states){
   const h=document.createElement('div'); h.innerHTML='<b>'+st+'</b> <span class="muted">('+d.by_state[st].length+')</span>'; el.appendChild(h);
   for(const i of d.by_state[st]){
     const c=document.createElement('div'); c.className='card';
     const risk=i.risk?' <span class="tag risk">risk:'+i.risk+'</span>':'';
     const who=i.assignee_role?' <span class="tag">'+i.assignee_role+'</span>':'';
     c.innerHTML='<b>'+(i.title||'(untitled)')+'</b>'+who+risk+
       (i.github_ref?' <span class="muted">'+i.github_ref+'</span>':'');
     el.appendChild(c);
   }
 }
}
loadTeams();
</script></body></html>"""


@router.get('/ui', response_class=HTMLResponse)
async def ui() -> HTMLResponse:
    """A thin read-only teams board (design §10/§15 — no GitHub parity)."""
    return HTMLResponse(_UI_HTML)

"""v2 cockpit tests: team-scoped read API + teams collection + create + UI.

Mounts the team routers on a bare FastAPI app, backed by a temp store injected
via the module singleton. Verifies the teams list/create, the attention filter,
issue drill-down (event timeline with conversation links), and that the read
routes don't mutate issue state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openhands.app_server.team import router as team_router_mod
from openhands.app_server.team import service as team_service_mod
from openhands.app_server.team.config import TeamConfig
from openhands.app_server.team.models import (
    ActorKind,
    Agent,
    AgentKind,
    EventKind,
    Origin,
    Provenance,
    State,
)
from openhands.app_server.team.service import TeamService
from openhands.app_server.team.store import TeamStore

DEFAULT = 'default'


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    base = TeamStore(db_path=str(tmp_path / 'team.db'), team_id=DEFAULT)
    base.create_team(team_id=DEFAULT, name='default', github_identity='mc')
    svc = TeamService(config=TeamConfig.from_env())
    svc._base_store = base
    monkeypatch.setattr(team_service_mod, '_service', svc)

    app = FastAPI()
    app.include_router(team_router_mod.router, prefix='/api/v1')
    app.include_router(team_router_mod.compat_router, prefix='/api/v1')
    yield TestClient(app), base
    base.close()


# -- teams collection ------------------------------------------------------
def test_list_teams(ctx):
    tc, base = ctx
    r = tc.get('/api/v1/teams')
    assert r.status_code == 200
    ids = [t['id'] for t in r.json()['items']]
    assert DEFAULT in ids


def test_create_team_spawns_lead(ctx):
    tc, base = ctx
    r = tc.post(
        '/api/v1/teams',
        json={'name': 'Web Team', 'github_identity': 'mc', 'repos': ['o/a']},
    )
    assert r.status_code == 200
    body = r.json()
    assert body['team']['id'] == 'web-team'
    assert body['lead']['role'] == 'lead'
    # the team's lead exists in its own scope
    web = base.for_team('web-team')
    assert web.get_agent('lead') is not None


def test_create_team_with_needs_opens_formation_issue(ctx):
    tc, base = ctx
    r = tc.post(
        '/api/v1/teams',
        json={'name': 'infra', 'github_identity': 'mc', 'needs': 'a backend eng'},
    )
    fid = r.json()['formation_issue_id']
    assert fid is not None
    infra = base.for_team('infra')
    assert infra.kv_get(f'formation:{fid}') == 'a backend eng'


# -- team-scoped reads -----------------------------------------------------
def test_dashboard_attention_filter(ctx):
    tc, base = ctx
    base.create_issue(
        origin=Origin.GITHUB,
        title='normal',
        author_role='lead',
        state=State.IN_PROGRESS,
    )
    base.create_issue(
        origin=Origin.GITHUB,
        title='needs me',
        author_role='lead',
        state=State.AWAITING_GL,
    )
    r = tc.get('/api/v1/teams/default/dashboard')
    body = r.json()
    assert body['total'] == 1
    assert State.AWAITING_GL.value in body['by_state']
    assert State.IN_PROGRESS.value not in body['by_state']


def test_dashboard_stuck_surfaces(ctx):
    tc, base = ctx
    issue = base.create_issue(
        origin=Origin.GITHUB, title='stuck', author_role='lead', state=State.ASSIGNED
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    base.mark_stuck(issue.id, old, 'lead')
    assert tc.get('/api/v1/teams/default/dashboard').json()['total'] == 1


def test_issue_drilldown_links_conversation(ctx):
    tc, base = ctx
    issue = base.create_issue(
        origin=Origin.GITHUB, title='t', author_role='lead', state=State.NEEDS_TRIAGE
    )
    base.transition(
        issue.id,
        to_state=State.ASSIGNED,
        actor_role='lead',
        reason='assign',
        assignee_role='eng:backend',
        conversation_id='conv-xyz',
    )
    base.add_comment(
        issue_id=issue.id,
        author_role='eng:backend',
        provenance=Provenance.AGENT,
        body='looking',
        conversation_id='conv-xyz',
    )
    body = tc.get(f'/api/v1/teams/default/issues/{issue.id}').json()
    assert body['issue']['state'] == State.ASSIGNED.value
    assert any(e['conversation_id'] == 'conv-xyz' for e in body['events'])
    assert body['comments'][0]['conversation_id'] == 'conv-xyz'


def test_issue_404(ctx):
    tc, _ = ctx
    assert tc.get('/api/v1/teams/default/issues/nope').status_code == 404


def test_unknown_team_404(ctx):
    tc, _ = ctx
    assert tc.get('/api/v1/teams/ghost/issues').status_code == 404


def test_agents_listing(ctx):
    tc, base = ctx
    base.upsert_agent(
        Agent(
            role='lead',
            display_name='Lead',
            actor_kind=ActorKind.AGENT,
            agent_kind=AgentKind.OPENHANDS,
            github_identity='mc',
        )
    )
    roles = [a['role'] for a in tc.get('/api/v1/teams/default/agents').json()['items']]
    assert 'lead' in roles


def test_events_endpoint(ctx):
    tc, base = ctx
    issue = base.create_issue(
        origin=Origin.GITHUB, title='t', author_role='lead', state=State.NEEDS_TRIAGE
    )
    kinds = [
        e['kind']
        for e in tc.get(f'/api/v1/teams/default/events?issue_id={issue.id}').json()[
            'items'
        ]
    ]
    assert EventKind.STATE_CHANGE.value in kinds


def test_lead_conversation_endpoint(ctx):
    tc, base = ctx
    base.set_team_conversation('default', 'conv-lead-1')
    r = tc.get('/api/v1/teams/default/lead-conversation')
    assert r.json()['conversation_id'] == 'conv-lead-1'


def test_ui_serves_board(ctx):
    tc, _ = ctx
    r = tc.get('/api/v1/teams/ui')
    assert r.status_code == 200
    assert 'text/html' in r.headers['content-type']
    assert 'AI Teams' in r.text


# -- compat ----------------------------------------------------------------
def test_compat_health(ctx):
    tc, _ = ctx
    r = tc.get('/api/v1/team/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'


def test_compat_dashboard_redirects(ctx):
    tc, _ = ctx
    r = tc.get('/api/v1/team/dashboard', follow_redirects=False)
    assert r.status_code in (307, 308)
    assert '/api/v1/teams/default/dashboard' in r.headers['location']


# -- read routes don't mutate issue state ---------------------------------
def test_read_routes_are_not_issue_state_mutations():
    """Only /teams (create) and /team/bootstrap may be POSTs; the rest of the
    team API is read-only (design §9). No PUT/PATCH/DELETE anywhere."""
    for rt in (team_router_mod.router, team_router_mod.compat_router):
        for route in rt.routes:
            methods = getattr(route, 'methods', set()) or set()
            assert not (methods & {'PUT', 'PATCH', 'DELETE'}), route.path
            if 'POST' in methods:
                assert route.path in ('/teams', '/team/bootstrap'), route.path

"""P4 tests: the read-only cockpit router.

Mounts just the team router on a bare FastAPI app, backed by a temp-file store
injected via the module singleton. Verifies the attention filter, issue
drill-down (event timeline with conversation links), and that there are no
state-mutation endpoints.
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = TeamStore(db_path=str(tmp_path / 'team.db'))
    svc = TeamService(config=TeamConfig.from_env())
    svc._store = store  # inject the temp store
    # Point the module singleton at our service for the duration of the test.
    monkeypatch.setattr(team_service_mod, '_service', svc)

    app = FastAPI()
    app.include_router(team_router_mod.router, prefix='/api/v1')
    yield TestClient(app), store
    store.close()


def test_health(client):
    tc, store = client
    r = tc.get('/api/v1/team/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'


def test_dashboard_attention_filter_hides_normal_work(client):
    tc, store = client
    # a normal in-progress issue should NOT appear under attention
    store.create_issue(
        origin=Origin.GITHUB,
        title='normal',
        author_role='lead',
        state=State.IN_PROGRESS,
    )
    # an awaiting_gl issue SHOULD appear
    store.create_issue(
        origin=Origin.GITHUB,
        title='needs me',
        author_role='lead',
        state=State.AWAITING_GL,
    )
    r = tc.get('/api/v1/team/dashboard')  # default filter=attention
    body = r.json()
    assert body['filter'] == 'attention'
    assert body['total'] == 1
    assert State.AWAITING_GL.value in body['by_state']
    assert State.IN_PROGRESS.value not in body['by_state']


def test_dashboard_all_groups_by_state(client):
    tc, store = client
    store.create_issue(
        origin=Origin.GITHUB, title='a', author_role='lead', state=State.IN_PROGRESS
    )
    store.create_issue(
        origin=Origin.GITHUB, title='b', author_role='lead', state=State.NEEDS_TRIAGE
    )
    r = tc.get('/api/v1/team/dashboard?filter=all')
    body = r.json()
    assert body['total'] == 2
    assert set(body['by_state'].keys()) == {
        State.IN_PROGRESS.value,
        State.NEEDS_TRIAGE.value,
    }


def test_dashboard_surfaces_stuck(client):
    tc, store = client
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='stuck one',
        author_role='lead',
        state=State.ASSIGNED,
    )
    # mark stuck well beyond the default threshold
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.mark_stuck(issue.id, old, 'lead')
    r = tc.get('/api/v1/team/dashboard')
    body = r.json()
    assert body['total'] == 1
    assert State.ASSIGNED.value in body['by_state']


def test_issue_drilldown_links_conversation(client):
    tc, store = client
    issue = store.create_issue(
        origin=Origin.GITHUB, title='t', author_role='lead', state=State.NEEDS_TRIAGE
    )
    store.transition(
        issue.id,
        to_state=State.ASSIGNED,
        actor_role='lead',
        reason='assign',
        assignee_role='eng:backend',
        conversation_id='conv-xyz',
    )
    store.add_comment(
        issue_id=issue.id,
        author_role='eng:backend',
        provenance=Provenance.AGENT,
        body='looking into it',
        conversation_id='conv-xyz',
    )
    r = tc.get(f'/api/v1/team/issues/{issue.id}')
    body = r.json()
    assert body['issue']['state'] == State.ASSIGNED.value
    # event timeline carries the conversation link
    convo_events = [e for e in body['events'] if e['conversation_id'] == 'conv-xyz']
    assert convo_events, 'expected an event linked to the conversation'
    assert any(e['kind'] == EventKind.STATE_CHANGE.value for e in body['events'])
    assert body['comments'][0]['conversation_id'] == 'conv-xyz'


def test_issue_404(client):
    tc, _ = client
    assert tc.get('/api/v1/team/issues/nope').status_code == 404


def test_agents_listing(client):
    tc, store = client
    store.upsert_agent(
        Agent(
            role='lead',
            display_name='Lead',
            actor_kind=ActorKind.AGENT,
            agent_kind=AgentKind.OPENHANDS,
            github_identity='mc',
        )
    )
    r = tc.get('/api/v1/team/agents')
    roles = [a['role'] for a in r.json()['items']]
    assert 'lead' in roles


def test_no_issue_state_mutation_endpoints():
    """The cockpit does not mutate *issue state* (design §9). The only write is
    the administrative /bootstrap setup endpoint; everything else is read-only."""
    mutating = {'POST', 'PUT', 'PATCH', 'DELETE'}
    allowed_write_paths = {'/team/bootstrap'}
    for route in team_router_mod.router.routes:
        methods = getattr(route, 'methods', set()) or set()
        if methods & mutating:
            assert route.path in allowed_write_paths, (
                f'unexpected mutating route: {route.path}'
            )

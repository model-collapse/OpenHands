"""P2c tests: AgentSpawner (kind-aware, config from the agents row) + bootstrap.

The spawner's only external effect is a POST to /app-conversations; we inject a
fake httpx client to capture the payload and assert config is derived solely
from the stored agents row (reproducibly), and that a spawn event is recorded.
"""

from __future__ import annotations

import json

import pytest

from openhands.app_server.team.bootstrap import (
    bootstrap_team,
    register_member,
    register_members,
)
from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    ROLE_LEAD,
    ActorKind,
    Agent,
    AgentKind,
    EventKind,
    Origin,
    State,
)
from openhands.app_server.team.spawn import AgentSpawner
from openhands.app_server.team.store import TeamStore


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    yield s
    s.close()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return {'id': 'conv-abc', 'app_conversation_id': 'conv-abc'}


class _FakeClient:
    """Captures the last POST payload."""

    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, timeout=None):  # noqa: A002
        self.calls.append({'url': url, 'json': json})
        return _FakeResponse(json)


# -- bootstrap -------------------------------------------------------------
def test_bootstrap_creates_grand_leader_and_lead(store):
    lead = bootstrap_team(store, github_identity='model-collapse', lead_model='m')
    assert lead.role == ROLE_LEAD
    assert lead.github_identity == 'model-collapse'
    assert lead.agent_kind == AgentKind.OPENHANDS
    gl = store.get_agent(ROLE_GRAND_LEADER)
    assert gl is not None and gl.actor_kind == ActorKind.HUMAN
    assert gl.agent_kind is None  # human, no agent kind


def test_bootstrap_is_idempotent(store):
    bootstrap_team(store, github_identity='mc')
    bootstrap_team(store, github_identity='mc')
    roles = [a.role for a in store.list_agents()]
    assert roles.count(ROLE_LEAD) == 1
    assert roles.count(ROLE_GRAND_LEADER) == 1


def test_register_acp_member_folds_config(store):
    bootstrap_team(store, github_identity='mc')
    m = register_member(
        store,
        {
            'role': 'eng:backend',
            'agent_kind': 'acp',
            'acp_server': 'claude-code',
            'llm_model': 'opus[1m]',
            'skills': ['python'],
        },
    )
    assert m.agent_kind == AgentKind.ACP
    assert m.created_by_role == ROLE_LEAD
    lc = json.loads(m.launch_config_json)
    assert lc['agent_kind'] == 'acp'
    assert lc['acp_server'] == 'claude-code'
    assert lc['acp_model'] == 'opus[1m]'
    assert json.loads(m.skills_json) == ['python']
    # forming members is audited
    assert any(
        e.kind == EventKind.FORM_MEMBER and e.detail.get('role') == 'eng:backend'
        for e in store.list_events()
    )


def test_register_members_batch(store):
    bootstrap_team(store, github_identity='mc')
    members = register_members(
        store,
        [
            {'role': 'eng:general', 'agent_kind': 'openhands', 'llm_model': 'opus'},
            {'role': 'reviewer', 'agent_kind': 'acp', 'acp_server': 'claude-code'},
        ],
    )
    assert {m.role for m in members} == {'eng:general', 'reviewer'}


# -- spawn -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_spawn_openhands_uses_llm_model(store):
    store.upsert_agent(
        Agent(
            role='eng:general',
            display_name='Eng',
            actor_kind=ActorKind.AGENT,
            agent_kind=AgentKind.OPENHANDS,
            llm_model='bedrock/opus',
        )
    )
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='lead',
        state=State.COMMITTED,
        repo='o/r',
    )
    fake = _FakeClient()
    spawner = AgentSpawner('http://127.0.0.1:3000', store)
    cid = await spawner.spawn(
        role='eng:general', issue=issue, instruction='do it', client=fake
    )
    assert cid == 'conv-abc'
    body = fake.calls[0]['json']
    assert body['selected_repository'] == 'o/r'
    assert body['llm_model'] == 'bedrock/opus'
    # OpenHands member needs no agent override
    assert 'agent_settings_override' not in body
    # spawn event recorded, linking the conversation id
    spawn_events = [e for e in store.list_events(issue.id) if e.kind == EventKind.SPAWN]
    assert len(spawn_events) == 1
    assert spawn_events[0].conversation_id == 'conv-abc'


@pytest.mark.asyncio
async def test_spawn_acp_sends_override_from_row(store):
    register_member(
        store,
        {
            'role': 'eng:backend',
            'agent_kind': 'acp',
            'acp_server': 'claude-code',
            'llm_model': 'opus[1m]',
        },
    )
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='lead',
        state=State.COMMITTED,
        repo='o/r',
    )
    fake = _FakeClient()
    spawner = AgentSpawner('http://127.0.0.1:3000', store)
    await spawner.spawn(role='eng:backend', issue=issue, instruction='fix', client=fake)
    override = fake.calls[0]['json']['agent_settings_override']
    assert override['agent_kind'] == 'acp'
    assert override['acp_server'] == 'claude-code'
    assert override['acp_model'] == 'opus[1m]'


@pytest.mark.asyncio
async def test_spawn_is_reproducible_from_row(store):
    """Two spawns of the same row produce identical request config (pure fn)."""
    register_member(
        store,
        {'role': 'eng:backend', 'agent_kind': 'acp', 'acp_server': 'claude-code'},
    )
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='lead',
        state=State.COMMITTED,
        repo='o/r',
    )
    spawner = AgentSpawner('http://127.0.0.1:3000', store)
    f1, f2 = _FakeClient(), _FakeClient()
    await spawner.spawn(role='eng:backend', issue=issue, instruction='x', client=f1)
    await spawner.spawn(role='eng:backend', issue=issue, instruction='x', client=f2)
    assert f1.calls[0]['json'] == f2.calls[0]['json']


@pytest.mark.asyncio
async def test_spawn_unknown_role_returns_none(store):
    issue = store.create_issue(
        origin=Origin.INTERNAL, title='t', author_role='lead', state=State.COMMITTED
    )
    spawner = AgentSpawner('http://127.0.0.1:3000', store)
    assert await spawner.spawn(role='nope', issue=issue, instruction='x') is None

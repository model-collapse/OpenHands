"""v2-V1 tests: multi-team isolation + team CRUD + v1->v2 migration.

Verifies two teams are fully isolated (issues, agents, events, github_ref
uniqueness, sync dedup are all team-scoped), team CRUD works, and a pre-v2
single-team database is migrated in place into team_id='default' with no loss.
"""

from __future__ import annotations

import sqlite3

import pytest

from openhands.app_server.team import db as team_db
from openhands.app_server.team.models import (
    ActorKind,
    Agent,
    AgentKind,
    Origin,
    State,
)
from openhands.app_server.team.store import TeamStore


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'), team_id='default')
    yield s
    s.close()


# -- team CRUD -------------------------------------------------------------
def test_create_and_list_teams(store):
    store.create_team(team_id='web', name='Web', github_identity='mc', repos=['o/a'])
    store.create_team(team_id='infra', name='Infra')
    ids = {t['id'] for t in store.list_teams()}
    assert {'web', 'infra'} <= ids
    web = store.get_team('web')
    assert web['name'] == 'Web'
    assert web['repos'] == ['o/a']
    assert web['github_identity'] == 'mc'


def test_set_team_conversation(store):
    store.create_team(team_id='web', name='Web')
    store.set_team_conversation('web', 'conv-123')
    assert store.get_team('web')['lead_conversation_id'] == 'conv-123'


# -- isolation -------------------------------------------------------------
def test_issues_are_team_isolated(store):
    web = store.for_team('web')
    infra = store.for_team('infra')
    store.create_team(team_id='web', name='Web')
    store.create_team(team_id='infra', name='Infra')

    wi = web.create_issue(
        origin=Origin.GITHUB,
        title='web bug',
        author_role='lead',
        state=State.NEEDS_TRIAGE,
        github_ref='o/r#1',
    )
    ii = infra.create_issue(
        origin=Origin.GITHUB,
        title='infra bug',
        author_role='lead',
        state=State.NEEDS_TRIAGE,
        github_ref='o/r#1',  # SAME ref, different team
    )
    # each team sees only its own
    assert [i.title for i in web.list_issues()] == ['web bug']
    assert [i.title for i in infra.list_issues()] == ['infra bug']
    # same github_ref is allowed across teams (unique only within a team)
    assert web.get_issue_by_github_ref('o/r#1').id == wi.id
    assert infra.get_issue_by_github_ref('o/r#1').id == ii.id
    assert wi.id != ii.id
    # a team cannot read the other's issue by id
    assert web.get_issue(ii.id) is None
    assert infra.get_issue(wi.id) is None


def test_agents_are_team_isolated(store):
    web = store.for_team('web')
    infra = store.for_team('infra')
    for st, name in ((web, 'web'), (infra, 'infra')):
        st.upsert_agent(
            Agent(
                role='lead',
                display_name=f'{name} lead',
                actor_kind=ActorKind.AGENT,
                agent_kind=AgentKind.OPENHANDS,
                github_identity=name,
            )
        )
    # both teams have their OWN 'lead' role
    assert web.get_agent('lead').github_identity == 'web'
    assert infra.get_agent('lead').github_identity == 'infra'
    assert len(web.list_agents()) == 1
    assert len(infra.list_agents()) == 1


def test_events_and_sync_are_team_isolated(store):
    web = store.for_team('web')
    infra = store.for_team('infra')
    web.create_issue(
        origin=Origin.GITHUB, title='w', author_role='lead', state=State.NEEDS_TRIAGE
    )
    # events for web are not visible to infra
    assert len(web.list_events()) >= 1
    assert len(infra.list_events()) == 0
    # sync dedup is per-team
    web.map_sync('o/r#1', 'o/r#1', 'issue')
    assert web.is_synced('o/r#1', 'issue') is True
    assert infra.is_synced('o/r#1', 'issue') is False


def test_kv_is_team_isolated(store):
    web = store.for_team('web')
    infra = store.for_team('infra')
    web.kv_set('cursor', 'web-value')
    assert web.kv_get('cursor') == 'web-value'
    assert infra.kv_get('cursor') is None


def test_for_team_shares_connection(store):
    web = store.for_team('web')
    assert web._conn is store._conn  # same underlying connection
    assert web.team_id == 'web'


# -- v1 -> v2 migration ----------------------------------------------------
def _make_v1_db(path: str) -> None:
    """Create a minimal pre-v2 (single-team, no team_id) database by hand."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE agents(
          role TEXT PRIMARY KEY, display_name TEXT NOT NULL, actor_kind TEXT NOT NULL,
          agent_kind TEXT, acp_server TEXT, github_identity TEXT, llm_model TEXT,
          launch_config_json TEXT, skills_json TEXT, created_by_role TEXT,
          enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE issues(
          id TEXT PRIMARY KEY, origin TEXT NOT NULL, github_ref TEXT, repo TEXT,
          title TEXT NOT NULL, body TEXT, author_role TEXT NOT NULL,
          assignee_role TEXT, state TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
          risk TEXT, round_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, stuck_since TEXT);
        CREATE TABLE comments(
          id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, author_role TEXT NOT NULL,
          addressed_to TEXT, provenance TEXT NOT NULL, conversation_id TEXT,
          github_comment_id TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT, actor_role TEXT NOT NULL,
          kind TEXT NOT NULL, from_state TEXT, to_state TEXT, conversation_id TEXT,
          detail_json TEXT, created_at TEXT NOT NULL);
        CREATE TABLE inbox(
          role TEXT NOT NULL, issue_id TEXT NOT NULL, reason TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(role, issue_id));
        CREATE TABLE sync_map(
          internal_id TEXT NOT NULL, github_ref TEXT NOT NULL, kind TEXT NOT NULL,
          last_synced_at TEXT NOT NULL, PRIMARY KEY(internal_id, kind));
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        INSERT INTO issues(id, origin, github_ref, title, author_role, state,
          priority, round_count, created_at, updated_at)
          VALUES ('old-1', 'github', 'o/r#7', 'legacy issue', 'lead', 'committed',
          'normal', 0, '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z');
        INSERT INTO agents(role, display_name, actor_kind, agent_kind, created_at)
          VALUES ('lead', 'Old Lead', 'agent', 'openhands', '2026-07-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()


def test_v1_db_migrates_to_default_team(tmp_path):
    path = str(tmp_path / 'legacy.db')
    _make_v1_db(path)

    # Opening with the v2 store triggers init_db -> migration.
    store = TeamStore(db_path=path, team_id='default')
    try:
        # legacy data is preserved under team_id='default'
        issue = store.get_issue('old-1')
        assert issue is not None
        assert issue.title == 'legacy issue'
        assert issue.state == State.COMMITTED
        assert store.get_agent('lead').display_name == 'Old Lead'
        # a default team row now exists
        assert store.get_team('default') is not None
        # schema stamped v2
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        assert row['value'] == team_db.SCHEMA_VERSION == '2'
    finally:
        store.close()


def test_migrated_agents_have_composite_pk(tmp_path):
    """Regression: after migration, agents' PK must be (team_id, role) so
    upsert_agent's ON CONFLICT(team_id, role) works and two teams can each have
    their own 'lead'. (A naive ALTER-ADD-COLUMN would leave PK=(role) and break
    upserts + cross-team roles.)"""
    path = str(tmp_path / 'legacy.db')
    _make_v1_db(path)
    store = TeamStore(db_path=path, team_id='default')
    try:
        # the pre-existing 'lead' row survived under default
        assert store.get_agent('lead') is not None
        # upsert (ON CONFLICT team_id,role) must not raise
        from openhands.app_server.team.models import Agent

        store.upsert_agent(
            Agent(
                role='lead',
                display_name='Updated',
                actor_kind=ActorKind.AGENT,
                agent_kind=AgentKind.OPENHANDS,
            )
        )
        assert store.get_agent('lead').display_name == 'Updated'
        # a DIFFERENT team can have its own 'lead' (composite PK)
        other = store.for_team('web')
        other.upsert_agent(
            Agent(
                role='lead',
                display_name='Web Lead',
                actor_kind=ActorKind.AGENT,
                agent_kind=AgentKind.OPENHANDS,
            )
        )
        assert store.get_agent('lead').display_name == 'Updated'
        assert other.get_agent('lead').display_name == 'Web Lead'
    finally:
        store.close()


def test_v2_db_reopen_is_idempotent(tmp_path):
    path = str(tmp_path / 'team.db')
    s1 = TeamStore(db_path=path, team_id='default')
    s1.create_team(team_id='web', name='Web')
    s1.close()
    # reopen — migration must not fire again or error
    s2 = TeamStore(db_path=path, team_id='default')
    try:
        assert s2.get_team('web') is not None
    finally:
        s2.close()

"""P7 tests: grand-leader influence.

Direct handler tests + integration through the sync bridge (a GL comment on
GitHub is imported and its influence applied in the same pass).
"""

from __future__ import annotations

import pytest

from openhands.app_server.team.influence import GrandLeaderInfluence, classify
from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    ROLE_LEAD,
    EventKind,
    Origin,
    Risk,
    State,
)
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sync import SyncBridge

LEAD_ID = 'model-collapse'


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    yield s
    s.close()


def _issue(store, state=State.IN_PROGRESS):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role=ROLE_LEAD,
        state=State.NEEDS_TRIAGE,
        repo='o/r',
        github_ref='o/r#1',
    )
    if state != State.NEEDS_TRIAGE:
        store.transition(issue.id, to_state=state, actor_role='lead', reason='setup')
    return store.get_issue(issue.id)


# -- classify --------------------------------------------------------------
def test_classify():
    assert classify('please HALT this now') == 'halt'
    assert classify('LGTM, go ahead') == 'approve'
    assert classify('can you use a different approach?') == 'guidance'
    assert classify(None) == 'guidance'


# -- direct handler --------------------------------------------------------
def test_halt_directive_halts_issue(store):
    issue = _issue(store, State.IN_PROGRESS)
    inf = GrandLeaderInfluence(store)
    assert inf.apply(issue.id, 'halt please') == 'halt'
    assert store.get_issue(issue.id).state == State.HALTED
    assert any(e.kind == EventKind.HALT for e in store.list_events(issue.id))


def test_approve_releases_awaiting_gl(store):
    issue = _issue(store, State.AWAITING_GL)
    store.inbox_add(ROLE_GRAND_LEADER, issue.id, 'needs approval')
    inf = GrandLeaderInfluence(store)
    assert inf.apply(issue.id, 'approved, ship it') == 'approve'
    assert store.get_issue(issue.id).state == State.COMMITTED
    # the GL inbox entry is cleared once approved
    assert all(iid != issue.id for iid, _ in store.inbox_list(ROLE_GRAND_LEADER))


def test_approve_outside_awaiting_gl_is_guidance(store):
    issue = _issue(store, State.DISCUSSING)
    inf = GrandLeaderInfluence(store)
    # "approve" while merely discussing shouldn't jump the gate; it's guidance.
    assert inf.apply(issue.id, 'lgtm') == 'guidance'
    assert store.get_issue(issue.id).state == State.DISCUSSING
    assert any(iid == issue.id for iid, _ in store.inbox_list(ROLE_LEAD))


def test_guidance_routes_to_lead_inbox(store):
    issue = _issue(store, State.DISCUSSING)
    inf = GrandLeaderInfluence(store)
    assert inf.apply(issue.id, 'consider the caching layer') == 'guidance'
    assert any(iid == issue.id for iid, _ in store.inbox_list(ROLE_LEAD))


def test_influence_noop_on_terminal(store):
    issue = _issue(store, State.DONE)
    inf = GrandLeaderInfluence(store)
    assert inf.apply(issue.id, 'halt') == 'noop'
    assert store.get_issue(issue.id).state == State.DONE


# -- integration via sync bridge ------------------------------------------
class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.comments = []

    async def list_issues(self, repo, *, since, state='open', per_page=50):
        return list(self.issues.values())

    async def list_issue_comments(self, repo, *, since, per_page=50):
        return list(self.comments)

    def add_issue(self, number, title):
        self.issues[number] = {
            'number': number,
            'title': title,
            'body': '',
            'labels': [],
            'updated_at': '2026-07-02T00:00:00Z',
        }

    def add_comment(self, number, author, body, cid):
        self.comments.append(
            {
                'id': cid,
                'user': {'login': author},
                'body': body,
                'issue_url': f'https://api.github.com/repos/o/r/issues/{number}',
                'created_at': '2026-07-02T01:00:00Z',
                'updated_at': '2026-07-02T01:00:00Z',
            }
        )


@pytest.mark.asyncio
async def test_sync_applies_halt_from_imported_comment(store):
    gh = FakeGitHub()
    gh.add_issue(1, 'Do X')
    bridge = SyncBridge(
        store, gh, lead_identity=LEAD_ID, first_run_lookback_seconds=3600
    )
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    store.transition(
        issue.id, to_state=State.IN_PROGRESS, actor_role='lead', reason='x'
    )
    # grand leader comments "halt" on GitHub
    gh.add_comment(1, author='some-human', body='HALT this', cid=9001)
    await bridge.import_repo('o/r')
    assert store.get_issue(issue.id).state == State.HALTED


@pytest.mark.asyncio
async def test_sync_approval_releases_awaiting_gl(store):
    gh = FakeGitHub()
    gh.add_issue(1, 'risky change')
    bridge = SyncBridge(
        store, gh, lead_identity=LEAD_ID, first_run_lookback_seconds=3600
    )
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    store.set_risk(issue.id, Risk.HIGH, 'lead', 'auth')
    store.transition(
        issue.id, to_state=State.AWAITING_GL, actor_role='lead', reason='gate'
    )
    store.inbox_add(ROLE_GRAND_LEADER, issue.id, 'needs approval')
    gh.add_comment(1, author='grand-leader', body='approved', cid=9002)
    await bridge.import_repo('o/r')
    assert store.get_issue(issue.id).state == State.COMMITTED

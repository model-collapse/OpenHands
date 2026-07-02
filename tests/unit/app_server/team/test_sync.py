"""P3 tests: the GitHub sync bridge.

Uses a fake GitHubClient (no network). Verifies:
- import is idempotent (one internal issue per external, across repeated polls);
- lead-authored comments are NOT re-ingested (echo-loop guard);
- a human-applied managed label advances internal state;
- publish-out records the github_comment_id so it is never re-imported.
"""

from __future__ import annotations

import pytest

from openhands.app_server.team.models import Origin, Provenance, State
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sync import SyncBridge

LEAD = 'model-collapse'


class FakeGitHub:
    """Stand-in for GitHubClient with in-memory issues/comments + write capture."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.posted_comments: list[dict] = []
        self.set_labels_calls: list[dict] = []
        self._next_comment_id = 1000

    # reads
    async def list_issues(self, repo, *, since, state='open', per_page=50):
        return list(self.issues.values())

    async def list_issue_comments(self, repo, *, since, per_page=50):
        return list(self.comments)

    # writes
    async def create_comment(self, repo, number, body):
        self._next_comment_id += 1
        cid = self._next_comment_id
        self.posted_comments.append({'repo': repo, 'number': number, 'body': body})
        return {'id': cid}

    async def set_labels(self, repo, number, labels):
        self.set_labels_calls.append({'repo': repo, 'number': number, 'labels': labels})
        return {}

    # test helpers
    def add_issue(
        self, number, title, body='', labels=None, updated='2026-07-02T00:00:00Z'
    ):
        self.issues[number] = {
            'number': number,
            'title': title,
            'body': body,
            'labels': [{'name': n} for n in (labels or [])],
            'updated_at': updated,
        }

    def add_comment(self, number, author, body, cid=None, ts='2026-07-02T01:00:00Z'):
        self._next_comment_id += 1
        self.comments.append(
            {
                'id': cid or self._next_comment_id,
                'user': {'login': author},
                'body': body,
                'issue_url': f'https://api.github.com/repos/o/r/issues/{number}',
                'created_at': ts,
                'updated_at': ts,
            }
        )


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    yield s
    s.close()


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def bridge(store, gh):
    return SyncBridge(store, gh, lead_identity=LEAD, first_run_lookback_seconds=3600)


@pytest.mark.asyncio
async def test_import_creates_internal_issue(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug', 'details')
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    assert issue is not None
    assert issue.state == State.NEEDS_TRIAGE
    assert issue.origin == Origin.GITHUB
    assert issue.title == 'Fix the bug'


@pytest.mark.asyncio
async def test_import_is_idempotent(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    await bridge.import_repo('o/r')  # poll again
    matches = [i for i in store.list_issues() if i.github_ref == 'o/r#1']
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_external_comment_imported(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    gh.add_comment(1, author='some-human', body='please prioritize', cid=5001)
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    comments = store.list_comments(issue.id)
    assert any(
        c.provenance == Provenance.SYNCED_IN and 'prioritize' in c.body
        for c in comments
    )


@pytest.mark.asyncio
async def test_lead_comment_not_reingested(bridge, store, gh):
    """Echo-loop guard: a comment authored by the lead identity is skipped."""
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    gh.add_comment(1, author=LEAD, body='I have assigned this', cid=6001)
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    comments = store.list_comments(issue.id)
    assert all('assigned this' not in c.body for c in comments)


@pytest.mark.asyncio
async def test_comment_dedup(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    gh.add_comment(1, author='human', body='once', cid=7001)
    await bridge.import_repo('o/r')
    await bridge.import_repo('o/r')  # same comment seen again
    issue = store.get_issue_by_github_ref('o/r#1')
    once = [c for c in store.list_comments(issue.id) if c.body == 'once']
    assert len(once) == 1


@pytest.mark.asyncio
async def test_human_label_advances_state(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    # human adds 'committed' on GitHub
    gh.issues[1]['labels'] = [{'name': 'committed'}]
    gh.issues[1]['updated_at'] = '2026-07-02T02:00:00Z'
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    assert issue.state == State.COMMITTED


@pytest.mark.asyncio
async def test_human_label_does_not_move_backward(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    store.transition(
        issue.id, to_state=State.IN_PROGRESS, actor_role='lead', reason='working'
    )
    # a stale 'committed' label should NOT drag it back
    gh.issues[1]['labels'] = [{'name': 'committed'}]
    await bridge.import_repo('o/r')
    assert store.get_issue_by_github_ref('o/r#1').state == State.IN_PROGRESS


@pytest.mark.asyncio
async def test_publish_comment_records_id_and_is_not_reingested(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    gh_id = await bridge.publish_comment(issue.id, 'Lead: assigned to backend')
    assert gh_id is not None
    assert gh.posted_comments[-1]['body'] == 'Lead: assigned to backend'
    # simulate that same comment coming back on the next poll (as the lead)
    gh.add_comment(1, author=LEAD, body='Lead: assigned to backend', cid=int(gh_id))
    await bridge.import_repo('o/r')
    synced_out = [
        c
        for c in store.list_comments(issue.id)
        if c.provenance == Provenance.SYNCED_OUT
    ]
    assert len(synced_out) == 1  # exactly the one we published; not duplicated


@pytest.mark.asyncio
async def test_publish_labels_reflects_state(bridge, store, gh):
    gh.add_issue(1, 'Fix the bug')
    await bridge.import_repo('o/r')
    issue = store.get_issue_by_github_ref('o/r#1')
    store.transition(
        issue.id,
        to_state=State.ASSIGNED,
        actor_role='lead',
        reason='assign',
        assignee_role='eng:backend',
    )
    await bridge.publish_labels(issue.id)
    labels = gh.set_labels_calls[-1]['labels']
    assert 'assigned' in labels
    assert 'assigned:eng:backend' in labels

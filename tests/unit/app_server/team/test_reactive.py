"""P6 tests: the reactive router.

Injects a fake spawner and deterministic status/eval functions, so the full
loop (assignee eval -> discussing, committed -> engineer -> in_progress ->
in_review/error, halt) is tested without a live backend.
"""

from __future__ import annotations

import json

import pytest

from openhands.app_server.team.bootstrap import bootstrap_team, register_members
from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    EventKind,
    Origin,
    State,
)
from openhands.app_server.team.reactive import ReactiveRouter
from openhands.app_server.team.store import TeamStore


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    bootstrap_team(s, github_identity='mc')
    register_members(
        s, [{'role': 'eng:backend', 'agent_kind': 'openhands', 'llm_model': 'm'}]
    )
    yield s
    s.close()


class FakeSpawner:
    def __init__(self, cid='conv-eng-1'):
        self.cid = cid
        self.calls = []

    async def spawn(self, *, role, issue, instruction, **kw):
        self.calls.append({'role': role, 'issue_id': issue.id})
        # mimic real spawner: record a spawn event so latest_spawn_conversation works
        issue_store.record_nonstate_event(
            actor_role=role,
            kind=EventKind.SPAWN,
            issue_id=issue.id,
            conversation_id=self.cid,
        )
        return self.cid


# module-level handle so FakeSpawner can write the spawn event
issue_store: TeamStore = None  # type: ignore[assignment]


def _assigned_issue(store, assignee='eng:backend'):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='Do X',
        author_role='lead',
        state=State.NEEDS_TRIAGE,
        repo='o/r',
        github_ref='o/r#1',
    )
    store.transition(
        issue.id,
        to_state=State.ASSIGNED,
        actor_role='lead',
        reason='assign',
        assignee_role=assignee,
    )
    return store.get_issue(issue.id)


def _eval_fn(obj):
    async def fn(prompt):
        return json.dumps(obj)

    return fn


async def _status_finished(cid):
    return 'finished'


async def _status_running(cid):
    return 'running'


async def _status_error(cid):
    return 'error'


@pytest.mark.asyncio
async def test_assignee_accept_moves_to_discussing(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    router = ReactiveRouter(
        store,
        FakeSpawner(),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn(
            {'decision': 'accept', 'notes': 'read the code, feasible'}
        ),
    )
    n = await router.run_once()
    assert n == 1
    updated = store.get_issue(issue.id)
    assert updated.state == State.DISCUSSING
    assert any(
        c.author_role == 'eng:backend' and 'feasible' in c.body
        for c in store.list_comments(issue.id)
    )


@pytest.mark.asyncio
async def test_assignee_concern_moves_to_discussing_with_note(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    router = ReactiveRouter(
        store,
        FakeSpawner(),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn(
            {'decision': 'concern', 'notes': 'the auth module makes this risky'}
        ),
    )
    await router.run_once()
    updated = store.get_issue(issue.id)
    assert updated.state == State.DISCUSSING
    assert any('risky' in c.body for c in store.list_comments(issue.id))


@pytest.mark.asyncio
async def test_assignee_not_re_evaluated(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    router = ReactiveRouter(
        store,
        FakeSpawner(),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({'decision': 'accept', 'notes': 'ok'}),
    )
    await router.run_once()
    # move it back to assigned (simulate) — but assignee already commented
    store.transition(
        issue.id,
        to_state=State.ASSIGNED,
        actor_role='lead',
        reason='re',
        assignee_role='eng:backend',
    )
    n = await router.run_once()
    # should NOT re-evaluate (assignee already has a comment)
    assert n == 0


@pytest.mark.asyncio
async def test_committed_spawns_engineer_and_goes_in_progress(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    store.transition(
        issue.id, to_state=State.COMMITTED, actor_role='lead', reason='commit'
    )
    spawner = FakeSpawner(cid='conv-eng-42')
    router = ReactiveRouter(
        store,
        spawner,
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({}),
    )
    n = await router.run_once()
    assert n == 1
    updated = store.get_issue(issue.id)
    assert updated.state == State.IN_PROGRESS
    assert spawner.calls[0]['role'] == 'eng:backend'
    assert store.latest_spawn_conversation(issue.id) == 'conv-eng-42'


@pytest.mark.asyncio
async def test_in_progress_finished_goes_in_review(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    store.transition(issue.id, to_state=State.COMMITTED, actor_role='lead', reason='c')
    router_run = ReactiveRouter(
        store,
        FakeSpawner(cid='conv-x'),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({}),
    )
    await router_run.run_once()  # -> in_progress
    # now poll with finished
    router_done = ReactiveRouter(
        store,
        FakeSpawner(cid='conv-x'),
        engineer_status_fn=_status_finished,
        assignee_eval_fn=_eval_fn({}),
    )
    await router_done.run_once()
    assert store.get_issue(issue.id).state == State.IN_REVIEW


@pytest.mark.asyncio
async def test_in_progress_error_escalates(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    store.transition(issue.id, to_state=State.COMMITTED, actor_role='lead', reason='c')
    r1 = ReactiveRouter(
        store,
        FakeSpawner(cid='conv-e'),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({}),
    )
    await r1.run_once()  # -> in_progress
    r2 = ReactiveRouter(
        store,
        FakeSpawner(cid='conv-e'),
        engineer_status_fn=_status_error,
        assignee_eval_fn=_eval_fn({}),
    )
    await r2.run_once()
    # error stays in_progress but escalates to grand leader
    assert any(iid == issue.id for iid, _ in store.inbox_list(ROLE_GRAND_LEADER))


class FailSpawner:
    """Spawner whose spawn() fails (returns None)."""

    async def spawn(self, *, role, issue, instruction, **kw):
        return None


@pytest.mark.asyncio
async def test_committed_spawn_failure_requeues_to_assigned(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    store.transition(
        issue.id, to_state=State.COMMITTED, actor_role='lead', reason='commit'
    )
    router = ReactiveRouter(
        store,
        FailSpawner(),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({}),
    )
    await router.run_once()
    # spawn failed -> re-queued to assigned (not left committed / not in_progress)
    assert store.get_issue(issue.id).state == State.ASSIGNED


@pytest.mark.asyncio
async def test_halted_issue_is_untouched(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    store.transition(
        issue.id, to_state=State.HALTED, actor_role='grand_leader', reason='stop'
    )
    router = ReactiveRouter(
        store,
        FakeSpawner(),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({'decision': 'accept', 'notes': 'x'}),
    )
    n = await router.run_once()
    assert n == 0  # halted issues are not acted on
    assert store.get_issue(issue.id).state == State.HALTED


@pytest.mark.asyncio
async def test_halt(store):
    global issue_store
    issue_store = store
    issue = _assigned_issue(store)
    router = ReactiveRouter(
        store,
        FakeSpawner(),
        engineer_status_fn=_status_running,
        assignee_eval_fn=_eval_fn({}),
    )
    router.apply_halt(issue.id, ROLE_GRAND_LEADER, 'stop it')
    updated = store.get_issue(issue.id)
    assert updated.state == State.HALTED
    assert any(e.kind == EventKind.HALT for e in store.list_events(issue.id))


def test_is_halt_directive():
    assert ReactiveRouter.is_halt_directive('please HALT this')
    assert ReactiveRouter.is_halt_directive('/halt')
    assert not ReactiveRouter.is_halt_directive('keep going')
    assert not ReactiveRouter.is_halt_directive(None)

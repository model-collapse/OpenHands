"""P1 acceptance tests for the AI-team store + audit spine.

Uses a temp-file SQLite DB (the store opens its own connection). Verifies:
- an issue can transition through every state;
- each transition emits exactly one event;
- state cannot change without an event (the raw UPDATE is private to the store);
- labels round-trip.
"""

from __future__ import annotations

import pytest

from openhands.app_server.team.labels import (
    labels_to_state,
    state_to_labels,
)
from openhands.app_server.team.models import (
    ActorKind,
    Agent,
    AgentKind,
    EventKind,
    Origin,
    Priority,
    Provenance,
    Risk,
    State,
)
from openhands.app_server.team.store import TeamStore


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    yield s
    s.close()


def _events_for(store, issue_id, kind=EventKind.STATE_CHANGE):
    return [e for e in store.list_events(issue_id) if e.kind == kind]


def test_create_issue_emits_one_state_event(store):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='grand_leader',
        state=State.NEEDS_TRIAGE,
        github_ref='o/r#1',
    )
    evs = _events_for(store, issue.id)
    assert len(evs) == 1
    assert evs[0].to_state == State.NEEDS_TRIAGE.value
    assert evs[0].from_state is None


def test_transition_through_every_state(store):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='grand_leader',
        state=State.NEEDS_TRIAGE,
    )
    # walk a realistic path; each hop is one transition.
    path = [
        State.ASSIGNED,
        State.DISCUSSING,
        State.COMMITTED,
        State.IN_PROGRESS,
        State.IN_REVIEW,
        State.DONE,
    ]
    for st in path:
        store.transition(issue.id, to_state=st, actor_role='lead', reason='test')
    cur = store.get_issue(issue.id)
    assert cur.state == State.DONE
    # 1 create + len(path) transitions == exactly one event per state change
    assert len(_events_for(store, issue.id)) == 1 + len(path)


def test_transition_records_from_and_to(store):
    issue = store.create_issue(
        origin=Origin.INTERNAL,
        title='t',
        author_role='lead',
        state=State.NEEDS_TRIAGE,
    )
    store.transition(
        issue.id,
        to_state=State.ASSIGNED,
        actor_role='lead',
        reason='assigning',
        assignee_role='eng:backend',
    )
    evs = _events_for(store, issue.id)
    last = evs[-1]
    assert last.from_state == State.NEEDS_TRIAGE.value
    assert last.to_state == State.ASSIGNED.value
    assert store.get_issue(issue.id).assignee_role == 'eng:backend'


def test_no_state_change_without_event(store):
    """The invariant: every state value in issues has a matching to_state event.

    We drive several mutations, then assert the set of states ever written to
    the row equals the set of to_states in the event log — i.e. nothing changed
    state silently.
    """
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='grand_leader',
        state=State.NEEDS_TRIAGE,
    )
    for st in [State.ASSIGNED, State.COMMITTED, State.HALTED]:
        store.transition(issue.id, to_state=st, actor_role='lead', reason='r')

    state_events = {e.to_state for e in _events_for(store, issue.id) if e.to_state}
    assert state_events == {
        State.NEEDS_TRIAGE.value,
        State.ASSIGNED.value,
        State.COMMITTED.value,
        State.HALTED.value,
    }


def test_raw_transition_is_private():
    """`_apply_transition` must not be part of the public surface used to change
    state; the only public path (`transition`) always writes an event."""
    # public API has no bare state setter
    assert not hasattr(TeamStore, 'set_state')
    # the raw writer exists but is name-mangled/underscored as private
    assert hasattr(TeamStore, '_apply_transition')


def test_priority_and_risk_emit_events(store):
    issue = store.create_issue(
        origin=Origin.INTERNAL, title='t', author_role='lead', state=State.INTERNAL
    )
    store.set_priority(issue.id, Priority.HIGH, 'lead', 'urgent-ish')
    store.set_risk(issue.id, Risk.HIGH, 'lead', 'touches auth')
    assert store.get_issue(issue.id).priority == Priority.HIGH
    assert store.get_issue(issue.id).risk == Risk.HIGH
    assert len(_events_for(store, issue.id, EventKind.PRIORITY)) == 1
    assert len(_events_for(store, issue.id, EventKind.RISK)) == 1


def test_round_bump(store):
    issue = store.create_issue(
        origin=Origin.INTERNAL, title='t', author_role='lead', state=State.DISCUSSING
    )
    assert store.bump_round(issue.id, 'lead') == 1
    assert store.bump_round(issue.id, 'lead') == 2
    assert store.get_issue(issue.id).round_count == 2


def test_comment_emits_event_and_links_conversation(store):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='grand_leader',
        state=State.NEEDS_TRIAGE,
    )
    c = store.add_comment(
        issue_id=issue.id,
        author_role='lead',
        provenance=Provenance.AGENT,
        body='assigning to backend',
        conversation_id='conv-123',
    )
    assert c.conversation_id == 'conv-123'
    comment_events = _events_for(store, issue.id, EventKind.COMMENT)
    assert len(comment_events) == 1
    assert comment_events[0].conversation_id == 'conv-123'


def test_agent_roster_is_audited(store):
    store.upsert_agent(
        Agent(
            role='lead',
            display_name='Team Lead',
            actor_kind=ActorKind.AGENT,
            agent_kind=AgentKind.OPENHANDS,
            github_identity='model-collapse',
        )
    )
    store.upsert_agent(
        Agent(
            role='eng:backend',
            display_name='Backend',
            actor_kind=ActorKind.AGENT,
            agent_kind=AgentKind.ACP,
            acp_server='claude-code',
            created_by_role='lead',
        )
    )
    assert store.get_agent('eng:backend').agent_kind == AgentKind.ACP
    assert len(store.list_agents()) == 2
    # forming a member (created_by_role set) is a FORM_MEMBER event
    form_events = [e for e in store.list_events() if e.kind == EventKind.FORM_MEMBER]
    assert any(e.detail.get('role') == 'eng:backend' for e in form_events)


def test_sync_map_dedup(store):
    assert store.is_synced('o/r#1', 'issue') is False
    store.map_sync('internal-1', 'o/r#1', 'issue')
    assert store.is_synced('o/r#1', 'issue') is True


def test_inbox(store):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='t',
        author_role='grand_leader',
        state=State.AWAITING_GL,
    )
    store.inbox_add('grand_leader', issue.id, 'risk tripwire')
    assert store.inbox_list('grand_leader') == [(issue.id, 'risk tripwire')]
    store.inbox_clear('grand_leader', issue.id)
    assert store.inbox_list('grand_leader') == []


# -- labels round-trip -----------------------------------------------------
@pytest.mark.parametrize('state', list(State))
def test_labels_state_round_trip(state):
    labels = state_to_labels(state=state)
    parsed_state, _, _, _, _ = labels_to_state(labels)
    assert parsed_state == state


def test_labels_full_round_trip():
    labels = state_to_labels(
        state=State.ASSIGNED,
        assignee_role='eng:backend',
        waiting_role='eng:backend',
        priority=Priority.HIGH,
        risk=Risk.HIGH,
    )
    state, assignee, waiting, priority, risk = labels_to_state(labels)
    assert state == State.ASSIGNED
    assert assignee == 'eng:backend'
    assert waiting == 'eng:backend'
    assert priority == Priority.HIGH
    assert risk == Risk.HIGH

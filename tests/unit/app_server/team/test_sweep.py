"""P5 tests: lead brain + sweep loop.

The lead's LLM call is injected as a deterministic ``think_fn`` returning canned
JSON, so we test the orchestration (transitions, risk tripwire, round-cap
convergence, escalation) without a model.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from openhands.app_server.team.bootstrap import bootstrap_team, register_members
from openhands.app_server.team.lead import LeadBrain
from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    Origin,
    Priority,
    Risk,
    State,
)
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sweep import LeadSweep


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    bootstrap_team(s, github_identity='mc')
    register_members(
        s,
        [
            {'role': 'eng:backend', 'agent_kind': 'openhands', 'llm_model': 'm'},
            {'role': 'reviewer', 'agent_kind': 'acp', 'acp_server': 'claude-code'},
        ],
    )
    yield s
    s.close()


def _canned(obj):
    """A think_fn that always returns the given object as JSON (with surrounding
    prose, to exercise the extractor)."""
    return lambda prompt: f'Here is my decision:\n{json.dumps(obj)}\nDone.'


def _new_issue(store, state=State.NEEDS_TRIAGE, **kw):
    return store.create_issue(
        origin=Origin.GITHUB,
        title=kw.get('title', 'Fix the thing'),
        author_role=ROLE_GRAND_LEADER,
        state=state,
        repo='o/r',
        github_ref=kw.get('github_ref', 'o/r#1'),
    )


# -- lead brain parsing ----------------------------------------------------
def test_triage_parses_and_validates(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'assignee_role': 'eng:backend',
                'rationale': 'backend owns this',
                'risk': None,
                'priority': 'high',
            }
        ),
    )
    issue = _new_issue(store)
    d = lead.triage(issue)
    assert d['assignee_role'] == 'eng:backend'
    assert d['priority'] == Priority.HIGH
    assert d['risk'] is None


def test_triage_rejects_unknown_role(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {'assignee_role': 'ghost', 'rationale': '', 'priority': 'normal'}
        ),
    )
    with pytest.raises(ValueError):
        lead.triage(_new_issue(store))


# -- sweep: triage ---------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_triages_and_assigns(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'assignee_role': 'eng:backend',
                'rationale': 'assigning to backend',
                'risk': None,
                'priority': 'normal',
            }
        ),
    )
    issue = _new_issue(store)
    sweep = LeadSweep(store, lead)
    n = await sweep.run_once()
    assert n == 1
    updated = store.get_issue(issue.id)
    assert updated.state == State.ASSIGNED
    assert updated.assignee_role == 'eng:backend'
    # rationale recorded as a lead comment
    assert any(
        c.author_role == 'lead' and 'backend' in c.body
        for c in store.list_comments(issue.id)
    )


@pytest.mark.asyncio
async def test_sweep_sets_risk_on_triage(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'assignee_role': 'eng:backend',
                'rationale': 'touches auth',
                'risk': 'high',
                'priority': 'high',
            }
        ),
    )
    issue = _new_issue(store)
    await LeadSweep(store, lead).run_once()
    updated = store.get_issue(issue.id)
    assert updated.risk == Risk.HIGH
    assert updated.priority == Priority.HIGH


# -- sweep: decide + risk tripwire ----------------------------------------
@pytest.mark.asyncio
async def test_commit_low_risk_goes_to_committed(store):
    lead = LeadBrain(
        store, model='m', think_fn=_canned({'action': 'commit', 'rationale': 'lgtm'})
    )
    issue = _new_issue(store, state=State.DISCUSSING)
    store.transition(
        issue.id,
        to_state=State.DISCUSSING,
        actor_role='lead',
        reason='x',
        assignee_role='eng:backend',
    ) if issue.state != State.DISCUSSING else None
    await LeadSweep(store, lead).run_once()
    assert store.get_issue(issue.id).state == State.COMMITTED


@pytest.mark.asyncio
async def test_commit_high_risk_holds_for_grand_leader(store):
    lead = LeadBrain(
        store, model='m', think_fn=_canned({'action': 'commit', 'rationale': 'ok'})
    )
    issue = _new_issue(store, state=State.DISCUSSING)
    store.set_risk(issue.id, Risk.HIGH, 'lead', 'auth')
    await LeadSweep(store, lead).run_once()
    updated = store.get_issue(issue.id)
    assert updated.state == State.AWAITING_GL  # tripwire: NOT committed
    assert (issue.id, 'high-risk commit needs approval') in store.inbox_list(
        ROLE_GRAND_LEADER
    )


@pytest.mark.asyncio
async def test_round_cap_forces_decision(store):
    # lead keeps trying to pushback, but past the cap the sweep forces commit
    lead = LeadBrain(
        store,
        model='m',
        round_cap=2,
        think_fn=_canned({'action': 'pushback', 'rationale': 'more questions'}),
    )
    issue = _new_issue(store, state=State.DISCUSSING)
    # drive round_count to the cap
    store.bump_round(issue.id, 'lead')
    store.bump_round(issue.id, 'lead')
    await LeadSweep(store, lead).run_once()
    # forced: pushback -> commit (low risk)
    assert store.get_issue(issue.id).state == State.COMMITTED


@pytest.mark.asyncio
async def test_reassign_changes_assignee(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'action': 'reassign',
                'rationale': 'reviewer is a better fit',
                'new_assignee': 'reviewer',
            }
        ),
    )
    issue = _new_issue(store, state=State.DISCUSSING)
    store.transition(
        issue.id,
        to_state=State.DISCUSSING,
        actor_role='lead',
        reason='x',
        assignee_role='eng:backend',
    )
    await LeadSweep(store, lead).run_once()
    updated = store.get_issue(issue.id)
    assert updated.state == State.ASSIGNED
    assert updated.assignee_role == 'reviewer'
    assert updated.round_count == 1


@pytest.mark.asyncio
async def test_pushback_under_cap_stays_discussing(store):
    lead = LeadBrain(
        store,
        model='m',
        round_cap=3,
        think_fn=_canned({'action': 'pushback', 'rationale': 'clarify?'}),
    )
    issue = _new_issue(store, state=State.DISCUSSING)
    await LeadSweep(store, lead).run_once()
    updated = store.get_issue(issue.id)
    assert updated.state == State.DISCUSSING
    assert updated.round_count == 1


# -- sweep: budget + escalation -------------------------------------------
@pytest.mark.asyncio
async def test_sweep_respects_spawn_budget(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'assignee_role': 'eng:backend',
                'rationale': 'r',
                'risk': None,
                'priority': 'normal',
            }
        ),
    )
    for i in range(5):
        _new_issue(store, github_ref=f'o/r#{i + 10}')
    sweep = LeadSweep(store, lead, max_spawns=2)
    n = await sweep.run_once()
    assert n == 2  # budget capped
    triaged = [i for i in store.list_issues() if i.state == State.ASSIGNED]
    assert len(triaged) == 2


@pytest.mark.asyncio
async def test_escalate_stuck(store):
    lead = LeadBrain(
        store, model='m', think_fn=_canned({'action': 'commit', 'rationale': 'x'})
    )
    issue = _new_issue(store, state=State.ASSIGNED)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.mark_stuck(issue.id, old, 'lead')
    await LeadSweep(store, lead, stuck_threshold=1800).run_once()
    assert any(iid == issue.id for iid, _ in store.inbox_list(ROLE_GRAND_LEADER))

"""v2-V4 tests: the grand-leader<->lead conversation channel.

Injects a deterministic message-fetcher and lead think_fn, so a human's
conversation turns are mapped to team actions (form team / halt / approve /
guidance) without a live conversation.
"""

from __future__ import annotations

import json

import pytest

from openhands.app_server.team.channel import LeadConversationChannel
from openhands.app_server.team.lead import LeadBrain
from openhands.app_server.team.models import (
    ROLE_LEAD,
    Origin,
    Risk,
    State,
)
from openhands.app_server.team.store import TeamStore

CONVO = 'conv-lead-1'


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'), team_id='default')
    s.create_team(team_id='default', name='default', github_identity='mc')
    yield s
    s.close()


def _fetch(turns):
    """turns: list of (id, text). Returns those after after_id."""

    async def fn(conversation_id, after_id):
        return [(i, t) for (i, t) in turns if i > after_id]

    return fn


def _lead(store, obj=None):
    return LeadBrain(
        store,
        model='m',
        think_fn=(lambda prompt: json.dumps(obj)) if obj is not None else None,
    )


@pytest.mark.asyncio
async def test_form_team_via_conversation(store):
    lead = _lead(
        store,
        {
            'members': [{'role': 'eng:backend', 'agent_kind': 'openhands'}],
            'rationale': 'need backend',
        },
    )
    ch = LeadConversationChannel(
        store,
        lead,
        fetch_messages=_fetch([(1, 'Please form a team with a backend engineer')]),
    )
    n = await ch.process(CONVO)
    assert n == 1
    assert store.get_agent('eng:backend') is not None


@pytest.mark.asyncio
async def test_halt_via_conversation(store):
    issue = store.create_issue(
        origin=Origin.GITHUB, title='t', author_role='lead', state=State.IN_PROGRESS
    )
    ch = LeadConversationChannel(
        store, _lead(store), fetch_messages=_fetch([(1, 'halt everything now')])
    )
    await ch.process(CONVO)
    assert store.get_issue(issue.id).state == State.HALTED


@pytest.mark.asyncio
async def test_approve_releases_awaiting_gl_via_conversation(store):
    issue = store.create_issue(
        origin=Origin.GITHUB,
        title='risky',
        author_role='lead',
        state=State.NEEDS_TRIAGE,
    )
    store.set_risk(issue.id, Risk.HIGH, 'lead', 'auth')
    store.transition(
        issue.id, to_state=State.AWAITING_GL, actor_role='lead', reason='g'
    )
    ch = LeadConversationChannel(
        store, _lead(store), fetch_messages=_fetch([(1, 'approved, go ahead')])
    )
    await ch.process(CONVO)
    assert store.get_issue(issue.id).state == State.COMMITTED


@pytest.mark.asyncio
async def test_guidance_routes_to_lead_inbox(store):
    issue = store.create_issue(
        origin=Origin.GITHUB, title='t', author_role='lead', state=State.DISCUSSING
    )
    ch = LeadConversationChannel(
        store,
        _lead(store),
        fetch_messages=_fetch([(1, 'consider using the caching layer')]),
    )
    await ch.process(CONVO)
    assert any(iid == issue.id for iid, _ in store.inbox_list(ROLE_LEAD))


@pytest.mark.asyncio
async def test_cursor_prevents_reprocessing(store):
    store.create_issue(
        origin=Origin.GITHUB, title='t', author_role='lead', state=State.IN_PROGRESS
    )
    turns = [(1, 'halt now')]
    ch = LeadConversationChannel(store, _lead(store), fetch_messages=_fetch(turns))
    assert await ch.process(CONVO) == 1
    # second pass: same turn is before the cursor -> not reprocessed
    assert await ch.process(CONVO) == 0


@pytest.mark.asyncio
async def test_guidance_with_no_issue_is_recorded(store):
    # no issues exist; a guidance turn should be recorded, not crash
    ch = LeadConversationChannel(
        store, _lead(store), fetch_messages=_fetch([(1, 'keep an eye on latency')])
    )
    assert await ch.process(CONVO) == 1
    assert any(e.detail.get('via') == 'lead_conversation' for e in store.list_events())

"""P8-formation tests: the lead forms its own team (design §2b).

Covers LeadBrain.form_team (validation) and the sweep's formation handling
(register members, run-once, close the issue).
"""

from __future__ import annotations

import json

import pytest

from openhands.app_server.team.bootstrap import bootstrap_team
from openhands.app_server.team.lead import LeadBrain
from openhands.app_server.team.models import (
    ROLE_LEAD,
    AgentKind,
    Origin,
    State,
)
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sweep import LeadSweep


@pytest.fixture
def store(tmp_path):
    s = TeamStore(db_path=str(tmp_path / 'team.db'))
    bootstrap_team(s, github_identity='mc')
    yield s
    s.close()


def _canned(obj):
    return lambda prompt: f'plan:\n{json.dumps(obj)}'


def test_form_team_validates_and_drops_bad_specs(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'members': [
                    {'role': 'eng:backend', 'agent_kind': 'openhands'},
                    {
                        'role': 'reviewer',
                        'agent_kind': 'acp',
                        'acp_server': 'claude-code',
                    },
                    {'agent_kind': 'openhands'},  # no role -> dropped
                    {'role': 'weird', 'agent_kind': 'nonsense'},  # bad kind -> dropped
                ],
                'rationale': 'small team',
            }
        ),
    )
    result = lead.form_team('need backend + a reviewer')
    roles = {m['role'] for m in result['members']}
    assert roles == {'eng:backend', 'reviewer'}
    # acp member defaulted its server
    acp = next(m for m in result['members'] if m['role'] == 'reviewer')
    assert acp['acp_server'] == 'claude-code'


def test_form_team_requires_members_list(store):
    lead = LeadBrain(store, model='m', think_fn=_canned({'rationale': 'oops'}))
    with pytest.raises(ValueError):
        lead.form_team('needs')


@pytest.mark.asyncio
async def test_sweep_forms_team_from_formation_issue(store):
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'members': [
                    {'role': 'eng:backend', 'agent_kind': 'openhands'},
                    {
                        'role': 'reviewer',
                        'agent_kind': 'acp',
                        'acp_server': 'claude-code',
                    },
                ],
                'rationale': 'covers the needs',
            }
        ),
    )
    issue = store.create_issue(
        origin=Origin.INTERNAL,
        title='Team formation',
        body='need a backend engineer and a reviewer',
        author_role='grand_leader',
        state=State.INTERNAL,
    )
    store.kv_set(f'formation:{issue.id}', issue.body)

    sweep = LeadSweep(store, lead)
    n = await sweep.run_once()
    assert n == 1
    # members registered as lead-formed
    roles = {a.role for a in store.list_agents()}
    assert 'eng:backend' in roles and 'reviewer' in roles
    backend = store.get_agent('eng:backend')
    assert backend.created_by_role == ROLE_LEAD
    assert backend.agent_kind == AgentKind.OPENHANDS
    reviewer = store.get_agent('reviewer')
    assert reviewer.agent_kind == AgentKind.ACP
    # formation issue closed and marker cleared (won't re-run)
    assert store.get_issue(issue.id).state == State.DONE
    assert not store.kv_get(f'formation:{issue.id}')
    n2 = await sweep.run_once()
    assert n2 == 0


@pytest.mark.asyncio
async def test_form_team_skips_existing_roles(store):
    # pre-register a backend; the lead proposing it again should not duplicate
    from openhands.app_server.team.bootstrap import register_member

    register_member(store, {'role': 'eng:backend', 'agent_kind': 'openhands'})
    lead = LeadBrain(
        store,
        model='m',
        think_fn=_canned(
            {
                'members': [
                    {'role': 'eng:backend', 'agent_kind': 'openhands'},
                    {'role': 'eng:frontend', 'agent_kind': 'openhands'},
                ],
                'rationale': 'add frontend',
            }
        ),
    )
    issue = store.create_issue(
        origin=Origin.INTERNAL,
        title='form',
        body='need frontend too',
        author_role='grand_leader',
        state=State.INTERNAL,
    )
    store.kv_set(f'formation:{issue.id}', issue.body)
    await LeadSweep(store, lead).run_once()
    roles = [a.role for a in store.list_agents()]
    assert roles.count('eng:backend') == 1  # not duplicated
    assert 'eng:frontend' in roles

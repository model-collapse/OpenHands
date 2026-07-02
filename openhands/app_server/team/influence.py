"""Grand-leader influence (design §9).

The human supervises through GitHub: comments imported as ``grand_leader`` are
influence directed at the lead, NOT direct state edits. This module classifies a
grand-leader comment and applies its effect:

- **halt** ("halt" / "stop the team" / "/halt") -> the issue is halted
  immediately (the escape hatch — strong enough to arrest a runaway, even though
  authority still flows through the grand-leader role).
- **approval** on an ``awaiting_gl`` issue ("approve" / "lgtm" / "go ahead") ->
  release it to ``committed`` (the risk-tripwire gate the lead is waiting on).
- otherwise -> record the guidance in the **lead's inbox** so the next sweep
  weighs it; the lead, not the human, takes the resulting action.

Kept dependency-light (operates on the store) so both the sync bridge and any
direct caller can apply influence without importing the reactive router.
"""

from __future__ import annotations

import logging

from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    ROLE_LEAD,
    EventKind,
    State,
)
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)

_HALT_TOKENS = ('halt', 'stop the team', '/halt')
_APPROVE_TOKENS = ('approve', 'approved', 'lgtm', 'go ahead', 'ship it', '/approve')

# Terminal / already-resolved states that influence should not disturb.
_INERT_STATES = {State.DONE, State.HALTED}


def classify(text: str | None) -> str:
    """Return 'halt' | 'approve' | 'guidance' for a grand-leader comment."""
    if not text:
        return 'guidance'
    low = text.lower()
    if any(tok in low for tok in _HALT_TOKENS):
        return 'halt'
    if any(tok in low for tok in _APPROVE_TOKENS):
        return 'approve'
    return 'guidance'


class GrandLeaderInfluence:
    def __init__(self, store: TeamStore) -> None:
        self.store = store

    def apply(self, issue_id: str, body: str | None) -> str:
        """Apply a grand-leader comment's influence to an issue.

        Returns the action taken ('halt' | 'approve' | 'guidance' | 'noop').
        """
        issue = self.store.get_issue(issue_id)
        if issue is None:
            return 'noop'
        kind = classify(body)

        if kind == 'halt':
            if issue.state in _INERT_STATES:
                return 'noop'
            self.store.transition(
                issue_id,
                to_state=State.HALTED,
                actor_role=ROLE_GRAND_LEADER,
                reason='grand-leader halt directive',
            )
            self.store.record_nonstate_event(
                actor_role=ROLE_GRAND_LEADER,
                kind=EventKind.HALT,
                issue_id=issue_id,
                detail={'source': 'grand_leader_comment'},
            )
            logger.info('influence: halted %s (grand-leader)', issue_id)
            return 'halt'

        if kind == 'approve':
            # Approval only releases the risk-tripwire gate.
            if issue.state == State.AWAITING_GL:
                self.store.transition(
                    issue_id,
                    to_state=State.COMMITTED,
                    actor_role=ROLE_GRAND_LEADER,
                    reason='grand-leader approved high-risk commit',
                )
                self.store.inbox_clear(ROLE_GRAND_LEADER, issue_id)
                logger.info('influence: approved %s -> committed', issue_id)
                return 'approve'
            # Approval elsewhere is just guidance for the lead.

        # guidance (or approval outside awaiting_gl): route to the lead.
        if issue.state not in _INERT_STATES:
            self.store.inbox_add(
                ROLE_LEAD, issue_id, 'grand-leader guidance (weigh next decision)'
            )
            self.store.record_nonstate_event(
                actor_role=ROLE_GRAND_LEADER,
                kind=EventKind.COMMENT,
                issue_id=issue_id,
                detail={'influence': 'guidance_to_lead'},
            )
            return 'guidance'
        return 'noop'

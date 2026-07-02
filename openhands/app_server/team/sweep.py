"""Lead sweep — the time-driven manager pass (design §6a).

Walks open issues and advances state via the lead's decisions. Key rules:
- ``needs_triage`` -> lead triages, assigns a member, sets risk/priority, posts
  the rationale, and moves the issue to ``assigned`` (``waiting`` the assignee).
- ``discussing``/``waiting:lead`` -> lead decides commit / pushback / reassign;
  bounded by the round cap (the lead is the convergence authority).
- **Risk tripwire:** an issue tagged ``risk:high`` never auto-commits — it goes
  to ``awaiting_gl`` and is escalated to the grand-leader inbox for explicit
  assent (design §5/§9).
- Stuck/failed issues are escalated to the grand-leader inbox.
- Budgeted: at most ``max_spawns`` lead LLM calls per pass.

The sweep only *decides*; the engineer execution on ``committed`` is the
reactive router's job (P6). The lead's rationale is published to GitHub via the
sync bridge (publish-out), keeping the external thread the human-facing record.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from openhands.app_server.team.lead import LeadBrain
from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    ROLE_LEAD,
    Priority,
    State,
)
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sync import SyncBridge

logger = logging.getLogger(__name__)

# Issues the lead triages (open, pre-assignment). Discussing issues are decided
# separately; assigned/committed/etc. are driven by the reactive router (P6).
_TRIAGE_STATES = {State.NEEDS_TRIAGE, State.INTERNAL}


class LeadSweep:
    def __init__(
        self,
        store: TeamStore,
        lead: LeadBrain,
        *,
        sync: SyncBridge | None = None,
        stuck_threshold: int = 1800,
        max_spawns: int = 5,
    ) -> None:
        self.store = store
        self.lead = lead
        self.sync = sync
        self.stuck_threshold = stuck_threshold
        self.max_spawns = max_spawns

    async def _publish(self, issue_id: str, body: str) -> None:
        if self.sync is not None:
            try:
                await self.sync.publish_comment(issue_id, body)
                await self.sync.publish_labels(issue_id)
            except Exception as e:  # noqa: BLE001
                logger.error('sweep: publish for %s failed: %s', issue_id, e)

    def _is_stuck(self, issue) -> bool:
        if not issue.stuck_since:
            return False
        try:
            since = datetime.fromisoformat(issue.stuck_since)
        except ValueError:
            return False
        return (
            datetime.now(timezone.utc) - since
        ).total_seconds() > self.stuck_threshold

    async def run_once(self) -> int:
        """One manager pass. Returns the number of lead actions taken."""
        actions = 0
        for issue in self.store.list_issues():
            if actions >= self.max_spawns:
                logger.info('sweep: hit max_spawns budget (%d)', self.max_spawns)
                break
            try:
                if issue.state in _TRIAGE_STATES:
                    if await self._triage(issue):
                        actions += 1
                elif issue.state == State.DISCUSSING:
                    if await self._decide(issue):
                        actions += 1
            except Exception as e:  # noqa: BLE001
                logger.error('sweep: error on issue %s: %s', issue.id, e)

        self._escalate_stuck()
        return actions

    async def _triage(self, issue) -> bool:
        decision = self.lead.triage(issue)
        assignee = decision['assignee_role']
        # Record risk/priority first (auditable), then assign.
        if decision['risk'] is not None:
            self.store.set_risk(issue.id, decision['risk'], ROLE_LEAD, 'triage')
        if decision['priority'] != Priority.NORMAL:
            self.store.set_priority(issue.id, decision['priority'], ROLE_LEAD, 'triage')
        self.store.transition(
            issue.id,
            to_state=State.ASSIGNED,
            actor_role=ROLE_LEAD,
            reason=f'triaged to {assignee}',
            assignee_role=assignee,
        )
        self.store.add_comment(
            issue_id=issue.id,
            author_role=ROLE_LEAD,
            provenance=_agent_provenance(),
            body=decision['rationale'],
        )
        self.store.mark_stuck(issue.id, _now_iso(), ROLE_LEAD)  # waiting on assignee
        await self._publish(issue.id, f'**[Team Lead]** {decision["rationale"]}')
        logger.info('sweep: triaged %s -> %s', issue.id, assignee)
        return True

    async def _decide(self, issue) -> bool:
        decision = self.lead.decide(issue)
        action = decision['action']
        rationale = decision['rationale']

        if action == 'reassign':
            new_assignee = decision.get('new_assignee')
            if new_assignee:
                self.store.transition(
                    issue.id,
                    to_state=State.ASSIGNED,
                    actor_role=ROLE_LEAD,
                    reason=f'reassigned to {new_assignee}',
                    assignee_role=new_assignee,
                )
                self.store.bump_round(issue.id, ROLE_LEAD)
                await self._publish(
                    issue.id, f'**[Team Lead]** Reassigning: {rationale}'
                )
                return True
            action = 'commit'  # no target given; fall through to commit

        if action == 'pushback':
            self.store.bump_round(issue.id, ROLE_LEAD)
            self.store.add_comment(
                issue_id=issue.id,
                author_role=ROLE_LEAD,
                provenance=_agent_provenance(),
                body=rationale,
            )
            await self._publish(issue.id, f'**[Team Lead]** {rationale}')
            return True

        # action == 'commit' — but the risk tripwire holds high-risk issues.
        if issue.risk is not None:
            self.store.transition(
                issue.id,
                to_state=State.AWAITING_GL,
                actor_role=ROLE_LEAD,
                reason='high-risk: awaiting grand-leader assent before commit',
            )
            self.store.inbox_add(
                ROLE_GRAND_LEADER, issue.id, 'high-risk commit needs approval'
            )
            await self._publish(
                issue.id,
                '**[Team Lead]** This is high-risk; holding for grand-leader '
                f'approval before implementation. {rationale}',
            )
            logger.info('sweep: %s held for grand-leader (risk tripwire)', issue.id)
            return True

        self.store.transition(
            issue.id,
            to_state=State.COMMITTED,
            actor_role=ROLE_LEAD,
            reason='committed for implementation',
        )
        await self._publish(issue.id, f'**[Team Lead]** Committed. {rationale}')
        logger.info('sweep: committed %s', issue.id)
        return True

    def _escalate_stuck(self) -> None:
        for issue in self.store.list_issues():
            if issue.state in (State.DONE, State.HALTED):
                continue
            if self._is_stuck(issue):
                self.store.inbox_add(
                    ROLE_GRAND_LEADER, issue.id, f'stuck in {issue.state.value}'
                )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_provenance():
    # Imported lazily to keep this module light; comments the lead authors
    # internally are 'agent' provenance (published copies are 'synced_out').
    from openhands.app_server.team.models import Provenance

    return Provenance.AGENT

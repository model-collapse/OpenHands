"""Grand-leader ↔ lead conversation channel (design §9, v2-V4).

The human steers a team by talking to its lead in a normal OpenHands
conversation. This module reads *new human turns* from that conversation and
maps each to a team action, reusing the existing influence + formation paths so
the audit trail and semantics match the GitHub channel:

- **halt / approve / guidance** → ``GrandLeaderInfluence`` applied to the team's
  most relevant issue (the one awaiting the grand leader, else the newest open).
- **form team** ("form ...", "hire ...", "add a ... engineer") → the lead's
  ``form_team`` over the message, registering members (design §2b).
- otherwise → recorded as grand-leader guidance for the lead (an event), so the
  next sweep weighs it.

The message source is injected (``fetch_messages``) so this is unit-testable
without a live conversation; the service wires it to the app-server's
``/conversation/{id}/events`` endpoint. Processed turns are de-duplicated via a
per-conversation kv cursor (last processed event id).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from openhands.app_server.team.bootstrap import register_member
from openhands.app_server.team.influence import GrandLeaderInfluence, classify
from openhands.app_server.team.lead import LeadBrain
from openhands.app_server.team.models import ROLE_LEAD, EventKind, State
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)

# fetch_messages(conversation_id, after_id) -> list of (event_id:int, text:str)
# for user-authored message turns after ``after_id`` (oldest first).
FetchMessages = Callable[[str, int], Awaitable[list[tuple[int, str]]]]

_FORM_TOKENS = ('form a team', 'form team', 'hire', 'add a ', 'add an ', 'build a team')


def _looks_like_formation(text: str) -> bool:
    low = text.lower()
    return any(tok in low for tok in _FORM_TOKENS)


class LeadConversationChannel:
    def __init__(
        self,
        store: TeamStore,
        lead: LeadBrain,
        *,
        fetch_messages: FetchMessages,
    ) -> None:
        self.store = store
        self.lead = lead
        self.influence = GrandLeaderInfluence(store)
        self._fetch = fetch_messages

    def _cursor_key(self, conversation_id: str) -> str:
        return f'lead_convo_cursor:{conversation_id}'

    def _target_issue_id(self) -> str | None:
        """Pick the issue a bare directive (halt/approve/guidance) applies to:
        prefer one awaiting the grand leader, else the newest open issue."""
        issues = self.store.list_issues()
        awaiting = [i for i in issues if i.state == State.AWAITING_GL]
        pool = awaiting or [
            i for i in issues if i.state not in (State.DONE, State.HALTED)
        ]
        return pool[0].id if pool else None

    async def process(self, conversation_id: str) -> int:
        """Process new human turns in the lead conversation. Returns the count
        of turns handled."""
        after = int(self.store.kv_get(self._cursor_key(conversation_id), '0') or '0')
        try:
            turns = await self._fetch(conversation_id, after)
        except Exception as e:  # noqa: BLE001
            logger.error('lead channel fetch failed for %s: %s', conversation_id, e)
            return 0

        handled = 0
        newest = after
        for event_id, text in turns:
            newest = max(newest, event_id)
            try:
                self._handle_turn(conversation_id, text)
                handled += 1
            except Exception as e:  # noqa: BLE001
                logger.error('lead channel: error handling turn: %s', e)
        if newest > after:
            self.store.kv_set(self._cursor_key(conversation_id), str(newest))
        return handled

    def _handle_turn(self, conversation_id: str, text: str) -> None:
        # 1) Team formation.
        if _looks_like_formation(text):
            result = self.lead.form_team(text)
            existing = {a.role for a in self.store.list_agents()}
            added = []
            for spec in result['members']:
                if spec['role'] not in existing:
                    register_member(self.store, spec)
                    added.append(spec['role'])
            self.store.record_nonstate_event(
                actor_role=ROLE_LEAD,
                kind=EventKind.FORM_MEMBER,
                conversation_id=conversation_id,
                detail={'via': 'lead_conversation', 'added': added},
            )
            logger.info('lead channel: formed team via conversation: %s', added)
            return

        # 2) Halt / approve / guidance — apply to the relevant issue.
        kind = classify(text)
        issue_id = self._target_issue_id()
        if issue_id is None:
            # No issue to act on; record the guidance for the record.
            self.store.record_nonstate_event(
                actor_role=ROLE_LEAD,
                kind=EventKind.COMMENT,
                conversation_id=conversation_id,
                detail={'via': 'lead_conversation', 'guidance': text[:200]},
            )
            return
        self.influence.apply(issue_id, text)
        logger.info(
            'lead channel: applied %s to issue %s (from conversation)', kind, issue_id
        )

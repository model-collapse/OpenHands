"""The audit spine: the single append path for team events.

Every state change, assignment, comment, spawn, and roster edit is recorded
here. The store calls ``record_event`` inside the same transaction as the
mutation, so no state change can exist without a corresponding event
(see ``store.TeamStore``). Kept append-only.
"""

from __future__ import annotations

import json
import sqlite3

from openhands.app_server.team.models import EventKind


def record_event(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    now: str,
    actor_role: str,
    kind: EventKind,
    issue_id: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    conversation_id: str | None = None,
    detail: dict | None = None,
) -> int:
    """Append one event. Uses the caller's connection so it shares the txn.

    ``now`` is passed in (not generated here) because ``Date.now``-style calls
    are centralized in the store for testability. Returns the new event id.
    """
    cur = conn.execute(
        'INSERT INTO events(team_id, issue_id, actor_role, kind, from_state, '
        'to_state, conversation_id, detail_json, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (
            team_id,
            issue_id,
            actor_role,
            kind.value,
            from_state,
            to_state,
            conversation_id,
            json.dumps(detail) if detail else None,
            now,
        ),
    )
    return int(cur.lastrowid or 0)

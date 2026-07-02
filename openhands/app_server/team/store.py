"""Data-access layer for the AI-team store.

Synchronous SQLite (wrap via ``call_sync_from_async`` when used from the async
loops, mirroring the github_poller's boto3 handling).

**Audit invariant:** every method that mutates an issue's ``state``,
``assignee_role``, ``priority``, or ``risk`` writes an event in the *same*
transaction via ``record_event``. The raw ``UPDATE issues ... SET state`` is
private to this module (``_apply_transition``) and never called without also
appending an event — so no state change can exist without an audit record.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from openhands.app_server.team import db as _db
from openhands.app_server.team.events import record_event
from openhands.app_server.team.models import (
    ActorKind,
    Agent,
    AgentKind,
    Comment,
    Event,
    EventKind,
    Issue,
    Origin,
    Priority,
    Provenance,
    Risk,
    State,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class TeamStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or _db.default_db_path()
        self._conn = _db.connect(self.db_path)
        _db.init_db(self._conn)

    def close(self) -> None:
        self._conn.close()

    # -- agents ------------------------------------------------------------
    def upsert_agent(self, agent: Agent) -> Agent:
        created = agent.created_at or _now()
        with self._conn:  # transaction
            self._conn.execute(
                'INSERT INTO agents(role, display_name, actor_kind, agent_kind, '
                'acp_server, github_identity, llm_model, launch_config_json, '
                'skills_json, created_by_role, enabled, created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(role) DO UPDATE SET '
                'display_name=excluded.display_name, actor_kind=excluded.actor_kind, '
                'agent_kind=excluded.agent_kind, acp_server=excluded.acp_server, '
                'github_identity=excluded.github_identity, llm_model=excluded.llm_model, '
                'launch_config_json=excluded.launch_config_json, '
                'skills_json=excluded.skills_json, enabled=excluded.enabled',
                (
                    agent.role,
                    agent.display_name,
                    agent.actor_kind.value,
                    agent.agent_kind.value if agent.agent_kind else None,
                    agent.acp_server,
                    agent.github_identity,
                    agent.llm_model,
                    agent.launch_config_json,
                    agent.skills_json,
                    agent.created_by_role,
                    1 if agent.enabled else 0,
                    created,
                ),
            )
            # Roster edits are auditable (design §2b / spec §4).
            kind = (
                EventKind.FORM_MEMBER
                if agent.created_by_role
                else EventKind.RECONFIGURE_MEMBER
            )
            record_event(
                self._conn,
                now=_now(),
                actor_role=agent.created_by_role or agent.role,
                kind=kind,
                detail={
                    'role': agent.role,
                    'agent_kind': (
                        agent.agent_kind.value if agent.agent_kind else None
                    ),
                },
            )
        agent.created_at = created
        return agent

    def get_agent(self, role: str) -> Agent | None:
        row = self._conn.execute(
            'SELECT * FROM agents WHERE role=?', (role,)
        ).fetchone()
        return _row_to_agent(row) if row else None

    def list_agents(self, enabled_only: bool = False) -> list[Agent]:
        q = 'SELECT * FROM agents'
        if enabled_only:
            q += ' WHERE enabled=1'
        return [_row_to_agent(r) for r in self._conn.execute(q + ' ORDER BY role')]

    # -- issues ------------------------------------------------------------
    def create_issue(
        self,
        *,
        origin: Origin,
        title: str,
        author_role: str,
        state: State,
        body: str | None = None,
        repo: str | None = None,
        github_ref: str | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> Issue:
        issue_id = _new_id()
        now = _now()
        with self._conn:
            self._conn.execute(
                'INSERT INTO issues(id, origin, github_ref, repo, title, body, '
                'author_role, state, priority, created_at, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (
                    issue_id,
                    origin.value,
                    github_ref,
                    repo,
                    title,
                    body,
                    author_role,
                    state.value,
                    priority.value,
                    now,
                    now,
                ),
            )
            record_event(
                self._conn,
                now=now,
                actor_role=author_role,
                kind=EventKind.STATE_CHANGE,
                issue_id=issue_id,
                from_state=None,
                to_state=state.value,
                detail={'created': True, 'origin': origin.value},
            )
        return self.get_issue(issue_id)  # type: ignore[return-value]

    def get_issue(self, issue_id: str) -> Issue | None:
        row = self._conn.execute(
            'SELECT * FROM issues WHERE id=?', (issue_id,)
        ).fetchone()
        return _row_to_issue(row) if row else None

    def get_issue_by_github_ref(self, github_ref: str) -> Issue | None:
        row = self._conn.execute(
            'SELECT * FROM issues WHERE github_ref=?', (github_ref,)
        ).fetchone()
        return _row_to_issue(row) if row else None

    def list_issues(
        self,
        *,
        states: list[State] | None = None,
        assignee: str | None = None,
    ) -> list[Issue]:
        clauses: list[str] = []
        params: list[str] = []
        if states:
            clauses.append('state IN (%s)' % ','.join('?' for _ in states))
            params.extend(s.value for s in states)
        if assignee:
            clauses.append('assignee_role=?')
            params.append(assignee)
        q = 'SELECT * FROM issues'
        if clauses:
            q += ' WHERE ' + ' AND '.join(clauses)
        q += ' ORDER BY updated_at DESC'
        return [_row_to_issue(r) for r in self._conn.execute(q, params)]

    def transition(
        self,
        issue_id: str,
        *,
        to_state: State,
        actor_role: str,
        reason: str,
        assignee_role: str | None = None,
        conversation_id: str | None = None,
    ) -> Issue:
        """The ONLY public way to change issue state. Writes an event in-txn."""
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(f'no such issue: {issue_id}')
        now = _now()
        with self._conn:
            self._apply_transition(
                issue_id,
                to_state=to_state,
                assignee_role=assignee_role,
                now=now,
            )
            record_event(
                self._conn,
                now=now,
                actor_role=actor_role,
                kind=EventKind.STATE_CHANGE,
                issue_id=issue_id,
                from_state=issue.state.value,
                to_state=to_state.value,
                conversation_id=conversation_id,
                detail={'reason': reason, 'assignee_role': assignee_role},
            )
        return self.get_issue(issue_id)  # type: ignore[return-value]

    def _apply_transition(
        self,
        issue_id: str,
        *,
        to_state: State,
        assignee_role: str | None,
        now: str,
    ) -> None:
        """Raw state write — PRIVATE. Never call without ``record_event`` in the
        same transaction (that is the audit invariant). Kept here so no other
        module can mutate ``issues.state`` directly."""
        if assignee_role is not None:
            self._conn.execute(
                'UPDATE issues SET state=?, assignee_role=?, updated_at=? WHERE id=?',
                (to_state.value, assignee_role, now, issue_id),
            )
        else:
            self._conn.execute(
                'UPDATE issues SET state=?, updated_at=? WHERE id=?',
                (to_state.value, now, issue_id),
            )

    def set_priority(
        self, issue_id: str, priority: Priority, actor_role: str, reason: str
    ) -> None:
        now = _now()
        with self._conn:
            self._conn.execute(
                'UPDATE issues SET priority=?, updated_at=? WHERE id=?',
                (priority.value, now, issue_id),
            )
            record_event(
                self._conn,
                now=now,
                actor_role=actor_role,
                kind=EventKind.PRIORITY,
                issue_id=issue_id,
                detail={'priority': priority.value, 'reason': reason},
            )

    def set_risk(
        self, issue_id: str, risk: Risk | None, actor_role: str, reason: str
    ) -> None:
        now = _now()
        with self._conn:
            self._conn.execute(
                'UPDATE issues SET risk=?, updated_at=? WHERE id=?',
                (risk.value if risk else None, now, issue_id),
            )
            record_event(
                self._conn,
                now=now,
                actor_role=actor_role,
                kind=EventKind.RISK,
                issue_id=issue_id,
                detail={'risk': risk.value if risk else None, 'reason': reason},
            )

    def bump_round(self, issue_id: str, actor_role: str) -> int:
        now = _now()
        with self._conn:
            self._conn.execute(
                'UPDATE issues SET round_count = round_count + 1, updated_at=? '
                'WHERE id=?',
                (now, issue_id),
            )
            row = self._conn.execute(
                'SELECT round_count FROM issues WHERE id=?', (issue_id,)
            ).fetchone()
            count = int(row['round_count']) if row else 0
            record_event(
                self._conn,
                now=now,
                actor_role=actor_role,
                kind=EventKind.ROUND,
                issue_id=issue_id,
                detail={'round_count': count},
            )
        return count

    def mark_stuck(self, issue_id: str, since: str | None, actor_role: str) -> None:
        now = _now()
        with self._conn:
            self._conn.execute(
                'UPDATE issues SET stuck_since=?, updated_at=? WHERE id=?',
                (since, now, issue_id),
            )
            record_event(
                self._conn,
                now=now,
                actor_role=actor_role,
                kind=EventKind.STUCK,
                issue_id=issue_id,
                detail={'stuck_since': since},
            )

    # -- comments ----------------------------------------------------------
    def add_comment(
        self,
        *,
        issue_id: str,
        author_role: str,
        provenance: Provenance,
        body: str,
        addressed_to: str | None = None,
        conversation_id: str | None = None,
        github_comment_id: str | None = None,
    ) -> Comment:
        comment_id = _new_id()
        now = _now()
        with self._conn:
            self._conn.execute(
                'INSERT INTO comments(id, issue_id, author_role, addressed_to, '
                'provenance, conversation_id, github_comment_id, body, created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (
                    comment_id,
                    issue_id,
                    author_role,
                    addressed_to,
                    provenance.value,
                    conversation_id,
                    github_comment_id,
                    body,
                    now,
                ),
            )
            record_event(
                self._conn,
                now=now,
                actor_role=author_role,
                kind=EventKind.COMMENT,
                issue_id=issue_id,
                conversation_id=conversation_id,
                detail={'comment_id': comment_id, 'addressed_to': addressed_to},
            )
        return Comment(
            id=comment_id,
            issue_id=issue_id,
            author_role=author_role,
            provenance=provenance,
            body=body,
            addressed_to=addressed_to,
            conversation_id=conversation_id,
            github_comment_id=github_comment_id,
            created_at=now,
        )

    def list_comments(self, issue_id: str) -> list[Comment]:
        return [
            _row_to_comment(r)
            for r in self._conn.execute(
                'SELECT * FROM comments WHERE issue_id=? ORDER BY created_at, id',
                (issue_id,),
            )
        ]

    # -- events (read) -----------------------------------------------------
    def list_events(self, issue_id: str | None = None, limit: int = 200) -> list[Event]:
        if issue_id is not None:
            rows = self._conn.execute(
                'SELECT * FROM events WHERE issue_id=? ORDER BY id LIMIT ?',
                (issue_id, limit),
            )
        else:
            rows = self._conn.execute(
                'SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,)
            )
        return [_row_to_event(r) for r in rows]

    def record_nonstate_event(
        self,
        *,
        actor_role: str,
        kind: EventKind,
        issue_id: str | None = None,
        conversation_id: str | None = None,
        detail: dict | None = None,
    ) -> int:
        """Append an event that is not itself a state mutation (spawn, halt, ...)."""
        with self._conn:
            return record_event(
                self._conn,
                now=_now(),
                actor_role=actor_role,
                kind=kind,
                issue_id=issue_id,
                conversation_id=conversation_id,
                detail=detail,
            )

    # -- inbox -------------------------------------------------------------
    def inbox_add(self, role: str, issue_id: str, reason: str) -> None:
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO inbox(role, issue_id, reason, created_at) '
                'VALUES (?,?,?,?)',
                (role, issue_id, reason, _now()),
            )

    def inbox_list(self, role: str) -> list[tuple[str, str]]:
        return [
            (r['issue_id'], r['reason'])
            for r in self._conn.execute(
                'SELECT issue_id, reason FROM inbox WHERE role=? ORDER BY created_at',
                (role,),
            )
        ]

    def inbox_clear(self, role: str, issue_id: str) -> None:
        with self._conn:
            self._conn.execute(
                'DELETE FROM inbox WHERE role=? AND issue_id=?', (role, issue_id)
            )

    # -- sync_map ----------------------------------------------------------
    def map_sync(self, internal_id: str, github_ref: str, kind: str) -> None:
        with self._conn:
            self._conn.execute(
                'INSERT OR REPLACE INTO sync_map(internal_id, github_ref, kind, '
                'last_synced_at) VALUES (?,?,?,?)',
                (internal_id, github_ref, kind, _now()),
            )

    def is_synced(self, github_ref: str, kind: str) -> bool:
        return (
            self._conn.execute(
                'SELECT 1 FROM sync_map WHERE github_ref=? AND kind=?',
                (github_ref, kind),
            ).fetchone()
            is not None
        )


# -- row -> dataclass helpers ---------------------------------------------
def _row_to_agent(r: sqlite3.Row) -> Agent:
    return Agent(
        role=r['role'],
        display_name=r['display_name'],
        actor_kind=ActorKind(r['actor_kind']),
        agent_kind=AgentKind(r['agent_kind']) if r['agent_kind'] else None,
        acp_server=r['acp_server'],
        github_identity=r['github_identity'],
        llm_model=r['llm_model'],
        launch_config_json=r['launch_config_json'],
        skills_json=r['skills_json'],
        created_by_role=r['created_by_role'],
        enabled=bool(r['enabled']),
        created_at=r['created_at'],
    )


def _row_to_issue(r: sqlite3.Row) -> Issue:
    return Issue(
        id=r['id'],
        origin=Origin(r['origin']),
        title=r['title'],
        state=State(r['state']),
        author_role=r['author_role'],
        priority=Priority(r['priority']),
        body=r['body'],
        github_ref=r['github_ref'],
        repo=r['repo'],
        assignee_role=r['assignee_role'],
        risk=Risk(r['risk']) if r['risk'] else None,
        round_count=int(r['round_count']),
        created_at=r['created_at'],
        updated_at=r['updated_at'],
        stuck_since=r['stuck_since'],
    )


def _row_to_comment(r: sqlite3.Row) -> Comment:
    return Comment(
        id=r['id'],
        issue_id=r['issue_id'],
        author_role=r['author_role'],
        provenance=Provenance(r['provenance']),
        body=r['body'],
        addressed_to=r['addressed_to'],
        conversation_id=r['conversation_id'],
        github_comment_id=r['github_comment_id'],
        created_at=r['created_at'],
    )


def _row_to_event(r: sqlite3.Row) -> Event:
    import json

    return Event(
        id=int(r['id']),
        actor_role=r['actor_role'],
        kind=EventKind(r['kind']),
        created_at=r['created_at'],
        issue_id=r['issue_id'],
        from_state=r['from_state'],
        to_state=r['to_state'],
        conversation_id=r['conversation_id'],
        detail=json.loads(r['detail_json']) if r['detail_json'] else {},
    )

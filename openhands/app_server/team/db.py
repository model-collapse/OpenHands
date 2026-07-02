"""SQLite connection + schema init for the AI-team store.

DB lives under the app persistence dir (``~/.openhands/team.db`` by default),
alongside ``openhands.db``. Schema is created idempotently; additive migrations
follow the ``PRAGMA table_info`` + ``ALTER TABLE`` pattern used elsewhere in the
app-server (e.g. the github_poller).

v2: every team-scoped table carries ``team_id`` and the root ``teams`` table
holds per-team config. A v1 database (no ``teams`` table, no ``team_id`` columns)
is migrated in place: a ``default`` team is created and existing rows are
backfilled with ``team_id='default'`` so no data is lost (design §14).
"""

from __future__ import annotations

import os
import sqlite3

from openhands.app_server.config import get_default_persistence_dir

DEFAULT_TEAM_ID = 'default'


def default_db_path() -> str:
    override = os.getenv('TEAM_DB_PATH')
    if override:
        return override
    return str(get_default_persistence_dir() / 'team.db')


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: the store connection is shared across the async
    # request handlers (run in a threadpool) and the background loops. Access is
    # serialized by a lock in TeamStore, and WAL keeps readers/writers from
    # blocking each other.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS teams(
  id                   TEXT PRIMARY KEY,
  name                 TEXT NOT NULL UNIQUE,
  github_identity      TEXT,
  repos_json           TEXT,                  -- JSON list of watched "owner/repo"
  lead_conversation_id TEXT,                  -- grand-leader <-> lead chat (design §9)
  enabled              INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv(
  team_id TEXT NOT NULL,
  key     TEXT NOT NULL,
  value   TEXT,
  PRIMARY KEY(team_id, key)
);

CREATE TABLE IF NOT EXISTS agents(
  team_id            TEXT NOT NULL,
  role               TEXT NOT NULL,
  display_name       TEXT NOT NULL,
  actor_kind         TEXT NOT NULL,
  agent_kind         TEXT,
  acp_server         TEXT,
  github_identity    TEXT,
  llm_model          TEXT,
  launch_config_json TEXT,
  skills_json        TEXT,
  created_by_role    TEXT,
  enabled            INTEGER NOT NULL DEFAULT 1,
  created_at         TEXT NOT NULL,
  PRIMARY KEY(team_id, role)
);

CREATE TABLE IF NOT EXISTS issues(
  id            TEXT PRIMARY KEY,
  team_id       TEXT NOT NULL,
  origin        TEXT NOT NULL,
  github_ref    TEXT,
  repo          TEXT,
  title         TEXT NOT NULL,
  body          TEXT,
  author_role   TEXT NOT NULL,
  assignee_role TEXT,
  state         TEXT NOT NULL,
  priority      TEXT NOT NULL DEFAULT 'normal',
  risk          TEXT,
  round_count   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  stuck_since   TEXT
);
CREATE INDEX IF NOT EXISTS ix_issues_team_state ON issues(team_id, state);
-- github_ref is unique WITHIN a team (v2).
CREATE UNIQUE INDEX IF NOT EXISTS ix_issues_team_github_ref
  ON issues(team_id, github_ref) WHERE github_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS comments(
  id                TEXT PRIMARY KEY,
  team_id           TEXT NOT NULL,
  issue_id          TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  author_role       TEXT NOT NULL,
  addressed_to      TEXT,
  provenance        TEXT NOT NULL,
  conversation_id   TEXT,
  github_comment_id TEXT,
  body              TEXT NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comments_issue ON comments(issue_id, created_at);

CREATE TABLE IF NOT EXISTS events(
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id         TEXT NOT NULL,
  issue_id        TEXT,
  actor_role      TEXT NOT NULL,
  kind            TEXT NOT NULL,
  from_state      TEXT,
  to_state        TEXT,
  conversation_id TEXT,
  detail_json     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_team_issue ON events(team_id, issue_id, id);

CREATE TABLE IF NOT EXISTS inbox(
  team_id    TEXT NOT NULL,
  role       TEXT NOT NULL,
  issue_id   TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(team_id, role, issue_id)
);

CREATE TABLE IF NOT EXISTS sync_map(
  team_id        TEXT NOT NULL,
  internal_id    TEXT NOT NULL,
  github_ref     TEXT NOT NULL,
  kind           TEXT NOT NULL,
  last_synced_at TEXT NOT NULL,
  PRIMARY KEY(team_id, internal_id, kind)
);
"""

SCHEMA_VERSION = '2'


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Fold a pre-v2 (single-team) database into ``team_id='default'``.

    Detected by an existing ``agents`` table that lacks a ``team_id`` column.
    Adds ``team_id`` (default 'default') to each scoped table and seeds the
    ``teams`` row from env. Additive + idempotent.
    """
    if not _table_exists(conn, 'agents'):
        return  # brand-new db — _SCHEMA already created v2 tables
    if 'team_id' in _table_columns(conn, 'agents'):
        return  # already v2

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    repos = os.getenv('TEAM_REPOS', '')
    identity = os.getenv('TEAM_GITHUB_IDENTITY')
    import json as _json

    repos_json = _json.dumps([r.strip() for r in repos.split(',') if r.strip()])
    # Ensure the teams table exists before seeding (the full _SCHEMA runs after
    # this migration; create the one table we need here).
    conn.execute(
        'CREATE TABLE IF NOT EXISTS teams('
        ' id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, github_identity TEXT,'
        ' repos_json TEXT, lead_conversation_id TEXT,'
        ' enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)'
    )
    conn.execute(
        'INSERT OR IGNORE INTO teams(id, name, github_identity, repos_json, '
        'enabled, created_at) VALUES (?,?,?,?,1,?)',
        (DEFAULT_TEAM_ID, DEFAULT_TEAM_ID, identity, repos_json, now),
    )

    # Tables whose PRIMARY KEY does NOT change (id / autoincrement) — just add a
    # team_id column and backfill 'default'.
    for table in ('issues', 'comments', 'events'):
        if _table_exists(conn, table) and 'team_id' not in _table_columns(conn, table):
            conn.execute(
                f'ALTER TABLE {table} ADD COLUMN team_id TEXT NOT NULL '
                f"DEFAULT '{DEFAULT_TEAM_ID}'"
            )

    # Tables whose PRIMARY KEY CHANGES (role -> (team_id, role), etc.). SQLite
    # can't alter a PK, so rename→recreate (via _SCHEMA, run right after this)→
    # copy→drop. We rename the old table aside here and let init_db's executescript
    # create the new one, then copy in the tail of this function.
    for table in ('agents', 'inbox', 'sync_map', 'kv'):
        if _table_exists(conn, table) and 'team_id' not in _table_columns(conn, table):
            conn.execute(f'ALTER TABLE {table} RENAME TO {table}__v1')
    conn.commit()


def _copy_v1_pk_tables(conn: sqlite3.Connection) -> None:
    """Second half of the v1->v2 migration: copy rows from the renamed ``*__v1``
    tables (PK-changed tables) into the fresh v2 tables, then drop the old ones.
    Runs after ``_SCHEMA`` has created the v2 tables."""
    copies = {
        'agents': (
            'role, display_name, actor_kind, agent_kind, acp_server, '
            'github_identity, llm_model, launch_config_json, skills_json, '
            'created_by_role, enabled, created_at'
        ),
        'inbox': 'role, issue_id, reason, created_at',
        'sync_map': 'internal_id, github_ref, kind, last_synced_at',
        'kv': 'key, value',
    }
    for table, cols in copies.items():
        if _table_exists(conn, f'{table}__v1'):
            conn.execute(
                f'INSERT OR IGNORE INTO {table}(team_id, {cols}) '
                f"SELECT '{DEFAULT_TEAM_ID}', {cols} FROM {table}__v1"
            )
            conn.execute(f'DROP TABLE {table}__v1')
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema idempotently and stamp the version."""
    # v1->v2: renames PK-changing tables aside + adds team_id to the others.
    _migrate_v1_to_v2(conn)
    # Create the v2 tables (including fresh agents/inbox/sync_map/kv).
    conn.executescript(_SCHEMA)
    # Copy rows from the renamed *__v1 tables into the fresh v2 tables.
    _copy_v1_pk_tables(conn)
    conn.execute(
        'INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)',
        ('schema_version', SCHEMA_VERSION),
    )
    conn.commit()

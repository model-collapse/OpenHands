"""SQLite connection + schema init for the AI-team store.

DB lives under the app persistence dir (``~/.openhands/team.db`` by default),
alongside ``openhands.db``. Schema is created idempotently; additive migrations
follow the ``PRAGMA table_info`` + ``ALTER TABLE`` pattern used elsewhere in the
app-server (e.g. the github_poller).
"""

from __future__ import annotations

import os
import sqlite3

from openhands.app_server.config import get_default_persistence_dir


def default_db_path() -> str:
    override = os.getenv('TEAM_DB_PATH')
    if override:
        return override
    return str(get_default_persistence_dir() / 'team.db')


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS agents(
  role               TEXT PRIMARY KEY,
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
  created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues(
  id            TEXT PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS ix_issues_state ON issues(state);
CREATE UNIQUE INDEX IF NOT EXISTS ix_issues_github_ref
  ON issues(github_ref) WHERE github_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS comments(
  id                TEXT PRIMARY KEY,
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
  issue_id        TEXT,
  actor_role      TEXT NOT NULL,
  kind            TEXT NOT NULL,
  from_state      TEXT,
  to_state        TEXT,
  conversation_id TEXT,
  detail_json     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_issue ON events(issue_id, id);

CREATE TABLE IF NOT EXISTS inbox(
  role       TEXT NOT NULL,
  issue_id   TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(role, issue_id)
);

CREATE TABLE IF NOT EXISTS sync_map(
  internal_id    TEXT NOT NULL,
  github_ref     TEXT NOT NULL,
  kind           TEXT NOT NULL,
  last_synced_at TEXT NOT NULL,
  PRIMARY KEY(internal_id, kind)
);
"""

SCHEMA_VERSION = '1'


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema idempotently and stamp the version."""
    conn.executescript(_SCHEMA)
    conn.execute(
        'INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)',
        ('schema_version', SCHEMA_VERSION),
    )
    conn.commit()

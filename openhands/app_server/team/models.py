"""Enums and dataclasses for the AI-team store.

Mirrors the SQLite schema in ``db.py``. Kept dependency-light (stdlib enum +
dataclasses) so the store layer stays easy to unit-test with in-memory SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    """Issue lifecycle state. Mirrors the labels in ``labels.py``."""

    NEEDS_TRIAGE = 'needs_triage'
    INTERNAL = 'internal'
    ASSIGNED = 'assigned'
    DISCUSSING = 'discussing'
    AWAITING_GL = 'awaiting_gl'  # risk tripwire: held for grand-leader assent
    COMMITTED = 'committed'
    IN_PROGRESS = 'in_progress'
    IN_REVIEW = 'in_review'
    DONE = 'done'
    HALTED = 'halted'


#: States that are terminal — no loop should act on them.
TERMINAL_STATES: frozenset[State] = frozenset({State.DONE, State.HALTED})


class Priority(str, Enum):
    LOW = 'low'
    NORMAL = 'normal'
    HIGH = 'high'
    URGENT = 'urgent'


class Risk(str, Enum):
    HIGH = 'high'  # tripwire; NULL in the row means "no elevated risk"


class ActorKind(str, Enum):
    HUMAN = 'human'
    AGENT = 'agent'


class AgentKind(str, Enum):
    OPENHANDS = 'openhands'
    ACP = 'acp'


class Origin(str, Enum):
    INTERNAL = 'internal'
    GITHUB = 'github'


class Provenance(str, Enum):
    HUMAN = 'human'
    AGENT = 'agent'
    SYNCED_IN = 'synced_in'
    SYNCED_OUT = 'synced_out'


class EventKind(str, Enum):
    """Audit-spine event kinds. Append-only; the only record of what happened."""

    STATE_CHANGE = 'state_change'
    ASSIGN = 'assign'
    PRIORITY = 'priority'
    RISK = 'risk'
    COMMENT = 'comment'
    SPAWN = 'spawn'
    COMMIT = 'commit'
    HALT = 'halt'
    FORM_MEMBER = 'form_member'
    RECONFIGURE_MEMBER = 'reconfigure_member'
    DISABLE_MEMBER = 'disable_member'
    ROUND = 'round'
    STUCK = 'stuck'


# Reserved role names.
ROLE_GRAND_LEADER = 'grand_leader'
ROLE_LEAD = 'lead'


@dataclass
class Agent:
    role: str
    display_name: str
    actor_kind: ActorKind
    agent_kind: AgentKind | None = None
    acp_server: str | None = None
    github_identity: str | None = None
    llm_model: str | None = None
    launch_config_json: str | None = None
    skills_json: str | None = None
    created_by_role: str | None = None
    enabled: bool = True
    created_at: str | None = None


@dataclass
class Issue:
    id: str
    origin: Origin
    title: str
    state: State
    author_role: str
    priority: Priority = Priority.NORMAL
    body: str | None = None
    github_ref: str | None = None
    repo: str | None = None
    assignee_role: str | None = None
    risk: Risk | None = None
    round_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    stuck_since: str | None = None


@dataclass
class Comment:
    id: str
    issue_id: str
    author_role: str
    provenance: Provenance
    body: str
    addressed_to: str | None = None
    conversation_id: str | None = None
    github_comment_id: str | None = None
    created_at: str | None = None


@dataclass
class Event:
    id: int
    actor_role: str
    kind: EventKind
    created_at: str
    issue_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    conversation_id: str | None = None
    detail: dict = field(default_factory=dict)

"""Mapping between issue ``State`` and GitHub labels.

Labels are the externally-visible state (design §5). The sync bridge (P3) uses
these to publish state out and to detect human-applied label changes.
"""

from __future__ import annotations

from openhands.app_server.team.models import Priority, Risk, State

# state -> the single canonical label that encodes it (role-bearing states also
# add assigned:/waiting: labels, handled separately).
STATE_LABEL: dict[State, str] = {
    State.NEEDS_TRIAGE: 'needs-triage',
    State.INTERNAL: 'internal',
    State.ASSIGNED: 'assigned',
    State.DISCUSSING: 'discussing',
    State.AWAITING_GL: 'awaiting-grand-leader',
    State.COMMITTED: 'committed',
    State.IN_PROGRESS: 'in-progress',
    State.IN_REVIEW: 'in-review',
    State.DONE: 'done',
    State.HALTED: 'halted',
}
LABEL_STATE: dict[str, State] = {v: k for k, v in STATE_LABEL.items()}

PRIORITY_PREFIX = 'priority:'
RISK_LABEL = 'risk:high'
ASSIGNED_PREFIX = 'assigned:'
WAITING_PREFIX = 'waiting:'


def state_to_labels(
    *,
    state: State,
    assignee_role: str | None = None,
    waiting_role: str | None = None,
    priority: Priority = Priority.NORMAL,
    risk: Risk | None = None,
) -> list[str]:
    """Render the full GitHub label set for an issue's current state."""
    labels = [STATE_LABEL[state]]
    if assignee_role:
        labels.append(f'{ASSIGNED_PREFIX}{assignee_role}')
    if waiting_role:
        labels.append(f'{WAITING_PREFIX}{waiting_role}')
    if priority != Priority.NORMAL:
        labels.append(f'{PRIORITY_PREFIX}{priority.value}')
    if risk == Risk.HIGH:
        labels.append(RISK_LABEL)
    return labels


def labels_to_state(
    labels: list[str],
) -> tuple[State | None, str | None, str | None, Priority, Risk | None]:
    """Parse a GitHub label set back into (state, assignee, waiting, priority, risk).

    ``state`` is ``None`` if no managed state label is present.
    """
    state: State | None = None
    assignee: str | None = None
    waiting: str | None = None
    priority = Priority.NORMAL
    risk: Risk | None = None
    for label in labels:
        if label in LABEL_STATE:
            state = LABEL_STATE[label]
        elif label.startswith(ASSIGNED_PREFIX):
            assignee = label[len(ASSIGNED_PREFIX) :]
        elif label.startswith(WAITING_PREFIX):
            waiting = label[len(WAITING_PREFIX) :]
        elif label.startswith(PRIORITY_PREFIX):
            try:
                priority = Priority(label[len(PRIORITY_PREFIX) :])
            except ValueError:
                pass
        elif label == RISK_LABEL:
            risk = Risk.HIGH
    return state, assignee, waiting, priority, risk


def all_managed_labels() -> set[str]:
    """Every label this system owns (for creating them on a repo / filtering)."""
    labels = set(STATE_LABEL.values())
    labels.add(RISK_LABEL)
    labels.update(f'{PRIORITY_PREFIX}{p.value}' for p in Priority)
    return labels

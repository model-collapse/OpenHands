"""GitHub sync bridge for the team.

Mostly-one-way (design §7):
- **import-in**: external GitHub issues -> team issues (needs_triage), idempotent
  via ``sync_map``; new comments imported as ``synced_in`` EXCEPT those authored
  by the lead's identity (echo-loop guard).
- **label-change detection**: when a human applies a managed label on GitHub that
  maps to a state ahead of ours, transition the internal issue.
- **publish-out**: post lead-curated output as the lead identity, record the
  ``github_comment_id`` (so it is never re-ingested), and sync labels to match
  the internal state.

Per-repo cursors are stored in the team ``kv`` table with a lookback grace on
first sync (mirrors the github_poller). Datetime.now is only used to compute the
first-run lookback; every other timestamp comes from GitHub payloads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from openhands.app_server.team.github import GitHubClient
from openhands.app_server.team.influence import GrandLeaderInfluence
from openhands.app_server.team.labels import (
    STATE_LABEL,
    labels_to_state,
    state_to_labels,
)
from openhands.app_server.team.models import (
    ROLE_GRAND_LEADER,
    Origin,
    Provenance,
    State,
)
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)

# A human applying a GitHub label may only drive these transitions (design
# §5/§9): HALTED is always honored (the grand-leader escape hatch); COMMITTED is
# honored ONLY as grand-leader assent releasing an ``awaiting_gl`` issue (the
# risk-tripwire gate). A human must NOT be able to commit an arbitrary issue —
# committing is the lead's authority. Enforced in _apply_human_label_change.
HUMAN_LABEL_STATES = {State.COMMITTED, State.HALTED}

# Ordering used to decide whether a label maps to a state "ahead of" ours, so a
# stale label doesn't drag an issue backward.
_STATE_ORDER = {
    State.NEEDS_TRIAGE: 0,
    State.INTERNAL: 0,
    State.ASSIGNED: 1,
    State.DISCUSSING: 2,
    State.AWAITING_GL: 3,
    State.COMMITTED: 4,
    State.IN_PROGRESS: 5,
    State.IN_REVIEW: 6,
    State.DONE: 7,
    State.HALTED: 8,
}


class SyncBridge:
    def __init__(
        self,
        store: TeamStore,
        gh: GitHubClient,
        *,
        lead_identity: str,
        first_run_lookback_seconds: int = 600,
        influence: GrandLeaderInfluence | None = None,
    ) -> None:
        self.store = store
        self.gh = gh
        self.lead_identity = lead_identity
        self.first_run_lookback = first_run_lookback_seconds
        # Applies grand-leader influence (halt/approve/guidance) to each imported
        # supervisor comment. Defaults to a store-backed handler (P7).
        self.influence = influence or GrandLeaderInfluence(store)

    def _cursor_key(self, repo: str, stream: str) -> str:
        return f'sync_cursor:{repo}:{stream}'

    def _get_cursor(self, repo: str, stream: str) -> str:
        existing = self.store.kv_get(self._cursor_key(repo, stream))
        if existing:
            return existing
        return (
            datetime.now(timezone.utc) - timedelta(seconds=self.first_run_lookback)
        ).isoformat()

    def _set_cursor(self, repo: str, stream: str, since: str) -> None:
        self.store.kv_set(self._cursor_key(repo, stream), since)

    # -- import-in ---------------------------------------------------------
    async def import_repo(self, repo: str) -> None:
        await self._import_issues(repo)
        await self._import_comments(repo)

    async def _import_issues(self, repo: str) -> None:
        since = self._get_cursor(repo, 'issues')
        newest = since
        try:
            issues = await self.gh.list_issues(repo, since=since, state='open')
        except Exception as e:  # noqa: BLE001
            logger.error('sync: list_issues %s failed: %s', repo, e)
            return
        for it in issues:
            if it.get('pull_request'):
                continue  # /issues returns PRs too; skip them here
            updated = it.get('updated_at', '')
            newest = max(newest, updated or '')
            github_ref = f'{repo}#{it["number"]}'
            if self.store.is_synced(github_ref, 'issue'):
                # already imported — still check for human label changes
                self._apply_human_label_change(github_ref, it.get('labels', []))
                continue
            issue = self.store.create_issue(
                origin=Origin.GITHUB,
                title=it.get('title', ''),
                body=it.get('body') or '',
                author_role=ROLE_GRAND_LEADER,  # external human author
                repo=repo,
                github_ref=github_ref,
                state=State.NEEDS_TRIAGE,
            )
            self.store.map_sync(issue.id, github_ref, 'issue')
            logger.info('sync: imported %s -> %s', github_ref, issue.id)
        self._set_cursor(repo, 'issues', newest)

    async def _import_comments(self, repo: str) -> None:
        since = self._get_cursor(repo, 'comments')
        newest = since
        try:
            comments = await self.gh.list_issue_comments(repo, since=since)
        except Exception as e:  # noqa: BLE001
            logger.error('sync: list comments %s failed: %s', repo, e)
            return
        for cm in comments:
            newest = max(newest, cm.get('updated_at') or cm.get('created_at', ''))
            author = (cm.get('user') or {}).get('login', '')
            gh_comment_id = str(cm.get('id'))
            # Echo-loop guard: never re-ingest what the lead itself posted.
            if author == self.lead_identity:
                continue
            number = int(cm.get('issue_url', '/0').rsplit('/', 1)[-1])
            github_ref = f'{repo}#{number}'
            issue = self.store.get_issue_by_github_ref(github_ref)
            if issue is None:
                continue  # comment on an issue we haven't imported (e.g. a PR)
            if self.store.is_synced(gh_comment_id, 'comment'):
                continue
            # External human comments are influence directed at the lead; we
            # attribute them to the grand_leader role (the human supervisor).
            body = cm.get('body') or ''
            self.store.add_comment(
                issue_id=issue.id,
                author_role=ROLE_GRAND_LEADER,
                provenance=Provenance.SYNCED_IN,
                body=body,
                github_comment_id=gh_comment_id,
            )
            # Dedup key for comments is the github comment id. sync_map PK is
            # (internal_id, kind), so use the comment id for both columns to
            # keep each comment a distinct row that is_synced(id,'comment') hits.
            self.store.map_sync(gh_comment_id, gh_comment_id, 'comment')
            # Apply the comment's grand-leader influence (halt / approve /
            # guidance-to-lead). Import records the comment; influence acts on it.
            self.influence.apply(issue.id, body)
        self._set_cursor(repo, 'comments', newest)

    # -- label-change detection -------------------------------------------
    def _apply_human_label_change(self, github_ref: str, gh_labels: list[dict]) -> None:
        issue = self.store.get_issue_by_github_ref(github_ref)
        if issue is None:
            return
        names = [lbl.get('name', '') for lbl in gh_labels]
        target_state, _, _, _, _ = labels_to_state(names)
        if target_state is None or target_state == issue.state:
            return
        # Only honor a human label edit into the safe set, and only forward.
        if target_state not in HUMAN_LABEL_STATES:
            return
        if _STATE_ORDER.get(target_state, 0) <= _STATE_ORDER.get(issue.state, 0):
            return
        # The lead owns the commit gate (design §5). A human `committed` label is
        # honored ONLY as grand-leader assent on an awaiting_gl issue (releasing
        # the risk tripwire, §9) — never to commit an arbitrary issue. HALTED is
        # always allowed (escape hatch, §9).
        if target_state == State.COMMITTED and issue.state != State.AWAITING_GL:
            logger.info(
                'sync: ignoring human committed label on %s (state=%s); commit is '
                "the lead's gate — not honored outside awaiting_gl",
                github_ref,
                issue.state.value,
            )
            return
        reason = (
            'grand-leader assent released awaiting_gl via label'
            if target_state == State.COMMITTED
            else f'human applied GitHub label -> {target_state.value}'
        )
        self.store.transition(
            issue.id,
            to_state=target_state,
            actor_role=ROLE_GRAND_LEADER,
            reason=reason,
        )
        logger.info(
            'sync: %s advanced to %s via human label', github_ref, target_state.value
        )

    # -- publish-out -------------------------------------------------------
    async def publish_comment(self, issue_id: str, body: str) -> str | None:
        """Post a lead-curated comment out to GitHub and record its id so the
        import path skips it (echo-loop guard)."""
        issue = self.store.get_issue(issue_id)
        if issue is None or not issue.github_ref:
            return None
        repo, num = issue.github_ref.split('#')
        try:
            resp = await self.gh.create_comment(repo, int(num), body)
        except Exception as e:  # noqa: BLE001
            logger.error('sync: publish comment for %s failed: %s', issue.github_ref, e)
            return None
        gh_comment_id = str(resp.get('id'))
        self.store.add_comment(
            issue_id=issue_id,
            author_role='lead',
            provenance=Provenance.SYNCED_OUT,
            body=body,
            github_comment_id=gh_comment_id,
        )
        self.store.map_sync(gh_comment_id, gh_comment_id, 'comment')
        return gh_comment_id

    async def publish_labels(self, issue_id: str) -> None:
        """Sync the issue's GitHub labels to match its internal state."""
        issue = self.store.get_issue(issue_id)
        if issue is None or not issue.github_ref:
            return
        repo, num = issue.github_ref.split('#')
        labels = state_to_labels(
            state=issue.state,
            assignee_role=issue.assignee_role,
            priority=issue.priority,
            risk=issue.risk,
        )
        try:
            await self.gh.set_labels(repo, int(num), labels)
        except Exception as e:  # noqa: BLE001
            logger.error('sync: publish labels for %s failed: %s', issue.github_ref, e)


# Re-exported for callers that only need the managed label set.
MANAGED_STATE_LABELS = set(STATE_LABEL.values())

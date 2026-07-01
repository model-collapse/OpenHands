"""GitHub poller service — in-process, lifespan-managed.

Recreates the enterprise "agent auto-triggered by GitHub activity" loop on the
OSS app-server WITHOUT a public webhook endpoint. It polls the GitHub REST API
outbound on an interval for a set of watched repos and, on a qualifying
trigger, starts an OpenHands conversation via the same in-process app so the
agent (using the stored git-provider token) clones the repo, does the work,
opens a PR, and comments back.

This runs as one of the app's lifespan services: it starts and stops with the
backend process — it is not a separate task or daemon. It is opt-in via the
``ENABLE_GITHUB_POLLER`` env flag (see ``app.py``).

State (watched repos, per-stream cursors, processed-trigger dedupe) is stored
in a SQLite file under the app's persistence dir (``~/.openhands`` by default),
alongside ``openhands.db``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from openhands.app_server.config import get_default_persistence_dir

logger = logging.getLogger(__name__)

GITHUB_API = 'https://api.github.com'


def _cfg(name: str, default: str) -> str:
    return os.getenv(name, default)


class GitHubPollerService:
    """Polls GitHub and starts conversations. Managed as an app lifespan."""

    def __init__(self) -> None:
        self.token = os.getenv('GITHUB_POLLER_TOKEN', '')
        # Self-call the in-process app. Defaults to the local bind; override
        # with GITHUB_POLLER_SELF_URL if the server binds elsewhere.
        self.self_url = _cfg('GITHUB_POLLER_SELF_URL', 'http://127.0.0.1:3000')
        self.llm_model = os.getenv('GITHUB_POLLER_LLM_MODEL', '') or None
        self.interval = int(_cfg('GITHUB_POLLER_INTERVAL_SECONDS', '60'))
        self.trigger_label = _cfg('GITHUB_POLLER_LABEL', 'openhands')
        self.mention = _cfg('GITHUB_POLLER_MENTION', '@openhands')
        self.first_run_lookback = int(
            _cfg('GITHUB_POLLER_FIRST_RUN_LOOKBACK_SECONDS', '600')
        )
        self.db_path = str(get_default_persistence_dir() / 'github_poller.db')
        # How often to sync the watch list from active agents (0 disables).
        self.sync_interval = int(
            _cfg('GITHUB_POLLER_AGENT_SYNC_INTERVAL_SECONDS', '300')
        )
        # Also fold OpenHands "suggested tasks" repos into the watch list.
        self.sync_suggested = _cfg('GITHUB_POLLER_SYNC_SUGGESTED', 'true').lower() in (
            'true',
            '1',
        )
        self._task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None

    # -- lifespan protocol -------------------------------------------------
    async def __aenter__(self) -> 'GitHubPollerService':
        self.init_db()
        if not self.token:
            logger.warning(
                'GitHub poller enabled but GITHUB_POLLER_TOKEN is not set; '
                'polling will fail until it is provided.'
            )
        self._task = asyncio.create_task(self._run())
        if self.sync_interval > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(
            'GitHub poller started (interval=%ss, label=%r, mention=%r, db=%s)',
            self.interval,
            self.trigger_label,
            self.mention,
            self.db_path,
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        for task in (self._task, self._sync_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        logger.info('GitHub poller stopped')

    # -- persistence -------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # Streams monitored per repo.
    STREAMS = ('issues', 'issue_comments', 'pr_comments')

    def init_db(self) -> None:
        with self._db() as c:
            # Watched repos. ``source`` records why the repo is watched:
            # 'manual' (added via UI/API), 'agent_sync' (a running agent works
            # it), or 'suggested' (OpenHands suggested-tasks import).
            c.execute(
                'CREATE TABLE IF NOT EXISTS repos ('
                ' full_name TEXT PRIMARY KEY,'
                ' added_at TEXT NOT NULL,'
                ' enabled INTEGER NOT NULL DEFAULT 1,'
                " source TEXT NOT NULL DEFAULT 'manual')"
            )
            # Migrate older DBs that predate the ``source`` column.
            cols = {r['name'] for r in c.execute('PRAGMA table_info(repos)')}
            if 'source' not in cols:
                c.execute(
                    "ALTER TABLE repos ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
                )
            c.execute(
                'CREATE TABLE IF NOT EXISTS processed ('
                ' key TEXT PRIMARY KEY,'
                ' processed_at TEXT NOT NULL,'
                ' conversation_id TEXT)'
            )
            # Monitor tasks: one first-class, persisted row per (repo, stream).
            # ``since`` is the poll cursor; ``last_run``/``last_status``/
            # ``last_error`` capture the most recent poll outcome so the state
            # of every recurring monitor survives restarts and is inspectable.
            c.execute(
                'CREATE TABLE IF NOT EXISTS monitor_tasks ('
                ' repo TEXT NOT NULL,'
                ' stream TEXT NOT NULL,'
                ' enabled INTEGER NOT NULL DEFAULT 1,'
                ' since TEXT NOT NULL,'
                ' last_run TEXT,'
                ' last_status TEXT,'
                ' last_error TEXT,'
                ' PRIMARY KEY (repo, stream))'
            )

    def list_repos(self, only_enabled: bool = False) -> list[dict]:
        with self._db() as c:
            q = 'SELECT full_name, added_at, enabled, source FROM repos'
            if only_enabled:
                q += ' WHERE enabled = 1'
            return [dict(r) for r in c.execute(q + ' ORDER BY full_name').fetchall()]

    def add_repo(self, full_name: str, source: str = 'manual') -> bool:
        """Register a repo and create its per-stream monitor tasks.

        Returns True if the repo was newly added. Existing repos keep their
        original ``source`` (a manual add is not overwritten by a later sync).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as c:
            cur = c.execute(
                'INSERT OR IGNORE INTO repos(full_name, added_at, enabled, source) '
                'VALUES (?,?,1,?)',
                (full_name, now, source),
            )
            newly_added = cur.rowcount > 0
        if newly_added:
            # Create the recurring monitor tasks for this repo.
            since = (
                datetime.now(timezone.utc) - timedelta(seconds=self.first_run_lookback)
            ).isoformat()
            with self._db() as c:
                for stream in self.STREAMS:
                    c.execute(
                        'INSERT OR IGNORE INTO monitor_tasks(repo, stream, enabled, since) '
                        'VALUES (?,?,1,?)',
                        (full_name, stream, since),
                    )
        return newly_added

    def set_repo_enabled(self, full_name: str, enabled: bool) -> None:
        with self._db() as c:
            c.execute(
                'UPDATE repos SET enabled=? WHERE full_name=?',
                (1 if enabled else 0, full_name),
            )
            c.execute(
                'UPDATE monitor_tasks SET enabled=? WHERE repo=?',
                (1 if enabled else 0, full_name),
            )

    def delete_repo(self, full_name: str) -> None:
        with self._db() as c:
            c.execute('DELETE FROM repos WHERE full_name=?', (full_name,))
            c.execute('DELETE FROM monitor_tasks WHERE repo=?', (full_name,))

    def list_monitor_tasks(self) -> list[dict]:
        """All persisted monitor tasks with their last-run status."""
        with self._db() as c:
            return [
                dict(r)
                for r in c.execute(
                    'SELECT repo, stream, enabled, since, last_run, last_status, '
                    'last_error FROM monitor_tasks ORDER BY repo, stream'
                ).fetchall()
            ]

    def _already_processed(self, key: str) -> bool:
        with self._db() as c:
            return (
                c.execute('SELECT 1 FROM processed WHERE key=?', (key,)).fetchone()
                is not None
            )

    def _mark_processed(self, key: str, conversation_id: str | None) -> None:
        with self._db() as c:
            c.execute(
                'INSERT OR REPLACE INTO processed(key, processed_at, conversation_id) '
                'VALUES (?,?,?)',
                (key, datetime.now(timezone.utc).isoformat(), conversation_id),
            )

    def _get_cursor(self, repo: str, stream: str) -> str:
        with self._db() as c:
            row = c.execute(
                'SELECT since FROM monitor_tasks WHERE repo=? AND stream=?',
                (repo, stream),
            ).fetchone()
            if row:
                return row['since']
        # No monitor task yet (repo added before this row existed): look back a
        # grace window so a just-added repo still catches recent activity.
        return (
            datetime.now(timezone.utc) - timedelta(seconds=self.first_run_lookback)
        ).isoformat()

    def _set_cursor(self, repo: str, stream: str, since: str) -> None:
        with self._db() as c:
            c.execute(
                'INSERT OR IGNORE INTO monitor_tasks(repo, stream, enabled, since) '
                'VALUES (?,?,1,?)',
                (repo, stream, since),
            )
            c.execute(
                'UPDATE monitor_tasks SET since=? WHERE repo=? AND stream=?',
                (since, repo, stream),
            )

    def _record_run(
        self, repo: str, stream: str, status: str, error: str | None = None
    ) -> None:
        """Persist the outcome of a monitor task's most recent poll."""
        with self._db() as c:
            c.execute(
                'UPDATE monitor_tasks SET last_run=?, last_status=?, last_error=? '
                'WHERE repo=? AND stream=?',
                (
                    datetime.now(timezone.utc).isoformat(),
                    status,
                    error,
                    repo,
                    stream,
                ),
            )

    # -- github ------------------------------------------------------------
    def _gh_headers(self) -> dict[str, str]:
        return {
            'Authorization': f'token {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

    async def _gh_get(
        self, client: httpx.AsyncClient, path: str, params: dict | None = None
    ):
        r = await client.get(
            f'{GITHUB_API}{path}', headers=self._gh_headers(), params=params or {}
        )
        r.raise_for_status()
        return r.json()

    def _has_trigger_label(self, labels: list[dict]) -> bool:
        return any(
            lbl.get('name', '').lower() == self.trigger_label.lower() for lbl in labels
        )

    def _mentions_bot(self, text: str | None) -> bool:
        if not text:
            return False
        return self.mention.lower() in text.lower()

    # -- start conversation (in-process self-call) -------------------------
    def _build_initial_message(
        self, repo: str, kind: str, number: int, title: str, body: str, url: str
    ) -> str:
        return (
            f'You are working on the GitHub repository `{repo}`.\n\n'
            f'A {kind} (#{number}) requires your attention:\n'
            f'Title: {title}\n'
            f'Link: {url}\n\n'
            f'--- {kind} body ---\n{body or "(no body)"}\n--- end ---\n\n'
            'Please:\n'
            f'1. Clone/inspect the repo and fully resolve this {kind}.\n'
            '2. Make the necessary code changes on a new branch.\n'
            "3. Run the project's tests/build to verify your change.\n"
            f'4. Open a pull request that closes #{number}, and post a comment on '
            f'#{number} summarizing what you did and linking the PR.\n'
            'Use the configured GitHub credentials for all git and API operations.'
        )

    async def _start_conversation(
        self,
        client: httpx.AsyncClient,
        repo: str,
        kind: str,
        number: int,
        title: str,
        body: str,
        url: str,
    ) -> str | None:
        payload: dict = {
            'selected_repository': repo,
            'initial_message': {
                'role': 'user',
                'content': [
                    {
                        'type': 'text',
                        'text': self._build_initial_message(
                            repo, kind, number, title, body, url
                        ),
                    }
                ],
                'run': True,
            },
        }
        if self.llm_model:
            payload['llm_model'] = self.llm_model
        try:
            r = await client.post(
                f'{self.self_url}/api/v1/app-conversations',
                json=payload,
                timeout=60.0,
            )
            r.raise_for_status()
            data = r.json()
            cid = data.get('app_conversation_id') or data.get('id')
            logger.info(
                'GitHub poller started conversation %s for %s #%s', cid, repo, number
            )
            return cid
        except Exception as e:  # noqa: BLE001
            logger.error(
                'GitHub poller failed to start conversation for %s #%s: %s',
                repo,
                number,
                e,
            )
            return None

    # -- poll loop ---------------------------------------------------------
    async def _poll_repo(self, client: httpx.AsyncClient, repo: str) -> None:
        # 1) Issues opened (label-gated, or @mention in body)
        since = self._get_cursor(repo, 'issues')
        newest = since
        try:
            issues = await self._gh_get(
                client,
                f'/repos/{repo}/issues',
                {
                    'since': since,
                    'state': 'open',
                    'sort': 'updated',
                    'direction': 'asc',
                    'per_page': 50,
                },
            )
            for it in issues:
                if it.get('pull_request'):
                    continue  # /issues includes PRs; skip in the issues stream
                created = it.get('created_at', '')
                updated = it.get('updated_at', '')
                newest = max(newest, updated or created)
                if created <= since:
                    continue  # only newly-opened issues, not merely updated
                if not self._has_trigger_label(
                    it.get('labels', [])
                ) and not self._mentions_bot(it.get('body')):
                    continue
                key = f'issue:{repo}#{it["number"]}'
                if self._already_processed(key):
                    continue
                cid = await self._start_conversation(
                    client,
                    repo,
                    'issue',
                    it['number'],
                    it.get('title', ''),
                    it.get('body', ''),
                    it.get('html_url', ''),
                )
                self._mark_processed(key, cid)
            self._set_cursor(repo, 'issues', newest)
            self._record_run(repo, 'issues', 'ok')
        except Exception as e:  # noqa: BLE001
            logger.error('GitHub poller: issues %s: %s', repo, e)
            self._record_run(repo, 'issues', 'error', str(e))

        # 2) Issue/PR comments mentioning the bot
        since = self._get_cursor(repo, 'issue_comments')
        newest = since
        try:
            comments = await self._gh_get(
                client,
                f'/repos/{repo}/issues/comments',
                {'since': since, 'sort': 'created', 'direction': 'asc', 'per_page': 50},
            )
            for cm in comments:
                newest = max(newest, cm.get('updated_at') or cm.get('created_at', ''))
                if not self._mentions_bot(cm.get('body')):
                    continue
                key = f'comment:{cm["id"]}'
                if self._already_processed(key):
                    continue
                num = int(cm.get('issue_url', '/0').rsplit('/', 1)[-1])
                cid = await self._start_conversation(
                    client,
                    repo,
                    'issue comment',
                    num,
                    f'Comment by @{cm.get("user", {}).get("login", "?")}',
                    cm.get('body', ''),
                    cm.get('html_url', ''),
                )
                self._mark_processed(key, cid)
            self._set_cursor(repo, 'issue_comments', newest)
            self._record_run(repo, 'issue_comments', 'ok')
        except Exception as e:  # noqa: BLE001
            logger.error('GitHub poller: issue_comments %s: %s', repo, e)
            self._record_run(repo, 'issue_comments', 'error', str(e))

        # 3) PR review comments mentioning the bot
        since = self._get_cursor(repo, 'pr_comments')
        newest = since
        try:
            comments = await self._gh_get(
                client,
                f'/repos/{repo}/pulls/comments',
                {'since': since, 'sort': 'created', 'direction': 'asc', 'per_page': 50},
            )
            for cm in comments:
                newest = max(newest, cm.get('updated_at') or cm.get('created_at', ''))
                if not self._mentions_bot(cm.get('body')):
                    continue
                key = f'prcomment:{cm["id"]}'
                if self._already_processed(key):
                    continue
                num = int(cm.get('pull_request_url', '/0').rsplit('/', 1)[-1])
                cid = await self._start_conversation(
                    client,
                    repo,
                    'PR review comment',
                    num,
                    f'Review comment by @{cm.get("user", {}).get("login", "?")}',
                    cm.get('body', ''),
                    cm.get('html_url', ''),
                )
                self._mark_processed(key, cid)
            self._set_cursor(repo, 'pr_comments', newest)
            self._record_run(repo, 'pr_comments', 'ok')
        except Exception as e:  # noqa: BLE001
            logger.error('GitHub poller: pr_comments %s: %s', repo, e)
            self._record_run(repo, 'pr_comments', 'error', str(e))

    # -- agent-repo sync ---------------------------------------------------
    async def sync_from_agents(self, client: httpx.AsyncClient) -> list[str]:
        """Gather the repos that active/recent agents are working on and any
        OpenHands suggested-task repos, and register them as monitor tasks.

        This keeps the monitored set in sync with what agents are actually
        doing, rather than relying on a one-shot manual import. Returns the
        list of newly-added repos.
        """
        discovered: set[str] = set()

        # 1) Active/recent conversations -> their selected_repository.
        try:
            r = await client.get(
                f'{self.self_url}/api/v1/app-conversations/search?limit=100',
                timeout=30.0,
            )
            r.raise_for_status()
            for conv in r.json().get('items', []):
                repo = conv.get('selected_repository')
                if repo:
                    discovered.add(repo)
        except Exception as e:  # noqa: BLE001
            logger.error('GitHub poller: agent sync (conversations) failed: %s', e)

        # 2) Optionally, OpenHands suggested-task repos.
        if self.sync_suggested:
            try:
                r = await client.get(
                    f'{self.self_url}/api/v1/git/suggested-tasks/search?limit=100',
                    timeout=30.0,
                )
                r.raise_for_status()
                for t in r.json().get('items', []):
                    repo = t.get('repo')
                    if repo:
                        discovered.add(repo)
            except Exception as e:  # noqa: BLE001
                logger.error('GitHub poller: agent sync (suggested) failed: %s', e)

        existing = {x['full_name'] for x in self.list_repos()}
        added = []
        for repo in sorted(discovered):
            if repo not in existing and self.add_repo(repo, source='agent_sync'):
                added.append(repo)
        if added:
            logger.info(
                'GitHub poller: agent sync added %d repos: %s', len(added), added
            )
        return added

    async def _sync_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await self.sync_from_agents(client)
                except Exception as e:  # noqa: BLE001
                    logger.error('GitHub poller sync loop error: %s', e)
                await asyncio.sleep(self.sync_interval)

    async def _run(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    for r in self.list_repos(only_enabled=True):
                        await self._poll_repo(client, r['full_name'])
                except Exception as e:  # noqa: BLE001
                    logger.error('GitHub poller loop error: %s', e)
                await asyncio.sleep(self.interval)


# Module-level singleton so the router and the lifespan share one instance.
_service: GitHubPollerService | None = None


def get_github_poller_service() -> GitHubPollerService:
    global _service
    if _service is None:
        _service = GitHubPollerService()
    return _service

"""TeamService — the lifespan owner for the AI team (v2: multi-team).

Owns one SQLite connection (via a base ``TeamStore``) and hands out team-scoped
stores. The sweep and reactive loops iterate over *all enabled teams*, running
one scoped pass per team; a team with no repos/lead safely no-ops. Managed as an
app lifespan (like github_poller), gated by ``ENABLE_AI_TEAM``.

Per-team config (github identity, repos, lead model) comes from the ``teams``
row, falling back to process env (`TeamConfig`) for anything unset.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from openhands.app_server.team.config import TeamConfig
from openhands.app_server.team.github import GitHubClient
from openhands.app_server.team.lead import LeadBrain, _default_think
from openhands.app_server.team.reactive import ReactiveRouter
from openhands.app_server.team.spawn import AgentSpawner
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sweep import LeadSweep
from openhands.app_server.team.sync import SyncBridge

logger = logging.getLogger(__name__)


class TeamService:
    def __init__(self, config: TeamConfig | None = None) -> None:
        self.config = config or TeamConfig.from_env()
        self._base_store: TeamStore | None = None
        self._sweep_task: asyncio.Task | None = None
        self._reactive_task: asyncio.Task | None = None

    @property
    def base_store(self) -> TeamStore:
        """A store bound to the default team; also the owner of the connection
        and the entry point for cross-team ops (list_teams, create_team)."""
        if self._base_store is None:
            self._base_store = TeamStore(db_path=self.config.db_path)
        return self._base_store

    # Backwards-compatible alias: v1 code / tests referenced ``.store``.
    @property
    def store(self) -> TeamStore:
        return self.base_store

    def store_for(self, team_id: str) -> TeamStore:
        return self.base_store.for_team(team_id)

    # -- per-team effective config ----------------------------------------
    def _team_identity(self, team: dict) -> str | None:
        return team.get('github_identity') or self.config.github_identity

    def _team_repos(self, team: dict) -> list[str]:
        return team.get('repos') or self.config.repos

    # -- per-team component builders --------------------------------------
    def _build_sweep(self, store: TeamStore, team: dict) -> LeadSweep:
        cfg = self.config
        lead = LeadBrain(
            store, model=cfg.lead_model, round_cap=cfg.negotiation_round_cap
        )
        sync = None
        identity = self._team_identity(team)
        if cfg.github_token and identity:
            sync = SyncBridge(
                store,
                GitHubClient(cfg.github_token),
                lead_identity=identity,
                first_run_lookback_seconds=cfg.first_run_lookback,
            )
        return LeadSweep(
            store,
            lead,
            sync=sync,
            stuck_threshold=cfg.stuck_threshold,
            max_spawns=cfg.sweep_max_spawns,
        )

    def _build_reactive(self, store: TeamStore) -> ReactiveRouter:
        cfg = self.config
        spawner = AgentSpawner(cfg.self_url, store)
        think = _default_think(cfg.lead_model)

        async def assignee_eval(prompt: str) -> str:
            return think(prompt)

        async def status_fn(conversation_id: str) -> str | None:
            url = f'{cfg.self_url}/api/v1/app-conversations?ids={conversation_id}'
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(url, timeout=15.0)
                    r.raise_for_status()
                    items = r.json().get('items', [])
                    if items:
                        return items[0].get('execution_status')
            except Exception as e:  # noqa: BLE001
                logger.debug('status_fn(%s) failed: %s', conversation_id, e)
            return None

        return ReactiveRouter(
            store,
            spawner,
            engineer_status_fn=status_fn,
            assignee_eval_fn=assignee_eval,
        )

    def _build_import_bridge(self, store: TeamStore, team: dict) -> SyncBridge | None:
        cfg = self.config
        identity = self._team_identity(team)
        if not (cfg.github_token and identity):
            return None
        return SyncBridge(
            store,
            GitHubClient(cfg.github_token),
            lead_identity=identity,
            first_run_lookback_seconds=cfg.first_run_lookback,
        )

    # -- loops (iterate all enabled teams) --------------------------------
    def _enabled_teams(self) -> list[dict]:
        teams = self.base_store.list_teams(enabled_only=True)
        if not teams:
            # No explicit teams yet: fall back to the implicit 'default' team so
            # a bare ENABLE_AI_TEAM deployment still works (env identity/repos).
            return [
                {
                    'id': 'default',
                    'github_identity': self.config.github_identity,
                    'repos': self.config.repos,
                }
            ]
        return teams

    async def _sweep_loop(self) -> None:
        logger.info(
            'AI team lead sweep started (interval=%ss)', self.config.sweep_interval
        )
        while True:
            try:
                for team in self._enabled_teams():
                    store = self.store_for(team['id'])
                    sweep = self._build_sweep(store, team)
                    await sweep.run_once()
            except Exception as e:  # noqa: BLE001
                logger.error('AI team sweep pass failed: %s', e)
            await asyncio.sleep(self.config.sweep_interval)

    async def _reactive_loop(self) -> None:
        logger.info(
            'AI team reactive router started (interval=%ss)', self.config.sync_interval
        )
        while True:
            try:
                for team in self._enabled_teams():
                    store = self.store_for(team['id'])
                    import_bridge = self._build_import_bridge(store, team)
                    if import_bridge is not None:
                        for repo in self._team_repos(team):
                            await import_bridge.import_repo(repo)
                    router = self._build_reactive(store)
                    await router.run_once()
            except Exception as e:  # noqa: BLE001
                logger.error('AI team reactive pass failed: %s', e)
            await asyncio.sleep(self.config.sync_interval)

    # -- lifespan ----------------------------------------------------------
    async def __aenter__(self) -> 'TeamService':
        _ = self.base_store  # create/migrate the schema at startup
        logger.info('AI team service started (db=%s)', self.base_store.db_path)
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        self._reactive_task = asyncio.create_task(self._reactive_loop())
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        for task in (self._sweep_task, self._reactive_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._sweep_task = None
        self._reactive_task = None
        if self._base_store is not None:
            self._base_store.close()
            self._base_store = None
        logger.info('AI team service stopped')


_service: TeamService | None = None


def get_team_service() -> TeamService:
    global _service
    if _service is None:
        _service = TeamService()
    return _service

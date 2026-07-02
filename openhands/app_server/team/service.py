"""TeamService — the lifespan owner for the AI team.

Holds the shared ``TeamStore`` and config, and (from P5) starts/stops the sweep
and reactive loops. In P4 it only owns the store so the cockpit router can read
it; the loops are added later. Managed as an app lifespan (like github_poller),
gated by ``ENABLE_AI_TEAM``.
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
        self._store: TeamStore | None = None
        self._sweep_task: asyncio.Task | None = None
        self._reactive_task: asyncio.Task | None = None

    @property
    def store(self) -> TeamStore:
        if self._store is None:
            self._store = TeamStore(db_path=self.config.db_path)
        return self._store

    def _build_sweep(self) -> LeadSweep:
        cfg = self.config
        lead = LeadBrain(
            self.store,
            model=cfg.lead_model,
            round_cap=cfg.negotiation_round_cap,
        )
        sync = None
        if cfg.github_token and cfg.github_identity:
            sync = SyncBridge(
                self.store,
                GitHubClient(cfg.github_token),
                lead_identity=cfg.github_identity,
                first_run_lookback_seconds=cfg.first_run_lookback,
            )
        return LeadSweep(
            self.store,
            lead,
            sync=sync,
            stuck_threshold=cfg.stuck_threshold,
            max_spawns=cfg.sweep_max_spawns,
        )

    async def _sweep_loop(self) -> None:
        sweep = self._build_sweep()
        logger.info(
            'AI team lead sweep started (interval=%ss)', self.config.sweep_interval
        )
        while True:
            try:
                await sweep.run_once()
            except Exception as e:  # noqa: BLE001
                logger.error('AI team sweep pass failed: %s', e)
            await asyncio.sleep(self.config.sweep_interval)

    def _build_reactive(self) -> ReactiveRouter:
        cfg = self.config
        spawner = AgentSpawner(cfg.self_url, self.store)
        # The assignee's evaluation is (for now) a lead-model reasoning call over
        # the issue; grounding it in a full member-conversation clone/inspect is
        # a P8 refinement. It still returns the structured accept/concern verdict
        # the router expects.
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
            self.store,
            spawner,
            engineer_status_fn=status_fn,
            assignee_eval_fn=assignee_eval,
        )

    def _build_import_bridge(self) -> SyncBridge | None:
        cfg = self.config
        if not (cfg.github_token and cfg.github_identity):
            return None
        return SyncBridge(
            self.store,
            GitHubClient(cfg.github_token),
            lead_identity=cfg.github_identity,
            first_run_lookback_seconds=cfg.first_run_lookback,
        )

    async def _reactive_loop(self) -> None:
        router = self._build_reactive()
        import_bridge = self._build_import_bridge()
        logger.info(
            'AI team reactive router started (interval=%ss, repos=%s)',
            self.config.sync_interval,
            self.config.repos or '(none)',
        )
        while True:
            try:
                # Inbound: import external issues + grand-leader comments (which
                # apply their influence on import). No repos or no token -> skip.
                if import_bridge is not None:
                    for repo in self.config.repos:
                        await import_bridge.import_repo(repo)
                # Then advance state (assignee eval, execute, poll, ...).
                await router.run_once()
            except Exception as e:  # noqa: BLE001
                logger.error('AI team reactive pass failed: %s', e)
            await asyncio.sleep(self.config.sync_interval)

    # -- lifespan ----------------------------------------------------------
    async def __aenter__(self) -> 'TeamService':
        # Touch the store to create/migrate the schema at startup.
        _ = self.store
        logger.info('AI team service started (db=%s)', self.store.db_path)
        # Both loops are cheap when idle — they no-op over an empty issue set
        # until a lead + repos are configured.
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
        if self._store is not None:
            self._store.close()
            self._store = None
        logger.info('AI team service stopped')


_service: TeamService | None = None


def get_team_service() -> TeamService:
    global _service
    if _service is None:
        _service = TeamService()
    return _service

"""TeamService — the lifespan owner for the AI team.

Holds the shared ``TeamStore`` and config, and (from P5) starts/stops the sweep
and reactive loops. In P4 it only owns the store so the cockpit router can read
it; the loops are added later. Managed as an app lifespan (like github_poller),
gated by ``ENABLE_AI_TEAM``.
"""

from __future__ import annotations

import asyncio
import logging

from openhands.app_server.team.config import TeamConfig
from openhands.app_server.team.github import GitHubClient
from openhands.app_server.team.lead import LeadBrain
from openhands.app_server.team.store import TeamStore
from openhands.app_server.team.sweep import LeadSweep
from openhands.app_server.team.sync import SyncBridge

logger = logging.getLogger(__name__)


class TeamService:
    def __init__(self, config: TeamConfig | None = None) -> None:
        self.config = config or TeamConfig.from_env()
        self._store: TeamStore | None = None
        self._sweep_task: asyncio.Task | None = None

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

    # -- lifespan ----------------------------------------------------------
    async def __aenter__(self) -> 'TeamService':
        # Touch the store to create/migrate the schema at startup.
        _ = self.store
        logger.info('AI team service started (db=%s)', self.store.db_path)
        # Only run the lead sweep once a lead has been bootstrapped; otherwise
        # there is no one to triage with. Starting the task is cheap — it no-ops
        # over an empty issue set until repos/lead exist.
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        # NOTE: the reactive router (P6) will be started here too.
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sweep_task = None
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

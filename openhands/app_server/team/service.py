"""TeamService — the lifespan owner for the AI team.

Holds the shared ``TeamStore`` and config, and (from P5) starts/stops the sweep
and reactive loops. In P4 it only owns the store so the cockpit router can read
it; the loops are added later. Managed as an app lifespan (like github_poller),
gated by ``ENABLE_AI_TEAM``.
"""

from __future__ import annotations

import logging

from openhands.app_server.team.config import TeamConfig
from openhands.app_server.team.store import TeamStore

logger = logging.getLogger(__name__)


class TeamService:
    def __init__(self, config: TeamConfig | None = None) -> None:
        self.config = config or TeamConfig.from_env()
        self._store: TeamStore | None = None

    @property
    def store(self) -> TeamStore:
        if self._store is None:
            self._store = TeamStore(db_path=self.config.db_path)
        return self._store

    # -- lifespan ----------------------------------------------------------
    async def __aenter__(self) -> 'TeamService':
        # Touch the store to create/migrate the schema at startup.
        _ = self.store
        logger.info(
            'AI team service started (loops not yet enabled; db=%s)',
            self.store.db_path,
        )
        # P5 will start the sweep + reactive loops here.
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
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

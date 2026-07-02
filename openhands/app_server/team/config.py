"""Env-parsed configuration for the AI team.

All knobs are read from the environment with safe defaults so the feature is
inert unless ``ENABLE_AI_TEAM`` is set. Boolean flags accept ``'true'``/``'1'``
(matching the repo convention for Helm compatibility).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: str = 'false') -> bool:
    return os.getenv(name, default).lower() in ('true', '1')


@dataclass
class TeamConfig:
    enabled: bool
    self_url: str
    github_identity: str | None
    github_token: str
    repos: list[str]
    lead_model: str
    default_eng_model: str
    sync_interval: int
    sweep_interval: int
    stuck_threshold: int
    negotiation_round_cap: int
    sweep_max_spawns: int
    first_run_lookback: int
    db_path: str | None

    @classmethod
    def from_env(cls) -> 'TeamConfig':
        return cls(
            enabled=_flag('ENABLE_AI_TEAM'),
            self_url=os.getenv('TEAM_SELF_URL', 'http://127.0.0.1:3000'),
            github_identity=os.getenv('TEAM_GITHUB_IDENTITY') or None,
            # Reuse the poller's token env if the team-specific one is unset.
            github_token=(
                os.getenv('TEAM_GITHUB_TOKEN') or os.getenv('GITHUB_POLLER_TOKEN') or ''
            ),
            repos=[
                r.strip() for r in os.getenv('TEAM_REPOS', '').split(',') if r.strip()
            ],
            lead_model=os.getenv(
                'TEAM_LEAD_MODEL', 'bedrock/us.anthropic.claude-sonnet-4-6'
            ),
            default_eng_model=os.getenv(
                'TEAM_DEFAULT_ENG_MODEL', 'bedrock/us.anthropic.claude-opus-4-8'
            ),
            sync_interval=int(os.getenv('TEAM_SYNC_INTERVAL', '60')),
            sweep_interval=int(os.getenv('TEAM_SWEEP_INTERVAL', '900')),
            stuck_threshold=int(os.getenv('TEAM_STUCK_THRESHOLD', '1800')),
            negotiation_round_cap=int(os.getenv('TEAM_NEGOTIATION_ROUND_CAP', '3')),
            sweep_max_spawns=int(os.getenv('TEAM_SWEEP_MAX_SPAWNS', '5')),
            first_run_lookback=int(os.getenv('TEAM_FIRST_RUN_LOOKBACK_SECONDS', '600')),
            db_path=os.getenv('TEAM_DB_PATH') or None,
        )

"""Minimal GitHub REST client for the team sync bridge.

Standalone (mirrors the github_poller's helper style) rather than coupling to
the poller service, so it can be unit-tested with a fake transport. Covers the
read paths the bridge imports from and the write paths the lead publishes with
(comment, labels). All writes go out as the lead's identity (the token).
"""

from __future__ import annotations

from typing import Any

import httpx

GITHUB_API = 'https://api.github.com'


class GitHubClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self.token = token
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> 'GitHubClient':
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            'Authorization': f'token {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

    async def get(self, path: str, params: dict | None = None) -> Any:
        assert self._client is not None
        r = await self._client.get(
            f'{GITHUB_API}{path}', headers=self._headers(), params=params or {}
        )
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, body: dict) -> Any:
        assert self._client is not None
        r = await self._client.post(
            f'{GITHUB_API}{path}', headers=self._headers(), json=body
        )
        r.raise_for_status()
        return r.json()

    async def put(self, path: str, body: dict) -> Any:
        assert self._client is not None
        r = await self._client.put(
            f'{GITHUB_API}{path}', headers=self._headers(), json=body
        )
        r.raise_for_status()
        return r.json()

    # -- reads (import-in) -------------------------------------------------
    async def list_issues(
        self, repo: str, *, since: str, state: str = 'open', per_page: int = 50
    ) -> list[dict]:
        return await self.get(
            f'/repos/{repo}/issues',
            {
                'since': since,
                'state': state,
                'sort': 'updated',
                'direction': 'asc',
                'per_page': per_page,
            },
        )

    async def list_issue_comments(
        self, repo: str, *, since: str, per_page: int = 50
    ) -> list[dict]:
        return await self.get(
            f'/repos/{repo}/issues/comments',
            {
                'since': since,
                'sort': 'created',
                'direction': 'asc',
                'per_page': per_page,
            },
        )

    async def get_issue(self, repo: str, number: int) -> dict:
        return await self.get(f'/repos/{repo}/issues/{number}')

    # -- writes (publish-out) as the lead identity ------------------------
    async def create_comment(self, repo: str, number: int, body: str) -> dict:
        return await self.post(
            f'/repos/{repo}/issues/{number}/comments', {'body': body}
        )

    async def set_labels(self, repo: str, number: int, labels: list[str]) -> dict:
        # PUT replaces the full label set on the issue.
        return await self.put(
            f'/repos/{repo}/issues/{number}/labels', {'labels': labels}
        )

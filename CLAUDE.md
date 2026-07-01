# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read AGENTS.md first

`AGENTS.md` (repo root) is the authoritative, detailed guide for working in this repo. It covers pre-commit
requirements, git/lockfile/GitHub-Actions conventions, the `.pr/` directory, and deep implementation notes
(conversation state, microagents, adding LLM models, adding settings, enterprise testing). Prefer it over
reproducing details here. `Development.md` covers OS-specific environment setup.

## Repository layout

This is OpenHands Agent Canvas: a Python backend + React frontend, plus an enterprise/SaaS overlay.

- `openhands/` — Python backend package (Python 3.12, `^3.12,<3.14`).
  - `openhands/app_server/` — the current **V1** application server: FastAPI REST API (conversations,
    sandboxes, events, secrets, settings, user, git, mcp, integrations). See its `README.md`.
  - `openhands/server/` — legacy server entrypoint. `make start-backend` launches
    `openhands.server.listen:app`, which **includes the V1 `app_server` routes by default** unless `ENABLE_V1=0`.
  - `openhands/app_server/integrations/vscode/` — the VSCode extension (separate Node project).
- `frontend/` — React SPA (Remix SPA mode: React Router + Vite, TypeScript, Redux, TanStack Query,
  Tailwind, i18next, Vitest, MSW). The agent/SDK code itself lives in the separate
  `OpenHands/software-agent-sdk` repo (see README note about code moving).
- `openhands-ui/` — shared UI component library.
- `enterprise/` — Polyform-licensed overlay extending the OSS server via dynamic imports; entrypoint
  `enterprise/saas_server.py` (sets `OPENHANDS_CONFIG_CLS=server.config.SaaSServerConfig`). Adds Keycloak
  auth, Alembic migrations (`enterprise/migrations/`), Stripe billing, telemetry, and GitHub/GitLab/Jira/
  Linear/Slack integrations. Enterprise code uses **relative imports without the `enterprise.` prefix**
  (e.g. `from storage.database import ...`) so it works in both contexts.
- `tests/unit/test_*.py` — backend unit tests (pytest). `skills/` — skills architecture.

## Common commands

Backend (run from repo root with `poetry run`):
- Build everything (backend + frontend): `make build`
- Run full app: `make run` (backend :3000, frontend :3001); or `make start-backend` / `make start-frontend`
- Run for local self-dogfooding: `export INSTALL_DOCKER=0 RUNTIME=local && make build && make run`
- Single test: `poetry run pytest tests/unit/test_xxx.py`
- Lint (staged files): `pre-commit run --config ./dev_config/python/.pre-commit-config.yaml`
- **Always run `make install-pre-commit-hooks` before making changes.**

Frontend (run from `frontend/`):
- Dev with mocked backend: `npm run dev:mock` — Dev against real backend: `npm run dev`
- Test: `npm run test` — single test: `npm run test -- -t "TestName"`
- Lint+fix and build (required before push): `npm run lint:fix && npm run build`
- `npm run lint` runs typecheck + eslint + prettier check.

VSCode extension (from `openhands/app_server/integrations/vscode/`):
- `npm run lint:fix && npm run compile` (required before push if edited); `npm run typecheck`, `npm run test`.

Enterprise (from `enterprise/`, after `make build` at root):
- Install: `poetry install --with dev,test` (slow). Run: `make start-backend` or `make run`.
- Lint (match CI exactly): `poetry run pre-commit run --all-files --show-diff-on-failure --config ./dev_config/python/.pre-commit-config.yaml`
- Tests use SQLite in-memory DBs; run per-module with `--confcutdir=tests/unit/[module]`.

## Before pushing (mandatory)

Pre-commit hooks must pass. If you touched the backend run the pre-commit config; if you touched the
frontend run `npm run lint:fix && npm run build`; if you touched the VSCode extension run its
`lint:fix && compile`. Fix any mypy / ruff / formatting issues that aren't auto-fixed, then re-run.

## Frontend data-flow rule

Strict layering: **UI components → TanStack Query hooks → Data Access Layer (`frontend/src/api`) → API endpoints.**
Never call `frontend/src/api` clients directly from components; always wrap in a query/mutation hook.
Query hooks live in `frontend/src/hooks/query/` named `use[Resource]`; mutation hooks in
`frontend/src/hooks/mutation/` named `use[Action]`.

## Environment-variable enable toggles

Boolean feature toggles read from env vars must accept **both `'true'` and `'1'`** (Helm charts default to `'1'`):
`os.getenv('MY_FEATURE', 'false').lower() in ('true', '1')`. Add a test for the `'1'` case.

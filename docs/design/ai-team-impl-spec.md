# AI Team — Implementation Spec

**Companion to:** [`ai-team.md`](./ai-team.md) (the design; read it for *why*).
This doc is the *how*: concrete module layout, schema DDL, API contracts,
signatures, prompts, config, and per-phase acceptance criteria. Developed in
the `model-collapse/OpenHands` fork.

Conventions reused from the existing `github_poller` module (verified in tree):
in-process lifespan services wired into `openhands/app_server/app.py`; SQLite
under `get_default_persistence_dir()` (`~/.openhands`); env-gated feature flag;
routers mounted under `/api/v1`; agents spawned via `POST /api/v1/app-conversations`.

---

## 0. Feature flag & module layout

Enable with `ENABLE_AI_TEAM=1` (accepts `'true'`/`'1'`, per repo convention).
When off, nothing loads (inert in tests / other deployments).

```
openhands/app_server/team/                 # NEW package
  __init__.py
  config.py            # TeamConfig: env parsing, thresholds, model routing
  db.py                # sqlite connection + schema init + migrations
  store.py             # data-access layer (agents/issues/comments/events/inbox/sync_map)
  models.py            # pydantic models + enums (State, Priority, AgentKind, ...)
  events.py            # audit-spine helpers: record_event(...) — the ONLY writer of state changes
  labels.py            # label <-> state mapping; canonical label strings
  spawn.py             # AgentSpawner: kind-aware conversation launch (openhands | acp)
  sync.py              # GitHub sync bridge (import-in / publish-out, provenance, sync_map)
  sweep.py             # lead sweep loop (time-driven manager pass)
  reactive.py          # reactive router (event-driven: comment/label change)
  lead.py              # lead agent orchestration: triage/decide/commit prompts
  bootstrap.py         # team formation (§2b): spawn lead, form roster
  service.py           # TeamService: lifespan owner; starts/stops the loops
  router.py            # cockpit API + UI  (/api/v1/team/...)
  prompts/             # jinja2 templates for lead/assignee/formation messages
    triage.j2  assign.j2  commit_decision.j2  assignee_eval.j2  formation.j2
  README.md
```

Wiring in `app.py` (mirror the `github_poller` block):
```python
_ai_team_enabled = os.getenv('ENABLE_AI_TEAM', 'false').lower() in ('true', '1')
if _ai_team_enabled:
    from openhands.app_server.team.service import get_team_service
    _team = get_team_service()

    @contextlib.asynccontextmanager
    async def _team_lifespan(app):
        async with _team:
            yield
    lifespans.append(_team_lifespan)
# ... after include_router(v1_router.router):
if _ai_team_enabled:
    from openhands.app_server.team.router import router as team_router
    app.include_router(team_router, prefix='/api/v1')
```

The existing `github_poller` is **not** deleted; `sync.py` reuses its GitHub
client helpers and polling/cursor patterns (see §6).

---

## 1. Schema (SQLite DDL — `~/.openhands/team.db`)

`db.py::init_db()` creates these; `schema_version` gates additive migrations
(same `PRAGMA table_info` + `ALTER TABLE` pattern the poller already uses).

```sql
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS agents(
  role              TEXT PRIMARY KEY,          -- 'grand_leader','lead','eng:backend',...
  display_name      TEXT NOT NULL,
  actor_kind        TEXT NOT NULL,             -- 'human' | 'agent'
  agent_kind        TEXT,                      -- 'openhands' | 'acp' (NULL for human)
  acp_server        TEXT,                      -- 'claude-code'|'codex'|'gemini-cli' (acp only)
  github_identity   TEXT,                      -- only the lead
  llm_model         TEXT,
  launch_config_json TEXT,                     -- JSON: acp_command/env, or oh agent cfg
  skills_json       TEXT,                      -- JSON list (openhands members)
  created_by_role   TEXT,                      -- 'lead' for formed members
  enabled           INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues(
  id            TEXT PRIMARY KEY,              -- internal uuid (hex)
  origin        TEXT NOT NULL,                 -- 'internal' | 'github'
  github_ref    TEXT,                          -- 'owner/repo#12' (github origin)
  repo          TEXT,                          -- 'owner/repo' for work targeting a repo
  title         TEXT NOT NULL,
  body          TEXT,
  author_role   TEXT NOT NULL,
  assignee_role TEXT,
  state         TEXT NOT NULL,                 -- see §2
  priority      TEXT NOT NULL DEFAULT 'normal',-- low|normal|high|urgent
  risk          TEXT,                          -- NULL|'high' (tripwire, §5 design)
  round_count   INTEGER NOT NULL DEFAULT 0,    -- negotiation rounds used
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  stuck_since   TEXT                           -- set when waiting; cleared on progress
);
CREATE INDEX IF NOT EXISTS ix_issues_state ON issues(state);
CREATE UNIQUE INDEX IF NOT EXISTS ix_issues_github_ref ON issues(github_ref)
  WHERE github_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS comments(
  id                TEXT PRIMARY KEY,
  issue_id          TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  author_role       TEXT NOT NULL,
  addressed_to      TEXT,                      -- role or NULL
  provenance        TEXT NOT NULL,             -- human|agent|synced_in|synced_out
  conversation_id   TEXT,                      -- link to the reasoning (sandbox events)
  github_comment_id TEXT,                      -- external mapping / echo-loop guard
  body              TEXT NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comments_issue ON comments(issue_id, created_at);

CREATE TABLE IF NOT EXISTS events(                       -- THE AUDIT SPINE (append-only)
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id        TEXT,                          -- NULL for team-level events
  actor_role      TEXT NOT NULL,
  kind            TEXT NOT NULL,                 -- 'state_change','assign','comment',
                                                 -- 'spawn','commit','halt','form_member',...
  from_state      TEXT,
  to_state        TEXT,
  conversation_id TEXT,
  detail_json     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_issue ON events(issue_id, id);

CREATE TABLE IF NOT EXISTS inbox(                        -- "who acts next" — drives loops
  role       TEXT NOT NULL,
  issue_id   TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(role, issue_id)
);

CREATE TABLE IF NOT EXISTS sync_map(                     -- echo-loop / dedup
  internal_id    TEXT NOT NULL,
  github_ref     TEXT NOT NULL,
  kind           TEXT NOT NULL,                 -- 'issue' | 'comment'
  last_synced_at TEXT NOT NULL,
  PRIMARY KEY(internal_id, kind)
);
```

**Invariant:** state/assignment/priority mutations go through `store.py` methods
that also call `events.record_event(...)` in the same transaction. No code path
mutates `issues.state` without writing an event (enforced by keeping the raw
UPDATE private to `store.py`).

---

## 2. Labels ↔ state (`labels.py`)

Canonical label strings (also the GitHub labels the sync bridge manages):

| State value        | GitHub label(s)                    | Notes |
|--------------------|------------------------------------|-------|
| `needs_triage`     | `needs-triage`                     | default for human-opened external |
| `internal`         | `internal`                         | agent-raised (type 2) |
| `assigned`         | `assigned:<role>`, `waiting:<role>`| role encoded in label suffix |
| `discussing`       | `discussing`, `waiting:<role>`     | negotiation open |
| `awaiting_gl`      | `awaiting-grand-leader`            | risk tripwire held (§5) |
| `committed`        | `committed`                        | → engineer executes |
| `in_progress`      | `in-progress`                      | agent working |
| `in_review`        | `in-review`                        | PR open |
| `done`             | `done` / issue closed              | terminal |
| `halted`           | `halted`                           | grand-leader stop |

Priority: `priority:low|normal|high|urgent`. Risk: `risk:high`.
`labels.py` provides `state_to_labels(issue) -> list[str]` and
`labels_to_state(labels) -> (state, assignee_role, priority, risk)`.

---

## 3. Core models & store API

`models.py` — pydantic + `StrEnum`s: `State`, `Priority`, `AgentKind`,
`ActorKind`, `Provenance`, `EventKind`. Dataclass-ish `Agent`, `Issue`,
`Comment`, `Event` mirroring the tables.

`store.py::TeamStore` (sync sqlite, wrapped via `call_sync_from_async` when
used from async loops — same approach the poller uses for boto3):

```python
class TeamStore:
    # agents
    def upsert_agent(self, agent: Agent) -> None
    def get_agent(self, role: str) -> Agent | None
    def list_agents(self, enabled_only: bool = False) -> list[Agent]

    # issues  (every state-changing method writes an event in-txn)
    def create_issue(self, *, origin, title, body, author_role, repo=None,
                     github_ref=None, state, priority='normal') -> Issue
    def get_issue(self, issue_id: str) -> Issue | None
    def get_issue_by_github_ref(self, github_ref: str) -> Issue | None
    def list_issues(self, *, states=None, assignee=None) -> list[Issue]
    def transition(self, issue_id: str, *, to_state: State, actor_role: str,
                   assignee_role: str | None = None, reason: str,
                   conversation_id: str | None = None) -> Issue
    def set_priority(self, issue_id, priority, actor_role, reason) -> None
    def set_risk(self, issue_id, risk, actor_role, reason) -> None
    def bump_round(self, issue_id) -> int
    def mark_stuck(self, issue_id, since: str | None) -> None

    # comments / events / inbox / sync_map ... (CRUD + queries)
    def add_comment(self, ...) -> Comment
    def inbox_add(self, role, issue_id, reason) -> None
    def inbox_take(self, role) -> list[Issue]
    def map_sync(self, internal_id, github_ref, kind) -> None
    def is_synced(self, github_ref, kind) -> bool
```

`events.py::record_event(store, **fields)` is the single append path; called
inside `store.transition` etc. Exposed separately for non-state events
(`spawn`, `form_member`, `halt`).

---

## 4. Agent spawn adapter (`spawn.py`)

The one abstraction that makes members interchangeable. Kind-aware; returns the
new `conversation_id`.

```python
class AgentSpawner:
    def __init__(self, self_url: str, store: TeamStore): ...

    async def spawn(self, *, role: str, issue: Issue, instruction: str,
                    run: bool = True) -> str | None:
        agent = self.store.get_agent(role)
        payload = {
            "selected_repository": issue.repo,
            "initial_message": {"role": "user",
                                "content": [{"type": "text", "text": instruction}],
                                "run": run},
        }
        if agent.agent_kind == "openhands":
            if agent.llm_model: payload["llm_model"] = agent.llm_model
            # skills/agent config applied via profile or agent_settings (see below)
        elif agent.agent_kind == "acp":
            # per §12: ACP is first-class; agent_kind/acp_server carried via the
            # member's saved profile OR a per-request agent field.
            payload["agent"] = agent.launch_config_json  # {agent_kind:'acp', acp_server:..., acp_model:...}
        cid = await POST(f"{self_url}/api/v1/app-conversations", payload)
        record_event(self.store, issue_id=issue.id, actor_role=role,
                     kind="spawn", conversation_id=cid, detail_json={"agent_kind": agent.agent_kind})
        return cid
```

**Open implementation detail (spiked in Phase 2):** whether per-member agent
config rides `POST /app-conversations` as an `agent` field or as a **saved LLM
profile per member** (the app-server already supports profiles + `switch_profile`).
The spike picks one; `spawn.py` hides it behind this interface either way.

Every spawned conversation's `conversation_id` is stored on the comment/event so
the cockpit can deep-link to the on-disk sandbox events (the reasoning).

---

## 5. Loops (lifespan tasks in `service.py`)

`TeamService.__aenter__` starts three asyncio tasks (like the poller's
`_run`/`_sync_loop`), cancels them on `__aexit__`. All catch-and-log per
iteration; all honor a global `halted` check.

### 5a. Sync loop (`sync.py`) — interval `TEAM_SYNC_INTERVAL` (default 60s)
- **Import-in:** for each watched repo (reuse poller's repo list / cursors),
  pull issues + comments since cursor. For each external issue not in `sync_map`,
  `create_issue(origin='github', github_ref=..., state=needs_triage)`; map it.
  Import new comments as `provenance='synced_in'` unless authored by the lead's
  `github_identity` (echo-loop guard) — those are skipped.
- **Label-change detection (NEW, design §12):** also fetch issues filtered by the
  managed labels; when a label maps to a state ahead of our stored state (e.g. a
  human added `committed`/`halted`), transition accordingly. Dedup on
  (github_ref, label, applied_at) via `sync_map`/events.
- **Publish-out:** for issues with pending lead-curated output (a comment flagged
  `to_publish`), post to GitHub via the lead identity, record `github_comment_id`,
  mark `provenance='synced_out'`, update labels to match state.

### 5b. Lead sweep (`sweep.py`) — interval `TEAM_SWEEP_INTERVAL` (default 900s)
Manager pass over open issues (design §6a). Per state:
- `needs_triage` → `lead.triage(issue)`: read issue + repo context, choose
  assignee, `transition(assigned, assignee)`, `inbox_add(assignee)`, publish
  assignment rationale.
- `discussing` / `waiting:lead` → `lead.decide(issue)`: if `round_count >= cap`
  force-decide; else commit / push-back / reassign. **Risk gate:** if
  `issue.risk == 'high'` → `transition(awaiting_gl)` + escalate instead of
  `committed`.
- `internal` → `lead.weigh(issue)` sets priority, pings stakeholders.
- Escalation pass: `stuck_since > STUCK_THRESHOLD`, failed, or `awaiting_gl`
  → `inbox_add('grand_leader', ...)`.
- Budgeted: at most `TEAM_SWEEP_MAX_SPAWNS` conversations per pass.

### 5c. Reactive router (`reactive.py`) — fed by the sync loop's new-comment/label events
- comment on `waiting:<role>` (role != lead) → `spawn(role, assignee_eval)`:
  assignee clones/inspects repo, accepts or raises concerns; `bump_round`;
  set `waiting:lead`.
- `committed` label present & state advanced → `spawn(engineer, execute)` (the
  proven full-auto path) → `in_progress`.
- comment authored by `grand_leader` → `inbox_add('lead', reason='gl_guidance')`
  at high priority; the *lead* acts next sweep (never a direct state flip). A
  body containing the `halt` directive sets the global halt + issue `halted`
  immediately.

---

## 6. Reuse of `github_poller`

`sync.py` imports and reuses (not forks):
- `_gh_headers`, `gh_get` HTTP helpers,
- the watched-repo table + agent-repo sync (the team's watched repos = repos its
  agents work),
- the cursor/dedup mechanics and `FIRST_RUN_LOOKBACK` grace window.

New in the bridge vs. today's poller: writes go to `team_issues` instead of
directly starting conversations; **label-change detection** is added; **publish-out**
is new. The poller's "issue opened / @mention → conversation" trigger is
subsumed by "import → needs_triage → lead triage".

---

## 7. Cockpit API + UI (`router.py`, prefix `/api/v1/team`)

Read-mostly (design §9/§10). No state-mutation endpoints in v1 — the human acts
through GitHub.

```
GET  /health                      -> {status, enabled_loops, watched_repos, halted}
GET  /dashboard?filter=attention  -> issues grouped by state;
                                     default filter = awaiting_gl + stuck>N + failed
GET  /issues                      -> list (filters: state, assignee, repo, priority)
GET  /issues/{id}                 -> issue + comments + event timeline;
                                     each event includes conversation_id + a
                                     deep link to sandbox events (reasoning)
GET  /agents                      -> roster: role, kind, model, load, last_activity
GET  /events?issue_id=&limit=     -> raw audit spine (debugging)
GET  /ui                          -> single grouped board (evolve poller UI; no GH parity)
POST /bootstrap                   -> (guarded) kick off team formation (§2b) if no lead
```

`/issues/{id}` is the debug centerpiece: for each event with a `conversation_id`,
link to the existing conversation/event view so the human drills from decision →
the agent's actual reasoning.

---

## 8. Lead prompting (`lead.py` + `prompts/`)

Each lead action is a spawned lead conversation with a rendered template; the
lead's structured reply drives a `store` transition. Templates:
- `triage.j2` — inputs: issue title/body, repo, roster (roles+kinds+skills).
  Output contract (JSON): `{assignee_role, rationale, risk: 'high'|null, priority}`.
- `assignee_eval.j2` — assignee clones repo, returns
  `{decision: 'accept'|'concern', notes}`.
- `commit_decision.j2` — inputs: thread + round_count. Output:
  `{action: 'commit'|'pushback'|'reassign'|'await_gl', rationale, new_assignee?}`.
- `formation.j2` — inputs: grand-leader's stated needs. Output: list of
  `{role, agent_kind, acp_server?, llm_model, skills[]}` to write as `agents` rows.

Structured output is validated (retry on mismatch) before any state change, so a
malformed lead reply never corrupts the machine.

---

## 9. Config (`config.py`, env-parsed)

| Env | Default | Meaning |
|-----|---------|---------|
| `ENABLE_AI_TEAM` | `false` | master flag |
| `TEAM_SELF_URL` | `http://127.0.0.1:3000` | in-process self-call base |
| `TEAM_SYNC_INTERVAL` | `60` | sync loop seconds |
| `TEAM_SWEEP_INTERVAL` | `900` | lead sweep seconds (15m) |
| `TEAM_STUCK_THRESHOLD` | `1800` | seconds before an issue is "stuck" |
| `TEAM_NEGOTIATION_ROUND_CAP` | `3` | rounds before lead force-decides |
| `TEAM_SWEEP_MAX_SPAWNS` | `5` | conversation budget per sweep pass |
| `TEAM_LEAD_MODEL` | `bedrock/us.anthropic.claude-sonnet-4-6` | cheaper model for lead/triage |
| `TEAM_DEFAULT_ENG_MODEL` | `bedrock/us.anthropic.claude-opus-4-8` | engineering |
| `TEAM_RISK_LABELS` | `migrations,auth,release` | path/keyword hints → `risk:high` |
| `TEAM_GITHUB_IDENTITY` | (required) | the lead's GitHub login |

Reuses the stored git-provider token (already set via
`POST /api/v1/secrets/git-providers`) for all GitHub writes.

---

## 10. Build phases & acceptance criteria

Maps design §11. Each phase is independently testable; unit tests use an
in-memory sqlite (`:memory:`) per repo convention.

**P1 — Spine + store.** `db.py`, `store.py`, `events.py`, `models.py`, `labels.py`.
✅ *Accept:* create issue → transition through every state; each transition emits
exactly one event; state cannot change without an event (test asserts the raw
UPDATE is unreachable outside `store`); `labels_to_state` round-trips.

**P2 — ACP spike + bootstrap.** First run an ACP/Claude-Code conversation on this
box (close the one real unknown, design §12). Then `spawn.py`, `bootstrap.py`.
✅ *Accept:* spawn an `openhands` member and an `acp` member for a trivial task,
both return a `conversation_id` and produce output; `bootstrap` creates a `lead`
row and, from a formation reply, writes ≥1 member row (all as events).

**P3 — Sync bridge.** `sync.py` import-in + label-change detection + publish-out;
reuse poller helpers.
✅ *Accept:* a labeled GitHub issue imports to `needs_triage` exactly once
(idempotent across polls); a lead comment publishes out and is **not** re-ingested
(echo-loop test); adding `committed` on GitHub transitions the internal issue.

**P4 — Read-only cockpit.** `router.py` + `/ui`.
✅ *Accept:* `/dashboard?filter=attention` shows only awaiting_gl/stuck/failed;
`/issues/{id}` timeline deep-links each decision to its conversation.

**P5 — Lead sweep.** `sweep.py` + `lead.py` triage/decide + `prompts/`.
✅ *Accept:* a `needs_triage` issue gets assigned with a published rationale;
a `discussing` thread force-decides at the round cap; a `risk:high` issue goes to
`awaiting_gl` (never auto-`committed`).

**P6 — Reactive router.** `reactive.py`.
✅ *Accept:* assignee eval spawns with real repo context and posts accept/concern;
`committed` spawns the engineer (full-auto → PR + comment); a grand-leader `halt`
comment stops spawning within one loop tick.

**P7 — Grand-leader influence.** GL comment ingestion → lead inbox; tripwire assent.
✅ *Accept:* GL guidance changes the lead's next decision (visible in events);
GL assent on an `awaiting_gl` issue releases it to `committed`.

**P8 — Tuning.** Negotiation value, model routing, thresholds (design §8/§13).

Each phase: run `pre-commit --config ./dev_config/python/.pre-commit-config.yaml`
(ruff+mypy) before commit; land as its own commit on `ai-team-foundation` (or a
child branch) in the fork.

---

## 11. Non-goals (v1)

- No GitHub-UI/Projects clone (design §10).
- No direct state-mutation controls in the cockpit (human acts via GitHub, §9).
- No bidirectional comment mirroring (mostly-one-way, §7).
- No new GitHub bot accounts (single lead identity, §2).
- Persistent-service / reboot durability is deployment, not app scope (§12).

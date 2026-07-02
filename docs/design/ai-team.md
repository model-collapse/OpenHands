# AI Team on OpenHands — Design Doc

**Author:** design session, 2026-07-01 · **Revised:** 2026-07-02 (v2 — multi-team + UI)
**Scope:** A supervised, multi-agent "engineering team" built on the OSS OpenHands
stack, using an internal issue system as the collaboration substrate and GitHub
as an external gateway owned by a single lead identity.

> Design doc for the AI-team feature, developed in the `model-collapse/OpenHands` fork.

### v2 revisions (2026-07-02)

Three grand-leader changes, applied throughout. **These supersede earlier
decisions where noted and require re-work of the v1 build (P1–P8), which assumed
a single global team.**

1. **Multiple teams are a first-class concept.** The system is no longer one
   implicit team. A **`teams` table** is introduced and *every* team-scoped
   row (agents, issues, events, cursors, …) carries a `team_id`. A team owns a
   lead, a roster, watched repos, and its own issue board. See §2, §4, §11.
2. **A real UI, not just JSON endpoints.** The main app gains a **Teams entry**
   that lists all teams and drills into one; each team has a board, roster, and
   lead view. See new **§15 (Frontend UI)**. This revises principle-style
   guidance that said "no UI" — we still avoid a GitHub-*clone*, but we do build
   first-class team screens in the React frontend.
3. **Grand-leader ↔ lead interaction moves to the conversation UI.** The human
   talks to a team's lead through a **normal OpenHands conversation** (the chat
   UI), not primarily through GitHub comments. This **reverses the v1 §9 "influence
   via GitHub" decision** for the human↔lead channel. GitHub remains the external
   gateway for *external* issues and for the lead's published outputs; but the
   supervisor's own steering happens in-app. See §9 (rewritten) and §15.

---

## 1. Purpose & framing

The human ("**grand leader**") sits *above* an AI team lead and supervises. Two
jobs, in priority order:

1. **Observe progress** — glance and know the state of the world.
2. **Debug the team** — understand *why* an agent decided something, and steer
   it when needed.

The human is a **supervisor who dips in**, not a daily operator. Therefore the
system is designed first as an **observability + control plane** ("cockpit"),
and only second as a collaboration engine. We build the ability to *see and
stop* the agents before we build more autonomy.

### Design principles (ranked)

1. **Legibility over features.** A supervisor needs to understand state fast, not
   manage tasks. No GitHub-UI clone.
2. **Explain, then act.** Every agent decision links back to the reasoning
   (conversation + events) that produced it. Intervention is first-class.
3. **Single identity at the boundary is correct, not a compromise.** Internally,
   identity is perfect (each agent is a distinct role). Externally, the team
   presents as *one accountable identity* (the lead). Fidelity where it matters,
   simplicity where it doesn't.
4. **Mostly-one-way sync.** External issues import *in* fully; only the lead's
   curated outputs publish *out*. Internal chatter never leaks to GitHub.
5. **Bounded autonomy.** Every negotiation is round-capped; every loop has a
   budget; silent truncation is logged. Reuse existing OpenHands machinery
   rather than rebuild.
6. **Surface only what needs the human.** Default view = "awaiting grand leader"
   + "stuck > N" + "failed". Success is measured by how *rarely* the human must
   look.

---

## 2. Identity model

**Multiple teams (v2).** The top-level object is a **team**. The grand leader
(the human) sits above *all* teams and can create/observe/steer any of them.
Each team is self-contained: one lead, a roster, watched repos, an issue board.

```
Grand Leader (human)  — supervises + steers ALL teams (via the conversation UI, §9)
   ├── Team "web"  (team_id=…)
   │      └── Lead (agent, role='lead')   — OWNS this team's GitHub identity;
   │             │                           decides commits; BUILDS this roster
   │             ├── eng:*        kind ∈ {openhands, acp}   — configured by the lead
   │             └── reviewer/…                              — configured by the lead
   └── Team "infra" (team_id=…)
          └── Lead …                        — its own identity + roster + board
```

Roles (`lead`, `eng:backend`, …) are **scoped to a team** — the PK becomes
`(team_id, role)`, so two teams can each have their own `lead` and `eng:backend`.
`grand_leader` is the one **cross-team** role (the human), not stored per-team.

- Each team's `lead` has its own `github_identity`; all GitHub reads/writes for
  that team go through it. (Distinct teams may use distinct identities, or share
  one — it's per-team config.)
- **The roster is not hard-coded — the lead constructs it** (§2b). Roles below
  the lead are created at runtime by the lead, each with a chosen agent **kind**
  and a **skill set**.
- **Agent kind is heterogeneous.** A member is either:
  - `openhands` — runs via the app-server's own conversation/sandbox path
    (`POST /api/v1/app-conversations`), or
  - `acp` — an external ACP agent (e.g. Claude Code / Codex / Gemini) launched
    through OpenHands' ACP support.
  The `agents` table records `agent_kind` + launch config so the reactive router
  knows how to spawn each member; the rest of the state machine is kind-agnostic.
- Agents are **not** long-lived processes. A "role acting" == spawning a
  conversation for that role (OpenHands or ACP) with the relevant issue context.
  A mention/assignment is an enqueued task, not a running daemon.
- The `grand_leader` is a real (cross-team) role, but is **influence-only**
  (read-first supervision): its input is authoritative *guidance the lead must
  weigh*, not direct state edits. The lead remains the actor that changes issue
  state, including the commit decision. **In v2 that guidance arrives through a
  conversation with the team's lead (the chat UI), not GitHub comments** (§9).

---

## 2b. Team bootstrap (how the roster comes to exist)

A team is **grown, not declared**. Lifecycle (per team):

1. **Create the team + spawn its lead.** Creating a team (UI "New team" or
   `POST /teams`) writes a `teams` row and exactly one agent — that team's Lead
   — with its GitHub identity and a "team-formation" skill set.
2. **Grand leader ↔ lead formation, in a conversation (v2).** The human opens a
   conversation with the new lead (the chat UI — §9/§15) and describes the needs
   (repos, kinds of work, how many engineers, specialties). The lead proposes and
   applies the roster there. Every member-config decision is still recorded as a
   team event (auditable), and the formation conversation's id is linked on those
   events. *(v1 ran formation on a GitHub `internal` issue; v2 uses the
   conversation channel to match the new human↔lead interaction model. A team may
   still have an internal formation issue for the audit record, but the working
   channel is the conversation.)*
3. **Lead configures members one by one.** For each member the lead decides:
   - `role` (e.g. `eng:backend`, `reviewer`),
   - `agent_kind` (`openhands` or `acp` — e.g. a Claude Code agent),
   - `llm_model` / launch config,
   - **skills**, whose mechanism depends on kind: an `openhands` member's skills
     load via `agent_context` / the OpenHands skills system; an `acp` member's
     server owns its own system prompt, tools, and skill loading, so the lead
     supplies launch config + credentials rather than a skills list.
   and writes an `agents` row. Team formation is itself auditable (events).
4. **Roster is mutable.** The lead can add, reconfigure, disable, or replace
   members later (e.g. add a `frontend` engineer when a UI issue arrives). The
   grand leader can *influence* these choices but the lead executes them.

`agents` is written at runtime, not seeded from a config file, and carries
enough launch config to spawn either agent kind (§4).

---

## 3. Architecture

```
                 ┌─────────────────────────── OpenHands app_server ───────────────────────────┐
   GitHub  <───► │  github_poller/ (SYNC BRIDGE)      team_issues/ (NEW)                        │
 (external,      │   • import external issues in       • issues, comments, state machine        │
  lead identity) │   • publish lead outputs out        • agents/roles, inbox                    │
                 │   • provenance / dedup              • event/audit spine (provenance)          │
                 │                                     • lead sweep loop (15m)                   │
                 │                                     • reactive router (on new comment/label)  │
                 │   app-conversations API  ◄──────────  spawn role conversation w/ context      │
                 │                                     • cockpit router: /api/v1/team/... + /ui  │
                 └────────────────────────────────────────────────────────────────────────────┘
                                          ▲
                            Grand Leader intervenes via GitHub
```

Modules, all **in-process lifespans** of the app-server (same pattern as the
existing `github_poller`):

- **`team/`** (built P1–P8): the internal issue system — store, state machine,
  loops, cockpit API. **v2: the loops iterate over *all* enabled teams**, each
  loop pass scoped per `team_id` (a team with no repos/lead just no-ops).
- **`github_poller/`** patterns are reused by the team sync bridge (import IN /
  publish OUT), now per-team.
- **OpenHands conversations** (existing): the execution layer. An issue is a
  layer *above* conversations — one issue may spawn several. The
  **grand-leader↔lead conversation** (§9) is also a normal conversation, linked
  from `teams.lead_conversation_id`.
- **Frontend (`frontend/`, v2, new work):** first-class Team screens — a Teams
  list on the main page, a per-team board/roster, and the lead conversation.
  See §15.

---

## 4. Data model (SQLite, in `~/.openhands/team.db`)

Kept deliberately tight. All timestamps ISO-8601 UTC. **v2: a `teams` table is
the root, and every team-scoped row carries `team_id`** (with an index on it).

```
teams(id PK, name UNIQUE, github_identity, repos_json,   -- watched repos for this team
      lead_conversation_id NULL,                          -- the grand-leader<->lead chat (§9)
      created_at, enabled)

agents(team_id, role, display_name,          -- PK (team_id, role); role scoped per team
       actor_kind[human|agent],
       agent_kind[openhands|acp] NULL,
       github_identity NULL,
       llm_model NULL,
       launch_config_json NULL,
       skills_json NULL,
       created_by_role NULL,
       enabled,
       PRIMARY KEY (team_id, role))
       -- grand_leader is cross-team and NOT stored here (it's the human).

issues(id PK, team_id,                        -- every issue belongs to a team
       origin[internal|github], github_ref NULL,
       title, body, author_role, assignee_role NULL,
       state, priority[low|normal|high|urgent],
       created_at, updated_at, stuck_since NULL)

comments(id PK, issue_id FK, team_id, author_role, addressed_to NULL,
         provenance[human|agent|synced_in|synced_out],
         conversation_id NULL, github_comment_id NULL, body, created_at)

events(id PK, team_id, issue_id FK NULL, actor_role, kind,       -- the AUDIT SPINE
       from_state NULL, to_state NULL, conversation_id NULL, detail_json, created_at)

inbox(team_id, role, issue_id, reason, created_at, PRIMARY KEY(team_id, role, issue_id))
kv(team_id, key, value, PRIMARY KEY(team_id, key))               -- per-team cursors, markers
sync_map(team_id, internal_id, github_ref, kind, last_synced_at) -- dedup / echo-loop
```

The store's public methods take a `team_id` (or a team-scoped `TeamStore`
instance bound to one). A `github_ref` is only unique **within** a team, so the
uniqueness/dedup keys become `(team_id, …)`. `TeamConfig` per-team fields (repos,
identity, models) move onto the `teams` row; process-wide settings (intervals,
flag) stay in env.

**Why `events` is built first:** it is the audit spine. Every state change,
assignment, comment, and conversation spawn writes an event with `actor_role`,
`conversation_id`, and `detail_json`. This is what makes the team *debuggable* —
the human drills from an issue → the event → the conversation transcript/events
on disk. Nothing the agents do is allowed to bypass it.

---

## 5. State machine (labels ARE the state)

```
                 (human opens)                         (agent raises)
                       │                                      │
                       ▼                                      ▼
                 ┌───────────┐                          ┌───────────┐
   external ───► │needs-triage│                          │  internal  │
                 └─────┬──────┘                          └─────┬──────┘
             lead diagnoses                        raiser pings stakeholder+lead
                       ▼                                      ▼
                 ┌───────────┐   assignee has concerns  ┌───────────┐
                 │assigned:R  │ ───────────────────────►│ discussing │
                 │ waiting:R  │ ◄─────────────────────── │ waiting:X  │
                 └─────┬──────┘        (round-capped)    └─────┬──────┘
        assignee accepts │  or lead force-decides             │ lead weighs priority
                         ▼                                      │
                    ┌───────────┐                              │
                    │ committed │ ◄────────────────────────────┘
                    │  (GATE)   │   ← LEAD decides (weighing grand-leader influence)
                    └─────┬─────┘
                          ▼  == existing full-auto engineer path
                    ┌───────────┐        ┌──────┐        ┌────────┐
                    │in-progress│ ─────► │  PR  │ ─────► │  done  │
                    └───────────┘        └──────┘        └────────┘
```

Key points:
- **`committed` is the trigger the engineer path already understands.** Flows (1)
  and (2) both funnel into it. The engineer execution is exactly the full-auto
  loop already tested (label → conversation → PR + comment-back).
- **`waiting:<role>`** is both a routing marker (who responds) *and* the human's
  debug view (who is blocking, for how long → `stuck_since`).
- **Two issue origins, one machine.** Type-2 internal issues skip external
  triage and enter at `internal`; priority-weighting is the lead setting
  `priority:*`.
- **The lead owns the commit gate.** Advancing to `committed` is the lead's
  decision (after the assignee accepts or the negotiation is force-resolved).
  The grand leader *influences* this via guidance the lead must weigh (§9), but
  does not flip the gate directly. **Exception (the risk tripwire, §9):** issues
  the lead tags high-cost/high-risk (migrations, auth, release, or over an effort
  threshold) require explicit grand-leader assent before `committed`.

---

## 6. The two loops

### 6a. Lead sweep (time-driven, ~15 min) — the "manager pass"

For each open issue, branch on state:
- `needs-triage` → lead reads issue + repo, posts an assignment rationale,
  sets `assigned:<role>` + `waiting:<role>`. Writes events.
- `discussing`/`waiting:lead` → lead reads the thread, decides: `commit`,
  push back (bounded), or `reassign`. **This is the convergence authority** —
  the negotiation never runs open-ended; the sweep force-decides at the round
  cap. The commit decision is the lead's (§5), subject to the risk tripwire (§9).
- `internal` → lead weighs `priority`, pings stakeholders.
- **Escalation:** anything blocked > N minutes, failed, tripped the risk
  tripwire, or explicitly needing a human → add to grand-leader inbox / surface
  on the cockpit.

### 6b. Reactive router (event-driven) — "respond to what was said"

Fires when the poller sees a new comment or label change:
- comment on `waiting:<role>` → spawn that role's conversation to respond
  (assignee evaluates feasibility *by cloning and inspecting the repo* — §8).
  Spawn respects the member's `agent_kind` (OpenHands conversation or ACP agent).
- `committed` label added → spawn engineer (full-auto path), per its kind.
- grand-leader guidance comment → route to the **lead** as high-priority
  influence (not a direct state change); the lead acts on it (§9).

**Separation of concerns:** reactive = respond to a single event; sweep = review
the whole board and make decisions. Don't triage in the reactive loop (partial,
racy decisions).

---

## 7. GitHub sync (the real hard part)

Two sources of truth (internal + GitHub) → reconcile carefully.

- **Direction:** external issues sync *in* fully; only the lead's **curated
  outputs** sync *out* (a resolution comment, PR link, a status label). Internal
  issues never sync out unless the lead explicitly escalates. This is
  dramatically simpler than bidirectional and matches the supervisor model (the
  human watches the internal cockpit, so GitHub needn't mirror chatter).
- **Echo-loop prevention:** every synced artifact carries `provenance`. The
  poller must **never re-ingest what the lead just wrote** — a comment with
  `github_comment_id` authored by the lead's identity is skipped on import.
- **Mapping/dedup:** `sync_map` links internal↔external ids so updates don't
  duplicate. Import is idempotent (keyed on `github_ref` + comment id).

---

## 8. Making collaboration *real*, not theater (critical)

Multi-agent discussion only earns its cost if agents have **genuinely different
information**. Guardrails:

- **Ground the assignee in real repo context.** When a role evaluates an
  assignment, it *clones and inspects the actual code* before accepting or
  raising concerns. "I have concerns" must mean "I read the code and this is
  harder/riskier than the issue implies" — a real signal a human triager values.
  Without this grounding, cut the negotiation phase and go triage→commit→execute.
- **Bound every negotiation:** max rounds (e.g. 3), then lead force-decides.
  Explicit `waiting:<role>` turn marker so both agents don't answer the same
  comment.
- **Cost discipline:** every comment round is a full agent run (sandbox spin-up +
  LLM). Use a cheaper model (Sonnet) for lead triage/sweep; reserve Opus for
  actual engineering. Budget the sweep (cap conversations spawned per pass).

---

## 9. The human's channel — a conversation with the lead (v2)

Oversight is still **influence, not direct state control** — the human shapes
the lead's decisions; the lead remains the single actor that changes issue state.
**What changed in v2: the channel is a conversation, not GitHub.**

- **Channel: a conversation with the team's lead.** Each team has a persistent
  grand-leader↔lead conversation (`teams.lead_conversation_id`), opened when the
  team is created and reachable from the team's UI (§15). The human talks to the
  lead there — "form a backend + reviewer", "prioritize #42", "reconsider the
  auth approach", "halt everything". This is a normal OpenHands conversation with
  the lead agent; the lead has team-management tools/authority behind it.
- **Why the change:** direct, low-latency steering in the tool the human is
  already using, with full transcript. It replaces v1's "comment on a GitHub
  issue" channel for the *human↔lead* path. (v1 rationale — "no new UI" — is
  superseded by the v2 decision to build real team UI.)
- **GitHub's role is unchanged for everything else:** external issues still
  import IN, and the lead's curated outputs still publish OUT under the team's
  identity. A human commenting on the *external* GitHub issue is still ingested
  as guidance (the P7 influence path stays) — it's just no longer the *primary*
  supervisor channel.
- **Not a direct override.** The human does not flip labels/state; the lead acts.
  Guidance (from the conversation or GitHub) is recorded as team events so its
  effect on the lead's choices stays auditable.
- **Escape hatch:** a `halt`/`stop` directive — said in the conversation or via a
  GitHub label — is honored immediately (stop spawning, pause the issue).
- **Risk tripwire (unchanged, §5):** high-cost/high-risk issues are held at
  `awaiting_gl` and need explicit grand-leader assent before `committed`. In v2
  that assent can be given in the conversation as well as by the GitHub label.

The team **board** remains read-mostly (observe + drill into reasoning); direct
*steering* now has a real home — the lead conversation — rather than being
routed only through GitHub.

---

## 10. Cockpit API (per-team, the supervisor's read model)

Read-mostly JSON API. **v2: team-scoped** under `/api/v1/teams/{team_id}/…`,
plus a top-level teams collection:

- `GET  /api/v1/teams` — list all teams (name, lead, counts, health).
- `POST /api/v1/teams` — create a team (name, github_identity, repos) → writes
  the `teams` row, spawns its lead, opens the lead conversation (§9). *(This
  supersedes the v1 `POST /team/bootstrap`.)*
- `GET  /api/v1/teams/{id}/dashboard` — issues grouped by state; **default
  filter = needs-grand-leader + stuck>N + failed**.
- `GET  /api/v1/teams/{id}/issues`, `/issues/{issue_id}` — issue + comment
  transcript + **event timeline**, each event deep-linking its `conversation_id`.
- `GET  /api/v1/teams/{id}/agents` — the roster.
- `GET  /api/v1/teams/{id}/events` — raw audit spine.
- `GET  /api/v1/teams/{id}/lead-conversation` — the id of the grand-leader↔lead
  chat, so the UI can open it.

These endpoints back the frontend screens in §15. The v1 single-team routes
(`/api/v1/team/*`) are migrated to the `{team_id}` form.

---

## 11. Build order — the full machine

Build the complete system (not a thin slice). Order is by dependency: the
observability spine comes first, because the human must be able to *see and stop*
the agents before they run autonomously.

1. **Event/audit spine + data model** (`team_issues/` store, `events` table,
   conversation linkage, `agents` with `agent_kind`/skills). Nothing else works
   without this.
2. **ACP spike, then team bootstrap** (§2b). First de-risk the one real unknown:
   start a conversation with an ACP / Claude-Code config on this box and confirm
   spawn + credentials + skill loading. Then implement bootstrap — spawn the lead;
   grand-leader ↔ lead formation on an `internal` issue; lead writes `agents` rows
   (OpenHands or ACP members). Carry per-member agent config at spawn (a
   start-request agent field, or one saved profile per member) so members of
   different kinds run concurrently.
3. **Sync bridge**: repurpose `github_poller` → import external issues in with
   provenance + `sync_map`; publish lead output out. Echo-loop tests. Add the
   **label-change detection** path (§12) needed for state transitions.
4. **Read-only cockpit** (`/api/v1/team/dashboard`, issue drill-down to events,
   roster view).
5. **Lead sweep loop** (triage → assign; lead-owned commit gate + risk tripwire;
   convergence authority; escalation).
6. **Reactive router** (assignee-evaluates-with-repo-context; kind-aware spawn
   for OpenHands *and* ACP members; committed→execute reuses the full-auto path).
7. **Grand-leader influence via GitHub** ingestion (routes to lead as guidance).
8. Negotiation richness / heterogeneous-agent tuning as the machine runs.

Team formation (§2b) and kind-aware spawning (ACP + OpenHands) are planned in
from the start rather than bolted on.

**Status: v1 (P1–P8) is built and tested (single implicit team).** The list
above is an accurate record of that work.

### v2 build order (multi-team + UI + conversation channel)

Layered on the v1 code; each step is independently testable.

- **V1 — Multi-team data model.** Add `teams`; add `team_id` to every scoped
  table + indexes; migrate the store methods to take/scope by `team_id`; move
  per-team config (repos, identity, models) onto the `teams` row. Write a
  migration that folds the existing single team into `team_id = 'default'` so no
  data is lost. *Biggest change — touches store, sweep, reactive, sync, cockpit;
  the v1 tests must be reworked to a team-scoped store.*
- **V2 — Teams API.** `GET/POST /api/v1/teams` + re-scope the cockpit routes to
  `/api/v1/teams/{id}/…`. `POST /teams` spawns the lead and opens the lead
  conversation.
- **V3 — Multi-team loops.** The sweep + reactive + sync loops iterate enabled
  teams, each pass scoped to a `team_id`; per-team budgets.
- **V4 — Conversation channel (§9).** Open + persist the grand-leader↔lead
  conversation per team; give the lead team-management authority in it; ingest
  the human's conversation turns as guidance/commands (form team, prioritize,
  halt, assent) with the same audit trail as the GitHub path.
- **V5 — Frontend UI (§15).** Teams entry on the main page → team list → team
  detail (board + roster + open-lead-conversation). React/TanStack Query/DAL per
  the repo's frontend rules.

---

## 12. Known deltas / gaps vs. what exists today

- The current `github_poller` triggers on issues *created* after the cursor or
  `@mention`. It does **not** yet fire on a **label added to an existing issue**
  — the `committed`-gate and state transitions need a new label-change detection
  path (poll issues by label; dedup on label-application, not creation). Small
  but not free.
- Identity: today all actions use one token. This design *embraces* that at the
  GitHub boundary but requires internal roles to be modeled distinctly (the
  `agents` table). No new GitHub accounts needed.
- **Heterogeneous agent kinds — less new than it looks (verified in code).** ACP
  is already first-class in the SDK/app-server: `agent_kind='acp'` with
  `acp_server ∈ {claude-code, codex, gemini-cli}`, built-in default launch
  commands, provider-credential handling via the conversation secrets channel,
  and the conversation-start path **already branches on `agent_kind=='acp'`**
  (`live_status_app_conversation_service.py`; there is even a `switch_acp_model`
  endpoint). So a Claude Code teammate is *configuration*, not new plumbing. The
  genuine gap is narrower: agent selection today flows from **per-user
  settings/profiles**, so running several members of *different* kinds at once
  needs per-member agent config at spawn (carry it on the start request, or one
  saved profile per member). This is the one real unknown — spiked first in §11.
- **Label-change detection:** the state machine needs firing on *label added to
  an existing issue*, which the current poller does not do (it triggers on issue
  creation / @mention). New detection path required (§11 step 3).
- "Always-on": still requires the backend to run as a persistent service to be
  meaningful (unchanged from prior discussion).

---

## 13. Risks & the honest caution

- **Oversight cost > work saved.** If reading negotiations and nudging labels
  costs the human more attention than doing the work, it's a net loss. Mitigation:
  aggressive default-hiding in the cockpit (§6a escalation, §10 default filter);
  cheaper model for lead; hard loop budgets.
- **Dashboard drift.** If the internal store is subtly out of sync with reality,
  the human debugs the wrong thing. Mitigation: the event spine is load-bearing
  and append-only; the cockpit reads from it, not from re-derived state.
- **Negotiation theater.** Covered in §8 — ground it in repo context or cut it.
- **Token cost.** A 15-min sweep over N open issues, each possibly spawning a
  conversation, adds up on Opus. Budget per sweep; cheaper model for
  triage/sweep.
- **Autonomous commit gate.** A lead-owned gate trades safety for autonomy,
  making guardrails load-bearing rather than backup. Mitigation: the immediate
  `halt` hatch, per-sweep budgets, and the high-cost/high-risk **tripwire** that
  still requires grand-leader assent before `committed` (§9).
- **v2 migration risk.** Retrofitting `team_id` touches the store, all loops,
  sync, and every test. Mitigation: fold the existing single team into
  `team_id='default'` via a migration; keep the audit invariant; rework tests to
  a team-scoped store before adding multi-team behavior.
- **v2 cost multiplies with teams.** N teams × per-team sweeps/spawns. Mitigation:
  per-team enable flag + budgets; loops skip disabled/empty teams.

---

## 14. Multi-team model (v2 detail)

- **A team is the unit of ownership and isolation.** It has: a `name`, a GitHub
  identity, a set of watched repos, a lead + roster, an issue board, and a
  grand-leader↔lead conversation. Teams do not share issues, rosters, cursors, or
  boards — everything is `team_id`-scoped.
- **Roles are per-team** (`(team_id, role)` PK). "The lead" / "eng:backend" mean
  *this team's*. The one global actor is the human `grand_leader`, who is above
  all teams (not a per-team row).
- **The store** is either a team-scoped handle (bound to one `team_id`) or takes
  `team_id` on each call. The **audit invariant is unchanged** — state changes
  still go through `transition()` with an event — now within a team's rows.
- **The loops** (sweep, reactive, sync) iterate enabled teams; each team's pass
  is independent and budgeted. A team with no repos/lead safely no-ops.
- **GitHub identities** may differ per team or be shared; the echo-loop guard is
  per-team (skip comments authored by *this team's* lead identity).
- **Backward-compat:** the v1 single team becomes `team_id='default'`; the v1
  `/api/v1/team/*` routes redirect to `/api/v1/teams/default/*`.

---

## 15. Frontend UI (v2, new)

Built in `frontend/` following the repo's rules (UI → TanStack Query hooks →
`src/api` DAL → endpoints; query hooks `use[Resource]`, mutations `use[Action]`).
We build first-class **team** screens — not a GitHub-Projects clone, but real
in-app operation of the teams.

**15.1 Teams entry (main page).** A "Teams" item in the main navigation/home
that lists all teams (`GET /api/v1/teams`): name, lead identity, watched repos,
issue counts by state, and a "needs you" badge (awaiting_gl/stuck/failed count).
A **"New team"** action opens a create form (name, GitHub identity, repos) →
`POST /api/v1/teams`. This is requirement (1) and (2) from the request.

**15.2 Team detail** (`/teams/:id`), three panes:
- **Board** — issues grouped by state (the §10 dashboard), default-filtered to
  what needs the grand leader; click an issue → its transcript + event timeline,
  each event linking to the conversation that produced it (the debug drill-down).
- **Roster** — the team's agents (role, kind openhands/acp, model, skills,
  who-formed-it, last activity). Read-only view of `GET …/agents`.
- **Lead conversation** — an entry that opens the grand-leader↔lead conversation
  (§9) in the standard OpenHands conversation UI, reusing the existing chat
  components. This is requirement (3): the human steers the team by *talking to
  its lead* here (form the team, prioritize, reconsider, halt, assent).

**15.3 Reuse, don't rebuild.** The lead conversation is a normal conversation —
reuse the existing conversation screen; the team just stores its id
(`teams.lead_conversation_id`) and links to it. The board/roster are new
read-mostly components fed by team query hooks. No direct issue-state mutation
controls in the UI (steering flows through the lead conversation, §9).

**15.4 Non-goals (UI).** No Gantt/kanban drag-drop, no GitHub-Projects parity, no
inline issue editing. The UI observes state and routes steering to the lead;
issue state remains lead/GitHub-owned.

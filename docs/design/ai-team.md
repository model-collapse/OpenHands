# AI Team on OpenHands — Design Doc

**Status:** Revised after grand-leader review · **Author:** design session, 2026-07-01
**Scope:** A supervised, multi-agent "engineering team" built on the OSS OpenHands
stack, using an internal issue system as the collaboration substrate and GitHub
as an external gateway owned by a single lead identity.

> Design doc for the AI-team feature, developed in the `model-collapse/OpenHands` fork.

**Grand-leader decisions (2026-07-01), applied throughout:**
1. **Commit gate → the team lead decides** whether to commit an issue for
   implementation (not the human). See §5, §9.
2. **Debug is read-first.** The human's actions are *influence directed at the
   lead*, not direct edits to issues/state. See §9.
3. **Roster is bootstrapped, not fixed.** We first spawn a **Team Lead agent**;
   the grand leader converses with the lead, and the lead *builds its own team* —
   configuring each member one by one, where a member may be an OpenHands agent
   or a Claude Code / ACP agent, and loading the appropriate skills for each.
   See §2 + new §2b.
4. **v1 scope = the full machine** (not a thin slice). See §11.

---

## 1. Purpose & framing

The human ("**grand leader**") sits *above* an AI team lead and supervises. Two
jobs, in priority order:

1. **Observe progress** — glance and know the state of the world.
2. **Debug the team** — understand *why* an agent decided something, and
   intervene when needed.

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

```
Grand Leader (human)         role=grand_leader   — supervises + influences the lead
   └── Team Lead (agent)     role=lead           — OWNS the GitHub identity (gateway);
          │                                        decides commits; BUILDS the team
          ├── Engineer       role=eng:*    kind ∈ {openhands, acp}   — configured by lead
          └── Specialist     role=reviewer/researcher/...            — configured by lead
```

- Only `lead` has a `github_identity`. All GitHub reads/writes go through it.
- **The roster is not hard-coded — the lead constructs it** (see §2b). Roles
  below the lead are created at runtime by the lead, each with a chosen agent
  **kind** and a **skill set**.
- **Agent kind is heterogeneous.** A member is either:
  - `openhands` — runs via the app-server's own conversation/sandbox path
    (`POST /api/v1/app-conversations`), or
  - `acp` — an external ACP agent (e.g. Claude Code / Codex / Gemini) launched
    through OpenHands' ACP support.
  The `agents` table records `kind` + launch config so the reactive router knows
  how to spawn each member; the rest of the state machine is kind-agnostic.
- Agents are **not** long-lived processes. A "role acting" == spawning a
  conversation for that role (OpenHands or ACP) with the relevant issue context.
  A mention/assignment is an enqueued task, not a running daemon.
- The `grand_leader` is a real role in the internal system, but is
  **influence-only** (read-first supervision): its comments are authoritative
  *guidance the lead must weigh*, not direct state edits. The lead remains the
  actor that changes issue state (including the commit decision). See §9.

---

## 2b. Team bootstrap (how the roster comes to exist)

The team is **grown, not declared**. Lifecycle:

1. **Spawn the lead.** On enable, the system creates exactly one agent — the
   Team Lead — with the GitHub identity and a "team-formation" skill set.
2. **Grand leader ↔ lead conversation.** The human talks to the lead (via the
   lead's own conversation, or via a GitHub `internal` issue) about the project's
   needs: what repos, what kinds of work, how many engineers, what specialties.
3. **Lead configures members one by one.** For each member the lead decides:
   - `role` (e.g. `eng:backend`, `reviewer`),
   - `kind` (`openhands` or `acp` — e.g. a Claude Code agent),
   - `llm_model` / launch config,
   - **skills to load** for that member (from the OpenHands skills system and/or
     ACP agent config),
   and writes an `agents` row. Team formation is itself auditable (events).
4. **Roster is mutable.** The lead can add, reconfigure, disable, or replace
   members later (e.g. add a `frontend` engineer when a UI issue arrives). The
   grand leader can *influence* these choices but the lead executes them.

Implication for the data model: `agents` is written at runtime, not seeded from
a config file, and carries enough launch config to spawn either agent kind
(§4). Team-formation actions are first-class events so the human can see *why*
the lead shaped the team as it did.

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
                            Grand Leader intervenes via GitHub (v1)
```

Three cooperating modules, all **in-process lifespans** of the app-server
(same pattern as the existing `github_poller`):

- **`team_issues/`** (new): the internal issue system — store, state machine,
  loops, cockpit API/UI.
- **`github_poller/`** (existing, repurposed): becomes the **sync bridge**. Its
  job shifts from "trigger a conversation" to "import GitHub issue → internal
  issue owned by lead" and "publish lead's curated output → GitHub".
- **OpenHands conversations** (existing): the execution layer. An internal issue
  is a layer *above* conversations — one issue may spawn several (a triage
  conversation, an engineer conversation). Never overload a conversation to mean
  an issue.

---

## 4. Data model (SQLite, in `~/.openhands/team.db`)

Kept deliberately tight. All timestamps ISO-8601 UTC.

```
agents(role PK, display_name,
       actor_kind[human|agent],           -- human (grand_leader) vs agent
       agent_kind[openhands|acp] NULL,     -- for agents: how to spawn (§2)
       github_identity NULL,               -- only the lead has one
       llm_model NULL,
       launch_config_json NULL,            -- ACP command / OpenHands agent cfg
       skills_json NULL,                   -- skills the lead loaded for this member
       created_by_role NULL,               -- 'lead' for members it forms (§2b)
       enabled)

issues(id PK, origin[internal|github], github_ref NULL,           -- e.g. "owner/repo#12"
       title, body, author_role, assignee_role NULL,
       state, priority[low|normal|high|urgent],
       created_at, updated_at, stuck_since NULL)

comments(id PK, issue_id FK, author_role, addressed_to NULL,
         provenance[human|agent|synced_in|synced_out],
         conversation_id NULL,                                    -- link to reasoning
         github_comment_id NULL,                                  -- external mapping
         body, created_at)

events(id PK, issue_id FK, actor_role, kind, from_state NULL, to_state NULL,
       conversation_id NULL, detail_json, created_at)             -- the AUDIT SPINE

inbox(role, issue_id, reason, created_at, PRIMARY KEY(role, issue_id))
                                                                  -- drives loops: "who acts next"
sync_map(internal_id, github_ref, kind[issue|comment], last_synced_at)
                                                                  -- dedup / echo-loop prevention
```

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
  does not flip the gate directly.

---

## 6. The two loops

### 6a. Lead sweep (time-driven, ~15 min) — the "manager pass"

For each open issue, branch on state:
- `needs-triage` → lead reads issue + repo, posts an assignment rationale,
  sets `assigned:<role>` + `waiting:<role>`. Writes events.
- `discussing`/`waiting:lead` → lead reads the thread, decides: `commit`,
  push back (bounded), or `reassign`. **This is the convergence authority** —
  the negotiation never runs open-ended; the sweep force-decides at the round
  cap.
- `internal` → lead weighs `priority`, pings stakeholders.
- **Escalation:** anything blocked > N minutes, failed, or explicitly needing a
  human → add to grand-leader inbox / surface on the cockpit.

### 6b. Reactive router (event-driven) — "respond to what was said"

Fires when the poller sees a new comment or label change:
- comment on `waiting:<role>` → spawn that role's conversation to respond
  (assignee evaluates feasibility *by cloning and inspecting the repo* — see §8).
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

## 9. The human's intervention channel — influence, not direct control

Per grand-leader decision, oversight is **read-first**, and the human's actions
are **influence directed at the lead**, not direct edits to issue state.

- **Channel (v1): through GitHub.** The grand leader comments on the real GitHub
  issue (or an `internal` issue / the lead's conversation) as themselves. The
  lead ingests it as **high-priority guidance it must weigh** — then the *lead*
  takes the resulting action (commit, reassign, reprioritize, halt). Rationale:
  zero new UI to trust, works from the tool the human already lives in, reuses
  the poller.
- **Not a direct override.** The human does not flip labels/state himself; he
  shapes the lead's decisions. This keeps a single, coherent actor (the lead)
  responsible for state, with the human as the influencing supervisor above it.
  Guidance is recorded as events so its effect on the lead's choices is auditable.
- **Escape hatch:** a hard `halt`/`stop` directive is honored immediately by the
  lead sweep (stop spawning, pause the issue) — influence is strong enough to
  arrest a runaway, even though it flows through the lead.

The cockpit is **read-mostly** in v1 (observe + drill into reasoning); it does
not expose direct state-mutation controls.

---

## 10. Cockpit (the supervisor's view)

Thin, read-mostly. Endpoints under `/api/v1/team/`:
- `/dashboard` — issues grouped by state; **default filter = needs-grand-leader
  + stuck>N + failed**. Everything proceeding fine is hidden.
- `/issues/{id}` — issue + comment transcript + **event timeline**, each event
  deep-linking to its `conversation_id` (→ on-disk sandbox events = the agent's
  actual reasoning).
- `/agents` — roles, current load, last activity.
- `/ui` — a single grouped board (evolve the existing poller UI; **no GitHub
  parity**).

The measure: can the human, in one screen, answer "what needs me / what's stuck /
what failed" and drill to *why* in one click.

---

## 11. Build order — v1 is the FULL machine

Grand-leader decision: build the complete system, not a thin slice. Order is by
dependency (observability spine still comes first, because the human must be able
to *see and stop* the agents before they run autonomously), but all of it is v1.

1. **Event/audit spine + data model** (`team_issues/` store, `events` table,
   conversation linkage, `agents` with `agent_kind`/skills). Nothing else works
   without this.
2. **Team bootstrap** (§2b): spawn the lead; grand-leader ↔ lead formation
   conversation; lead writes `agents` rows (OpenHands or ACP members, with
   skills). Roster is runtime-built.
3. **Sync bridge**: repurpose `github_poller` → import external issues in with
   provenance + `sync_map`; publish lead output out. Echo-loop tests. Add the
   **label-change detection** path (§12) needed for state transitions.
4. **Read-only cockpit** (`/api/v1/team/dashboard`, issue drill-down to events,
   roster view).
5. **Lead sweep loop** (triage → assign; lead-owned commit gate; convergence
   authority; escalation).
6. **Reactive router** (assignee-evaluates-with-repo-context; kind-aware spawn
   for OpenHands *and* ACP members; committed→execute reuses the full-auto path).
7. **Grand-leader influence via GitHub** ingestion (routes to lead as guidance).
8. Negotiation richness / heterogeneous-agent tuning as the machine runs.

Because v1 is the full machine, plan for the added surface of §2b (team
formation) and kind-aware spawning (ACP + OpenHands) from the start, rather than
bolting them on.

---

## 12. Known deltas / gaps vs. what exists today

- The current `github_poller` triggers on issues *created* after the cursor or
  `@mention`. It does **not** yet fire on a **label added to an existing issue**
  — the `committed`-gate and state transitions need a new label-change detection
  path (poll issues by label; dedup on label-application, not creation). Small
  but not free.
- Identity: today all actions use one token. This design *embraces* that at the
  GitHub boundary but requires internal roles to be modeled distinctly (the
  `agents` table). No new GitHub accounts needed for v1.
- **Heterogeneous agent kinds:** members may be OpenHands agents *or* ACP agents
  (Claude Code / Codex / Gemini). The reactive router must spawn each per its
  `agent_kind`. OpenHands spawn is proven (full-auto path); the **ACP spawn +
  skill-loading path is new integration work** and the main unknown in the
  bootstrap (§2b). De-risk it early.
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

---

## 14. Resolved decisions (grand leader, 2026-07-01)

1. **Commit gate authority → the LEAD decides.** The team lead determines whether
   an issue is committed for implementation. The grand leader influences but does
   not hold the gate. (Applied: §5, §6a, §9.)
2. **Debug emphasis → read-first.** The human's actions are *influence on the
   lead*, not direct state edits. Cockpit is read-mostly; no direct-mutation
   controls in v1. (Applied: §9, §10.)
3. **Roster → bootstrapped by the lead.** Do not pre-declare roles. Spawn the
   Team Lead agent first; the grand leader converses with the lead; the lead
   forms the team, configuring each member (OpenHands *or* ACP/Claude-Code kind)
   and loading its skills. (Applied: §2, new §2b, §4 `agents` schema.)
4. **v1 scope → the full machine.** Build the complete system, not a thin slice.
   (Applied: §11.)

### Remaining questions surfaced by the decisions

- **ACP integration depth:** exactly how does the lead launch and load skills
  into an ACP agent (Claude Code) as a team member? This is the main new unknown
  (§12) — spike it during §11 step 2.
- **Formation UX:** does the grand-leader ↔ lead team-formation conversation
  happen in the lead's OpenHands conversation, or via a dedicated GitHub
  `internal` issue thread? (Both are viable; the latter keeps formation auditable
  in the same substrate as everything else.)
- **Runaway safety with a lead-owned gate:** since the lead now commits
  autonomously, confirm the `halt` escape hatch (§9) and per-sweep budgets are
  sufficient guardrails, or whether high-cost/high-risk issues should still
  require explicit grand-leader assent before `committed`.
```

# AI Team on OpenHands — Design Doc

**Status:** Draft for review · **Author:** design session, 2026-07-01
**Scope:** A supervised, multi-agent "engineering team" built on the OSS OpenHands
stack, using an internal issue system as the collaboration substrate and GitHub
as an external gateway owned by a single lead identity.

> Design doc for the AI-team feature, developed in the `model-collapse/OpenHands` fork.

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
Grand Leader (human)         role=grand_leader   — override authority, above all
   └── Team Lead (agent)     role=lead           — OWNS the GitHub identity (gateway)
          ├── Engineer       role=eng:backend, eng:frontend, ...
          └── Specialist     role=reviewer, researcher, ...
```

- Only `lead` has a `github_identity`. All GitHub reads/writes go through it.
- Agents are **not** long-lived processes. A "role acting" == spawning an
  OpenHands conversation for that role with the relevant issue context. A
  mention/assignment is an enqueued task, not a running daemon.
- The `grand_leader` is a real role in the internal system whose comments carry
  **override authority** the lead sweep must honor (force-commit, reassign,
  deprioritize, halt).

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
agents(role PK, display_name, kind[human|agent], github_identity NULL,
       llm_model NULL, enabled)

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
                    │  (GATE)   │   ← grand-leader / lead green-light
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
- `committed` label added → spawn engineer (full-auto path).
- grand-leader override comment → apply immediately, bypass normal flow.

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

## 9. The human's intervention channel (v1 decision)

**v1: intervene through GitHub.** The grand leader comments on the real GitHub
issue as themselves; the lead ingests it as an authoritative `grand_leader`
directive. Rationale: zero new UI to trust, works from the tool the human
already lives in, reuses the poller. The cockpit is **read-mostly** in v1.

**v2 (optional):** direct override in the internal cockpit (richer, but new
surface + new trust burden). Deferred until the read-only cockpit proves useful.

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

## 11. Build order (observability spine first)

1. **Event/audit spine + data model** (`team_issues/` store, `events` table,
   conversation linkage). Nothing else works without this.
2. **Sync bridge**: repurpose `github_poller` → import external issues in with
   provenance + `sync_map`; publish lead output out. Echo-loop tests.
3. **Read-only cockpit** (`/api/v1/team/dashboard`, issue drill-down to events).
4. **Lead sweep loop** (triage → assign; convergence authority; escalation).
5. **Reactive router** (assignee-evaluates-with-repo-context; committed→execute
   reuses existing full-auto path).
6. **Grand-leader override via GitHub** ingestion.
7. *Then* iterate on negotiation richness — only if §8 grounding proves it adds
   signal.

This inverts a "collaboration-first" ordering: because the human must *observe
and stop* autonomous agents, the observability + control spine comes first.

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

## 14. Open questions for the grand leader

1. **Commit gate authority:** does the human always hold the final `committed`
   gate (agents negotiate, human commits — safer), or may the lead commit
   autonomously once the assignee accepts? (Recommendation: human holds the gate
   in v1.)
2. **Debug emphasis:** is "debug" mostly *reading* (understand why) or *acting*
   (redirect)? Decides how much of v1 is dashboard vs. override tooling.
   (Recommendation: reading-first; override via GitHub.)
3. **Roster:** which roles exist in v1? (Recommendation: `lead`, one
   `eng:general`, `reviewer` — smallest set that exercises assign→negotiate→
   commit→execute→review.)
4. **Scope of v1 slice:** build the full machine, or first prove
   triage→commit→execute on one repo (`diagon`) with the event spine + read-only
   cockpit, then add negotiation? (Recommendation: the latter.)
```

# Varyz architecture

Shared Varyz (2026-10-09) uses separate publication/membership/question tables and
the scoped `/mcp/shared` resource. Guest reads use immutable reviewed projections;
private edits await review, while retractions suppress in the canonical sync
transaction. Atomic owner replies can independently remember and publish.
Owners' agents can add people by Varyz username: the first add shares that
project's confirmed preferences (never General ones) and sends an email-bound
invitation the guest's agent accepts with `shared_join`. Collaboration levels, GitHub access handshakes,
work reports and owner review items live in Cloud tables too; local Varyz reaches them through
`varyz.cloud.collaboration` and keeps no copy. Invitations need the recipient's verified (free)
Cloud account. Each action that needs someone writes a content-free row to `shared_notices` in
the same transaction; Cloud emails them from a one-minute Cloud loop (hourly
per account, one-click unsubscribe) and the local autosync daemon shows them through
`varyz/desktop_notify.py`. See `docs/SHARED_VARYZ.md` and `docs/contracts/shared-varyz-v1.json`.

Promise reliability (2026-10-09): `resolution/continuation.py` composes bounded,
attributed current-session snapshots for continuation requests, with project/client
filters, exclusion of the new conversation, current consent checks and no stale fallback
when the newest state is withheld. Hooks and local/hosted MCP carry the same distinction
between agent reports and owner authority. Cloud continuation reads separately permitted
encrypted session reports, never uploads them implicitly.

`resolution/verification.py` returns one completion receipt per applicable standard.
Bounded local SVG-favicon checks inspect current `index.html` and an in-root file and
retain hashes. Other standards require explicitly labeled agent-reported check evidence;
unchecked or failed items do not become ready merely because `finish_check` was called.
The done hook recognizes incomplete result flags. No checker executes arbitrary commands,
follows external links or reads outside the root. Subjective review is not independently
verified by an agent assertion.

Trial exposure remains auditable but cannot promote a rule to owner confirmation.
`Principle.advice` distinguishes owner-confirmed, learned-trial and historical
learned-unconfirmed authority. Prior ledger rows are retained. Explicit keeps/corrections,
scope isolation, trial kill switches and pending permission boundaries remain in place.

Session progress (2026-10-07) uses `src/varyz/session_progress.py`, MCP tools
`record_progress` / `session_history`, `src/varyz/session_progress_cli.py` and
migration 0046. Checkpoints are bounded current-state snapshots in a separate
local append-only journal, with credential-derived writer identity and precise
session chronology. They never enter owner knowledge, graph extraction or Cloud
sync. See `docs/SESSION_PROGRESS.md` for validation, privacy and retrieval rules.

Historical mining uses `src/varyz/session_miner.py`, MCP `mine_sessions` and
migration 0047. It reuses approved transcript parsers, extracts bounded source
statements per prompt/last-response pair, and optionally uses installed local AI
to select those statements. Source dates order retrieval; live snapshots take
precedence. Privacy and source-selection fingerprints gate retrieval after policy
changes. File receipts resume scans, immutable report keys deduplicate partial
runs, and erasure tombstones prevent backfill from recreating forgotten sessions.

Task routing (2026-10-07) uses `src/varyz/coordination.py` and per-task backend
selections acknowledged on local and Cloud MCP connections. Runtime guards and
local hooks suppress the unselected workflow; history stays independently gated.
See `docs/LOCAL_HOSTED_COORDINATION.md` for the non-atomic handoff protocol.

> Plugin surfaces updated 2026-10-05: full local Claude Code/Codex packages run
> the installed engine; full hosted ChatGPT/Claude packages combine separate
> preference and read-only history/context OAuth grants. Runtime boundaries,
> retrieval differences and build verification are in `docs/PLUGIN_PARITY.md`.

Optional improvement analytics (2026-10-04) use consent-gated local SQLite counters,
the existing daemon's worker thread, a strict aggregate schema and an account-independent
Cloud receipt table. Creator reporting requires a separate secret. Personal text and
permanent user identifiers are excluded. Browser setup collects consent in a separate
optional dialog before starting the run; terminal setup asks with a default-no prompt.
See `docs/ANALYTICS.md` for boundaries and limits.

The Varyz network (2026-10-18) extends that one consent (`network-v1`) to anonymized
examples. `contribute.py` reads the archive read-only, scrubs text locally and fail-closed
against `contribution_schema.py`, and queues records in the analytics state database; the
same daemon tick uploads them. Cloud stores them keyed by the hash of a
random contributor key, which also deletes them. Receipt is off unless
`VARYZ_CONTRIBUTIONS_ENABLED` is set.

Varyz is a local owner-context resolution service. Its product boundary is one call:

```text
resolve_for_owner(question, caller, purpose) -> Resolution
```

The alpha is single-device and loopback-only, but its contracts do not encode those
deployment limits. Where implementation detail differs from this page, the code is
authoritative.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Product primitive | One ordered resolution call | Agents need a safe alternative to guessing, not a menu of internal subsystems. |
| Runtime | One loopback daemon | CLI, UI, and MCP share the same model, queue, ordering, and durable state. |
| Resolution order | Explicit → evidence → inference → policy → escalation | The owner's direct answer always outranks derived context. |
| Failure mode | Withhold, abstain, or escalate | A fluent unsupported answer is a product failure. |
| Identity | Principal and capability carried in `Caller` | Transport is data, not authority; local stdio is not implicitly trusted. |
| Writes | Append-only with supersession | Corrections stay auditable and can later merge across devices. |
| Scope identity (2026-10-04) | `scopes.py` defines one canonical scope parser for local ledgers, Cloud payloads, legacy reads, cards and the rules projection | Complete general labels such as `general`, `all_projects` and `every project` become `*`. Similar project names stay project names. Existing rows are folded when read or compared, preserving audit history and account boundaries. |
| Notifications | Question notifications off by default; the notifier seam takes id and urgency only | A question could never leak through a future relay. News (below) is the one thing Varyz passes on unasked. |
| News | Hourly on use from `varyzai.com/news.json`, on by default, `varyz news off`; the owner's agent passes each post on once | The project can reach the people who use it through the agent they already talk to. The check is a plain GET that carries nothing about them. |
| Accounts | Coming: an account ties a person to the data they sync to it, private to them unless they publish it themselves | Rules and preferences should follow a person between computers and agents. Private is the default and publishing is the person's own act. |
| Role toward coding agents | Advisor, not lead | Claude Code and Codex plan, build and judge; Varyz supplies the owner's confirmed rules, vision and decisions so the work is done the owner's way. |
| Rule source | Owner-confirmed only | Mined suggestions wait in review and never reach an agent; an agent records a rule only after asking the owner. |
| Shareable card (`varyz wrapped`) | Deterministic Pillow render from general rules, read-only DB, preview before write | A card people post must never show a project rule, a guess or an unreviewed line; no model, no network, same rules give the same card. |
| Evaluation | TRR plus explicit error/abstention buckets | Blanket abstention and confident invention must both score badly. |
| Tier 3 | Optional incremental compile | Slow synthesis cannot gate explicit knowledge, evidence, or escalation. |
| Onboarding | One idempotent `varyz setup`: archive, daemon, agents, data sources, doctor | Five commands in the right order lost people; each step asks, `--yes` answers all, and a re-run keeps what is there. |
| Data-source visibility | One `sources.source_statuses` read by setup, `varyz sources`, the doctor and the trust centre | An archive reading nothing and one reading five sources used to get the same GO; every surface now names the connected sources and their item counts, and they cannot disagree. |
| Daemon identity | `GET /v1/identity` names the archive; the client refuses a daemon serving another | Every config started on port 8765, so a second archive silently read and wrote the first one's rules. `setup` and `init` move a new config to a free port. |
| Daemon availability (2026-10-04) | CLI requests recover refused loopback connections; an optional Windows login supervisor checks every minute on the archive's configured port (default 1207) | Health/shutdown observations do not autostart; timeouts, HTTP errors and archive mismatches are never replayed. A dedicated pause sentinel controls recovery without enabling source sync, updates or reading. |
| Source boundary | Local AI chats, files, mail, calendar, and git; no chat apps, contacts or browsing history | Varyz is an authority layer for agents, not a social-message profiler. The WhatsApp, Telegram, Instagram, Google Contacts and browser-history connectors were removed on 2026-09-27, and conversation analysis reads only agent sessions. |

## Components

Account navigation (2026-10-17): Cloud exposes only signed-in flags at
`GET /api/browser-session`. The public website reads them with credentials
and an exact `https://varyzai.com` CORS grant; other account APIs keep their existing
origin restrictions. The browser credential stays in Cloud's Secure, HttpOnly,
host-only cookie, with the existing 30-day renewal. `scripts/site/account.js`
refreshes the menu on page return/focus; logout or an account change is reflected
without copying tokens to the website. The site builder adds a plain sign-in link
on every public page; JavaScript failure leaves that link usable.
Local app OAuth grants and upload consent remain separate from browser navigation.

Away mode (2026-09-27): `resolution/away.py` holds durable mode state and return
reviews (migration `0042`). MCP's `away` and the local `varyz away` command share it.
The working agent recognises owner instructions and judges delegation, reversibility
and rework. No local model infers absence. Task settings take precedence over project
settings, then all-project settings; an explicit wider change resets narrower settings.
Optional durations expire at read time. Hooks supply conversation identity and current
policy without mutating the mode.

Resolution still checks confirmed knowledge and policy first. A delegated
implementation lookup disables new escalation in normal and away mode; an unknown
owner preference remains a real unanswered escalation. While away, the queue suppresses notification and links the question
to its task. MCP adds a `deferred_owner` block rather than `ask_owner`, and records
the proposed choice/reason in `away_reviews`, never `owner_knowledge`. Repeated
questions reuse the queue id while retaining separate task links. Returning or
completing a task preserves unanswered reviews. Mode mutation requires a registered
MCP caller with the existing owner-context grant; the CLI is the local owner surface.

Question routing (2026-10-04): `resolution/questioning.py` supplies shared guidance
to hooks, briefings and MCP. The working agent checks task relevance, existing answers
and repository facts before asking. Unknown delegated choices return `agent_decides`
only without withheld evidence or an egress request; daemon failures remain failures.
Necessary owner questions use one short sentence (usually at most 15 words) and brief
options. Rationale and technical context stay in `reason` and `proposed_choice`.
Shortening must preserve meaning, permissions and the escalation id. Question
selection and wording are agent responsibilities, not a local keyword classifier or
a hard truncation limit. Existing pending questions and recorded answers are retained.

The state coordinates agents on one archive/device. It is not an execution scheduler,
and language recognition is agent-instruction behaviour, not a locally enforced
classifier guarantee. Bare slash-command parsing belongs to each client; the shipped
`away` skill supplies Claude's `/away` and Codex's supported skill invocation.

```text
Agent / CLI / local UI
        |
        v
resolution/client.py       only supported daemon client
        |
        v
resolution/daemon.py       loopback HTTP, configured bind/auth/device
        |
        v
resolution/resolver.py     ordered tiers + self-contained ledger
   |          |          |
   v          v          v
knowledge   archive     life model
 tier 1      tier 2      tier 3
   \          |          /
    \         v         /
     policy / withholding
              |
              v
       durable escalation
              |
              v
    toast + CLI banner + UI badge
```

Key ownership:

```text
src/varyz/resolution/models.py     frozen Caller/Request/Resolution contract
src/varyz/resolution/ports.py      frozen store/queue/notifier/tier protocols
src/varyz/resolution/knowledge.py  explicit append-only knowledge
src/varyz/resolution/escalation.py durable pending/answered/expired queue
src/varyz/resolution/outcomes.py   append-only outcomes of resolutions
src/varyz/resolution/principles.py append-only owner rules and project vision
src/varyz/resolution/advice.py     advise(plan): rules, vision, related decisions
src/varyz/resolution/briefing.py   start_task / finish_check: the owner briefing
src/varyz/resolution/domains.py    kinds of work and their detection
src/varyz/resolution/scope.py      general preference vs one project's choice
src/varyz/hooks.py                 client hooks: brief on prompt, check on stop
src/varyz/resolution/rule_miner.py suggestions from the owner's turns and files
src/varyz/resolution/rule_reader.py a model (local, cloud, or the agent's subagent) reads turns for rules
src/varyz/resolution/resolver.py   ladder, evidence tier, inference tier, ledger
src/varyz/resolution/daemon.py     service, HTTP protocol, lifecycle
src/varyz/resolution/client.py     JSON transport boundary
src/varyz/trust.py                 principals, tokens, capabilities, callers
src/varyz/mcp_server.py            agent tools and identity binding
src/varyz/ui/server.py             authenticated owner inbox; daemon client only
src/varyz/card/                    `varyz wrapped` card: source, archetype, layout, SVG/PNG, bundled OFL fonts
scripts/trr.py                     deterministic Trusted Resolution Rate scorer
scripts/advice_bench.py            advice recall fixture and live replay (counts only)
```

## Contracts

`Caller` contains agent id, display name, purpose, optional capability id, device id,
and transport. `ResolutionRequest` contains the bounded question, caller, purpose,
conversation id, wait behavior, and escalation choice. `Resolution` wraps the existing
`LifeQueryResult` and adds tier, confidence, escalation id, withheld markers, receipt id,
wall time, and monotonic sequence.

The models are frozen. The HTTP client validates daemon responses with JSON semantics so
strict tuples and enums remain strict in Python while using arrays and strings on the wire.

## Request lifecycle

1. The MCP boundary resolves a token to a caller. Missing credentials produce an explicit
   bounded low-trust principal; revoked credentials fail closed.
2. The surface sends a `ResolutionRequest` to the configured daemon through
   `ResolutionClient`. No surface reads resolution tables directly.
3. Tier 1 selects only the latest non-superseded explicit answer. A correction appends a
   row pointing at the answer it supersedes, and records `overridden` in
   `resolution_outcomes` for every resolution the old answer gave. The precedence is:
   the owner's own project answer, then their general `*` answer, then an `org:<id>`
   answer, then `published:<id>` taste. An own answer, even a paraphrased one, beats
   anyone else's; ambiguous own answers escalate rather than fall through. When no
   own answer matches exactly or by strict paraphrase, `ParaphraseVerifier`
   (`resolution/paraphrase_verify.py`, `[workflows] paraphrase_verifier`) takes the
   owner's own current, applicable decisions. It drops any that name a capitalised
   project or product the question does not, and ranks the rest by the archive
   embedding. It then asks the local model about the nearest 3. Only a single
   P >= 0.9 answers, at confidence 0.7. An answer
   that is not the owner's own resolves as `reported`. Every tier-1 answer cites its
   knowledge entry (generation `owner_knowledge`) with its author and origin, which
   MCP surfaces as `answered_by`.
4. Tier 2 performs local archive retrieval, applies sensitivity and capability ceilings,
   rejects `graph.excluded_sources` before lexical grounding or ranking, and only returns
   grounded, source-addressed evidence. MCP `recall` and `brief` apply the same boundary.
   Grounding is checked twice: the archive must use at least half of the question's words
   somewhere, and at least one returned record must hold half of them (names count double)
   within 25 words (`evidence_coverage`). Otherwise the question escalates. Words the
   archive never uses drop out of that count, and they are often the subject, so the local
   model (`[workflows] evidence_verifier`, loopback only) then reads the top three records
   against the question: one about the thing asked answers, none escalates, and a model
   that cannot answer leaves the coverage check to decide. When a local model is there to
   judge (`[retrieval] judge`: the managed model, else the `[workflows]` classifier model
   on a GPU; the alpha never installs the managed model), tier 2 searches the way MCP `recall` does instead: keyword and vector hits,
   the cross-encoder's shortlist and the model's picks from it (`retrieval/loaded_models.py`
   starts loading the three in the background on the first question and lets them go after
   five idle minutes). Records
   the model picked skip the coverage check, the verifier still reads them, and a model
   that is still loading or fails leaves the question to keyword search. The classifier and
   the judge ask Ollama for the same 8k window, since a changed window reloads the model.
   The daemon answers each request on a thread of its own. Tier 2 reads through a read-only
   connection opened for the question, without the lock that the other tiers, the other
   endpoints and the ledger take turns on, so a question that waits on the models holds up
   nothing else. The cross-encoder runs on an NVIDIA card when onnxruntime can reach one
   (the Windows installer adds DirectML for it; `retrieval/rerank.py`; `rerank = "auto"`
   downloads it only where a judge is set up, since only the judge gained from it), where questions
   take turns on one session, and on the processor otherwise, where they read side by side.
   Tier-2 confidence is the fused rank score, not a probability, so no confidence threshold
   stands in for these checks.
5. Tier 3 queries the active compiled generation when one exists. Missing generations and
   optional-tier failures are treated as no result, not as daemon failures.
6. Existing policy applies caller, purpose, sensitivity, category, and capability bounds.
7. If no tier can answer, the queue deduplicates and persists the question for its
   recipient (the owner, `owner:local`, unless routed elsewhere). With escalation
   disabled, the result is `NOT_KNOWN` with no basis. Nothing is announced; the default
   notifier does nothing, and any notifier receives only the escalation id and urgency.
8. An owner answer closes the escalation and appends tier-1 `USER_CONFIRMED` knowledge
   authored by the recipient. The next equivalent request returns immediately from tier 1.
9. A self-contained ledger records question, caller, answer, citations, policy version,
   serialized request/result, device id, timestamp, and monotonic sequence. Later
   signals about a resolution (`adopted`, `overridden`, `corrected`) are new rows in
   `resolution_outcomes`, never edits.

## Owner workflow

The workflow an agent runs is brief → ask → check, all read-mostly over the same ledgers:

- `start_task(task, project, cwd)` (`briefing.py`) detects the kinds of work in the task
  (`domains.py`: wording first, project folder markers only when the wording names none),
  checking the built-in kinds and every kind the owner's confirmed rules are filed under;
  an owner's own kind ('gardening') counts when the task names it as a word. It then
  assembles: every-task rules, the project's vision and rules, the project's
  one-off choices, general preferences that share terms with the task, the definition of
  done per kind of work (rules with that `domain`, the project's own first), and the
  choices to ask about. It never plans or ranks steps.
- `ask` is `resolve` with the project attached. Tier 1 filters knowledge by scope
  (`scope_applies`: `*`, the asking project, or a project the question names). It falls
  back to `org:<id>` and then `published:<id>` answers only when the owner has none, and
  labels them. Briefing and advice use the owner's own knowledge only. An escalation
  stores its project and recipient; pending questions are unique per question, project
  and recipient.
- A project is its folder's name without a trailing version (`normalize_scope`):
  `san7.13`, `san7.14` and `san7.15` are `san7`, and `shop-v2` is `shop`. The ledgers
  are append-only, so a row stored under a version before this rule is folded when it
  is read (`stored_scope`) and when SQL compares it (`varyz_scope(scope) = ?`, a
  function each store registers on its connection), never rewritten.
  The `ask_owner` block carries a scope question, and `answer_owner_question` stores the
  owner's pick. Without one, `suggest_scope` guesses and fails narrow — a leaked one-off
  choice is worse than asking again in another project. Explicit wording decides first.
  Then the local model (`[workflows] scope_classifier`) widens to general only at
  P >= 0.9; below that the answer is kept to the project. Then the wording
  heuristics.
- `finish_check(task, project)` returns every definition-of-done item and every open
  owner question for the task, to be reported as done/verified or not applicable.

On Windows, managed hooks enter through a GUI executable compiled locally with
the inbox .NET Framework compiler. It relays stdin/stdout/stderr as bytes and
starts Python with `CreateNoWindow`, preventing a console before Python starts.
Its adjacent `.launch` file pins the interpreter, event and archive config without
credentials. Compilation failure leaves client settings unchanged; old `.cmd`
files remain for clients that have not reloaded their hooks. Other platforms use
shell launchers. Hook command changes may require the client's trust confirmation.

Hooks (`hooks.py`, run as `python -m varyz.hooks <event> --config ...` through
launchers in `data/hooks/`) make the first and last steps automatic. The session state
(`data/runtime/hooks/<sha256(session)>.json`) holds what was briefed; the Stop hook reads
the client's transcript and fires once per task, only when files were edited since the
owner's last prompt. Prompts the client writes itself (background-task notices) are not
tasks, and neither are plain questions. Work or question is asked of the local model
(`resolution/prompt_kind.py`, `[workflows] prompt_classifier`), then the wording rule
(every sentence asks, none requests work). The hook never waits for a model load:
`LocalModel(when_cold="warm")` starts the load in a detached process, because Ollama
drops a load whose caller leaves, and lets the rule answer. SessionStart starts the
same warm-up. The Stop hook skips a small change (`[agent] done_gate_small_changes`)
only when the model is already loaded (`when_cold="skip"`) and calls it small; a
cold, absent or crowded model gates as before. Every hook fails open: an error is logged to `data/runtime/hooks/errors.log` and
the agent carries on.

## Advice

`advise(plan, project)` is the call an agent makes before it implements something.
It returns the owner's confirmed rules and project vision that bear on the plan, plus
recorded owner decisions sharing at least two salient terms with it. It does not
rank plans or propose steps.

- Scope is a hard filter. A rule scoped to another project is never returned unless
  the plan names that project. The project defaults to the agent's working directory
  name.
- Inside scope, the ordering is: project vision first, then rules sharing more terms
  with the plan, then project rules before global ones. Rules with no shared term
  still fill the remaining places, because a standing rule ("dry-run first")
  governs tasks that never mention it.
- The number of rules returned is bounded by the caller's capability. An
  unregistered agent gets one rule and a `withheld_hint`. A grant without
  `owner_knowledge` is denied.

Rules enter the ledger three ways:

- `varyz rules add`.
- `remember_owner_rule`, which an agent calls after asking the owner "remember this
  for next time?" and records with `agent_relay:<agent>` source.
- `varyz rules mine --apply`, which only queues *candidates*. From the owner's
  authored, non-sensitive turns, `resolution/profile_reader.py` builds a profile
  in two kinds of task: a *read* hands a model one project's unread turns, oldest
  first, and keeps the notes it gives (vision, never, rule, done, workflow,
  decision), each with quotes that must be the owner's words from the message
  they name and the owner's reason where they gave one (the principle's
  `rationale`); a
  *merge* hands it a project's notes, plus the statements the ledger already
  holds for that scope, and keeps the items that join them. An item needs two
  messages behind it (one for a vision, or a rule whose quote says always,
  never, from now on and the like). What holds in every project goes to a pool
  merged last across projects, at global scope. Visions and nevers become
  `kind="vision"` principles scoped to the project; done items carry their
  domain; a workflow is queued as a principle with `provenance.workflow` and
  becomes a stated workflow when kept, because learned workflows reach agents
  without review. A decision is a question and the owner's answer
  (`provenance.decision`), the latest-dated note's answer when a merge joins
  several, worded as a domain's `decision_points` question when it matches one;
  kept, it is recorded with `remember` as an explicit knowledge entry at its scope,
  superseding the owner's earlier answer to the same normalized question, so `ask`
  and the briefing's `already_decided` answer from it. Merges are also told the
  knowledge entries already recorded. What was read and the notes live in
  `data/rules/profile_<tier>.json`: the local tier reads every non-sensitive
  turn with `[workflows] classifier_model` through Ollama; the cloud tier, shared
  by the `[llm] provider` (the default; `--no-cloud` sets `[workflows]
  rule_reader_cloud = false`; capped by `rule_reader_cloud_max_calls`) and the
  agent's subagent, reads only
  public or personal turns through `privacy/secrets_scan.redact`, so no local
  note reaches a cloud merge. The agent reads through its own sign-in:
  its first briefing in a session (the prompt hook's, or `start_task`'s
  `background_job`) asks it to run a subagent on its
  cheapest model (in Claude Code the `varyz-rule-miner` agent `varyz connect`
  installs: Sonnet, limited to those two tools), which loops `profile_reading_task` /
  `submit_profile_reading`; a merge's state is saved only once the daemon has
  kept its candidates (`[workflows] rule_reader_agent`, on by default). The ask
  is counted only when a reader takes it (its first `profile_reading_task`), so
  one the agent passes over is made again. The setup
  page's result carries `reading` (`first_reading`): how many turns the connected
  agent's first `start_task` will start reading, with nothing to paste. A long
  turn that opens the same way in several sessions is a pasted prompt and is
  skipped (`resolution/rule_reader.py`). Instruction files are matched
  lexically (`resolution/rule_miner.py`).
  `rule_reader.reading_order` puts the cloud first and the local model after it,
  or the local model alone when the owner keeps their turns on the machine;
  `local_classifier.machine_fit` leaves the local model out under 2 GB of free
  disk. With no reader, or `--lexical`, or
  `[workflows] rule_miner = "lexical"`, turns are matched lexically as well. A
  candidate is never shown to agents. `varyz rules review` (a local page,
  `review_web.py`, or `--terminal`) accepts it (appending a confirmed row that
  supersedes it, or a stated workflow), rewords it, rescopes it, edits a workflow's
  name and steps, or rejects it (a
  retraction). `varyz rules stats` reports the review acceptance rate, which is the
  miner's live precision.
- **Trial rules** (`resolution/trials.py`, migration `0044_trial_rule_uses.sql`).
  When the daemon queues a candidate read by a model (`model:`/`cloud:`/`agent:profile`),
  `TrialRules.consider` puts it into effect on trial if `assess` finds strong
  evidence: a correction, two conversations, or two projects. A vision, decision,
  workflow, a rule said about the moment, one that loosens what an agent may do with
  something sensitive, and one close to a rule in effect always wait for review.
  A trial rule is a confirmed row superseding the candidate, with `provenance.trial`
  (`since`, `signals`); briefings mark it "(on trial, id ...)" and tell the agent to
  replace it on a correction. The hook and MCP `start_task` report which trial rules
  a task was shown (`POST /v1/principles/uses`, one row per rule and task); after
  `trial_promote_tasks` tasks over `trial_promote_days` days it is confirmed for good
  (`provenance.trial_ended.how = "promoted"`). `varyz rules review` adds a fixed
  random sample of `trial_spot_check` trial rules; keeping one confirms it, dropping
  one retracts it as `TRIAL_DROPPED`. New trials pause once at least 5 were
  spot-checked and fewer than `trial_min_kept` kept, or while `rules/trials.off`
  exists. Trial rules do not count toward the review acceptance rate. A quote that
  says the rule holds from now on (`rule_miner._GENERAL`, not about the moment) is
  a signal of its own ("said to hold from now on").
- **Live reading** (`resolution/live.py`, `profile_reader.live_offer`). The
  `UserPromptSubmit` and `Stop` hooks send `POST /v1/live/nudge`. Two seconds later
  the daemon's `LiveIngest` syncs just the `ai_conversations` connector under
  `WriteLock`, folding nudges that arrive together into one read. It skips when the
  connector is not approved, while autosync's `RUNNING` claim exists, or under
  `PAUSED`. A prompt that may hold a rule gets a `background_job` with `live: true`:
  an instruction or a preference, or any prompt while unread turns from the last
  `live_reading_hours` wait. That job is a non-task prompt's whole hook output; on a
  task it is the briefing's job. It is asked at most once a minute (the hook and
  `start_task` brief the same prompt), and again only after
  `live_reading_min_minutes` once a live reader took it. Standing wording skips that
  wait. The subagent calls `profile_reading_task` with `recent: true`. That syncs
  the connector once (at most once a minute) and cuts tasks only from turns said
  since `live_since`. Readers share `profile_cloud.json` under `reading_lock`.
  `rules/agent_offer.json` keeps `taken_at` for the daily reading separate from
  `live_taken_at` and `live_offered_at`.
- **Headless reader** (`resolution/headless_reader.py`, `resolution/usage.py`). With
  `[workflows] reader_runner` not `agent`, `hand_off` takes the reading offer away
  from the working agent: `request` records it as pending in
  `rules/headless_reader.json` and spawns `varyz rules read` detached. `supervise`
  holds `headless_reader.lock`, reads each runner's plan usage (Claude
  `api/oauth/usage`, Codex `wham/usage` or its newest rollout; tokens from the CLIs'
  files, never refreshed or logged) and runs the one with most headroom above the
  gate: `large_reading_min_left_*` for the full backlog, `small_reading_min_left_*`
  for live reading; unknown usage allows live reading only. `run_one` launches
  `claude -p --model <reader_claude_model>` or the native `codex exec` (newest
  usable luna slug from `models_cache.json`) with a strict MCP config exposing only
  `profile_reading_task` and `submit_profile_reading`, `VARYZ_HEADLESS_READER` set
  (hooks skip it) and no transcript. It polls usage every `reader_usage_poll_seconds`
  and reads the stream's `rate_limit_event`s. On a breach, unknown usage beyond the
  grace, `reader_max_minutes`, `PAUSED` or `rules read --stop` it sets `stop_run` (the
  next `profile_reading_task` answers `stopped`), then kills the process tree. Turns
  are marked read only on submit, so nothing is lost. A blocked reading records
  `resume_after` (the latest reset among the blocking windows); the daemon's
  `ResumeWatcher` and the next hook restart it then. `headless_reader.log.jsonl` is
  the append-only run log. `rule_reader.UsageGuard` gives `varyz rules mine` the large
  gate when its cloud model spends a Claude or ChatGPT sign-in.
- **Quiet reading** (`profile_reader._full_offer`, `live_offer`). Every job's `do`
  tells the agent not to mention it or relay the subagent's result. A major full
  reading (no state yet, or `MAJOR_READING_TURNS` = 300 unread) instead carries
  `announce`, one plain `ANNOUNCEMENT` line to tell the owner, at most once per
  `ANNOUNCE_AGAIN_SECONDS` (30 days): offering it records `announce_offered_at` in
  `agent_offer.json`, and the reader taking it records `announced_at`. When the
  headless reader takes a major reading, `hand_off` returns a notice-only job (no
  `subagent_prompt`) so the line still reaches the owner.
- **Setup reading step** (`profile_reader.setup_step`, 2026-10-10). The first reading
  (no state yet, announcement due) carries `setup`: the agent explains it in a short
  block, then starts it without asking. On "not now" it asks when and calls the
  `schedule_reading` MCP tool, which stores `deferred_until` in `agent_offer.json`.
  Until then `agent_reading_offer`, `live_offer` and `profile_reading_task` stand down,
  a running reader stops (`run_one` reason "put off"), and the headless reader sits in
  state `scheduled` with `next_check` at that time, when `maybe_resume` starts it.
  `varyz rules read --now` clears the deferral.
- **Computer Cloud connection** (`cloud/computer.py`, 2026-10-04). The known-config
  loopback daemon exposes only `/cloud/computer` to the exact approved Cloud Origin,
  with a loopback Host check and one expiring, single-use sign-in capability. Browser
  clicks launch the shared PKCE connection flow, bound to the account-page identity
  and active entitlement. CORS never extends to resolution endpoints. No selection,
  archive access or sync runs through this bridge; failures return a generic state.
  The paid account page uses a bundled, nonce-authorized script with a fixed local
  endpoint, and retains manual setup when browser access or the daemon is unavailable.
- **Guided Cloud approval** (`cloud/guided.py`, 2026-10-04). Scope choices and projected
  upload/deletion counts stay local during preview; general-only is represented by
  `['*']`, whereas an explicitly chosen all-projects projection uses `[]`. A one-use
  approval binds to the journal and selected records. `LocalSync.sync(prepare=...)`
  checks that snapshot and enables the selected projection under its existing lock,
  then performs versioned sync and hosted retrieval. Changes require another preview;
  conflicts retain explicit review. The browser shows one step at a time and never
  treats a failed retrieval check or an empty selection as verified AI rule use.
- **Realtime Cloud sync** (`cloud/live_sync.py`). `run_resolution_daemon` starts
  `LiveCloudSync` when the config is known. Its watcher syncs when `PRAGMA
  data_version` changes and `LocalSync.plan` has work. Its listener long-polls
  `GET /api/sync?wait=25` and wakes the watcher when records arrive. On failure it
  backs off, up to 5 minutes; payloads are never logged. On the server, `pull` is
  async. It waits on an `asyncio.Event` that `push_records` sets from any push by
  the same account, and re-reads the store every 5 s for other replicas. It holds at
  most `VARYZ_CLOUD_SYNC_WAITERS` requests (default 8) and 4 per account; past
  either limit it answers at once.

## Workflows

A workflow (`resolution/workflows.py`, migration `0039_owner_workflows.sql`) is the
order the owner guides an agent through a kind of work: named, scoped (`*` or a
project), optionally tied to a kind of work (`website`), and a list of steps, each
with how the owner guides it. The ledger is append-only like the rules: a new order
supersedes the old row, and a withdrawal is a retraction row.

| Decision | Choice | Why |
|---|---|---|
| Learned orders | used straight away, marked `learned`, withdrawable | the owner chose it for varyz; a review queue would sit unread |
| Stated vs learned | a stated order is never replaced by a learned one | the owner's words beat inference |
| Withdrawn orders | an exact learned order the owner withdrew is never learned back | withdrawing is the correction |
| Out-of-order starts | do the task, mention the open earlier steps once, offer them, never block | varyz advises; it does not lead |
| Settled steps | settled by this project's decisions and principles | a project that picked its colours is not asked again |
| Which starts are raised | only new work at a later step; a small change is just done | a fix to the page is not the moment to reopen the colours |
| Sizing a task | a local model, then the local embedding, then a verb list; loopback only | the input is the owner's prompt; 68/69 vs 42/69 held out |
| Owner vs order | the owner wins the task; the answer is recorded, not argued | varyz advises; the order earns its place with evidence |
| Changing the order | after 2 skips within 7 days with no rework, ask once: keep / optional / drop | never silently; older skips expire so a rare skip never adds up; a skip that cost rework is never put up for dropping |

`start_task` returns `owner_workflows`: the workflow whose kind of work or name the task
matches (project scope before global), each step marked settled or open, the task's
step (the step whose terms the task covers best), the open steps before it, and one
line of guidance. `finish_check` repeats the open earlier steps as
`workflow_steps_open`.

When the task's step has open steps before it, the task is sized `new` or `small`
by a `TaskSizer` (`resolution/task_size.py`, built by `sizer_from_config` from
`[workflows]`); at the first step, or with nothing open, no sizer runs and `work` is
null. The default chain is:

1. `ModelSizer`: a local Ollama model (`qwen3.5:4b`) is shown eight authored examples
   and asked for one letter (A new work, B small change); P(new) is read from the
   first token's log-probabilities, `num_predict` 1, no thinking. It raises
   `SizerUnavailable` (and the next sizer answers) when Ollama is not running, the
   model is not pulled, loading it (file size + 15% + 512 MiB, measured) would
   leave less than `classifier_vram_headroom`
   (30%) of the card free (checked with `nvidia-smi` only when it is not already
   loaded), or it does not answer within `classifier_timeout_seconds`. The URL must be
   loopback, checked by the config and again by the class.
2. `EmbeddingSizer`: nearest centroid of the 81 authored `EXAMPLES` on the archive's
   local sentence embedding; no GPU.
3. `lexicon_sizer`: the verb lexicon (`task_size`), which always answers.

`sized_by` names the one that answered, and `varyz hooks doctor` says which one
would answer now (it probes Ollama; it never loads the model). Held out (69 authored tasks, never tuned on):
model 68/69, embedding 62/69, lexicon 42/69; model ~144 ms warm, ~4 s when it has to
load (`reports/TASK_SIZE_CLASSIFIER_2026-09-25.md`, `scripts/task_size_bench.py`).
The same `LocalModel` (`resolution/local_classifier.py`) answers the rework, work-or-question and scope questions, and verifies `ask` paraphrases. Each one falls back to the rule it replaced (`reports/LOCAL_CLASSIFIER_ROLLOUT_2026-09-25.md`).
The MCP server builds the chain once; each hook process builds its own. Only `new`
work with open earlier steps sets `nudge`, and only that goes to
`workflow_steps_open`. Optional steps are shown but never open.

The nudge ledger (`resolution/workflow_nudges.py`, migration
`0041_workflow_nudges.sql`) records, per raised step, what the owner answered:
`took`, `skipped`, or `rework` against a skip id. Agents record these with
`record_workflow_nudge`, because the hook's database connection is read-only.
Rework is detected read-only in `guide()`: a redo task at step T, in a project with
an open skip to T whose skipped step is now settled, names that skip for the agent to
record. The chain decides whether a task redoes a step (`[workflows] redo_classifier`:
the local model, then the verb list "redo", "rebuild", "again", ...). It is asked
only when such a skip exists. History is counted across
the workflow's versions (same name and scope) since the last review of each step;
skips older than `REVIEW_WINDOW` no longer count toward a review but still count
for rework.
When a non-optional step has at least `REVIEW_AFTER_SKIPS` (2) skips within
`REVIEW_WINDOW` (7 days) and no rework after any skip since its last review
and has not been reviewed on this version, `start_task` carries a `review` question
and `finish_check` lists it under `workflow_reviews_due`. The answer
(`answer_workflow_review` or `varyz workflows review`) goes to
`owner_workflow_reviews`, one per step per version. `keep` changes nothing; `optional`
and `drop` write a new stated version with the review in its provenance. Only the
current version can be reviewed, and a drop never leaves fewer than two steps. Both
tables are append-only by trigger.

The learner (`resolution/workflow_miner.py`) reads only the owner's own,
non-sensitive `ai_conversations` turns, with code blocks and code-like lines removed,
and matches a small fixed lexicon of steps (website: colour, logo, fonts, page
content, legal pages, SEO, hosting; product: vision, naming, design, monetization,
website, marketing, launch). A unit is one website session (thread) or one project.
Each unit casts one vote per pair of steps, from the strongest evidence it has:

1. an explicit order ("colours before the logo", "after the logo, ...");
2. a correction: a step begun earlier was redone once another was settled ("now redo
   the hero with the new colours"), or a regret ("should have picked the colours
   first"). The step returned to counts as first, so a slip is not copied;
3. otherwise, the step mentioned first, but only in a website session. A project runs
   for months, and what came up first there ("launch the dev server", "design the
   schema") is not the order the owner guides it in; on the real archive it produced
   *marketing → design → website → launch → vision*. Product orders use levels 1
   and 2 only.

Learned orders reach agents straight away, so the bar is high: at least 3 units cover
each step, and each link has 3 votes with at least 3/4 agreeing. Steps are ranked by
Copeland score. Provenance records unit counts,
agreement, dropped steps and up to 5 sample entry ids, never text. Autosync runs the
learner after new AI-conversation turns arrive only when `[autosync] learn_workflows` is
on. It is off by default: on the owner's archive it cost about 2.6 s a sync and learned 0
workflows from 5,876 turns.

Task recipes (`resolution/recipes.py`, migration 0051) take its place for the agent's
side. A recipe is what an agent learned doing one task: a name, 2 to 8 steps of at most
200 characters, a check and one pitfall, at most 1,600 characters rendered. The agent
writes it through `record_recipe`, so no AI call is made; a credential found by the
secrets scan refuses it. `task_recipes`, `task_recipe_retractions` and
`task_recipe_outcomes` are append-only. The same name and scope again is one recipe; a
correction supersedes, and an outcome is `worked`, `changed` or `failed`. `start_task`
serves the best match by word overlap with the task (the project's own recipe before a
general one) unless its last two outcomes failed, and `finish_check` either offers to
record one or asks how the served one went. Cloud sync carries current recipes (name,
steps, check, pitfalls, scope; never provenance or outcomes) as the `recipe` kind, which
the Cloud validates with the same caps; hosted `start_task` serves them from synced
records, and following and project sharing publish them like workflows. They are not on
the owner's Cloud dashboard.

Decisions come from the same transcripts (`resolution/decision_capture.py`). Each
autosync cycle reads the lines each Claude Code transcript gained since its stored byte
offset (`data/autosync/decision_capture.json`). The `toolUseResult` of an AskUserQuestion
call carries the questions and the options the owner picked. Each pick becomes
user-confirmed knowledge scoped to the session's project, stated at the time of the
pick. Its source is `claude_code_pick:<tool_use_id>:<n>`, so a rescan or a resumed
session's copy is recognised, and a retracted pick is never recorded again. A pick
supersedes the project's current answer to the same normalised question only when it
is newer. Relayed questions (header `varyz`), reach questions, rule offers, declined
calls and sidechains are skipped. There are at most 50 picks per cycle. A file's offset
advances only when all its picks were handled (`[autosync] capture_decisions`).


## Persistence

Migrations `0032_owner_knowledge.sql`, `0033_escalations.sql` and later ones, up to
`0039_owner_workflows.sql` (the workflow ledger) and `0040_workflow_turn_labels.sql`
(an empty step-label cache kept for archives that already applied it; its labeller is
parked, see `reports/VARYZ_SHIP_2026-09-25.md`) and `0041_workflow_nudges.sql` (the
nudge answers and step reviews), are additive.
`0038_authorship_outcomes_recipients.sql` added knowledge `author_id`/`origin`, the
append-only `resolution_outcomes` table and escalation `recipient_id`.
Existing rows default to the local owner's own answers and questions. The pivot
snapshot retains entries, chunks, FTS/vector indexes, the content-addressed vault, graph
extraction cache, and personal-model annotation cache. Old Mirror and compiled-model
generations are derived data and are excluded.

Durability rules:

- owner knowledge and resolution outcomes are immutable after insertion;
- a correction uses `supersedes_id`;
- every new write carries `device_id`, wall time, and monotonic sequence;
- pending escalations survive daemon crashes and restarts;
- a ledger row can be understood without joining compactable derived tables.

The future sync encryption boundary is the serialized knowledge/escalation/ledger payload.
Question, answer, caller identity, purpose, policy decision, and provenance would be
encrypted end to end. Routing metadata is limited to opaque object/device ids, version,
sequence, and ciphertext length. The alpha implements no remote transport or encryption.

## Identity and disclosure

`doctor.py` (2026-10-03) reads operational metadata and checks the archive,
resolution daemon, registered client credentials, hooks, sync outcomes, autosync
heartbeat and reader outcomes. Token validation queries hashes and capabilities
without updating last-use timestamps. The CLI performs an archive integrity check;
session probes omit that scan and classifier probes. Session hooks and MCP calls
attach brief failure notices with per-session throttling (60 seconds) and
deduplication. Runtime notice state stores only check names and timestamps.
Hook errors remain fail-open; a failed briefing can still carry a health note.
Delivery is event-driven, not an independent chat push while an agent is idle.

`doctor_repair.py` supplies `doctor --fix` and archive-scoped repair handoffs.
As of 2026-10-04, unchanged blocked failures stay blocked until a healthy check
or a code/config change. Abandoned handoffs retry with exponential backoff
(20 minutes to 6 hours), independently of the renewable running lease. A healthy
interval starts a new failure episode. Reader quota waits are healthy only until
their scheduled recheck plus a three-minute grace; orphaned queues resume, and
supervisor exceptions record a failed state without private exception text.

Explicit agent connection pins its Python in `data/hooks/runtime.json`. Automatic
refreshes from another checkout or old runtime cannot overwrite that installation's
hooks or instruction files. A top-level `redirect_config` in a config file aliases
one canonical config without copying settings or moving the old archive. Redirects
preserve command/environment precedence and fail closed on cycles or unreadable
targets. Package freshness includes JavaScript, JSON and text assets.

Windows shared archive leases use `LockFileEx` without the exclusive flag.
MSVCRT's `LK_NBRLCK` is exclusive on Windows, so it cannot implement a shared
release lease: a daemon otherwise blocks every separate agent as a supposed
upgrade. Cross-process tests allow simultaneous shared holders and reject the
exclusive upgrader until the last holder closes; an exclusive release still
blocks new clients. See Microsoft's `_locking` constants and `LockFileEx` docs.

Known repairs use the existing CLI (30-second timeout per command) and verify
against a fresh diagnosis. A local file lock leases one background repair offer
for 20 minutes across sessions; no daemon authentication is required to create
it. The managed agent-instructions block authorises a continuing repair subagent
with local tools, one start note and one verified result. The owner-written
instructions stay intact and uninstall removes the managed block. The rule
reader suppresses health handoffs to prevent recursive repair agents.
Installation writes both mining and repair instructions together. Session launch
and MCP startup refresh that block even when the code fingerprint is unchanged;
this needs no archive write, daemon or valid agent token. Only connected clients
for this archive or existing managed blocks are eligible.
Repair lifetime is independent of the parent's turn: the parent answers without
waiting and never cancels repair just because its own task finishes. Repairs end
on verified recovery or a concrete owner-action blocker, not an elapsed ten-minute
deadline or one failed pass. `doctor --repair-job ID --repair-state running`
renews the lease, preventing duplicate offers while a long repair is active.
Marking the job `fixed` runs an independent doctor check and rejects failures.
Native subagent lifetime is controlled by the agent client; this protocol cannot
keep a subagent alive if its host forcibly terminates it.

`agent_principals`, `agent_tokens`, and `agent_capabilities` supply the existing schema.
Tokens are stored only as hashes, can expire or be revoked, and select a capability scoped
by purpose/tool with `max_items`, `max_chars`, and background-use bounds. The resolved
caller is passed into existing policy and egress accounting.

Accounts are planned. An account will tie a person to the data they sync to it (answers,
rules, preferences), kept private to them; nothing of it becomes public unless they
publish it themselves. The local archive stays the working copy, and the privacy notice
(`docs/PRIVACY.md`) will say what is stored and for how long before accounts open.

Sensitive or unclassified content never enters a notification body or cloud path. The
local inbox uses loopback Host checks, a short-lived session cookie, Origin/Referer and
CSRF validation on writes, autoescaping, no JavaScript, and a default-deny CSP.

## Local inference and compile

The alpha works with zero compiled generations. Tier 1, tier 2, policy, and escalation are
independent of the compiler. When used, it retains its supervisor, checkpointing,
annotation cache, GPU guards, and atomic generation swap. Incremental mode can restrict
work to the last N days or episodes touching hot topics.

The llama.cpp runtime and GGUF may be shared through read-only directory junctions. They
contain no personal archive data. If links are unavailable, the verified installer may put
fresh assets below this tree's `data/` directory.

The runtime comes from llama.cpp's release archives, pinned by SHA-256 per platform:
Vulkan, CUDA or CPU zips on Windows, Metal (Apple Silicon) or CPU (Intel) tarballs on
macOS, Vulkan or CPU tarballs on Linux. The tarballs wrap `build/bin` in one
`llama-<release>/` folder, which the installer lifts into place. Their shared libraries
are versioned, so the server loads `libllama.0.dylib` (or `.so.0`) through a link. Links
to a sibling by bare name are extracted; any other link, absolute path or `..` is
refused. The host-memory floor reads `GlobalMemoryStatusEx` on Windows, `vm_stat` on
macOS (free, inactive and speculative pages) and `sysconf` on Linux.

## Platforms

The same code runs on Windows, macOS and Linux; `.github/workflows/clean-machine.yml`
tests all three. Where they differ:

| | Windows | macOS | Linux |
|---|---|---|---|
| Autosync at login | hidden Startup `.vbs` running `daemon_runner.cmd` | launchd agent `~/Library/LaunchAgents/com.varyz.autosync.<id>.plist`, `ProcessType` Background, loaded with `launchctl bootstrap` at install | systemd user service, or XDG autostart |
| Hook launchers | `.cmd` | `.sh` | `.sh` |
| File locks | `LockFileEx` / `UnlockFileEx` (shared archive leases, exclusive writers) | `fcntl.flock` | `fcntl.flock` |
| Secrets | Credential Manager | Keychain | Secret Service |

On macOS, launchd starts the agent with only the system folders on `PATH`. The plist
therefore carries the installing shell's `PATH` plus `/opt/homebrew/bin` and
`/usr/local/bin`. A reinstall of a loaded agent uses `launchctl kickstart`, which never
kills a running sync. Uninstall runs `launchctl bootout`. If `launchctl` is missing or
refuses, the daemon starts as a plain background process and launchd takes over at the
next login.

macOS privacy protection blocks a background process from Desktop, Documents,
Downloads, iCloud Drive, Pictures, Movies, Music and `/Volumes`, without asking. Each
cycle, the agent tests those configured folders (and the archive itself) from its own
process, since a Terminal with Full Disk Access would see them all. It records the
blocked ones in `data/autosync/macos_privacy.json`, warns in the run report, and
`autosync doctor` says how to allow them.

macOS asks the owner about a folder only for an app, and a launchd agent running Python
directly has no switch but Full Disk Access. So install builds `data/autosync/Varyz.app`
(`varyz.mac_app`): the system's AppleScript applet (`osacompile`), its Info.plist given
the agent's label as bundle id, `LSUIElement`, and a reason for each folder key only,
then signed ad hoc (`codesign --sign - --identifier <label>`). The agent runs
`/usr/bin/open -W -n -g -a Varyz.app`; the applet runs `varyz-autosync.sh` beside it and
waits, so the daemon's reads are the app's to ask about. The script sets the working
directory and `PATH` (an app inherits neither) and runs the daemon under `taskpolicy
-b`. Only the script changes between installs; the app is rebuilt only when
`mac_app.APP_VERSION` changes, since a rebuilt app is new to macOS and is asked about
again. Where `osacompile` or `codesign` is missing, the agent runs Python as before.

## Updates

`varyz.update` keeps a git checkout on the latest commit of the branch it tracks.
`[update] mode` is `off` by default: a check fetches from the branch's remote, the one
network call Varyz would otherwise make on its own. With `notify` or `auto`, the first
MCP tool call or CLI read command of each local day starts a detached
`python -m varyz.update`; the day is marked before the launch, so one day never starts
two checks.

A check fetches the upstream without tags and compares. Only a fast-forward is applied.
A detached HEAD, a branch without upstream, local commits, a diverged branch or local
changes to tracked files leave the checkout as it is, with the reason recorded. When
the new commits touch `src/varyz/db/migrations/`, the database is first copied with
SQLite's backup API to `data/autosync/pre-update.db` (one copy, replaced each time;
refused when the disk lacks room), since migrations cannot be undone. When they touch
`uv.lock` or `pyproject.toml`, `uv sync --frozen` follows the merge. On Windows that
sync cannot replace libraries open sessions hold, so an automatic check reports
`waiting` and leaves it to `varyz update apply`. The new code must then import
`varyz.cli`, `varyz.mcp_server` and `varyz.autosync_daemon` in a fresh interpreter,
without opening the archive, or `git reset --keep` returns the checkout to the commit
it came from.

The running autosync daemon sees its source fingerprint change and restarts on the new
code; MCP servers pick it up with the agent's next session. State is in
`data/autosync/update.json`, every attempt is appended to `updates.jsonl`, and
`UPDATE_PAUSED` (`varyz update pause`) or the autosync `PAUSED` switch stops it.

An installed copy (a `uv tool install`, not a checkout) is updated from the source in
its `uv-receipt.toml`. For a GitHub source, the installed commit (from the
distribution's `direct_url.json`) is compared with `git ls-remote`. For a folder, the
files the wheel ships (honouring `source-exclude`) are compared with the installed
package, after the folder is fast-forwarded as above when it is a git clone. An
automatic check never installs a folder with uncommitted changes. The update is
`uv tool upgrade varyzai --reinstall`, which keeps extras such as the DirectML build.
The new install must pass the same import check. On Windows an automatic reinstall
waits for `varyz update apply`.

After any applied update, the new code rewrites what `varyz connect` installed for every
client whose MCP entry starts this Varyz against this archive: the routing and away
skills, the reader agent, the reader-fallback section of the client's own instructions
(`~/.claude/CLAUDE.md`, Codex's `AGENTS.md`, between `<!-- varyz:reader-fallback -->`
markers; the owner's text around it is kept and backed up once), the hook launchers and
the entry. A client that was never
connected, or whose entry starts another Varyz (a checkout beside an installed copy), is
left alone. The MCP server does the same once per code fingerprint when it starts
(`data/hooks/connected.json`, under a file lock), so an update made outside Varyz
needs no reconnect either.

## News

`varyz.news` passes posts from varyzai.com/news to the people who use Varyz. News is on
by default; `varyz news off` writes `data/news/OFF` and stops both the check and the
notices, `varyz news on` removes it. There is no setup question for it.

While Varyz is in use (the `session-start` and `user-prompt` hooks, an MCP tool call, a
CLI read command) a check is due once an hour. It is started the way the update check
is: `attempted_at` is written to `data/news/state.json` before a detached
`python -m varyz.news` is launched, so nothing waits on the network and one hour never
starts two checks. The check is a GET of `[news] url` (default
`https://varyzai.com/news.json`) with a 10 s timeout and three tries, 1 s and 3 s apart.
It sends no identifier and no archive content; the server sees the request, the IP
address and `User-Agent: Varyz/<version>`. The feed is capped at 256 KB and 50 posts,
validated (id, date, title) and cached in `data/news/feed.json`.

The `user-prompt` hook hands the agent at most two unseen posts per prompt, newest
first, as quoted text with an instruction to end the reply with one line for the
owner. A post shown is recorded in `data/news/seen.json` and never shown again. Posts
older than 30 days or dated after today are not shown, so a new install gets no
backlog. Client-generated prompts (task notifications) and `briefing_hooks = false`
get no news, and the hooks-paused switch silences it with everything else.

The same hook passes on an update an automatic check found (`[update] mode = "notify"`,
or `auto` waiting for `varyz update apply`), once per target version
(`data/autosync/announced.json`); before this, `notify` only wrote `update.json`, which
nobody read. A check the owner ran by hand is not repeated, and `varyz news off` does
not silence it: `[update] mode` does.

Posts are written in `docs/NEWS.md` as `## YYYY-MM-DD Title` sections; the first
paragraph is the summary. `scripts/site/build_site.py` builds `website/news/index.html`,
`website/news.json` (what Varyz reads) and `website/news.xml` (Atom, for feed readers)
from it, holds back posts dated after the build day, and fails the build on a title or
summary the client would cut.

## Operational invariants

- default bind is `127.0.0.1`; non-loopback requires stronger configured auth;
- no runtime path requires an API key or network access; Varyz makes two calls on its
  own: the hourly news check (on by default, `varyz news off`), which sends nothing but
  the request, and the update check, off unless `[update] mode` turns it on;
- tests use synthetic fixtures and never touch real config or source archives;
- the notifier protocol cannot carry question text;
- capability ceilings are enforced after retrieval;
- `graph.excluded_sources` is enforced before graph/compile work and before every
  agent-facing archive retrieval; direct owner `varyz search` remains unfiltered;
- no answer basis is assigned to `NOT_KNOWN`;
- held-out TRR is generated only after tuning is frozen;
- the sibling `varyz/` checkout is never written by this build.

## Evaluation

TRR scores correct answers, correct abstentions, appropriate escalations, fabricated
answers, mis-sourced answers, and wrongly withheld answers. It reports answer and
abstention rates alongside the aggregate. Synthetic corpora seed an empty database and run
the actual resolver offline. Dev may guide changes; held-out is authorized and recorded
once after all code is frozen. The dated report under `reports/` compares it with the
frozen A/B/C/D baseline.

The GBrain technique comparison (2026-10-04) uses
`scripts/evaluate_gbrain_techniques.py`: an immutable pre-change vector baseline,
the actual candidate implementation, the frozen public retrieval fixture, and independent
synthetic document-density windows. Vector retrieval starts at `3 * limit` chunks and
expands fourfold only when entry aggregation underfills the requested result count.
It stops after three expansions, index exhaustion, or the candidate cap (20,000 chunks;
an explicitly larger initial request retains its existing budget). Each entry keeps its
nearest chunk. The experiment measures source recall and preserves abstention checks;
it does not establish live-model quality or owner-resolution accuracy. Title boosts and
intent weighting remain experiment-only: 32 fresh synthetic signal-activation probes
find a narrow named-document gain for titles and no gain for intent weighting, without
establishing a general ranking policy.

The follow-up `scripts/evaluate_title_boost.py` retains adaptive scanning in both
arms and tests the unchanged 1.25 title multiplier on 144 frozen synthetic cases
with cached local MiniLM embeddings. First relevant source improves from 37.5% to
58.3% in each 48-case evaluation window. A separately frozen, targeted 12-case audit
introduces ten wrong-first-source flips: obsolete pages, drafts, unanswered questions
and indexes can outrank valid evidence. Correct evidence remains in the top three.
General title boosts remain off. These are source-ranking experiments, not generated
answer tests or real archive acceptance; see `reports/TITLE_BOOST_FURTHER_2026-10-04.md`.

## A/B rule shadow (added 2026-10-04)

`resolution/shadow_policy.py` projects the append-only principles, knowledge and
workflow ledgers into typed records. Identity follows `supersedes_id` to its root;
record type, lifecycle, approval evidence, scope/domain and proposed sync eligibility
are separate fields. A read savepoint gives one source snapshot without changing the
caller's transaction. Exposure-promoted trial descendants remain provisional in A.
Legacy confirmed rows are labelled `recorded`, not asserted to have fresh owner proof.

`resolution/shadow.py` owns a separate append-only SQLite event/comparison/review
journal. An observation appends metadata changes and a paired baseline/proposal record
in one transaction. Idempotency keys include task/session HMACs, source hash, actual
policy-source hash, phase, origin and UTC day. Different packets after ledger changes
are new observations. Hook/MCP/daemon observers return the original current result;
errors and limits stop collection, with no fallback that changes live rules.

| Decision | Choice | Rationale |
|---|---|---|
| Activation | Per-archive ENABLED/PAUSED files | Immediate pause for persistent processes; default off |
| Storage | Separate SQLite under data/rules/shadow | No archive migration or cloud payload changes |
| Policy | B selection over A's state projection | Preserve trials; test working-project scope and critical-rule inclusion |
| Unknown semantics | Flag unannotated rules; only typed conflicts/default overrides | Annotation stress in RULES_SYSTEM_REVIEW_2026-10-04 failed three cases |
| Answer control | Exact recorded preferences; no permission reuse | Do not pretend to benchmark all retrieval/model tiers |
| Collection | Existing request paths, no scheduled worker | Real tasks arrive naturally; no new process or external requests |

The source ledger is immutable ground truth. The shadow event log is an observation
history, not a distributed command/event authority or replacement sync protocol.
See `docs/RULE_SHADOW.md`; transport contract models remain unchanged.


## Approved Cloud snapshot — 2026-10-05

| Decision | Choice | Rationale |
| --- | --- | --- |
| Approval | Named projects plus separately approved General | Existing and future work without per-record selection |
| Authority | Local archive remains ground truth | Cloud replaces approved active snapshots; no bidirectional archive edits |
| Privacy | Sensitive/unclassified/credential-bearing records fail closed | General is a project category, not a sensitivity override |
| Storage | Authenticated source/vector encryption; HMAC postings | Database copies cannot reveal readable passages; service retains decryption access |
| Retrieval | Bounded account-scoped BM25 plus optional cached MiniLM cosine/RRF | Offline query inference, no external provider fallback or hosted answer model |
| ChatGPT | Dedicated OAuth read scope and only search/fetch | Retrieval credentials cannot upload or modify preferences |
| Operations | Quota/chunk bounds, two simultaneous MCP reads, resumable acknowledgements | Predictable initial limits; production load and backup gates remain |

`cloud/archive_sync.py` selects eligible project/General records, builds local
vectors, emits versioned updates and checkpoints each acknowledgement.
`cloud/archive.py` owns encrypted storage and source-addressed retrieval.
`cloud/archive_cli.py` provides offline preflight and local approval controls.
`cloud/background.py` reuses the existing autosync cycle; no additional daemon.
The Cloud service enforces OAuth grants, local-only uploads and a separate MCP route.
See `docs/CLOUD_ARCHIVE.md` and `reports/CLOUD_ARCHIVE_2026-10-05.md`.


### Combined Cloud context deployment — 2026-10-05

`/mcp/context` uses explicit `varyz.context.read` consent for read-only preferences
and approved evidence; legacy archive grants stay search/fetch only. The
`archive_views.py` mixin owns dated timelines, retained first/recent source versions,
owner export and persistent project revocation. Guided preview binds source content,
account and journal; project approval includes future eligible records. General is
separate. The server retains encrypted bounded revisions within quota, and rejects
stale uploaders after revocation. Cloud evidence is not an authoritative task ledger.

### Separately approved session reports, 2026-10-07

| Decision | Implementation | Reason |
|---|---|---|
| Project consent | The prepared 2026-10-08 browser redesign uses one explicit project choice and a reviewed preview; `.cloud-reports.json` retains separate engine receipts | One project choice explicitly names included context; legacy category controls remain available |
| Report-only boundary | `session_report_sync.py` exports gated compact checkpoints | No raw transcript upload or preference learning |
| Resume and retry | Immutable account/checkpoint IDs, versions, fingerprints, batch receipts | An interrupted acknowledgement cannot duplicate a checkpoint |
| Privacy and revocation | Recheck each batch; erase prior copies when current state becomes private; require fresh owner review to renew approval | No stale public snapshot or automatic reapproval |
| Storage and discovery | `session_reports.py`, encrypted payloads and account-specific hashed terms | Tenant isolation, exact project identity, bounded topic/date retrieval |
| Hosted use | Read-only `session_history` on plugin/context/archive MCP routes | Preserve source dates, provenance, citations and unresolved work |

Existing live/background sync carries approved reports through the local account
credential. The hosted plugin cannot upload, mine local chats or approve projects.
An optional original-source `cloud_since` bound narrows backfill without granting
sharing. Cloud deployment and local runtime activation remain pending in this
change's dated report; source/package availability alone does not prove activation.

Recovery stages on disk and encrypts to the existing off-host age identity. Exact
checksum receipts follow verified restores; server snapshots and admission budgets
bound disk growth. Archive-key recovery is separately encrypted. The deployed
read-only transport passed official MCP SDK tests; actual phone verification is pending.

### Hosted plugin candidate — 2026-10-05

`/mcp/plugin` combines eight confirmed-preference tools and five approved-history
read tools under explicit `varyz.plugin` consent. This new resource does not widen
legacy grants or refresh tokens. Scoped tokens permit the plugin endpoint and
account-bound source reads only; they cannot use uploader, management or export APIs,
even under `varyz-local`. Dashboard computer-connect intent cannot bypass new consent.
The primary ChatGPT portable manifest declares one remote server with five positive
and three negative initial-review cases; the remote Claude package uses the same
URL and shared skills. Source and in-process tests are prepared. Remote deployment,
reviewer provisioning and native/platform acceptance remain pending.

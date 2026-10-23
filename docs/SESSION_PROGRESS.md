# Compact agent session progress

Implemented 2026-10-07. Local Claude Code, Codex and registered MCP clients can
write concise work checkpoints and retrieve what a recorded session completed.
The journal contains **agent reports**, not owner beliefs or independently
verified facts. The historical miner uses source statements or optional installed
local AI; it adds no paid provider, download, cloud upload or background daemon.

## Agent contract

`record_progress` takes a project, conversation ID, unique checkpoint ID and a
current-state snapshot. The agent writes once the goal is clear, after meaningful
steps and before its final answer. Hooks, MCP instructions, the finish-check
reminder and the managed routing skill teach that cadence. Recording relies on
the agent following those instructions; there is no claim that every tool call
or interrupted step is captured.

| Field | Meaning | Bound |
|---|---|---|
| `task` | Current session goal | 240 characters |
| `status` | `working`, `blocked`, `complete`, `abandoned` | Enum |
| `done` | Concrete outcomes, with scope | Six facts, 180 characters each |
| `checks` | Check result and scope; an agent's claim | Six facts, 180 characters each |
| `pending` | Required unfinished work, failure or blocker | Six facts, 180 characters each |
| `artifacts` | Paths, commits, URLs or other corroborating references | Six facts, 180 characters each |
| `sensitivity` | `personal` or `sensitive` | Sensitive reports withheld |

The full checkpoint is bounded to 3000 characters. No hidden reasoning, transcript
copies, tool dumps or secrets. Plans belong in pending. Complete is rejected when
pending is nonempty; blocked requires a pending fact. Relevant completed work and
unresolved failures carry forward in each snapshot. Snapshots are concise enough
that retrieval never has to slice away a failure or verification qualifier.

Example payload (the calling tool identity is supplied by Varyz, not this JSON):

```json
{
  "project": "varyz",
  "conversation_id": "claude-session-id",
  "checkpoint_id": "seo-3",
  "task": "Prepare SEO and submit discovery endpoints",
  "status": "blocked",
  "done": ["Added sitemap, robots and canonical metadata locally"],
  "checks": ["Offline SEO checks passed; deployment not checked"],
  "pending": ["Google property verification requires owner sign-in"],
  "artifacts": ["site/sitemap.xml", "reports/SEO_2026-10-07.md"],
  "sensitivity": "personal"
}
```

`session_history(project="varyz", agent="claude_code", topic="SEO")`
returns the latest recorded matching session's **current** checkpoint. Agent is
a client name (`claude_code`, also `claude` or `claude-code`; `codex`, `openclaw`,
`hermes`) or an exact registered principal ID. Always filter when the question
names a client. Client names match live and mined reports across devices. Use
`origin="agent_report"` for live checkpoints or `origin="mined_report"` for
historical extraction; omit origin to search both. Topic is literal words ANDed against checkpoint
goals and facts. A topic can match an earlier checkpoint, but the answer is still
the current snapshot of that session. Original source dates order mined sessions;
live checkpoints use their server write time. Sequence breaks timestamp ties.
Undated sources sort last. Retrying an old write never makes it latest. A mined
snapshot never replaces a live snapshot of the same conversation.

For mined sessions, the final exchange may be a checklist rather than the original
task. A latest result includes `earlier_session_outcomes` when earlier visible
reports fit the same report's character budget. The context contains at most four
earlier exchanges (bounded by the requested limit), including for `limit=1`.
It carries earlier goals,
done/checks, source dates and citations, with extraction revisions deduplicated.
It excludes old pending items and sensitive evidence. These are earlier claims,
not the current state. Use paged history for a complete session summary; the
bounded context may omit outcomes.

For history, use a known conversation ID and `latest=false`; reports are newest
first, paged using `before_seq` from `next_before_seq`. Each has a server timestamp,
writer ID, sequence and `varyz-progress:<id>` citation. Retrieval obeys the caller's
item and character budgets. If a whole report does not fit, it is omitted and the
result says truncated, rather than stripping pending work. A withheld latest
snapshot never silently becomes an older falsely current snapshot.

## Storage and authority

`src/varyz/session_progress.py` owns strict pydantic validation, transactional
append, retries, search and erasure. Migration `0046_session_progress.sql` creates
the journal and FTS index in the existing local SQLite archive under `data/`.
Natural key: writer + project + conversation + checkpoint. A conflicting retry
is rejected. Projects are case-insensitive exact names, without general-scope or
version inference. Writer identity comes from the authenticated MCP credential;
unregistered and revoked clients cannot write or read these reports. The existing
owner-context grant is required. No new authority or credentials are minted.

Credential redaction runs before storage and indexing. Sensitive reports are
withheld on retrieval and cannot be discovered by searching their private words.
Reports never enter `entries`, preference learning, the graph or the personal
model. Cloud report sync reads only separately approved eligible reports. The journal does not reinterpret
withheld archive evidence or change the assistant-transcript ingestion setting.

## Owner controls

The live 2026-10-08 Cloud browser redesign has one project chooser. Its copy
explicitly includes concise work reports alongside eligible history and confirmed
preferences. Review uploads/removals and finish to approve existing and future
eligible context in those projects. General history still requires its own project
row; new projects are not enrolled automatically. Uncheck a project and finish to
revoke its sharing and erase Cloud copies; local records remain. Legacy CLI and
older helper category approvals remain supported.

`config.cloud-reports.json` beside the active config holds this separate approval
ledger and per-checkpoint receipts. Existing live/background sync sends bounded
report batches through the account's local Cloud credential. It rechecks source
approval and privacy each batch; when the newest state becomes private, it removes
older synced reports for that conversation instead of presenting them as current.
Historical mining still stays local until this additional report approval exists.
Disconnect disables report uploads, and account switching preserves the old ledger
as a backup without carrying its consent into the new account.
Optional `[session_reports] cloud_since = "2026-01-01"` in the active config bounds
backfill by original source date, not mining or upload date. Moving that bound
forward removes now-excluded Cloud copies at the next sync; it grants no approval.

Cloud stores encrypted reports and account-specific hashed lookup terms, separately
from raw history and confirmed preferences. The hosted plugin, context and archive
MCP endpoints expose read-only `session_history`. Use `project`, `agent`, `topic`
and `latest:true`; for a known session, set `conversation_id`, `latest:false`
and follow `next_cursor` using the same filters. Source dates and provenance stay
attached. Returned reports are agent claims, not verified outcomes or owner rules.
Cloud cannot mine the computer or record new local checkpoints through this tool.
Plugin access requires the same Cloud account and its existing authorized grant.
The computer can be off after sync; fresh work waits for the next approved sync.

```powershell
varyz sessions latest varyz --topic SEO --agent claude_code
varyz sessions latest varyz --topic SEO --agent claude_code --origin agent_report
varyz sessions history varyz <conversation-id>
varyz sessions mine varyz
varyz sessions mine varyz --apply --adapter claude_code --limit 25
varyz sessions mine varyz --apply --conversation-id <source-session-id> --method local
varyz sessions doctor
varyz sessions pause
varyz sessions resume
varyz sessions forget varyz <conversation-id>
```

All commands accept `--config`. Pause prevents new MCP writes and mining while existing
reports remain readable. Forget explicitly erases the session's checkpoints and
search text, and prevents a future backfill from recreating the erased session.
Normal writes are append-only. Doctor checks SQLite and FTS integrity.

## Historical miner

`mine_sessions` and `varyz sessions mine` default to preview. `apply: true` or
`--apply` stores compact reports in the same journal. The default run records at
most 25 new reports; repeat limited runs to continue. The owner CLI can request
up to 1000; MCP up to 100. A process lock prevents concurrent miners. Recording
pause and the release-update pause also stop mining, including before writes
after inference. No unattended scheduler is enabled by this feature.

Only approved `ai_conversations` paths, adapters and date limits are read. The
assistant ingestion setting stays unchanged. Existing parsers exclude injected
context, machine-created sessions, subagents and tool traffic; mining also drops
Codex analysis channels. Paths escaping selected roots through symlinks are
excluded. Files above 256 MB and lines above 2 MB are counted and skipped.
Malformed lines are counted; affected files receive no successful scan receipt.

Each prompt's last assistant response produces one report. Extractive mining
keeps short whole statements, categorises reported outcomes/checks/pending work,
and omits statements that do not fit. `--method local` asks installed local AI to
select among those exact statements. It cannot rewrite or invent a fact. Only
loopback inference is allowed; model unavailability is explicit, with extractive
mining still available. No OAuth credentials or paid/cloud providers are used.

Reports expose source agent, original timestamp, source turn references, line
numbers for each fact, omission counts and partial-coverage qualifiers. They use
`history:<adapter>` attribution and `provenance: mined_report`. Source-sensitive
and sticky archive-sensitive exchanges remain withheld. Revoking source approval,
changing selected sources or changing privacy rules withholds previous mined
reports until eligible sources are mined under the new policy. Privacy remains
sticky for existing sensitive reports. No report changes entries or owner rules.
MCP reloads current configuration on each journal request and checks source and
privacy changes between files and before/after inference. A changed permission
stops the batch, suppresses previews and marks `permission_changed: true`.

The miner conservatively leaves overall completion unknown: `status: working`
and a mandatory pending qualifier. An assistant's reported check or deployment
is still an unverified claim. A user-only prompt never establishes completed work.
Reports describe each prompt's last response, not every step or the whole session.
Use paged history to inspect earlier exchanges. The English keyword extractor
may miss noun-only or non-English outcomes; omitted long prose is not summarised.

File content-hash receipts avoid unchanged parsing and inference. Immutable source-content and
policy keys resume partially recorded files without repeating local inference.
Growing files are rescanned; a file changed during scanning receives no receipt.
Successfully stored reports survive interruption. Source deletion does not erase
historical reports automatically; use `sessions forget` for explicit erasure.

## Limits

Backfill is owner/agent invoked; there is no automatic scheduler. No hosted or phone journal tool yet. No
independent verification of agent assertions or externally confirmed deployment.
An empty answer means no matching recorded report, not that no work happened.
Already-running MCP connections must reload/reconnect to discover the new tools.

Validation: deterministic offline tests in `tests/test_session_progress.py`
exercise chronology, retry conflict, current-state retrieval, isolation, secret
redaction, sensitivity, paging, erasure and MCP authentication. Broader checks and
their outcomes are recorded in `reports/SESSION_PROGRESS_2026-10-07.md`.


## Cloud writes and pickup (2026-10-16)

The hosted workflow exposes `record_progress` with the same six-fact bounds and
3000-character checkpoint limit as local recording. A deterministic checkpoint
identity makes identical retries safe; changing a previously written checkpoint
fails. The Cloud account and project approval are authenticated; the optional
`agent` field labels the reported client, not independently verified authorship.
Sensitive checkpoints stay local. Hosted writes require an existing report grant,
respect archive/project pauses and revocations, and use encrypted report storage.
They do not enter preference knowledge.

`start_task.open_threads` shows up to three unfinished threads, within 4000
characters, without requiring a resume keyword. Threads whose words match the task
come first (`match: "topic"`); the most recent unfinished ones fill the rest
(`match: "recent"`), so a vague "continue" still finds work. The asking session's
own conversation is left out. Matching uses whole current
session snapshots from at most 200 recent sessions; a completed pickup closes its
root thread. Search remains bounded: absence is not proof that no unfinished work
exists. `session_history` provides the full checkpoint timeline for a known chat.

Before pickup, call `claim_work(project, conversation_id, thread_id)`. Its 15-minute
lease is renewed by subsequent pickup checkpoints. A competing conversation gets
a conflict; expired leases can be claimed, and `action="release"` releases one.
Write the supplied `thread_id` into every pickup checkpoint. Marking that linked
thread complete or abandoned releases the lease. Claims are advisory coordination
for cooperating agents, not authorization to edit code or a replacement for Git
conflict handling. Local registered agents use the same Cloud lease through
`cloud/report_bridge.py`; pickup writes must reach Cloud before the local copy.

Existing `LiveCloudSync` and background sync automatically upload future eligible
checkpoints after report-project selection. They recheck project revocation and
privacy each batch. No account login, payment, transcript upload or new project
approval is inferred from enabling this feature. The 2026-10-16 owner instruction
authorized report upload for existing selected project scopes; future unselected
projects still require approval.


## Automatic hand-offs (2026-10-22)

The owner should never have to brief an agent on earlier work, so neither half
depends on the agent remembering to journal.

**Capture.** After every turn the stop hook starts `python -m varyz.handoff capture`
in a hidden background process, scoped to this chat's own transcript. It mines the
turns the agent did not journal (a turn with a live checkpoint at or after it is
skipped), stores them as mined reports and, when the project is approved for Cloud
reports, sends them up at once through the usual `ReportSync`. It needs approved
chats and no pause; it never blocks the turn, and a busy lock defers to the next one.

**Current state.** For each conversation the newest report wins, whatever its
origin; a live checkpoint wins a tie. So a turn captured after the agent's last
checkpoint becomes the thread's current state, and an older backfill can never
replace live work. Locally (`history`) and in Cloud (`_states`) the rule is the same.
A mined report counts as unfinished only when it lists real open items beyond the
standard "completion unknown" qualifier (`left_open`), so ordinary chat turns do not
show up as open work.

**Pickup.** The first briefing of every session, including a vague first prompt,
carries a "Where you left off" section: up to three unfinished threads with what was
done, what is open, checks, artifacts and ids. `handoff.open_work` takes Cloud's
threads first (every computer's work), drops any this computer has moved on since,
then fills in from the local journal; with no Cloud, no approval or no network it is
local only. Cloud can take seconds to answer `open` (12 s before the 2026-10-22
deploy, 1 to 3 s after), so the briefing never waits for it: it reads a local copy of Cloud's last answer
(`data/runtime/handoff/<project hash>.json`, ignored after three days) in well
under a second, and a hidden `python -m varyz.handoff refresh` renews the copy when
it is over two minutes old. Session start and every capture that sends reports also
renew it. The section is shown once per session. Agents can ask again any time with the read-only MCP tool
`catch_up(project, task?)`; `start_task.open_threads` uses the same source.

The recency fallback and newest-wins rule went live in Cloud on 2026-10-22: a
vague "continue" now returns the three most recent open threads (`match: "recent"`).
Tests: `tests/test_handoff.py`, `tests/test_cloud_progress.py`.

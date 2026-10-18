# Rule-system shadow experiment

> Design 2026-10-04. Research: `reports/RULES_SYSTEM_REVIEW_2026-10-04.md`.
> **Current Varyz remains the authority delivered to agents.** Shadow is an
> experiment, with no activation command for making B govern agents.

The owner requested implementation of A and/or B and collection in shadow mode.
The implementation combines A's explicit state/lineage projection with B's local
task compiler. It preserves candidates, trials, confirmed records, visions,
workflows and immutable history. Synced placement remains independent: the shadow
projection proposes eligibility but performs no export, import or upload.

## Operation

`varyz rules shadow probe --dry-run` compiles from the active archive without
enabling collection or writing a journal. `control enable` opts that archive in;
`control pause`, `control resume` and `control disable` take effect on each new
observation, including in running processes. Disabling retains history.

`status` reports counts, phase counts, latest capture time, bounds and review
outcomes. `doctor` checks SQLite integrity and reports reached limits and the last
error's type/time. `comparisons` returns IDs and metrics, not personal wording.
`probe --no-dry-run` exercises storage; probe observations are excluded from the
prospective count. No scheduled job is needed: observations come from existing
hook, registered MCP and registered resolution-daemon requests. After installing
new code, persistent MCP servers need a client restart; short-lived hooks load it
on their next invocation. Existing daemon code-watch restart is retained.

Collection defaults to at most 500 comparisons per UTC day, 10,000 source rows per
record table, 50,000 registry events and 64 MiB database/journal budget. SQLite's
page cap reserves half for a transient rollback journal. Registry-event and byte
caps persist across days; there is no automatic deletion of experiment history.
Change bounds through `[rule_shadow]` in config, then restart persistent processes
to reload those settings. Repeated identical requests with the same session,
phase, origin, policy, source and UTC day are idempotent; changed packets or source
states generate new observations. Distinct hook and MCP observations are retained
as different surfaces, not counted as independent task families in evaluation.

Failures never change the current packet or answer. A sidecar lock waits at most
50 ms; read/compile work still adds latency. There is no claim of zero overhead.
Denied, withheld or unregistered MCP/answer requests do not read the shadow
projection. Hook collection obeys the existing briefing/done-gate switches.
Pausing shadow collection does not pause Varyz itself.

## A projection

Each typed record carries a stable root identity, physical row identity, content
revision hash, record type, lifecycle, authority evidence, scope/domain, source and
proposed sync eligibility. Superseded and retracted states remain auditable. A
legacy confirmed row has authority `recorded`; this does not manufacture proof of
a fresh owner confirmation. A trial has authority `provisional`. A descendant
auto-promoted from briefing exposures remains provisional in shadow A, while an
explicitly kept trial is active. Exposure cannot change shadow authority.

This is a read projection of the existing archive and an append-only observation
journal. It is not a new distributed event protocol, a migration of live records
or a replacement for the source archive. Changes occurring while collection is
paused can only be reconstructed from the source ledger on the next observation.

## B task compiler

The proposal applies global rules and the working project's rules. Mentioning
another project in a task does not change the working-project scope. Global alias
`all_projects` is recognised in the projection. Applicable restrictions detected
from explicit prohibitive wording, domain checklist items and project visions are
retained before the ordinary rule quota. Critical constraints can exceed that quota.
Relevant decisions and workflows are bounded. Sensitive provisional rules and
potential permission decisions are quarantined for review in the proposal. A
narrow single-clause primary restriction such as "never upload without asking"
can remain a provisional trial; mixed positive/negative clauses are held for review.

Topic/value/strength annotations are optional `provenance.shadow_policy` metadata:
`{"topic":"accent_colour","value":"blue","strength":"default"}`. No annotations
are written to live records by this experiment. Only well-formed annotations can
drive conflict groups and local-over-global default overrides; hard constraints
are never overridden this way. Conflicting tagged rules are surfaced together as
unresolved IDs. Unknown metadata is reported under `unannotated`, not silently
claimed correct. The first version uses conservative lexical restriction and
sensitivity detection: it can over-pin harmless wording, quarantine legitimate
preferences and miss implied permissions/conflicts. Owner review is necessary.

The answer control covers exact recorded preference questions only, with strict
working-project scope and ambiguity abstention. Permission/blocking requests
(`owner_answers_only`) always propose fresh owner review. It does not run retrieval,
paraphrase verifiers, model tiers or new escalations. An `outside_coverage` proposal
must not be scored as an incorrect full-system answer. There is no proof here that
the current system's semantic answer should be replaced by this narrower control.

## Journal and privacy

State lives under the archive's `data/rules/shadow`: ENABLED/PAUSED switches, a
random 32-byte hash key, `shadow.sqlite3`, and optional last-error metadata.
Registry events, comparisons and human reviews have append-only SQLite triggers.
Registry metadata and the paired comparison are committed atomically. The source
archive receives no shadow writes or schema migrations.

The journal stores IDs, scopes, states, source labels, hashes and metrics. It never
copies prompts, question/answer text, rule statements, rationale, provenance quotes
or exception messages. Task/session and short-answer hashes are keyed with the
per-archive key. Source-row hashes identify immutable revisions. The journal is
local and private but still reveals record IDs, project scopes and activity times;
it is not an anonymised public dataset. Existing cloud selection and sync do not
include it. Reviewing wording requires following IDs into the owner archive.

## Evaluation gate fixed before prospective claims

Primary metric: paired owner/reviewer judgment of task relevance, omitted binding
constraints, trial clarity, conflicts and inappropriate permission reuse. Secondary
diagnostics: selection differences, characters and observer latency. Serialized
baseline packet length and proposal rule-text length measure different envelopes;
never report their ratio as a like-for-like token saving or an accuracy gain.

Exclude probes and implementation tasks. Group repeated requests by task/session
hash and related task family; do not treat each hook/MCP/finish observation as an
independent sample. Freeze the policy-source hash for evaluation. Reserve at least
30 fresh task families for an untouched held-out review and a subsequent 30-family
fresh replication window. Development feedback may change the policy only before
those windows, or starts a new preregistered run. No tuning on held-out labels.

Append manual judgments using `review <seq> <outcome> <window>`, where outcome is
`better`, `worse`, `equivalent` or `unclear`, and window is `development`, `held_out`
or `replication`. Corrected judgments append another review; summaries count the
latest judgment per comparison. Labels are not invented automatically.

Report task-family N, coverage, tied/unclear cases, failure categories, policy/data
hashes and paired statistics. Compare with the current system and a 50% random
forced-choice floor only on binary non-tied judgments; report ties/coverage
separately. Replication must show no regression in binding constraints or permission
handling. Neither shorter packets nor the earlier synthetic prototype wins establish
owner compliance. No live migration or automatic B activation follows a numerical
threshold; that is a separate reviewed change.

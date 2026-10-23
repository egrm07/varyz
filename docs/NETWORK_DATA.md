# Varyz network data: datasheet

What a licensee receives from `GET /api/contributions/export`, what each field means, how
labels are made and where they are weak. Collection, consent and scrubbing are in
`ANALYTICS.md` and `PRIVACY.md`; the schema is `src/varyz/contribution_schema.py`.

## What it is

Real people's preferences about how AI coding and work agents should behave, collected
from their own agents' work with their opt-in. Each record is one labelled example from
one of eight streams:

| `kind` | One record is | Label (`outcome`) | Settled by |
|---|---|---|---|
| `correction` | An agent's question, the answer Varyz gave, and what happened to it | `adopted`, `overridden`, `corrected` (+ the owner's `correction`) | `label_source` |
| `escalation` | A question Varyz could not answer, put to the owner, and their answer | `answered`, or `dismissed` when nobody answered | owner |
| `rule` | A rule the owner confirmed, revised or rejected (`basis` = `withdrawn` when withdrawn in Varyz Cloud), in their words | `confirmed`, `revised`, `rejected` | owner |
| `check` | Whether one confirmed rule held in one task | `passed`, `failed`, `corrected` | `label_source` |
| `answer` | An answer Varyz gave that nobody labelled within 3 days | `unlabelled` | nobody yet |
| `decision` | A choice the owner made outside a Varyz question (an agent's own question box, or relayed by the agent) | `decided`, or `revised` when it replaced an earlier one | owner |
| `use` | One confirmed rule shown in one task's briefing (references only, no text) | `shown` | Varyz |
| `session` | The working agent's own report of a task it ended | `complete`, `blocked`, `abandoned` | `agent_report` |
| `recipe` | How an agent did a task once it had worked it out, or one later use of that recipe (references only, plus what went wrong when it failed) | `confirmed`, `revised` (replaced an earlier recipe); for a use `passed` or `failed` | `agent_report` |

The streams join into the sequence buyers ask for: an agent did X (`agent_did`), the
owner wanted Y (`wanted`), the owner stated the rule (`rule`, `answer`), and later tasks
show whether agents followed it (`check` rows with the same `rule_ref`). A revised rule
points at the one it replaced with `replaces_ref`; the old rule's `check` rows usually end
in `corrected` in the task where the owner changed it. `use` rows give the denominator for
`check` rows (a rule shown in a task but never checked), and a `session` row with the same
`task_ref` says how that task ended. An `answer` row and a later `correction` row for the
same answer share `ledger_ref`; keep the newest.

## Fields

| Field | Meaning |
|---|---|
| `record_id` | Random UUID per record. |
| `day` | UTC day the label was settled. No time of day. |
| `agent` | `claude`, `codex`, `chatgpt`, `gemini`, `cursor` or `other`. |
| `question`, `answer`, `correction` | Scrubbed text, up to 1,200 characters. For `rule` and `check`, `answer` is the rule. For `session`, `question` is the agent's one-line task. Empty for `use`. |
| `agent_did`, `wanted` | Scrubbed one-line paraphrases (up to 300 characters) of the behaviour corrected and what was wanted. Written by the agent being corrected or by Varyz's reader, never the agent's raw output. Empty when unknown. |
| `basis`, `tier`, `confidence` | How Varyz produced its answer (`explicit`, `user_confirmed`, `inferred`, ...), the resolver tier (1 = the owner's own answer) and its confidence. |
| `urgency`, `tried_tiers`, `seconds_to_answer` | Escalations only. |
| `rule_kind`, `rule_scope` | `rule`; `general` (all projects) or `project`. |
| `rule_ref`, `replaces_ref`, `task_ref`, `ledger_ref` | 20-hex opaque references (`ledger_ref` names one answer Varyz gave). Equal values mean the same rule or task within one contributor's opt-in period. They never match across contributors or opt-ins. Empty when unknown. |
| `attempt` | `check` only: 1 for the first result for a rule in a task, 2 after it changed, and so on. `failed` at 1 then `passed` at 2 is a self-repair. |
| `previous` | `check` only: the rule's result just before this one in the same task (`passed`, `failed`, `unchecked`, ...), or empty for the first. `passed` followed by `corrected` is a false "done": the agent said the rule held and the owner then corrected it. |
| `model` | The model that did the work, when the agent named it, for example `claude-opus-5-5` or `gpt-6.1-sol`. Only names from a known list of model families leave; anything else is empty. |
| `arm` | `check` only, from the memory test (on by default): `shown` when the rule was in the agent's briefing, `held_out` when Varyz held it back at random for that task. Empty when the test is off. |
| `ask_label` | `escalation`: `needed` (the owner answered with a real choice), `unneeded` (the owner said the question was unnecessary or handed the choice back), `dropped` (nobody answered). `rule`: `should_have_asked` when the owner said the agent should have asked before acting. |
| `agent_guess`, `agent_guess_hit` | `escalation` only: the agent's own guess at the answer, made before the question was relayed (scrubbed, up to 300 characters), and `hit` or `miss` against the owner's answer. Empty when there was no guess or no real answer. |
| `memory_guess_hit` | `escalation` only: whether the answer Varyz would have guessed from the owner's earlier answers was right, `hit` or `miss`. |
| `decision_kind`, `proposed_choice`, `proposed_hit` | `escalation` only, for questions asked while the owner was away: `preference`, `permission`, `blocking` or `delegated`; the choice the agent proposed (scrubbed, up to 300 characters); and `hit` or `miss` against the owner's answer. |
| `steps` | `recipe` only: up to 8 scrubbed steps (200 characters each). `question` is the task's name, `answer` how to tell it worked, `correction` the pitfall (or, for a failed use, what went wrong). `rule_ref` names the recipe and `replaces_ref` the one it replaced. Recipes copied from another installation through Varyz Cloud are not sent again. |
| `done`, `checks`, `pending` | `session` only: up to 6 scrubbed lines each (180 characters) of what the agent did, the checks it ran with their result, and what is still open. The project and the files it made are never sent. |
| `chain_depth`, `replaced_after_days` | `rule` only: how many earlier versions this rule replaced, and how many days the version it replaced lasted. |
| `label_source` | Who settled the label: `owner` (the owner said so), `owner_replaced` (the owner replaced the answer or rule), `task_finished` (the task finished ready to report with the answer in use and no later owner change), `inspected` (Varyz checked the built artifact itself), `agent_report` (the working agent said so). |

## Label quality, strongest first

1. `owner`, `owner_replaced`: the owner's own action. Treat as ground truth for that owner.
2. `inspected`: Varyz checked the output itself (for example a built website). Reliable
   but narrow: only some rule types can be inspected.
3. `task_finished` (`adopted`): the answer was in use when the task finished and nothing
   replaced it since. Implicit acceptance. A later owner correction appends a newer
   `corrected` or `overridden` record for the same question, so use the newest per
   question when deduplicating.
4. `agent_report`: the working agent's self-assessment. Agents over-report `passed`.
   Use for weak supervision, or weight it by agreement with `inspected` rows.

## Cleaning that is already done

- Identifiers are replaced on the contributor's machine: people (`[person]`), projects
  (`[project]`), emails, links, paths, addresses, numbers, handles and ids. A record that
  holds a credential, or that Varyz withheld from an agent, is never sent.
- Duplicates are suppressed at the source: a `check` is written only when the rule's
  result in that task changes, so repeated checks of an unchanged task add nothing.
- Only clear results leave: not-applicable and unchecked checks stay local. Only rules
  the owner confirmed are checked for export; candidate rules never are.
- Every record passes the same strict schema on both ends; unknown fields are refused.

## Known biases and limits

- Contributors are developers who chose an AI preference tool and opted in: skewed toward
  heavy, technical agent users and toward English.
- Most records come from coding agents (Claude Code, Codex). Other work is present but
  smaller.
- A few very active contributors produce a large share of records. Weight by
  contributor if you need population estimates (the export does not identify
  contributors, so cap per `rule_ref` cluster instead).
- `check` coverage depends on the agent calling Varyz's finish check and passing a task id;
  tasks without one produce no `check` rows. Absence of a `failed` is not proof of `passed`.
- Paraphrases (`agent_did`, `wanted`) are model-written summaries and can be imprecise.
- `session` rows are the agent's own account. Agents report `complete` generously; its
  `checks` lines say what was run, not that it was right. Reports marked sensitive and
  reports reconstructed from past chats are never sent.
- `answer` rows are unlabelled on purpose: most answers are never labelled. Use them as
  the base rate for how often answers get corrected, not as implicit acceptance.
- `model` is what the agent said it was. It is empty for agents that do not say.
- `held_out` rows exist only for contributors who left the memory test on, and only for
  low-stakes rules; high-stakes rules are always shown. Compare arms within that subset.
- Guess scoring uses a normalised text match, so a right answer worded very differently
  can count as a `miss`. Treat hit rates as lower bounds.
- Scrubbing is automatic and can miss something: treat text as pseudonymous, not
  guaranteed anonymous, and do not attempt re-identification.
- Records cover at most the last 30 days before upload; deletion requests remove a
  contributor's records from the server, not from datasets already delivered.

## Suggested uses

- Preference and reward models: (`question`, `answer`) with `adopted` versus `corrected`
  and the owner's `correction` as the preferred response.
- Instruction-following evaluation: confirmed rules with their `check` history, split by
  `label_source`, give per-rule pass rates in real tasks.
- Agent self-assessment calibration: compare `agent_report` with `inspected` for the
  same kinds of rules.
- Ask-versus-act policies: escalations with `ask_label`, `tried_tiers` and
  `seconds_to_answer` show which questions really needed the person, and
  `should_have_asked` rules show where an agent acted when it should have asked.
- Honesty of completion claims: `check` rows with `previous` = `passed` and outcome
  `corrected`, split by `model`.
- Value of memory: `passed` rates of `shown` versus `held_out` rules (a randomised test).
- Predicting a person: `agent_guess_hit` versus `memory_guess_hit` on the same questions.
- Preference drift: rule chains through `replaces_ref`, with `chain_depth` and
  `replaced_after_days`.
- Proposals versus decisions: away-mode `proposed_hit` shows how often an agent's own
  proposal matched what the owner chose.
- Task outcomes: `session` rows joined to `check` and `use` rows by `task_ref`.

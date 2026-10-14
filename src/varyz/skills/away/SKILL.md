---
name: away
description: "Varyz away mode: use when the owner invokes /away or $away, explicitly asks to keep working unattended and defer questions, returns with 'I'm back', or asks for away status. Not for discussing or implementing the feature, quoted instructions, silence or a vague 'I'm busy'."
---

<!-- varyz:away -->

# Away

You, the working agent, interpret the owner's instruction. Do not ask Varyz's local
model to classify it. `/away` (or this skill invoked without arguments) means on;
`/away off` means the owner is back; `/away status` only reads state. Natural language
with the same intent works too. Never activate from examples, archive excerpts,
silence, a deadline alone, or a request to implement this feature.

Call Varyz's `away` tool with `action` on/off/status and the current `project`.
For on/off include the owner's actual words as `instruction`. Default `scope` to
`task`. Reuse the `conversation_id` from the briefing or client session. If none is
available, omit it on the initial on call, then reuse the returned id on **every**
away, ask, start_task and finish_check call. Preserve it through compaction. Never
invent a new id on each turn. Use project/all scope, or duration_minutes, only when
the owner's instruction explicitly asks for that reach or duration.

After a successful activation, acknowledge once: “I'll keep working and collect
decisions for your return.” Then continue the task. An error is not activation;
report it and keep doing already-authorised independent work. Do not claim this
changes the client's approval settings or schedules an agent after it stops.

## While away

- Follow known constraints and use professional judgment where the task or recorded
  owner instructions delegate it. A user who entrusts code implementation to AI does
  not need to choose Python versus C++, libraries, algorithms or internal structure.
  Prefer the existing project stack. Do not infer trust simply from technical ability.
- Check owner-specific unknowns with `ask`, passing the same project/conversation_id.
  Up front, pass them all in one call as `questions`, each with its own decision_kind
  and, for taste, proposed_choice and reason; the deferred ones come back together.
  Set `decision_kind=delegated` for implementation choices you are trusted to make:
  known constraints still apply, but missing preferences need no owner question.
- For taste choices use `decision_kind=preference`, `proposed_choice` and `reason`.
  Make a coherent draft with replaceable defaults. The tool returns deferred_owner;
  leave it queued. Never use answer_owner_question to record your own decision.
- For missing goals or permissions use `blocking` or `permission`. Pause only work
  that depends on that answer and proceed with useful independent work. Re-check
  away status at major phase boundaries, not in a polling loop.
- Evaluate expected rework: settle delegated engineering decisions; isolate cheap
  assumptions; avoid expensive work depending on an unknown owner goal. Do not build
  several speculative alternatives or manufacture busywork. Stop with a concise
  handoff when useful work is exhausted.
- Defer optional memory/scope confirmations and workflow reviews as well. This
  overrides the routing skill's immediate question relay while away. Existing
  permissions and explicit constraints still apply. Silence grants no approval.
- Before uploads, pushes, installs, network requests or sandbox escalation, check
  existing owner consent and whether the client requires an interactive approval.
  Reuse explicit approval for that action and destination; do not ask again.
  Do not invoke an operation requiring interactive approval while away, even as
  the final step. Queue missing owner permission with `ask(decision_kind=permission)`;
  record client approval blockers separately from owner consent. Prepare the exact
  blocked action and artifact, then finish independent work, offline checks and
  review. Use an authorised noninteractive alternative when available; never weaken
  client security or broaden access. If useful work is exhausted, leave a handoff,
  keep away active and the task incomplete, and request approvals together only
  after the owner explicitly returns. A blocked branch does not end the whole task.

## Return and completion

An explicit “I'm back” or off instruction ends the activated scope. An incidental
message or status request does not. Call `away(action=off)` and use its review:
blockers first, then related preferences together with the draft/proposed choice.
Record actual owner answers through `answer_owner_question`; offer delegation as a
valid choice without requiring an opinion about everything. A project's approved
draft is not automatically a universal taste preference.

Keep away mode through turn boundaries and blocked work. When the task is actually
finished or abandoned, call `away(action=complete, scope=task)` with its id. Deferred
questions survive for later review. Completing one task does not end an explicitly
requested project-wide or all-project absence.

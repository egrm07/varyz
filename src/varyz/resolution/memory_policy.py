"""Shared agent guidance: a correction is not automatically a standing rule."""

REALTIME_CORRECTION_POLICY = (
    "On each owner correction, apply it to the current work immediately. When the "
    "owner explicitly states a reusable preference (for example, 'don't use . in "
    "titles'), call remember_owner_rule now, before continuing work; do not ask "
    "whether to make it a rule or wait for the background reader. Use scope='all' "
    "for general writing, design or working preferences unless the owner limits "
    "them to this project or situation; preserve every stated condition. A one-off "
    "edit such as 'remove the period from this title' stays in this task. Never "
    "save inferred preferences, quoted examples or agent defaults as owner rules. "
    "Use replaces for an existing rule the owner corrected, retaining its scope "
    "unless they changed it. Verify the write succeeded; an outage is not a saved rule."
)

# Said with every later prompt of a briefed session: the agent already has the
# whole policy from session start, so one line keeps it in mind without repeating it.
REALTIME_CORRECTION_REMINDER = (
    "If this message corrects your work, apply it now; if it states a reusable "
    "preference, save it now with remember_owner_rule (replaces for a corrected rule) "
    "and do not ask whether to."
)

MEMORY_POLICY = REALTIME_CORRECTION_POLICY + " " + (
    "Before offering to remember a correction, the working agent must judge whether "
    "the owner's words clearly express a reusable rule for future work. "
    "Keep one-off task choices and ordinary fixes in the current task context; "
    "do not offer memory or turn them into standing rules. For example, 'General "
    "first, then groups with the most preferences' is a task layout choice, not a "
    "reusable rule. Project scope alone does not make a choice reusable. "
    "If future applicability is unclear, skip the memory offer rather than asking "
    "another question. Do not invent a broader preference from a specific edit. "
    "Ask about scope only when a reusable instruction has consequential ambiguity "
    "that the owner's words and context cannot settle. Honor explicit "
    "requests to remember a rule. When the owner corrects an existing rule that "
    "caused the mistake, update that rule at once without another memory offer: "
    "call remember_owner_rule with replaces set to its id and the correction in "
    "their words, and say in one line which rule you updated."
)

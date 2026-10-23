"""Fit owner context as whole records; reserve the current continuation first."""

from __future__ import annotations

import json
from typing import Any

from varyz.resolution.knowledge import _terms

FIELDS = (
    "continuation",
    "away_mode",
    "writing_rules",
    "message_rules",
    "how_owner_works",
    "project_vision",
    "project_rules",
    "definition_of_done",
    "owner_workflows",
    "task_recipe",
    "project_decisions",
    "general_preferences",
    "pending_owner_questions",
    "already_decided",
)


def disclosed_size(result: dict[str, Any]) -> int:
    return sum(len(json.dumps(result.get(key), ensure_ascii=False)) for key in FIELDS)


def fit(result: dict[str, Any], max_chars: int, task: str = "") -> int:
    """Keep complete rules/decisions and expose omissions, never weaken a grant.

    Previously a full project decision backlog displaced the continuation and
    could exceed the declared owner-context budget by itself. Continuation is
    first because it is the concrete state requested by a resume task.
    """
    originals = {key: result.get(key) for key in FIELDS}
    for key in FIELDS:
        value = originals[key]
        result[key] = (
            {}
            if isinstance(value, dict) and key == "definition_of_done"
            else ([] if isinstance(value, list) else None)
        )
    omitted: dict[str, int] = {}
    # Select whole relevant records, with standing constraints ahead of trials.
    # Ranking changes disclosure order only; it never changes a rule's authority.
    state = (originals.get("continuation") or {}).get("state", {})
    context = task + " " + str(state.get("task", ""))
    terms = _terms(context)
    queues = {}
    for key in FIELDS:
        value = originals[key]
        if isinstance(value, list):
            queues[key] = sorted(value, key=lambda record: (
                not bool(record.get("trial", False)) if isinstance(record, dict) else True,
                len(terms & _terms(json.dumps(record, ensure_ascii=False))),
            ), reverse=True)
        elif key == "definition_of_done" and isinstance(value, dict):
            queues[key] = [
                (domain, record) for domain, records in value.items() for record in records
            ]
    # Reserve one complete record from each collection before filling the rest.
    # A large project-rule backlog cannot monopolize all remaining owner context.
    for key in FIELDS:
        value = originals[key]
        if key not in queues:
            result[key] = value
            if disclosed_size(result) > max_chars:
                result[key] = None
                if value is not None:
                    omitted[key] = 1
    while any(queues.values()):
        for key, queue in queues.items():
            if not queue:
                continue
            record = queue.pop(0)
            domain = None
            if key == "definition_of_done":
                domain, record = record
                target = result[key].setdefault(domain, [])
            else:
                target = result[key]
            target.append(record)
            if disclosed_size(result) > max_chars:
                target.pop()
                omitted[key] = omitted.get(key, 0) + 1
                if domain is not None and not target:
                    result[key].pop(domain)
    if omitted:
        result["context_omitted"] = omitted
        result["context_followup"] = (
            "Context was bounded by the existing grant. Omitted rules still apply; "
            "retrieve relevant rules with advise and the full checklist with finish_check. "
            "Use session_history for omitted continuation details. Do not assume unknown "
            "decisions or treat omissions as permission."
        )
    return disclosed_size(result)

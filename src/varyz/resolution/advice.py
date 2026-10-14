"""Advice for a coding agent: how the owner would have it built.

The agent brings a task or a plan; varyz answers with the owner's confirmed rules
and project vision that bear on it, and the owner's recorded decisions that share
its subject. It does not rank plans, propose steps or say whether a plan is good —
the agent does that, now knowing the owner's constraints.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from varyz.resolution.domains import detect_domains
from varyz.resolution.knowledge import SQLiteKnowledgeStore, _terms, scope_applies
from varyz.resolution.memory_policy import MEMORY_POLICY
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.workflows import owner_kinds

MAX_DECISIONS = 5
# A recorded decision is shown when it shares at least this many salient terms
# with the plan; one shared word ("agent", "project") is coincidence.
DECISION_MIN_OVERLAP = 2

HOW_TO_USE = (
    "These are the owner's standing rules for how work is done, and their vision "
    "for the project. Treat each as a constraint on your plan, not a suggestion: "
    "where your plan conflicts with one, change the plan, or ask the user before "
    "departing from it and say why. They do not plan for you, and a subject with no "
    "rule here has no recorded owner preference - use resolve for a specific open "
    "choice rather than guessing. " + MEMORY_POLICY
)

NOTHING_RECORDED = (
    "The owner has no confirmed rule or decision that bears on this. That is an "
    "answer: use professional judgment for delegated implementation; ask only for "
    "consequential owner preferences, necessary owner-specific facts or missing permissions."
)


def default_project(cwd: Path | None = None) -> str:
    """The working directory's name: agents are launched in the project they work on."""
    return (cwd or Path.cwd()).name


def advise(
    conn: sqlite3.Connection,
    plan: str,
    *,
    project: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    plan = " ".join(plan.split())
    if not plan:
        raise ValueError("plan is required: the task or plan you are about to implement")
    principles = SQLitePrincipleStore(conn)
    domains = detect_domains(plan, extra=owner_kinds(conn))
    chosen, withheld = principles.applicable(
        plan, project=project, limit=limit, domains=domains
    )
    vision = [
        {**item.principle.advice(), "matched_terms": list(item.matched_terms)}
        for item in chosen if item.principle.kind == "vision"
    ]
    rules = [
        {**item.principle.advice(), "matched_terms": list(item.matched_terms)}
        for item in chosen if item.principle.kind == "rule"
    ]
    decisions = _related_decisions(conn, plan, project)
    pending = len(principles.current("candidate"))
    payload: dict[str, Any] = {
        "plan": plan,
        "project": project,
        "project_vision": vision,
        "owner_rules": rules,
        "more_rules_in_scope": withheld,
        "owner_decisions": decisions,
        "known": bool(vision or rules or decisions),
        "how_to_use": HOW_TO_USE if (vision or rules or decisions) else NOTHING_RECORDED,
    }
    if pending:
        payload["pending_rule_suggestions"] = pending
        payload["pending_hint"] = (
            f"{pending} rule(s) mined from the owner's own words await review. They are "
            "not advice until confirmed; the owner reviews them with `varyz rules review`."
        )
    return payload


def _related_decisions(
    conn: sqlite3.Connection, plan: str, project: str | None = None
) -> list[dict[str, Any]]:
    plan_terms = _terms(plan)
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in SQLiteKnowledgeStore(conn).current():
        # Another project's one-off choice is not this project's decision.
        if not scope_applies(entry.scope, project, plan):
            continue
        shared = plan_terms & _terms(entry.question + " " + entry.answer)
        if len(shared) < DECISION_MIN_OVERLAP:
            continue
        scored.append((
            len(shared),
            {
                "id": entry.id,
                "question": entry.question,
                "answer": entry.answer,
                "basis": entry.basis.value,
                "stated_at": entry.stated_at.isoformat()[:10],
                "scope": "all projects" if entry.general else entry.scope,
                "matched_terms": sorted(shared),
            },
        ))
    scored.sort(key=lambda item: -item[0])
    return [item for _, item in scored[:MAX_DECISIONS]]


__all__ = ["HOW_TO_USE", "NOTHING_RECORDED", "advise", "default_project"]

"""The rules part of a Cloud briefing, narrowed the way the local briefing is.

Cloud used to hand an agent every confirmed rule it held, whole records and all:
for one owner that was 175 rules and 62,000 characters before the task began.
Now the standing rules are ranked for the task and shown up to a character budget
(not a small fixed count), the definition of done comes only for the kinds of work
the task is, and the rest stays one call away (``rules_page``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from varyz.resolution.domains import BY_NAME, decision_points, detect_domains
from varyz.resolution.knowledge import GLOBAL_SCOPE, normalize_scope
from varyz.resolution.rule_selection import RuleView, cost, select

# Characters of standing-rule text a Cloud briefing shows before it pages. Large
# enough for most owners' whole standing set; it bounds the outliers.
RULES_BUDGET = 16_000
# Rules always shown, budget or not, as in the local briefing.
RULES_FLOOR = 12


def _view(record: Mapping[str, Any]) -> RuleView:
    payload = record["payload"]
    return RuleView(
        id=str(record["id"]),
        statement=str(payload.get("statement", "")),
        scope=str(payload.get("scope") or GLOBAL_SCOPE),
        domain=str(payload.get("domain") or ""),
        kind=str(payload.get("kind") or "rule"),
        rationale=str(payload.get("rationale") or ""),
        trial=bool(payload.get("trial")),
    )


def _shown(rule: RuleView, merged: Mapping[str, list[str]]) -> dict[str, Any]:
    value = rule.advice()
    if merged.get(rule.id):
        # Copies saved twice: a receipt for either id maps to this rule.
        value["same_as"] = merged[rule.id]
    return value


def briefing(
    records: Iterable[Mapping[str, Any]],
    task: str,
    project: str,
    *,
    page: int = 1,
    budget: int = RULES_BUDGET,
    known_projects: Iterable[str] = (),
) -> dict[str, Any]:
    """Standing rules and the definition of done for one task, as an agent reads them."""
    rules = [_view(record) for record in records if record.get("kind") == "rule"]
    rules.reverse()  # Stored oldest first; ranking breaks ties newest first.
    domains = detect_domains(task, extra={rule.domain for rule in rules if rule.domain})
    # Without a task, return all approved rules within the same page budget.
    # Setup uses this read-only projection to verify domain rules as well.
    standing = [rule for rule in rules if not rule.domain or not task]
    known = {*known_projects, *(rule.scope for rule in rules)}
    first = select(
        standing, task, project=project, limit=RULES_FLOOR, budget_chars=budget,
        domains={rule.domain for rule in standing},
        known_projects=known, normalize_scope=normalize_scope,
    )
    value: dict[str, Any] = {"kinds_of_work": [
        {"domain": name, "title": BY_NAME[name].title if name in BY_NAME else name}
        for name in domains
    ]}
    if page <= 1:
        shown = [rule for rule, _ in first.chosen]
        later = first.withheld
    else:
        everything = select(
            standing, task, project=project, limit=len(standing) or 1,
            domains={rule.domain for rule in standing},
            known_projects=known, normalize_scope=normalize_scope,
        )
        seen = {rule.id for rule, _ in first.chosen}
        remaining = [rule for rule, _ in everything.chosen if rule.id not in seen]
        pages: list[list[RuleView]] = []
        for rule in remaining:
            if not pages or sum(cost(item) for item in pages[-1]) + cost(rule) > budget:
                pages.append([])
            pages[-1].append(rule)
        shown = pages[page - 2] if page - 2 < len(pages) else []
        later = sum(len(chunk) for chunk in pages[page - 1:])
    value["rules"] = [_shown(rule, first.merged) for rule in shown]
    value["more_rules_in_scope"] = later
    if later:
        value["more_rules"] = (
            f"{later} more standing rules are in scope but less related to this task. "
            f"Call start_task again with rules_page={max(page, 1) + 1} if a choice "
            "may depend on them."
        )
    checklist: dict[str, list[dict[str, Any]]] = {}
    project_scope = normalize_scope(project)
    for rule in reversed(rules):  # Oldest first: a checklist reads as written down.
        if rule.domain in domains and rule.scope in (GLOBAL_SCOPE, project_scope):
            checklist.setdefault(rule.domain, []).append(
                {"id": rule.id, "item": rule.statement,
                 **({"scope": rule.scope} if rule.scope != GLOBAL_SCOPE else {})}
            )
    value["definition_of_done"] = checklist
    points = decision_points(domains)
    if points:
        value["ask_varyz_about"] = list(points)
    left_out = len(rules) - len(standing) - sum(len(items) for items in checklist.values())
    if left_out > 0:
        value["other_kinds_of_work"] = (
            f"{left_out} checklist rules for other kinds of work are not shown."
        )
    return value


def checked_rules(
    records: Iterable[Mapping[str, Any]], task: str, project: str,
    known_projects: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """What finish_check verifies for a task: the rules the briefing showed and the
    definition of done for the task's kinds of work."""
    brief = briefing(records, task, project, known_projects=known_projects)
    items = [{"id": rule["id"], "item": rule["statement"], "domain": "standing"}
             for rule in brief["rules"]]
    for domain, checks in brief["definition_of_done"].items():
        items.extend({"id": check["id"], "item": check["item"], "domain": domain}
                     for check in checks)
    return items


__all__ = ["RULES_BUDGET", "RULES_FLOOR", "briefing", "checked_rules"]

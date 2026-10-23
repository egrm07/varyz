"""Which confirmed rules a briefing shows: one narrowing step for local and Cloud.

Both briefings used to differ: local ranked a dozen rules, Cloud sent every rule it
held (175 rules, 62,000 characters in one briefing). This module is the shared
step. It removes only what is safe to remove and says how much it left out:

- rules for another kind of work (a website checklist on a video task);
- rules that name another of the owner's projects, unless the task names it too;
- rules for a situation the task is not in ("When preparing creator outreach, ..."
  on a bug fix);
- formatting-only copies of the same rule (the newest copy stays);

then ranks what is left the way the local briefing always has, and fills places
up to a count and, when given, a character budget.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from varyz.resolution.knowledge import GLOBAL_SCOPE, _terms

# Characters a shown rule costs beyond its words: id, quotes, keys.
RULE_OVERHEAD = 48

_CONTRACTIONS = {
    "dont": "do not", "don't": "do not", "doesnt": "does not", "doesn't": "does not",
    "didnt": "did not", "didn't": "did not", "cant": "can not", "can't": "can not",
    "cannot": "can not", "wont": "will not", "won't": "will not", "isnt": "is not",
    "isn't": "is not", "arent": "are not", "aren't": "are not", "shouldnt": "should not",
    "shouldn't": "should not", "never": "do not ever",
}
_VERSION = re.compile(r"(?:[.\-_ ]v?|v)\d+(?:\.\d+)*$")
# "When preparing creator outreach, ..." holds only in that situation: the words
# before the first comma or colon.
_CONDITION = re.compile(
    r"^\s*(?:when|whenever|if|for|in|before|after|during|while)\s+([^,:;]{3,160})[,:;]", re.I)
# A situation every task is in ("When you finish a task") does not narrow a rule,
# and a bare verb ("When testing ...") says too little to rule one out.
_ANY_WORK = _terms(
    "work task code coding engineering change finish done report reply answer respond "
    "write writing ask question agent session anything everything something owner user "
    "every each any all new existing unsure unclear uncertain doubt ambiguous possible "
    "needed necessary blocked stuck error failure wrong missing problem mistake correct "
    "correction instruction request project app application tool program product feature "
    "choice decision next step explain result describe summary")
_VERBS = _terms(
    "make making prepare preparing create creating build building test testing use using "
    "run running add adding update updating fix fixing send sending show showing keep "
    "keeping do doing work working handle handling start starting declare declaring call "
    "calling say saying claim claiming give giving get getting tell telling extend "
    "extending review reviewing offer offering help helping")


class RuleLike(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def statement(self) -> str: ...
    @property
    def scope(self) -> str: ...
    @property
    def domain(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def rationale(self) -> str: ...
    @property
    def trial(self) -> bool: ...


@dataclass(frozen=True)
class RuleView:
    """A rule as Cloud stores it, shaped like a local principle."""

    id: str
    statement: str
    scope: str = GLOBAL_SCOPE
    domain: str = ""
    kind: str = "rule"
    rationale: str = ""
    trial: bool = False

    def advice(self) -> dict[str, Any]:
        value: dict[str, Any] = {"id": self.id, "statement": self.statement}
        if self.scope != GLOBAL_SCOPE:
            value["scope"] = self.scope
        if self.rationale:
            value["why"] = self.rationale
        if self.domain:
            value["domain"] = self.domain
        return value


@dataclass
class Selection:
    chosen: list[tuple[Any, tuple[str, ...]]] = field(default_factory=list)
    withheld: int = 0
    # Kept rule id -> ids of formatting copies left out.
    merged: dict[str, list[str]] = field(default_factory=dict)
    other_project: int = 0
    off_situation: int = 0


def other_situation(statement: str, task_terms: set[str], project_terms: set[str]) -> bool:
    """A conditional rule whose situation the task shares no word with.

    Only the condition decides, never the instruction after it, and project names
    and bare verbs do not count as a shared word: "When preparing Varyz creator
    outreach" stays out of a Varyz bug fix even though both say "Varyz".
    """
    found = _CONDITION.match(unicodedata.normalize("NFKC", statement))
    if found is None or not task_terms:
        return False
    situation = _terms(found.group(1)) - project_terms - _ANY_WORK - _VERBS
    return bool(situation) and not situation & task_terms


def _words(statement: str) -> set[str]:
    text = unicodedata.normalize("NFKC", statement).casefold().replace("’", "'")
    words: list[str] = []
    for word in re.findall(r"[\w']+", text):
        words.extend(_CONTRACTIONS.get(word, word.strip("'")).split())
    return {word for word in words if word}


def same_rule(first: str, second: str) -> bool:
    """Collapse formatting duplicates only; similarity cannot prove equal conditions."""
    def canonical(text: str) -> list[str]:
        text = unicodedata.normalize("NFKC", text).casefold().replace("?", "'")
        return [part for word in re.findall(r"[\w']+", text)
                for part in _CONTRACTIONS.get(word, word.strip("'")).split()]

    return canonical(first) == canonical(second)


def project_base(scope: str) -> str:
    """'san7.14' and 'san7' are one product for this test; versions are folders."""
    return _VERSION.sub("", scope.casefold()).strip() or scope.casefold()


def names_other_project(
    statement: str, project: str | None, task: str, known_projects: Iterable[str]
) -> bool:
    """A rule for every project that is plainly about one other project ('When
    launching SAN7 for preview ...') does not bear on this one."""
    here = project_base(project) if project else ""
    lowered = unicodedata.normalize("NFKC", statement).casefold()
    asked = unicodedata.normalize("NFKC", task).casefold()
    if here and re.search(rf"(?<![\w-]){re.escape(here)}(?![\w-])", lowered):
        return False
    generic = {"site", "website", "mobile", "projects", "project", "private", "public",
               "demo", "documents", "marketing", "promo", "codex", "egrm", "varys"}
    for name in {project_base(p) for p in known_projects if p and p != GLOBAL_SCOPE}:
        if len(name) < 3 or name == here or name in generic:
            continue
        pattern = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
        specific = re.search(rf"(?:^|\bfor\s+|\bon\s+|\blaunching\s+|\bbuilding\s+)"
                             rf"{re.escape(name)}(?![\w-])", lowered)
        if specific and not re.search(pattern, asked):
            return True
    return False


def cost(rule: RuleLike) -> int:
    """Characters one shown rule adds to a briefing."""
    return len(rule.statement) + len(rule.rationale) + RULE_OVERHEAD


def _in_scope(rule: RuleLike, project_scope: str | None, lowered_task: str) -> bool:
    if rule.scope == GLOBAL_SCOPE or rule.scope == project_scope:
        return True
    return bool(re.search(rf"(?<!\w){re.escape(rule.scope)}(?!\w)", lowered_task))


def collapse(rules: Sequence[RuleLike]) -> tuple[list[RuleLike], dict[str, list[str]]]:
    """Keep one copy of each rule saved twice: a direct owner rule over a trial,
    otherwise the first (callers pass newest first)."""
    kept: list[RuleLike] = []
    merged: dict[str, list[str]] = {}
    for rule in sorted(rules, key=lambda item: bool(item.trial)):
        twin = next((k for k in kept if k.kind == rule.kind
                     and k.domain == rule.domain and k.rationale == rule.rationale
                     and same_rule(k.statement, rule.statement)),
                    None)
        if twin is None:
            kept.append(rule)
        else:
            merged.setdefault(twin.id, []).append(rule.id)
    order = {id(rule): index for index, rule in enumerate(rules)}
    kept.sort(key=lambda item: order[id(item)])
    return kept, merged


def select(
    rules: Sequence[RuleLike],
    task: str,
    *,
    project: str | None = None,
    domains: Collection[str] = (),
    limit: int = 12,
    budget_chars: int | None = None,
    known_projects: Iterable[str] = (),
    normalize_scope: Any = None,
    exclude: Collection[str] = (),
    context: str = "",
) -> Selection:
    """Standing rules that bear on a task, newest first within equal rank.

    Scope is a hard filter: a rule for another project never applies. Within
    scope, rules sharing terms with the task come first; standing rules with no
    shared term still fill the remaining places, because "dry-run first" governs
    a task that never says "dry-run". At least half of ``limit`` goes to rules for
    every project when there are that many. ``exclude`` names rules the briefing
    shows in a section of their own (``writing_rules``). With ``budget_chars``, places past
    ``limit`` keep filling in rank order while the shown statements fit. A rule for
    a situation the task is not in stays out (``other_situation``); ``context`` adds
    words that describe the task without being part of it (its kinds of work).
    """
    project_scope = (normalize_scope or (lambda value: value))(project) if project else None
    lowered = unicodedata.normalize("NFKC", task).casefold()
    wanted = set(domains)
    known = list(known_projects)
    candidates: list[RuleLike] = []
    other_project = off_situation = 0
    situation = _terms(f"{task} {context}") if task.strip() else set()
    project_terms = _terms(f"{project or ''} {project_scope or ''}")
    for rule in rules:
        if rule.id in exclude:
            continue
        if rule.domain and rule.domain not in wanted:
            continue
        if not _in_scope(rule, project_scope, lowered):
            continue
        if rule.scope == GLOBAL_SCOPE and names_other_project(
            rule.statement, project_scope, task, known
        ):
            other_project += 1
            continue
        if rule.kind != "vision" and other_situation(rule.statement, situation, project_terms):
            off_situation += 1
            continue
        candidates.append(rule)
    candidates, merged = collapse(candidates)
    task_terms = _terms(task)
    ranked: list[tuple[int, int, int, int, RuleLike, tuple[str, ...]]] = []
    for rule in candidates:
        matched = tuple(sorted(task_terms & _terms(rule.statement + " " + rule.rationale)))
        specific = rule.scope != GLOBAL_SCOPE
        ranked.append((
            0 if rule.kind == "vision" and specific else 1,
            # A direct owner rule must not be crowded out by speculative
            # learned trials with more overlapping task words.
            1 if rule.trial else 0,
            -len(matched),
            0 if specific else 1,
            rule,
            matched,
        ))
    # Stable sort keeps newest-first within equal keys.
    ranked.sort(key=lambda item: item[:4])
    limit = max(0, limit)
    general = sum(1 for item in ranked if item[4].scope == GLOBAL_SCOPE)
    own = len(ranked) - general
    take_general = min(general, max(limit // 2, limit - own))
    take_own = min(own, limit - take_general)
    chosen: list[tuple[Any, tuple[str, ...]]] = []
    rest: list[tuple[Any, tuple[str, ...]]] = []
    for *_, rule, matched in ranked:
        if rule.scope == GLOBAL_SCOPE and take_general > 0:
            chosen.append((rule, matched))
            take_general -= 1
        elif rule.scope != GLOBAL_SCOPE and take_own > 0:
            chosen.append((rule, matched))
            take_own -= 1
        else:
            rest.append((rule, matched))
    if budget_chars is not None:
        used = sum(cost(rule) for rule, _ in chosen)
        for rule, matched in rest:
            cost_here = cost(rule)
            if used + cost_here > budget_chars:
                break
            chosen.append((rule, matched))
            used += cost_here
        order = {id(rule): index for index, (*_, rule, _m) in enumerate(ranked)}
        chosen.sort(key=lambda pair: order[id(pair[0])])
    return Selection(
        chosen=chosen,
        withheld=max(0, len(ranked) - len(chosen)),
        merged={key: ids for key, ids in merged.items()
                if any(rule.id == key for rule, _ in chosen)},
        other_project=other_project,
        off_situation=off_situation,
    )


__all__ = [
    "RULE_OVERHEAD", "RuleLike", "RuleView", "Selection", "collapse", "cost",
    "names_other_project", "other_situation",
    "project_base", "same_rule", "select",
]

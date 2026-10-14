"""Which rules go on the card, and in which slot. Deterministic, no model.

Privacy fails closed here. Only general rules (scope ``*``) are eligible by
default; a project rule can name a client or an unreleased product, so it joins
only when the owner names it with ``include``, one id at a time. ``exclude``
removes any rule and ``only`` restricts the card to the rules it names. An id
that matches nothing is an error, never silently ignored: the owner must not
believe a rule is off the card when it is not.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from varyz.card.archetype import Verdict, classify
from varyz.card.source import CardRule, LedgerCounts

MAX_HOUSE_RULES = 5
MIN_ID_PREFIX = 6
MAX_HANDLE = 30
# Each absolute after the first adds a point to a rule's firmness, up to this
# many. Long rules pile up absolutes, so the strongest opinion is picked among
# every rule within this band of the firmest: extra absolutes order the band,
# they cannot push a short, equally firm rule off the quote.
EXTRA_ABSOLUTES = 2
FIRMEST_BAND = EXTRA_ABSOLUTES
# The owner chose this headline (2026-09-24); it echoes the site's "done your way".
HEADLINE = "My Way"

_ABSOLUTE = re.compile(
    r"(?<!\w)(?:never|always|every|must|only|nothing|no one|nobody|non-negotiable|"
    r"under no circumstances|do not|don't|cannot|without exception|no)(?!\w)",
    re.IGNORECASE,
)
_NORMATIVE = re.compile(
    r"(?<!\w)(?:should|need to|needs to|have to|has to|required?|make sure|ensure|"
    r"expect)(?!\w)",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"(?<!\w)(?:prefer\w*|ideally|usually|try to|where possible|if possible|maybe|"
    r"consider|generally|tend to|when in doubt|might)(?!\w)",
    re.IGNORECASE,
)


class SelectionError(ValueError):
    """An id the owner passed does not name a rule that could go on the card."""


class HandleError(ValueError):
    """The --handle value does not look like a handle."""


_HANDLE = re.compile(r"[A-Za-z0-9_.-]+")


def normalize_handle(raw: str) -> str:
    """``egor`` or ``@egor`` becomes ``@egor``; anything else is refused, never altered.

    A handle is letters, digits, ``_``, ``.`` and ``-``, at most MAX_HANDLE
    characters after the optional ``@``. An empty value means no handle.
    """
    value = raw.strip()
    if not value:
        return ""
    bare = value[1:] if value.startswith("@") else value
    if not bare or len(bare) > MAX_HANDLE or not _HANDLE.fullmatch(bare):
        raise HandleError(
            f"--handle {raw!r} is not a handle: use letters, digits, _ . or -, "
            f"at most {MAX_HANDLE} characters (for example @egor)."
        )
    return "@" + bare


# An answer prints on the card only when the owner's answer is a sentence on its
# own. "None." or "Yes, the second one." means nothing without the question, and
# the card never prints a question, so such an answer is left off.
_MIN_STANDALONE_WORDS = 4
_REPLY_WORDS = frozenset({
    "yes", "no", "none", "nope", "nah", "sure", "ok", "okay", "both", "neither",
    "either", "correct", "right", "agreed", "depends",
})
_POINTS_BACK = frozenset({"it", "that", "this", "they", "these", "those", "same", "them"})


def stands_alone(answer: str) -> bool:
    """Whether an answer reads as a rule without its question. Words only, no model."""
    words = answer.split()
    if len(words) < _MIN_STANDALONE_WORDS or answer.rstrip().endswith("?"):
        return False
    first = words[0]
    if not first[0].isalpha() or not first[0].isupper():
        return False
    bare = first.casefold().rstrip(",.;:!")
    if bare in _POINTS_BACK:
        return False
    # "No, use ..." answers the question; "No analytics on any site." is a rule.
    return not (bare in _REPLY_WORDS and first[-1] in ",.;:!")


def card_text(rule: CardRule) -> str | None:
    """The words the card prints for ``rule``, or None when it would read as a question.

    A rule prints as the owner stated it. An answer prints as the owner's answer
    alone, verbatim, when that answer stands on its own; otherwise it is left
    off the card (it still counts towards the archetype).
    """
    text = rule.core if rule.kind == "answer" else rule.text
    if rule.kind == "answer" and not stands_alone(text):
        return None
    if text.rstrip().endswith("?"):
        return None
    return text


def strength(text: str) -> int:
    """How firmly a rule is worded: absolutes up, hedges down. Words only, no model."""
    score = 0
    absolutes = len(_ABSOLUTE.findall(text))
    if absolutes:
        # A second and third absolute ("no analytics, no telemetry, no tracking")
        # make a rule firmer, but only a little: wording, not length, decides.
        score += 3 + min(absolutes - 1, EXTRA_ABSOLUTES)
    if _NORMATIVE.search(text):
        score += 2
    if "!" in text:
        score += 1
    if _HEDGE.search(text):
        score -= 2
    return score


@dataclass(frozen=True)
class Selection:
    """The rules on the card's list, and every rule that was left off and why."""

    chosen: tuple[CardRule, ...]
    excluded: tuple[CardRule, ...]
    project_left_off: tuple[CardRule, ...]
    not_selected: tuple[CardRule, ...]


def _resolve(ids: Iterable[str], rules: Sequence[CardRule], flag: str) -> set[str]:
    wanted: set[str] = set()
    for raw in ids:
        value = raw.strip()
        exact = [rule for rule in rules if rule.id == value]
        if exact:
            wanted.add(exact[0].id)
            continue
        if len(value) < MIN_ID_PREFIX:
            raise SelectionError(
                f"{flag} {value!r}: give the whole id, or at least {MIN_ID_PREFIX} characters."
            )
        matches = [rule for rule in rules if rule.id.startswith(value)]
        if not matches:
            raise SelectionError(f"{flag} {value!r} matches no rule of yours.")
        if len(matches) > 1:
            raise SelectionError(f"{flag} {value!r} matches {len(matches)} rules; give more of it.")
        wanted.add(matches[0].id)
    return wanted


def select(
    rules: Sequence[CardRule],
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    only: Iterable[str] = (),
) -> Selection:
    """Apply the owner's choices. General rules by default; project rules by name only."""
    rules = list(rules)
    included = _resolve(include, rules, "--include-project")
    excluded = _resolve(exclude, rules, "--exclude")
    only_ids = _resolve(only, rules, "--only")
    for rule_id in only_ids:
        rule = next(rule for rule in rules if rule.id == rule_id)
        if not rule.general and rule_id not in included:
            raise SelectionError(
                f"--only {rule_id}: that rule belongs to the project '{rule.scope}'. "
                f"Add --include-project {rule_id} if you want it on the card."
            )
    chosen: list[CardRule] = []
    left_excluded: list[CardRule] = []
    project_left: list[CardRule] = []
    not_selected: list[CardRule] = []
    for rule in rules:
        if rule.id in excluded:
            left_excluded.append(rule)
        elif not rule.general and rule.id not in included:
            project_left.append(rule)
        elif only_ids and rule.id not in only_ids:
            not_selected.append(rule)
        else:
            chosen.append(rule)
    return Selection(
        tuple(chosen), tuple(left_excluded), tuple(project_left), tuple(not_selected)
    )


def _rank_key(rule: CardRule) -> tuple[int, int, int, str, str]:
    # Firmest wording first; then the rule said most often; then the shorter one,
    # which reads better on a card; then the oldest, then the id, so the order
    # never depends on anything but the rules themselves.
    return (
        -strength(rule.words), -rule.stated, len(card_text(rule) or rule.text),
        rule.created_at, rule.id,
    )


@dataclass(frozen=True)
class CardContent:
    handle: str
    verdict: Verdict
    strongest: CardRule | None
    house_rules: tuple[CardRule, ...]
    # Chosen rules the card cannot print because they read as a question.
    unprintable: tuple[CardRule, ...]
    counts: LedgerCounts | None

    @property
    def headline(self) -> str:
        return HEADLINE

    def stats_line(self) -> str:
        """One short stat for the footer, straight from the tables, or nothing."""
        if self.counts is None or not self.counts.rules:
            return ""
        rules = self.counts.rules
        return f"{rules:,} rule{'s' if rules != 1 else ''} on record"


def compose(
    chosen: Sequence[CardRule],
    *,
    handle: str = "",
    counts: LedgerCounts | None = None,
    max_items: int = MAX_HOUSE_RULES,
    quote_fits: Callable[[str], bool] | None = None,
) -> CardContent:
    """Fill the card's slots from the chosen rules.

    The strongest opinion comes from the most firmly worded rules (within
    FIRMEST_BAND of the firmest); among those the first, by the fixed order, that
    ``quote_fits`` without shortening wins, and only when none fits is the
    firmest one shortened. The house rules are the
    next ones in the same order. Rules that would read as a question are left
    off the card and reported in ``unprintable``.
    """
    ranked = sorted(chosen, key=_rank_key)
    printable = [rule for rule in ranked if card_text(rule) is not None]
    strongest: CardRule | None = None
    if printable:
        top = strength(printable[0].words)
        firmest = [
            rule for rule in printable if strength(rule.words) >= top - FIRMEST_BAND
        ]
        strongest = firmest[0]
        if quote_fits is not None:
            strongest = next(
                (rule for rule in firmest if quote_fits(card_text(rule) or "")), firmest[0]
            )
    house = [rule for rule in printable if rule is not strongest][: max(0, max_items)]
    return CardContent(
        handle=handle,
        verdict=classify(chosen),
        strongest=strongest,
        house_rules=tuple(house),
        unprintable=tuple(rule for rule in ranked if card_text(rule) is None),
        counts=counts,
    )


__all__ = [
    "FIRMEST_BAND",
    "HEADLINE",
    "MAX_HANDLE",
    "MAX_HOUSE_RULES",
    "CardContent",
    "HandleError",
    "Selection",
    "SelectionError",
    "card_text",
    "compose",
    "normalize_handle",
    "select",
    "stands_alone",
    "strength",
]

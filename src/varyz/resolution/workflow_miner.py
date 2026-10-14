"""Find the order in which the owner guides agents through a kind of work.

The owner's turns to coding agents show what they settle first. On a website:
the colour, then the logo, then what goes on the page. On a product: the vision,
then the design, then monetization. This reads that order across many sessions
and keeps only what repeats.

What the owner did first is not always what they meant to do first. They can
start building the page and pick the colours afterwards, then have the page
redone with the new colours. Copying that order would teach agents the slip, so
the evidence is ranked, per session and per pair of steps:

1. An explicit order in the owner's words: "colours before the logo", "first the
   colour, then the logo", "the logo after the colours".
2. A correction: a step started early and redone once a later one was settled
   ("now redo the hero with the new palette"), or regret ("should have done the
   colours first"). That counts for the step that had to come first.
3. Otherwise, which step the owner brought up first - only within one focused
   session. A project runs for months, and what came up first there is what was
   discussed first, not the order the owner guides it in; product orders are
   learned from the owner's statements and corrections alone.

Each session gives each pair at most one vote, from the strongest evidence it
has. A step needs to appear in at least ``MIN_UNITS`` sessions, and each link
in the learned chain needs ``MIN_PAIR_VOTES`` sessions agreeing at least
``MIN_AGREEMENT`` of the time. Learned orders reach agents straight away, so the
bar is high; anything weaker is left out and reported.

It reads only turns varyz can show the owner wrote, never sensitive ones, and
reports counts, never the owner's words.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from varyz.resolution.domains import BY_NAME
from varyz.resolution.knowledge import GLOBAL_SCOPE
from varyz.resolution.rule_miner import _turn_scope, sentences
from varyz.resolution.workflows import Step

MIN_UNITS = 3
MIN_PAIR_VOTES = 3
MIN_AGREEMENT = 3 / 4
MAX_SAMPLE_UNITS = 5
LEARNED_SOURCE = "learned:ai_conversations"

_EXPLICIT, _CORRECTION, _FIRST_MENTION = 0, 1, 2
# Folder copies of one product (SAN7.13, SAN7.14) are one project.
_VERSION_SUFFIX = re.compile(r"(?:\.\d+)+$")
_LEVEL_NAMES = ("explicit", "correction", "first_mention")


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(alternatives) + r")(?![\w-])", re.I)


@dataclass(frozen=True)
class Facet:
    """One step of a workflow and the words the owner uses for it."""

    step: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Lexicon:
    name: str
    domain: str
    unit: str
    facets: tuple[Facet, ...]
    # In a correction turn, a generic reference to the work itself ("apply the
    # colours to the site") stands for this facet: the thing being redone.
    rework_target: str | None = None
    rework_alias: re.Pattern[str] | None = None
    # Whether the step raised first counts as a vote at all. It does in a focused
    # session; across a project's months it only shows what was discussed first.
    first_mention: bool = True


WEBSITE = Lexicon(
    name="website",
    domain="website",
    unit="website sessions",
    facets=(
        Facet("colour", re.compile(
            r"(?<![\w-])(?:colou?rs?|colou?r (?:scheme|palette)|palettes?|brand colou?rs?|"
            r"accent colou?rs?|colou?rways?)(?![\w-])|#[0-9a-f]{6}\b",
            re.I,
        )),
        Facet("logo", _words(r"logos?", r"wordmarks?", r"logotypes?", r"brand ?marks?")),
        Facet("fonts", _words(r"fonts?", r"typefaces?", r"typography")),
        # What goes on the page. Not "page" or "site" themselves: those name the
        # website being built, and every website session mentions them first.
        Facet("page content", _words(
            r"hero(?: section)?", r"sections?", r"layouts?", r"nav ?bars?", r"navigation",
            r"footers?", r"headlines?", r"taglines?", r"copywriting",
            r"(?:site|page|web|landing|hero|marketing|website) copy", r"cta",
            r"call to action", r"testimonials?", r"faq", r"screenshots?",
            r"illustrations?", r"imagery", r"images?",
        )),
        Facet("legal pages", _words(
            r"privacy(?: policy)?", r"terms of (?:service|use)", r"legal pages?",
            r"cookies? banner",
            r"imprint",
        )),
        Facet("SEO", _words(
            r"seo", r"meta tags?", r"sitemap", r"open ?graph", r"og image", r"robots\.txt",
            r"social (?:preview|image|card)s?",
        )),
        Facet("hosting", _words(
            r"hosting", r"host(?:ed)? (?:it|on|the site)", r"deploy(?:ed|ing|ment)?", r"dns",
            r"domain names?", r"vercel", r"netlify", r"cloudflare pages", r"github pages",
        )),
    ),
    rework_target="page content",
    rework_alias=_words(
        r"(?:web ?)?sites?", r"landing(?: page)?s?", r"(?:web ?|home ?)?pages?", r"front-?end",
    ),
)

PRODUCT = Lexicon(
    name="product",
    domain="",
    unit="projects",
    facets=(
        Facet("vision", _words(
            r"vision", r"positioning", r"value prop(?:osition)?", r"mission",
            r"target (?:users?|audience|customers?)",
            r"who (?:it'?s|this is) for", r"north star",
        )),
        Facet("naming", _words(r"naming", r"(?:product|brand|app) names?", r"rename")),
        Facet("design", _words(
            r"design(?:s|ed|ing)?", r"ux", r"ui", r"wireframes?", r"mock-?ups?", r"prototypes?",
            r"figma", r"user flows?",
        )),
        Facet("monetization", _words(
            r"moneti[sz](?:e|ation|ing)", r"pricing", r"prices?", r"subscriptions?", r"paywalls?",
            r"business model", r"revenue", r"freemium", r"in-app purchases?", r"billing",
            r"paid (?:plan|tier)s?",
        )),
        Facet("website", _words(r"web ?sites?", r"landing(?: page)?s?")),
        Facet("marketing", _words(
            r"marketing", r"campaigns?", r"promot(?:e|ion|ing)", r"ads", r"advertis(?:e|ing)",
            r"social media", r"launch posts?",
        )),
        Facet("launch", _words(
            r"launch(?:ing|ed)?", r"go(?:ing)? live", r"app store (?:review|submission)",
            r"ship(?:ping)? (?:it|to users)", r"public release",
        )),
    ),
    first_mention=False,
)

LEXICONS = (WEBSITE, PRODUCT)

# The owner redoing or regretting what was done too early.
_REWORK = re.compile(
    r"\b(?:re-?do(?:ne|ing)?|rebuild|re-build|rework|redesign|re-design|start (?:over|again)|"
    r"from scratch|go back|again|now that|apply (?:the|these|this|those|our|my|it|them)|"
    r"(?:with|to|use|match) the new|to match|in hindsight|wrong order|too early)\b",
    re.IGNORECASE,
)
_REGRET_FIRST = re.compile(
    r"\b(?:should(?: have|'ve)|ought to have|needed to|we need to)\b[^.!?]{0,80}"
    r"\b(?:first|before (?:any|every|any ?thing|anything) else)\b",
    re.IGNORECASE,
)
# Words that order two steps in one sentence. Forward: left comes first.
_FORWARD = re.compile(r"\b(?:before|prior to|ahead of|then|followed by|and next)\b", re.I)
_BACKWARD = re.compile(r"\b(?:after|once)\b", re.I)
_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_CODE_LINE = re.compile(r"[{};]\s*$|^\s{4,}\S|^\s*[<$>#]|\w\(\)|=>|::")


# Per unordered pair (a, b), a < b: (+1 when a comes first else -1, evidence
# level); the steps the unit worked on; the steps it showed had to come first.
UnitVotes = tuple[dict[tuple[str, str], tuple[int, int]], set[str], set[str]]


@dataclass(frozen=True)
class LearnedWorkflow:
    name: str
    domain: str
    steps: tuple[Step, ...]
    provenance: Mapping[str, Any]


@dataclass
class LearningStats:
    """Aggregate counts only: safe to print on live data."""

    turns_read: int = 0
    sensitive_skipped: int = 0
    units: dict[str, int] = field(default_factory=dict)
    votes: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_LEVEL_NAMES, 0))
    corrections: int = 0
    workflows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "turns_read": self.turns_read,
            "sensitive_skipped": self.sensitive_skipped,
            "units": dict(self.units),
            "votes": dict(self.votes),
            "corrections": self.corrections,
            "workflows": self.workflows,
        }


def prose(text: str) -> str:
    """The owner's words without pasted code, logs or markup."""
    text = _FENCE.sub(" ", text)
    return "\n".join(line for line in text.splitlines() if not _CODE_LINE.search(line))


def _mentions(lexicon: Lexicon, text: str) -> dict[str, int]:
    """Facet -> position of its first mention in ``text``."""
    found: dict[str, int] = {}
    for facet in lexicon.facets:
        match = facet.pattern.search(text)
        if match is not None:
            found[facet.step] = match.start()
    return found


def _explicit_pairs(lexicon: Lexicon, text: str) -> list[tuple[str, str]]:
    """(earlier, later) pairs the owner ordered in so many words."""
    pairs: list[tuple[str, str]] = []
    for sentence in sentences(text):
        spans = sorted(
            (match.start(), facet.step)
            for facet in lexicon.facets
            for match in facet.pattern.finditer(sentence)
        )
        if len({step for _, step in spans}) < 2:
            continue
        for pattern, forward in ((_FORWARD, True), (_BACKWARD, False)):
            for connective in pattern.finditer(sentence):
                left = [step for at, step in spans if at < connective.start()]
                right = [step for at, step in spans if at >= connective.end()]
                if not left or not right or left[-1] == right[0]:
                    continue
                if forward:
                    pairs.append((left[-1], right[0]))
                else:
                    pairs.append((right[0], left[-1]))
    return pairs


def _unit_votes(lexicon: Lexicon, turns: Sequence[str]) -> UnitVotes:
    """Per unordered pair: (direction, level); plus facets seen and facets corrected.

    Direction is +1 when the pair's first element (alphabetically) comes first.
    """
    first: dict[str, tuple[int, int]] = {}
    # When the thing being redone was first worked on, counting generic references
    # to it ("build the site"): only for spotting corrections, never for the raw
    # order, where every website session would name the site first.
    begun: dict[str, int] = {}
    mentions: list[dict[str, int]] = []
    for index, text in enumerate(turns):
        found = _mentions(lexicon, text)
        mentions.append(found)
        for step, at in found.items():
            first.setdefault(step, (index, at))
            begun.setdefault(step, index)
        target, alias = lexicon.rework_target, lexicon.rework_alias
        if target is not None and alias is not None and alias.search(text):
            begun.setdefault(target, index)
    evidence: dict[tuple[str, str], list[list[int]]] = defaultdict(
        lambda: [[0, 0] for _ in _LEVEL_NAMES]
    )
    seen = set(first)

    def vote(earlier: str, later: str, level: int) -> None:
        a, b = sorted((earlier, later))
        evidence[(a, b)][level][0 if earlier == a else 1] += 1
        seen.update((earlier, later))

    corrected: set[str] = set()
    for index, text in enumerate(turns):
        for earlier, later in _explicit_pairs(lexicon, text):
            vote(earlier, later, _EXPLICIT)
        here = set(mentions[index])
        if not here:
            continue
        if _REWORK.search(text):
            # A step begun early (redone) being done again now that a step first
            # raised later (settled) is decided: that one should have come first.
            if (
                lexicon.rework_target is not None
                and lexicon.rework_alias is not None
                and lexicon.rework_alias.search(text)
            ):
                here.add(lexicon.rework_target)
            nearby = here | (set(mentions[index - 1]) if index else set())
            for redone in here:
                for settled in nearby:
                    if redone == settled or redone not in begun or settled not in first:
                        continue
                    if begun[redone] < first[settled][0] <= index:
                        vote(settled, redone, _CORRECTION)
                        corrected.add(settled)
        for sentence in sentences(text):
            if not _REGRET_FIRST.search(sentence):
                continue
            wanted = _mentions(lexicon, sentence)
            for settled in wanted:
                for step, turn in begun.items():
                    if step not in wanted and settled in first and turn < first[settled][0]:
                        vote(settled, step, _CORRECTION)
                        corrected.add(settled)

    steps = sorted(seen)
    decided: dict[tuple[str, str], tuple[int, int]] = {}
    for position, a in enumerate(steps):
        for b in steps[position + 1:]:
            levels = evidence.get((a, b))
            if levels is not None:
                chosen = next(
                    (
                        (1 if forward > backward else -1, level)
                        for level, (forward, backward) in enumerate(levels)
                        if forward != backward
                    ),
                    None,
                )
                if chosen is not None:
                    decided[(a, b)] = chosen
                    continue
            if (
                lexicon.first_mention
                and a in first
                and b in first
                and first[a][0] != first[b][0]
            ):
                decided[(a, b)] = (1 if first[a] < first[b] else -1, _FIRST_MENTION)
    return decided, seen, corrected


def _consensus(
    lexicon: Lexicon,
    units: Mapping[str, UnitVotes],
    stats: LearningStats,
) -> LearnedWorkflow | None:
    support: dict[str, set[str]] = defaultdict(set)
    corrected: dict[str, set[str]] = defaultdict(set)
    wins: dict[tuple[str, str], int] = defaultdict(int)
    for unit_id, (decided, seen, fixed) in units.items():
        if len(seen) < 2:
            continue
        for step in seen:
            support[step].add(unit_id)
        for step in fixed:
            corrected[step].add(unit_id)
        for (a, b), (direction, level) in decided.items():
            stats.votes[_LEVEL_NAMES[level]] += 1
            wins[(a, b) if direction > 0 else (b, a)] += 1
    stats.corrections += sum(len(ids) for ids in corrected.values())
    kept = [facet.step for facet in lexicon.facets if len(support[facet.step]) >= MIN_UNITS]
    if len(kept) < 2:
        return None

    def score(step: str) -> float:
        total = 0.0
        for other in kept:
            if other == step:
                continue
            ahead, behind = wins[(step, other)], wins[(other, step)]
            total += 1.0 if ahead > behind else 0.5 if ahead == behind and ahead else 0.0
        return total

    order = sorted(kept, key=lambda step: -score(step))
    chain: list[str] = []
    dropped: list[str] = []
    agreement: dict[str, str] = {}
    for step in order:
        if not chain:
            chain.append(step)
            continue
        last = chain[-1]
        ahead, behind = wins[(last, step)], wins[(step, last)]
        contradicted = any(wins[(step, g)] > wins[(g, step)] for g in chain)
        if (
            ahead >= MIN_PAIR_VOTES
            and ahead / (ahead + behind) >= MIN_AGREEMENT
            and not contradicted
        ):
            chain.append(step)
            agreement[f"{last} < {step}"] = f"{ahead}/{ahead + behind}"
        else:
            dropped.append(step)
    if len(chain) < 2:
        return None
    steps = []
    for step in chain:
        fixes = len(corrected[step])
        how = (
            f"a later step was started before this and redone in {fixes} of "
            f"{len(support[step])} {lexicon.unit} - settle it first"
            if fixes
            else ""
        )
        steps.append(Step(step, how))
    used = set().union(*(support[step] for step in chain))
    return LearnedWorkflow(
        name=lexicon.name,
        domain=lexicon.domain,
        steps=tuple(steps),
        provenance={
            "units": len(used),
            "unit": lexicon.unit,
            "agreement": agreement,
            "dropped": dropped,
            "sample": sorted(used)[:MAX_SAMPLE_UNITS],
            "thresholds": {
                "min_units": MIN_UNITS,
                "min_pair_votes": MIN_PAIR_VOTES,
                "min_agreement": round(MIN_AGREEMENT, 3),
            },
        },
    )


def project_of(scope: str) -> str:
    """Folder copies of one product (SAN7.13, SAN7.14) are one project."""
    return _VERSION_SUFFIX.sub("", scope) or scope


def _owner_turns(
    conn: sqlite3.Connection, stats: LearningStats
) -> Iterable[tuple[str, str, str]]:
    """(thread_id, scope, prose) for each non-sensitive turn the owner wrote, in order."""
    stats.sensitive_skipped += int(conn.execute(
        """SELECT count(*) FROM entries WHERE source = 'ai_conversations'
           AND authored_by_user = 1 AND sensitivity = 'sensitive'"""
    ).fetchone()[0])
    rows = conn.execute(
        """SELECT id, body, thread_id FROM entries
           WHERE source = 'ai_conversations' AND authored_by_user = 1
             AND sensitivity != 'sensitive'
           ORDER BY occurred_start, id"""
    )
    for row in rows:
        stats.turns_read += 1
        scope, text = _turn_scope(str(row["body"]))
        yield str(row["thread_id"] or row["id"]), scope, prose(text)


def learn_workflows(conn: sqlite3.Connection) -> tuple[list[LearnedWorkflow], LearningStats]:
    """The orders repeated across the owner's sessions, and counts of what was read."""
    stats = LearningStats()
    threads: dict[str, list[str]] = defaultdict(list)
    projects: dict[str, list[str]] = defaultdict(list)
    website_threads: set[str] = set()
    website = BY_NAME["website"].triggers
    for thread_id, scope, text in _owner_turns(conn, stats):
        threads[thread_id].append(text)
        if website.search(text):
            website_threads.add(thread_id)
        if scope != GLOBAL_SCOPE:
            projects[project_of(scope)].append(text)

    learned: list[LearnedWorkflow] = []
    for lexicon in LEXICONS:
        units: dict[str, UnitVotes] = {}
        # A website is learned per chat, where the owner guides it step by step;
        # a product per project, whose steps span months of chats.
        if lexicon is WEBSITE:
            for thread in sorted(website_threads):
                units[thread] = _unit_votes(lexicon, threads[thread])
        else:
            for name, texts in sorted(projects.items()):
                units[name] = _unit_votes(lexicon, texts)
        stats.units[lexicon.name] = len(units)
        found = _consensus(lexicon, units, stats)
        if found is not None:
            learned.append(found)
    stats.workflows = len(learned)
    return learned, stats


def record_learned(
    record: Callable[..., str | None], learned: Iterable[LearnedWorkflow]
) -> tuple[int, int]:
    """Write learned orders through ``record`` (a store's ``learn`` or a client's
    ``add_workflow`` with basis 'learned'); (written, unchanged).

    Learned orders reach agents at once, marked as learned: the owner chose that
    over a review queue. A stated order, an unchanged one, or one the owner
    withdrew is left as it is.
    """
    written = unchanged = 0
    for workflow in learned:
        created = record(
            name=workflow.name,
            steps=[step.to_dict() for step in workflow.steps],
            scope=GLOBAL_SCOPE,
            domain=workflow.domain,
            source=LEARNED_SOURCE,
            provenance=dict(workflow.provenance),
        )
        if created is None:
            unchanged += 1
        else:
            written += 1
    return written, unchanged


__all__ = [
    "LEARNED_SOURCE",
    "LEXICONS",
    "LearnedWorkflow",
    "LearningStats",
    "MIN_AGREEMENT",
    "MIN_PAIR_VOTES",
    "MIN_UNITS",
    "learn_workflows",
    "prose",
    "record_learned",
]

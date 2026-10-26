"""The owner's portrait: what a reading found, as a few traits and one habit.

A reading leaves hundreds of rules, many of them one point in different words.
The portrait groups them by meaning and keeps three that stand out (the most
conversations behind them, a boost for what few public rule files are about,
distinct points), with brief labels and the owner's original words behind them. It finds the one
thing the owner keeps correcting agents about across projects (``Habit``),
counted from every message where they pushed back.

Read-only: it changes no rule. Only the owner's typed turns count
(``rule_reader.owner_turns``): a quote an agent wrote is not evidence. The same
archive and rules always give the same portrait.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, NamedTuple

from varyz.card.archetype import SIGNALS
from varyz.card.rarity import BASIS, SHARE, share_text
from varyz.resolution.knowledge import _terms
from varyz.resolution.principles import GLOBAL_SCOPE, Principle, SQLitePrincipleStore
from varyz.resolution.rule_miner import MiningStats
from varyz.resolution.rule_reader import Turn, owner_turns
from varyz.resolution.trials import MODEL_SOURCES

# Embeds texts as vectors for grouping by meaning; without one, shared words group.
Embed = Callable[[list[str]], Sequence[Sequence[float]]]

TRAITS = 3
# A trait needs this many conversations behind it, a habit this many.
MIN_TRAIT_CONVERSATIONS = 2
MIN_HABIT_CONVERSATIONS = 4
# A project with fewer conversations than this is not named in the headline.
MIN_PROJECT_CONVERSATIONS = 3
# The rules grouped: the strongest by evidence (more adds time, not traits).
MAX_RULES = 600
# Mean similarity a rule needs to join a group: by meaning, or by shared words.
SAME_MEANING = 0.62
SAME_WORDS = 0.3
# Two groups whose leading rules are this close make one trait ("do it yourself"
# and "drive the browser yourself"), and a trait this close to the habit is the
# habit again.
RELATED_MEANING = 0.5
RELATED_WORDS = 0.2
# A trait is named by one of its strongest rules this many deep.
LEADERS = 5
# A family fewer public rule files lead with than this is shown as rare.
RARE_SHARE = 0.05
LABELS = {signal.key: signal.label for signal in SIGNALS}
# Slots that describe how the owner works; a vision, decision or workflow does not.
TRAIT_SLOTS = ("rule", "never", "done")
QUOTE_WORDS = (4, 24)
# Pushback comes in short sentences; a longer one is a brief or pasted text. A
# message as short as "didnt work" counts, though it is too short to read a rule from.
PUSHBACK_WORDS = 30
PUSHBACK_MIN_WORDS = 2


@dataclass(frozen=True)
class Quote:
    text: str
    when: str
    project: str


@dataclass(frozen=True)
class Trait:
    id: str
    statement: str
    rule_ids: tuple[str, ...]
    conversations: int
    projects: tuple[str, ...]
    corrections: int
    quote: Quote | None
    family: str
    family_label: str
    # "1%" when few public rule files lead with this family, else "".
    rare: str
    on_trial: bool
    headline: str = ""


@dataclass(frozen=True)
class Kind:
    key: str
    title: str
    # A rule that would stop it, in the owner's terms, offered to keep.
    rule: str
    # The owner pushing back, in their own words.
    owner: re.Pattern[str]
    # What the agent did, as a reading described it.
    agent: re.Pattern[str]
    # A rule the owner already has against it.
    against: re.Pattern[str]


@dataclass(frozen=True)
class Habit:
    kind: str
    title: str
    rule: str
    times: int
    conversations: int
    projects: tuple[str, ...]
    first: str
    last: str
    quotes: tuple[Quote, ...]
    # The rules a reading already wrote against it: one point in many words.
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Portrait:
    messages: int
    conversations: int
    since: str
    projects: tuple[tuple[str, int], ...]
    pushback: int
    traits: tuple[Trait, ...]
    habit: Habit | None
    on_trial: int
    waiting: int
    rarity_basis: str = BASIS
    kinds: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["projects"] = [{"name": name, "conversations": n} for name, n in self.projects]
        return value


def _any(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")", re.IGNORECASE)


def _both(first: str, second: str) -> re.Pattern[str]:
    """A statement that says both, in either order."""
    return re.compile(rf"^(?=.*\b(?:{first}))(?=.*\b(?:{second}))", re.IGNORECASE | re.DOTALL)


# Ordered: when two kinds have as many conversations, the earlier names the habit.
KINDS: tuple[Kind, ...] = (
    Kind(
        "unfinished", "Agents tell you it's done before it works",
        "Before saying something is done or fixed, test it end to end the way I would use it.",
        _any(
            r"still (?:not|doesn'?t|doesnt|isn'?t|isnt|broken|fails?|failing|the same)\b",
            r"^(?:it |this )?(?:still )?(?:didn'?t|didnt|did not|doesn'?t|doesnt|does not) work",
            r"(?:it|this|still) (?:didn'?t|didnt|did not|doesn'?t|doesnt|does not) "
            r"(?:work|fix|change)",
            r"you (?:didn'?t|didnt|did not) (?:finish|fix|test|check)",
            r"(?<!what )did you (?:actually |really |even )?(?:test|check|verify|try) it",
            r"nothing (?:changed|happened|was (?:created|saved|made))",
            r"(?:isn'?t|isnt|not|wasn'?t|wasnt) (?:actually |really |properly |fully )?"
            r"(?:fixed|working)\b",
            r"same (?:error|issue|bug|problem)",
            r"still (?:there|happening|getting)",
        ),
        _any(
            r"(?:reported|declared|claimed|said|called|marked|treated) (?:\w+ ){0,6}?"
            r"(?:as )?(?:done|finished|complete|completed|fixed|working|successful|success)",
            r"(?:report|declare|claim)\w* (?:completion|success)",
            r"without (?:testing|verifying|checking|running|confirming)",
            r"(?:defects|bugs|failures|problems) (?:remained|still)",
        ),
        _both(r"test|verif|check|confirm|run ",
              r"(?:done|finished|complete|completed|fixed|works|working|operational|ready)\b"),
    ),
    Kind(
        "handed_back", "Agents hand the work back to you",
        "Do the steps yourself; hand me only what truly needs me, like a sign-in or a payment.",
        _any(
            r"do it (?:yourself|for me)", r"can'?t you (?:just )?do (?:it|that|this)",
            r"why (?:do|should|would|must) i (?:have to |need to )?"
            r"(?:do|run|copy|paste|set|click|open|install)",
            r"why do i need to", r"i (?:don'?t|dont) want to (?:do|run|copy|paste|click|set)",
            r"without me (?:doing|having)",
        ),
        _any(
            r"(?:asked|told|prompted|left|made|had|expected) (?:the )?(?:user|owner|him|them) "
            r"to (?:run|do|copy|paste|set|click|install|open|perform|write|finish|connect)",
            r"(?:gave|offered|listed) (?:\w+ ){0,3}(?:steps|instructions|commands) "
            r"(?:for|to) (?:the )?(?:user|owner)",
            r"manual (?:steps|work|setup)",
        ),
        _any(
            r"(?:do|drive|run|handle|complete|carry out|make) (?:\w+ ){0,4}?yourself",
            r"do (?:everything|all) you can", r"without (?:the owner|me)\b",
            r"rather than (?:listing|handing|asking|telling)",
            r"instead of (?:listing|handing|asking|telling)",
        ),
    ),
    Kind(
        "stopped", "Agents stop and wait for you",
        "Keep going until the task is done; save questions for the end unless one blocks the work.",
        _any(
            r"^(?:ok |okay |alright |so |now )?keep going", r"don'?t stop", r"dont stop",
            r"why (?:did|have) you stop", r"you stopped",
            r"continue (?:until|till|to the end|autonomously)",
            r"(?:don'?t|dont|stop|no need to) ask(?:ing)? (?:me|for)", r"just do it",
            r"you didn'?t finish", r"you didnt finish",
        ),
        _any(
            r"(?:stopped|paused|halted) (?:\w+ ){0,4}?(?:to ask|and waited|early|before|after "
            r"part|at)",
            r"waited for (?:a |the )?(?:go-ahead|confirmation|permission|approval|owner)",
            r"asked (?:for )?(?:permission|confirmation|approval|a go-ahead)",
        ),
        _any(
            r"without (?:waiting|stopping|pausing|check-?ins)", r"keep (?:working|going)",
            r"continue (?:autonomously|working|until)", r"proceed autonomously",
            r"(?:don'?t|do not) stop", r"until (?:the work is |it is |the task is )?"
            r"(?:done|finished|complete|truly blocked)",
        ),
    ),
    Kind(
        "wordy", "Agents write too much",
        "Keep reports and copy short: what changed and what is left, in plain words.",
        _any(
            r"too (?:wordy|verbose|much text|many words|detailed|dense|cluttered)",
            r"so much text", r"(?:less|fewer) (?:text|words)",
            r"shorter (?:summar|report|answer|text|message|repl|version|copy)",
            r"(?:more|way more|much more) concise", r"keep it short", r"say less",
            r"wall of text", r"(?:too much|unnecessary) (?:detail|info|explanation|jargon)",
            r"plain(?:er)? (?:words|language|english)",
        ),
        _any(
            r"(?:long|lengthy|verbose|wordy|dense|over-?complicated) "
            r"(?:summary|summaries|text|report|answer|explanation|copy|reply|message|sentence)",
            r"too (?:many words|much text|much detail)", r"jargon",
        ),
        _both(r"concise|short|shorter|brief|plain|jargon|wordy|verbose",
              r"copy|text|reports?\b|summar|messages?\b|answers?\b|explanations?\b|words?\b|"
              r"wording|repl"),
    ),
    Kind(
        "generic", "Designs come out generic",
        "Before showing visual work, look at it yourself and make it distinctive, not generic.",
        _any(
            r"generic", r"ai[- ]?(?:looking|generated|slop)", r"looks? (?:ai|cheap|bad|weird|off)",
            r"(?:no|lacks?|without) (?:aura|personality|soul|flare|flair|character)",
            r"(?:very|so|kinda|pretty) mid\b", r"\bbland\b", r"\bugly\b", r"\bcringe\b",
            r"(?:very|so|too) boring",
        ),
        _any(
            r"(?:generic|bland|plain|boring|ai-?looking|template-?like|cluttered|default)"
            r" (?:\w+ )?(?:design|visual|look|layout|image|style|page|font|icon|logo|colou?r)",
        ),
        _any(r"generic", r"distinctive", r"bland", r"personality", r"\baura\b",
             r"template-?like", r"ai-?looking"),
    ),
)
BY_KIND = {kind.key: kind for kind in KINDS}


# -- grouping by meaning --------------------------------------------------------


def _similarity(texts: list[str], embed: Embed | None
                ) -> tuple[list[list[float]], float, float]:
    """Pairwise similarity, the mean a member needs to join a group, and the
    similarity at which two groups' leaders make one point."""
    if embed is not None and texts:
        import numpy as np

        vectors = np.asarray(embed(texts), dtype=float)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)
        return (vectors @ vectors.T).tolist(), SAME_MEANING, RELATED_MEANING
    terms = [_terms(text) for text in texts]
    matrix = [[
        len(a & b) / len(a | b) if a and b else 0.0 for b in terms
    ] for a in terms]
    return matrix, SAME_WORDS, RELATED_WORDS


def _groups(count: int, similar: list[list[float]], threshold: float) -> list[list[int]]:
    """Leader grouping in the given order: each joins the closest group on average."""
    groups: list[list[int]] = []
    for index in range(count):
        best, best_score = None, threshold
        for number, members in enumerate(groups):
            score = sum(similar[index][member] for member in members) / len(members)
            if score >= best_score:
                best, best_score = number, score
        if best is None:
            groups.append([index])
        else:
            groups[best].append(index)
    return groups


# -- evidence -------------------------------------------------------------------


def _slot(rule: Principle) -> str:
    slot = rule.provenance.get("slot")
    return slot if isinstance(slot, str) else "rule"


def _quotes(rule: Principle) -> list[dict[str, Any]]:
    quotes = rule.provenance.get("quotes")
    return [quote for quote in quotes if isinstance(quote, dict)] \
        if isinstance(quotes, list) else []


def _date(value: str | None) -> str:
    return (value or "")[:10]


def _project(scope: str) -> str:
    return "" if scope == GLOBAL_SCOPE else scope


def _owned(rule: Principle, turns: dict[str, Turn]) -> list[tuple[dict[str, Any], Turn]]:
    """The rule's quotes the owner typed, each with its turn."""
    return [(quote, turns[str(quote.get("entry_id"))]) for quote in _quotes(rule)
            if str(quote.get("entry_id")) in turns]


def _family(statement: str) -> str:
    """The one word family the statement speaks to, or "" when it speaks to none or
    to several: a family named on a mixed rule would be a guess."""
    found = [signal.key for signal in SIGNALS if signal.pattern.search(statement)]
    return found[0] if len(found) == 1 else ""


def _short(text: str, words: tuple[int, int] = QUOTE_WORDS) -> str | None:
    text = " ".join(text.split()).strip(" \"'")
    count = len(text.split())
    return text if words[0] <= count <= words[1] else None


def _best_quote(evidence: list[tuple[dict[str, Any], Turn]], statement: str) -> Quote | None:
    """The owner's line that says it best: sharing the rule's words, said for good
    rather than in the moment, the most recent first."""
    terms = _terms(statement)
    recent = sorted(evidence, key=lambda item: _date(item[1].occurred), reverse=True)
    lines = [(text, turn, item) for item, turn in recent
             if (text := _short(str(item.get("quote") or "")))]
    for text, turn, _ in sorted(lines, key=lambda line: (
            -min(len(_terms(line[0]) & terms), 2), bool(line[2].get("moment")))):
        return Quote(text, _date(turn.occurred), _project(turn.scope))
    return None


# -- the portrait ---------------------------------------------------------------


def _pointed(statements: list[str], names: Iterable[str]) -> list[str]:
    """The statements without project names, so rules group by the point they
    make, not by the project they mention."""
    names = sorted({name for name in names if name and name != GLOBAL_SCOPE}, key=len,
                   reverse=True)
    if not names:
        return statements
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")(?:'s|’s)?\b",
                         re.IGNORECASE)
    return [" ".join(pattern.sub(" ", statement).split()) for statement in statements]


def headline(statement: str) -> str:
    """A short display label; the complete rule remains the authority.

    Only explicit patterns get a rewrite. Conditions stay in the original rule
    and its evidence, never in a newly invented owner instruction.
    """
    text = " ".join(statement.split()).strip()
    lower = text.casefold()
    if re.search(r"\b(?:note|item|requirement)\s+\d+\b", lower):
        return ""  # A reference to an unreadable note is not a finding.
    if re.search(r"\b(?:reversible|can be undone|easy to undo)\b", lower) and re.search(
            r"\b(?:act without asking|proceed without asking|don.t ask|do not ask)\b",
            lower) and not re.search(r"\b(?:never|do not|don.t) act\b", lower):
        return "Reversible work gets a green light"
    if not re.search(r"\b(?:unless|except|only if|when|if)\b", lower):
        if re.search(r"\b(?:do|drive|run|handle|complete)\b.{0,50}\byourself\b", lower):
            return "You put the AI in the driver's seat"
        if re.search(r"\b(?:keep going|keep working|continue autonomously|do not stop)\b",
                     lower) and not re.search(
                r"\b(?:never|do not|don.t) (?:keep|continue)\b", lower):
            return "You expect momentum without prompting"
        if re.search(r"\b(?:automat\w*|unattended|overnight|by itself)\b", lower) and not re.search(
                r"\b(?:never|not|don.t|avoid|no)\b.{0,25}\b(?:automat|unattended)", lower):
            return "You build things that run themselves"
        if re.search(r"\b(?:include|report|show|keep)\b.{0,25}\bfailures\b", lower):
            return "The failures belong in the results"
        if re.search(r"\b(?:concise|short|brief)\b", lower) and re.search(
                r"\b(?:plain|everyday|jargon)\b", lower):
            return "Fewer words. Human language."
        if re.search(r"\b(?:look|inspect|review)\b.{0,40}\b(?:yourself|own)\b",
                     lower) and re.search(
                r"\b(?:visual|screenshots?|design)\b", lower):
            return "You make AI look at its own work"
    # Never cut a condition, a negation or a sentence in half to make it fit.
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    conditioned = re.search(r"\b(?:unless|except|only if|when|if)\b", lower)
    if conditioned and first != text:
        return ""
    if 2 <= len(first.split()) <= 12 and len(first) <= 90:
        return first.rstrip(".! ")
    return ""


def interest(statement: str, *, conversations: int = 1, corrections: int = 0,
             projects: int = 1) -> float:
    """Prefer repeated, distinctive behaviour over routine implementation rules."""
    family = _family(statement)
    rare = bool(family) and SHARE.get(family, 1.0) < RARE_SHARE
    routine = bool(re.search(r"\b(?:pytest|ruff|mypy|pathlib|pep ?8|type hints?|readme)\b",
                             statement, re.IGNORECASE))
    return ((conversations + min(corrections, 8) * 2) * (1.5 if projects >= 2 else 1)
            * (1.8 if rare else 1) * (0.25 if routine else 1))


def traits(rules: Sequence[Principle], turns: dict[str, Turn],
           embed: Embed | None = None, *, limit: int = TRAITS) -> tuple[Trait, ...]:
    """The few rules that stand out, each a group of the rules that say it.

    Rules group by the point they make; groups whose leading rules make one point
    merge into one trait, so the traits shown are distinct points.
    """
    evidence = {rule.id: _owned(rule, turns) for rule in rules}

    def strength(rule: Principle) -> tuple[int, int, str]:
        threads = {turn.thread_id for _, turn in evidence[rule.id]}
        return (-len(threads), -len(rule.provenance.get("corrections") or []), rule.id)

    ranked = sorted((rule for rule in rules if evidence[rule.id]), key=strength)[:MAX_RULES]
    names = {rule.scope for rule in ranked} | {turn.scope for turn in turns.values()}
    pointed = _pointed([rule.statement for rule in ranked], names)
    similar, same, related = _similarity(pointed, embed)

    def leader(members: list[int]) -> Principle:
        # Among the strongest few, a rule for every project names the trait best.
        general = (index for index in members[:LEADERS]
                   if ranked[index].scope == GLOBAL_SCOPE
                   and pointed[index] == " ".join(ranked[index].statement.split()))
        return ranked[next(general, members[0])]

    def score(members: list[int]) -> float:
        group = [ranked[index] for index in members]
        said = [turn for rule in group for _, turn in evidence[rule.id]]
        projects = {_project(turn.scope) for turn in said} - {""}
        threads = {turn.thread_id or turn.entry_id for turn in said}
        lead = leader(members)
        # A way of working seen across projects says more about the owner than one
        # project's house style.
        wide = len(projects) >= 2 or lead.scope == GLOBAL_SCOPE
        return interest(lead.statement, conversations=len(threads),
                        corrections=len(_corrected(group, turns)),
                        projects=2 if wide else 1)

    merged: list[list[int]] = []
    for members in sorted(_groups(len(ranked), similar, same),
                          key=lambda members: (-score(members), ranked[members[0]].id)):
        lead_index = members[0]
        home = next((trait for trait in merged if similar[trait[0]][lead_index] >= related), None)
        if home is None:
            merged.append(list(members))
        else:
            home.extend(members)
    found: list[tuple[float, Trait]] = []
    for members in merged:
        group = [ranked[index] for index in members]
        said = [item for rule in group for item in evidence[rule.id]]
        threads = {turn.thread_id or turn.entry_id for _, turn in said}
        if len(threads) < MIN_TRAIT_CONVERSATIONS:
            continue
        projects = Counter(_project(turn.scope) for _, turn in said)
        projects.pop("", None)
        lead = leader(members)
        title = headline(lead.statement)
        if not title:
            quote = _best_quote(said, lead.statement)
            title = headline(quote.text) if quote else ""
        if not title:
            continue
        family = _family(lead.statement)
        rare = bool(family) and SHARE.get(family, 1.0) < RARE_SHARE
        found.append((score(members), Trait(
            id=lead.id, statement=lead.statement,
            rule_ids=tuple(rule.id for rule in group), conversations=len(threads),
            projects=tuple(name for name, _ in projects.most_common()),
            corrections=len(_corrected(group, turns)),
            quote=_best_quote(said, lead.statement),
            family=family, family_label=LABELS.get(family, ""),
            rare=share_text(family) if rare else "", on_trial=lead.trial, headline=title,
        )))
    found.sort(key=lambda item: (-item[0], item[1].id))
    return tuple(trait for _, trait in found[:limit])


def _corrected(group: Iterable[Principle], turns: dict[str, Turn]) -> set[str]:
    """The owner's messages that corrected an agent on any of these rules."""
    return {str(item.get("entry_id")) for rule in group
            for item in rule.provenance.get("corrections") or []
            if isinstance(item, dict) and str(item.get("entry_id")) in turns}


def _corrections(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every correction a reading found, once per message, superseded rows included."""
    marks = ", ".join("?" * len(MODEL_SOURCES))
    found: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        f"SELECT provenance_json FROM owner_principles WHERE source IN ({marks})",  # noqa: S608
        MODEL_SOURCES,
    )
    for row in rows:
        try:
            provenance = json.loads(row[0] or "{}")
        except ValueError:
            continue
        for item in provenance.get("corrections") or []:
            if isinstance(item, dict) and item.get("entry_id"):
                found.setdefault(str(item["entry_id"]), item)
    return found


def _pushed(text: str, pattern: re.Pattern[str]) -> tuple[bool, str | None]:
    """Whether a sentence of the owner's pushes back this way, and the line to quote.

    Pushback is said in a short sentence ("still not fixed."); the same words in a
    long one are usually a task brief or pasted text ("fix anything that doesn't
    work"), so they do not count.
    """
    for sentence in re.split(r"(?<=[.!?\n])\s+", text.strip()):
        if len(sentence.split()) <= PUSHBACK_WORDS and pattern.search(sentence):
            return True, _short(sentence, (3, 18))
    return False, None


class Said(NamedTuple):
    """One message where the owner pushed back, and the line to quote from it."""
    turn: Turn
    line: str | None
    # The line itself says it ("still doesn't work"); a reading's quote may not.
    own: bool


def habits(turns: Sequence[Turn], corrections: dict[str, dict[str, Any]]
           ) -> tuple[dict[str, list[Said]], set[str]]:
    """Each kind's pushback messages, and every message that pushed back."""
    by_id = {turn.entry_id: turn for turn in turns}
    found: dict[str, list[Said]] = defaultdict(list)
    pushback: set[str] = set()
    for turn in turns:
        correction = corrections.get(turn.entry_id) or {}
        described = " ".join(str(correction.get(key) or "") for key in ("agent_did", "wanted"))
        for kind in KINDS:
            pushed, line = _pushed(turn.text, kind.owner)
            if pushed:
                found[kind.key].append(Said(turn, line, True))
            elif described.strip() and kind.agent.search(described):
                quote = _short(str(correction.get("quote") or ""), (3, 18))
                found[kind.key].append(Said(turn, quote, False))
            else:
                continue
            pushback.add(turn.entry_id)
    pushback.update(entry for entry in corrections if entry in by_id)
    return found, pushback


def _habit(kind: Kind, said: list[Said]) -> Habit:
    threads = {item.turn.thread_id or item.turn.entry_id for item in said}
    projects = Counter(_project(item.turn.scope) for item in said)
    projects.pop("", None)
    dates = sorted(_date(item.turn.occurred) for item in said if item.turn.occurred)
    quotes: list[Quote] = []
    seen_threads: set[str | None] = set()
    seen_text: set[str] = set()
    # Lines that say it themselves first, then most recent, one per conversation,
    # a new project before a repeat.
    ordered = sorted(said, key=lambda item: _date(item.turn.occurred), reverse=True)
    ordered.sort(key=lambda item: not item.own)
    for prefer_new in (True, False):
        for turn, line, _ in ordered:
            if len(quotes) == 3:
                break
            if not line or turn.thread_id in seen_threads or line.casefold() in seen_text:
                continue
            if prefer_new and _project(turn.scope) in {quote.project for quote in quotes}:
                continue
            quotes.append(Quote(line, _date(turn.occurred), _project(turn.scope)))
            seen_threads.add(turn.thread_id)
            seen_text.add(line.casefold())
    return Habit(
        kind=kind.key, title=kind.title, rule=kind.rule, times=len(said),
        conversations=len(threads), projects=tuple(name for name, _ in projects.most_common()),
        first=dates[0] if dates else "", last=dates[-1] if dates else "", quotes=tuple(quotes),
    )


def build(conn: sqlite3.Connection, *, embed: Embed | None = None,
          cloud: bool = False) -> Portrait:
    """The portrait of the archive at ``conn``. ``cloud`` limits it to turns a cloud
    model may read, as the reading did."""
    turns = owner_turns(conn, MiningStats(), cloud=cloud, min_words=PUSHBACK_MIN_WORDS)
    by_id = {turn.entry_id: turn for turn in turns}
    store = SQLitePrincipleStore(conn)
    candidates = store.current("candidate")
    confirmed = store.current("confirmed")
    rules = [rule for rule in (*confirmed, *candidates)
             if rule.source in MODEL_SOURCES and _slot(rule) in TRAIT_SLOTS]
    pushed, pushback = habits(turns, _corrections(conn))
    habit = None
    ranked = sorted(
        (kind for kind in KINDS if pushed.get(kind.key)),
        key=lambda kind: (-_conversations(pushed[kind.key]), KINDS.index(kind)),
    )
    habit_rules: set[str] = set()
    if ranked:
        top = _habit(ranked[0], pushed[ranked[0].key])
        if top.conversations >= MIN_HABIT_CONVERSATIONS:
            # The rules already written against it are the habit again, not traits.
            against = tuple(rule.id for rule in rules if ranked[0].against.search(rule.statement))
            habit, habit_rules = replace(top, rule_ids=against), set(against)
    projects: dict[str, set[str]] = defaultdict(set)
    for turn in turns:
        if _project(turn.scope):
            projects[turn.scope].add(turn.thread_id or turn.entry_id)
    named = sorted(((name, len(threads)) for name, threads in projects.items()
                    if len(threads) >= MIN_PROJECT_CONVERSATIONS), key=lambda item: -item[1])
    dates = [_date(turn.occurred) for turn in turns if turn.occurred]
    return Portrait(
        messages=len(turns),
        conversations=len({turn.thread_id or turn.entry_id for turn in turns}),
        since=min(dates) if dates else "",
        projects=tuple(named),
        pushback=len(pushback),
        traits=traits([rule for rule in rules if rule.id not in habit_rules], by_id, embed),
        habit=habit,
        on_trial=sum(1 for rule in confirmed if rule.trial),
        waiting=sum(1 for rule in candidates if rule.source in MODEL_SOURCES),
        kinds={kind: _conversations(said) for kind, said in sorted(pushed.items())},
    )


def _conversations(said: Iterable[Said]) -> int:
    return len({item.turn.thread_id or item.turn.entry_id for item in said})


def embedder(cfg: Any) -> Embed | None:
    """Group by meaning with the archive's own embedding model, once it is downloaded.

    The portrait never starts a download: without the model, shared words group.
    """
    try:
        from pathlib import Path

        from varyz.pipeline.embed import Embedder

        model = Embedder(cfg)
        if not any(Path(model.cache_dir).glob("models--*")):
            return None
        return model.embed_documents
    except Exception:  # noqa: BLE001 - the portrait falls back to shared words
        return None


__all__ = [
    "BY_KIND", "KINDS", "Embed", "Habit", "Kind", "Portrait", "Quote", "Said", "Trait",
    "build", "embedder", "habits", "traits",
]

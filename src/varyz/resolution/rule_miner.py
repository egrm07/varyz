"""Find standing rules in the owner's own words, for the owner to confirm.

Two places hold them. The owner's turns to coding agents, where a correction
("no — always run it in dry-run first") states how they want work done; and the
instruction files they wrote for agents (CLAUDE.md, AGENTS.md, memory notes).

This is a lexical detector, deliberately: it only *suggests*. Every hit becomes a
candidate the owner accepts, rewords, rescopes or rejects in `varyz rules review`,
and nothing reaches an agent until then. Precision is measured by that review's
acceptance rate, not asserted here. Sensitive entries are never read, and only
turns varyz can show the owner authored are considered — an assistant's summary
of what it thinks the owner wants is not the owner saying so.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varyz.resolution.knowledge import stored_scope
from varyz.resolution.principles import GLOBAL_SCOPE, normalize_statement

MIN_WORDS = 4
MAX_WORDS = 40
MIN_SCORE = 3
DEFAULT_CAP = 200
MINED_TURN_SOURCE = "mined:ai_conversations"
MINED_FILE_SOURCE = "mined:file"

# How work should always be done: the strongest sign of a standing rule.
_GENERAL = re.compile(
    r"\b(always|never|from now on|going forward|every time|each time|by default|"
    r"in general|as a rule|whenever|no matter)\b",
    re.IGNORECASE,
)
# An instruction or prohibition.
_NORMATIVE = re.compile(
    r"\b(do not|don't|dont|stop|avoid|must|make sure|ensure|no more|should|"
    r"shouldn't|has to|have to|only)\b",
    re.IGNORECASE,
)
# The owner stating a preference in the first person.
_PREFERENCE = re.compile(
    r"\b(i (?:want|prefer|like|need|expect|hate|don't want|dont want|would rather)|"
    r"i'd rather|prefer)\b",
    re.IGNORECASE,
)
# ("I need to" scores once, as a preference: "I need to go." is not a rule.)
# A question typed without its "?": "What do i need to do", "How long does it take".
_QUESTION_OPENER = re.compile(
    r"^\s*(what|how|which|where|why|who|is|are|does|did|can|could|would|will|should)\b",
    re.IGNORECASE,
)
# "For now" or "yet" is an instruction about the moment, not a standing rule.
_TEMPORARY = re.compile(r"\b(for now|right now|for the moment|at the moment|yet)\b", re.IGNORECASE)
# A contrast with what was done: weaker, since it is often about one choice.
_CONTRAST = re.compile(r"\b(instead of|rather than|instead)\b", re.IGNORECASE)
# A turn that opens by correcting the agent.
_CORRECTION_OPENER = re.compile(
    r"^\s*(no\b|nope\b|actually\b|wait\b|instead\b|not like that|that's wrong|"
    r"thats wrong|wrong\b|stop\b|correction\b|don't\b|dont\b)",
    re.IGNORECASE,
)
# Filler that opens a correction but is not part of the rule.
_LEADING_FILLER = re.compile(
    r"^\s*(?:(?:no|nope|actually|wait|ok(?:ay)?|so|also|and|but|correction|please|pls)"
    r"\s*[,:;.!—–-]*\s+)+",
    re.IGNORECASE,
)
# One-off references: a rule does not point at "this file" or "that line".
_DEICTIC = re.compile(
    r"\b(this|that|these|those|it|here|there) (file|line|function|one|bug|error|test|"
    r"commit|pr|branch|step|part|thing)\b",
    re.IGNORECASE,
)
_CODEISH = re.compile(r"(```|\{|\}|=>|::|\(\)|^\s*[$>#])")
# A sentence may end inside emphasis: "**Privacy is fail-closed.** Sensitive ..."
_SENTENCE_END = re.compile(r"(?<=[.!?])[*_]*\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TRUNCATION = "… truncated by varyz …"
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_CONVERSATION_HEADER = re.compile(r"^Conversation: [^\n]*? — (?P<where>[^\n]+)$", re.MULTILINE)


@dataclass(frozen=True)
class RuleCandidate:
    statement: str
    scope: str
    score: int
    source: str
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class MiningStats:
    """Aggregate counts only: safe to print on live data."""

    turns_read: int = 0
    files_read: int = 0
    hits: int = 0
    candidates: int = 0
    duplicates: int = 0
    capped: int = 0
    sensitive_skipped: int = 0
    # Only when a model reads the turns (``profile_reader``): items it gave that
    # failed the checks, tasks it answered malformed, turns left for the next run
    # by the cloud call cap, and turns skipped as a prompt pasted in many sessions.
    dropped: int = 0
    unread: int = 0
    deferred: int = 0
    pasted: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def score_sentence(sentence: str, *, opens_correction: bool = False) -> int:
    """How strongly one sentence reads as a standing rule; 0 when it cannot be one."""
    words = sentence.split()
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        return 0
    if sentence.rstrip().endswith("?") or _CODEISH.search(sentence):
        return 0
    if _QUESTION_OPENER.match(sentence) or _TEMPORARY.search(sentence):
        return 0
    # A lead-in ("Cross-project patterns (what I always do):") introduces rules.
    if sentence.rstrip(" \t*_`").endswith(":"):
        return 0
    if "http://" in sentence or "https://" in sentence:
        return 0
    score = 0
    if _GENERAL.search(sentence):
        score += 3
    if _NORMATIVE.search(sentence):
        score += 2
    if _PREFERENCE.search(sentence):
        score += 2
    if _CONTRAST.search(sentence):
        score += 1
    if score == 0:
        return 0
    if opens_correction:
        score += 1
    if _DEICTIC.search(sentence):
        score -= 2
    return max(score, 0)


def clean_statement(sentence: str) -> str:
    sentence = _BULLET.sub("", sentence)
    sentence = _LEADING_FILLER.sub("", sentence)
    sentence = sentence.strip(" \t\"'*_`")
    sentence = " ".join(sentence.split())
    if sentence[:1].islower():
        sentence = sentence[0].upper() + sentence[1:]
    return sentence


def sentences(text: str) -> Iterator[str]:
    text = text.replace(_TRUNCATION, " ")
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        if part:
            yield part


def rules_in_text(
    text: str, *, correction_turn: bool = False
) -> Iterator[tuple[str, int]]:
    """(statement, score) for each sentence of ``text`` that reads as a rule."""
    opens_correction = correction_turn and bool(_CORRECTION_OPENER.match(text))
    for index, sentence in enumerate(sentences(text)):
        score = score_sentence(sentence, opens_correction=opens_correction and index == 0)
        if score >= MIN_SCORE:
            statement = clean_statement(sentence)
            if len(statement.split()) >= MIN_WORDS:
                yield statement, score


def _turn_scope(body: str) -> tuple[str, str]:
    """(scope, text) from an ai_conversations body: its project, and the turn's words."""
    header, _, text = body.partition("\n\n")
    match = _CONVERSATION_HEADER.search(header)
    where = match.group("where").strip() if match else ""
    # A Claude Code session outside a known cwd is named by its mangled folder
    # (C--Users-...); that is not a project name the owner would scope a rule by.
    # The folder's version is not part of the project: san7.13 and san7.14 are san7.
    # A session in no project was once named by its id, which names nothing either.
    named = (where and "--" not in where and "/" not in where
             and not _SESSION_ID.fullmatch(where))
    scope = stored_scope(where) if named else GLOBAL_SCOPE
    # Recheck older stored rows too, before either deterministic or model-based
    # mining can learn preferences from generated tool/reviewer transcripts.
    from varyz.connectors.ai_conversations import is_machine_text

    if is_machine_text(text):
        text = ""
    return scope, text


def mine_turns(
    conn: sqlite3.Connection, stats: MiningStats
) -> Iterator[RuleCandidate]:
    """Candidates from the owner's own, non-sensitive turns to coding agents."""
    stats.sensitive_skipped += int(conn.execute(
        """SELECT count(*) FROM entries WHERE source = 'ai_conversations'
           AND authored_by_user = 1 AND sensitivity = 'sensitive'"""
    ).fetchone()[0])
    rows = conn.execute(
        """SELECT id, body, thread_id, occurred_start FROM entries
           WHERE source = 'ai_conversations' AND authored_by_user = 1
             AND sensitivity != 'sensitive'
           ORDER BY occurred_start"""
    )
    for row in rows:
        stats.turns_read += 1
        scope, text = _turn_scope(str(row["body"]))
        for statement, score in rules_in_text(text, correction_turn=True):
            stats.hits += 1
            yield RuleCandidate(
                statement=statement,
                scope=scope,
                score=score,
                source=MINED_TURN_SOURCE,
                provenance={
                    "entry_id": row["id"],
                    "thread_id": row["thread_id"],
                    "occurred": row["occurred_start"],
                },
            )


# Markdown under these is somebody else's (dependencies, environments, tool state).
_NOT_THE_OWNERS = frozenset({"node_modules", "venv", "site-packages", "vendor", "__pycache__"})


def _markdown_files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(
                p for p in path.rglob("*.md")
                if p.is_file() and not any(
                    part.startswith(".") or part in _NOT_THE_OWNERS
                    for part in p.relative_to(path).parts[:-1]
                )
            )
        elif path.is_file():
            yield path


def mine_files(
    paths: Iterable[Path], stats: MiningStats, *, scope: str = GLOBAL_SCOPE
) -> Iterator[RuleCandidate]:
    """Candidates from instruction files the owner wrote for agents."""
    for path in _markdown_files(paths):
        stats.files_read += 1
        for number, paragraph in _paragraphs(path.read_text(encoding="utf-8")):
            # A file of instructions is written as rules: no correction needed.
            for statement, score in rules_in_text(paragraph):
                stats.hits += 1
                yield RuleCandidate(
                    statement=statement,
                    scope=scope,
                    score=score,
                    source=MINED_FILE_SOURCE,
                    provenance={"file": str(path), "line": number},
                )


def _paragraphs(text: str) -> Iterator[tuple[int, str]]:
    """(first line number, text) of each prose paragraph or list item.

    Instruction files are hard-wrapped, so a rule read line by line loses its
    first or last words at the wrap. Headings, tables, quotes, comments, rules
    and fenced code are not prose and are dropped.
    """
    start, lines = 0, []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        prose = bool(stripped) and not (
            in_fence or stripped.startswith(("```", "#", "|", ">", "<!--", "---"))
        )
        if lines and (not prose or _BULLET.match(line)):
            yield start, " ".join(lines)
            lines = []
        if prose:
            if not lines:
                start = number
            lines.append(stripped)
    if lines:
        yield start, " ".join(lines)


def select(
    candidates: Iterable[RuleCandidate], stats: MiningStats, *, cap: int = DEFAULT_CAP
) -> list[RuleCandidate]:
    """One candidate per wording, strongest first, at most ``cap``."""
    best: dict[str, RuleCandidate] = {}
    for candidate in candidates:
        key = normalize_statement(candidate.statement)
        kept = best.get(key)
        if kept is None:
            best[key] = candidate
        else:
            stats.duplicates += 1
            if candidate.score > kept.score:
                best[key] = candidate
    ranked = sorted(best.values(), key=lambda item: -item.score)
    stats.capped += max(0, len(ranked) - cap)
    chosen = ranked[: max(0, cap)]
    stats.candidates = len(chosen)
    return chosen


__all__ = [
    "MINED_FILE_SOURCE",
    "MINED_TURN_SOURCE",
    "MiningStats",
    "RuleCandidate",
    "clean_statement",
    "mine_files",
    "mine_turns",
    "rules_in_text",
    "score_sentence",
    "select",
]

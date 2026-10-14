"""The owner's picks in an agent's questions, recorded as that project's decisions.

When Claude Code asks the owner a multiple-choice question (AskUserQuestion),
the transcript keeps both the question and the option they picked. That pick is
a decision in the owner's own hand, and without this module varyz only ever
learned the ones it had relayed itself: everything settled in an ordinary
session was lost, and an answer the owner later changed stayed current.

The owner chose this on 2026-09-25 ("Yes, record them", for all projects), so
autosync records every pick as a user-confirmed answer scoped to the session's
project, and a newer pick to the same question supersedes the older answer.

Four things are never recorded:

- a question varyz relayed itself (header ``varyz``): the agent records that one
  with ``answer_owner_question`` and the reach the owner chose. The same goes for
  the questions around a relay: where an answer applies ("Just for varyz") and
  whether to remember a rule, which ``remember_owner_rule`` records;
- a declined question, or one answered "Skip for now";
- a subagent's sidechain line;
- anything already recorded from the same tool call, including an answer the owner
  retracted since. The source names the call, so a retraction is never undone by
  a rescan.

The question text reaches agents again as an answer. Transcripts are
``sensitive`` as a source, but this pair was written by an agent and sent to its
provider when it was asked. No other transcript text is read.

Only Claude Code transcripts are read. Codex has no equivalent recorded tool.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from varyz.connectors.ai_conversations import ADAPTERS_BY_ID
from varyz.connectors.base import expand
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.knowledge import (
    _CURRENT,
    GLOBAL_SCOPE,
    OWNER_ID,
    SQLiteKnowledgeStore,
    fold_stored_scopes,
    normalize_question,
    normalize_scope,
)
from varyz.resolution.models import Caller

SOURCE_PREFIX = "claude_code_pick"
# The relay header varyz asks agents to use (mcp_server.py): those answers are
# recorded by answer_owner_question with the reach the owner picked.
_VARYZ_HEADER = "varyz"
_SKIPPED = frozenset({"skip for now", "skip", ""})
_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*", re.IGNORECASE)
# A question about where another answer applies, not a decision of its own.
_REACH_ANSWER = re.compile(
    r"^(?:always,?\s+)?(?:in\s+)?all\s+(?:my\s+|your\s+)?projects\b|^just\s+for\b"
    r"|^(?:this|for\s+this)\s+project\s+only\b|^only\s+(?:here|for\b)",
    re.IGNORECASE,
)
# An offer to keep a rule; accepted ones are recorded as rules, not decisions.
_RULE_OFFER = re.compile(r"^should\s+vary[sz]\s+remember\b", re.IGNORECASE)
# A pick is recorded at most this often per run; the rest wait for the next one.
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class Pick:
    """One question the owner answered by picking, where and when they did."""

    question: str
    answer: str
    project: str | None
    answered_at: str | None
    source: str


@dataclass
class CaptureStats:
    files_read: int = 0
    picks_found: int = 0
    recorded: int = 0
    superseded: int = 0
    already_known: int = 0
    older_than_current: int = 0
    no_project: int = 0
    deferred: int = 0
    recorded_picks: list[Pick] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("recorded_picks")
        return data

    def seen(self, source: str) -> bool:
        """Whether this run already handled a call: a resumed session repeats it."""
        return any(pick.source == source for pick in self.recorded_picks)


def _clean_answer(answer: str) -> str:
    return ", ".join(
        part for part in (_RECOMMENDED.sub(" ", item).strip() for item in answer.split(", "))
        if part
    )


def picks_in_line(record: dict[str, Any]) -> list[Pick]:
    """The picks one transcript line carries, or none.

    The answers come from ``toolUseResult``, which the harness writes with the
    tool result: the questions as asked and the option the owner chose for each.
    """
    if record.get("isSidechain"):
        return []
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return []  # declined, or not a question at all
    answers = result.get("answers")
    questions = result.get("questions")
    if not isinstance(answers, dict) or not isinstance(questions, list):
        return []
    if any(
        isinstance(item, dict) and str(item.get("header", "")).strip().lower() == _VARYZ_HEADER
        for item in questions
    ):
        return []
    tool_use_id = next(
        (
            block.get("tool_use_id")
            for block in (record.get("message") or {}).get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ),
        None,
    )
    if not tool_use_id:
        return []
    cwd = record.get("cwd")
    # Either separator, whichever machine wrote the transcript.
    project = PureWindowsPath(str(cwd)).name or None if cwd else None
    annotations = result.get("annotations") if isinstance(result.get("annotations"), dict) else {}
    picks = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        raw = answers.get(question, "")
        # A multi-select pick is a list in newer transcripts, a joined string in older.
        answer = _clean_answer(
            ", ".join(str(item) for item in raw) if isinstance(raw, list) else str(raw)
        )
        if not question or answer.lower() in _SKIPPED:
            continue
        if _REACH_ANSWER.match(answer) or _RULE_OFFER.match(question):
            continue
        note = (annotations.get(question) or {}).get("notes") if annotations else None
        if isinstance(note, str) and note.strip():
            answer = f"{answer} (note: {note.strip()})"
        picks.append(
            Pick(
                question=question,
                answer=answer,
                project=project,
                answered_at=record.get("timestamp"),
                # The call id, not the session: a resumed session copies its lines.
                source=f"{SOURCE_PREFIX}:{tool_use_id}:{index}",
            )
        )
    return picks


def _default_home() -> Path:
    """Where the agents keep transcripts by default; the test suite points it away."""
    return Path.home()


def transcript_files(options: dict[str, Any], home: Path | None = None) -> list[Path]:
    """The Claude Code transcripts the ai_conversations connector may read.

    The same reach as the connector: its approval, its adapters, and its
    configured paths, which replace the default location.
    """
    if not options.get("approved", False):
        return []
    adapters = options.get("adapters")
    if adapters and "claude_code" not in {str(name).strip().lower() for name in adapters}:
        return []
    adapter = ADAPTERS_BY_ID["claude_code"]
    configured = [expand(str(p)) for p in options.get("paths", []) if str(p).strip()]
    if home is None:
        home = Path(str(options["_home"])) if options.get("_home") else _default_home()
    roots = [p for p in configured if p.is_dir()] if configured else adapter.roots(
        home.expanduser()
    )
    files: list[Path] = []
    for root in roots:
        for path in sorted(root.glob(adapter.file_glob)):
            if path.is_file() and not path.name.startswith((".", "rollout-")):
                files.append(path)
    return files


def read_new_picks(
    files: Iterable[Path], offsets: dict[str, int]
) -> Iterator[tuple[str, int, list[Pick]]]:
    """Picks written since each file's last offset: (file, new offset, picks).

    Only whole lines are consumed, so a line the harness is still writing is
    read next time. A file shorter than its offset was rewritten and is read
    from the start; the source names each call, so nothing is recorded twice.
    """
    for path in files:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = offsets.get(key, 0)
        if start > size:
            start = 0
        if start == size:
            continue
        picks: list[Pick] = []
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(size - start)
        end = data.rfind(b"\n")
        if end < 0:
            continue
        for raw in data[: end + 1].splitlines():
            # Cheap filter first: one session is over 100 MB of other traffic.
            if b'"toolUseResult"' not in raw or b'"answers"' not in raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                picks.extend(picks_in_line(record))
        yield key, start + end + 1, picks


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Transcripts write UTC; a time without an offset is read as UTC, never local.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def record_picks(
    conn: sqlite3.Connection,
    picks: Iterable[Pick],
    *,
    caller: Caller,
    device_id: str,
    stats: CaptureStats,
    apply: bool,
    limit: int = DEFAULT_LIMIT,
) -> None:
    """Record each pick as its project's decision; a newer pick supersedes.

    Without ``apply`` nothing is written and ``stats`` says what would be.
    """
    fold_stored_scopes(conn)
    for pick in picks:
        stats.picks_found += 1
        if not pick.project:
            stats.no_project += 1
            continue
        if stats.seen(pick.source) or conn.execute(
            "SELECT 1 FROM owner_knowledge WHERE source = ?", (pick.source,)
        ).fetchone() is not None:
            stats.already_known += 1
            continue
        scope = normalize_scope(pick.project)
        if scope == GLOBAL_SCOPE:
            stats.no_project += 1
            continue
        current = conn.execute(
            f"""SELECT knowledge.id, knowledge.answer, knowledge.stated_at
                FROM owner_knowledge AS knowledge
                WHERE knowledge.normalized_question = ? AND varyz_scope(knowledge.scope) = ?
                  AND knowledge.origin = 'own' AND {_CURRENT}
                ORDER BY knowledge.monotonic_seq DESC LIMIT 1""",
            (normalize_question(pick.question), scope),
        ).fetchone()
        supersedes = None
        if current is not None:
            if str(current[1]).strip().casefold() == pick.answer.casefold():
                stats.already_known += 1
                continue
            stated, answered = _parse_time(str(current[2])), _parse_time(pick.answered_at)
            if stated is not None and (answered is None or answered <= stated):
                # A rescan of an old session must not undo a later answer.
                stats.older_than_current += 1
                continue
            supersedes = str(current[0])
        if stats.recorded >= limit:
            stats.deferred += 1
            continue
        if apply:
            # Stated when the owner picked it, so a later pick is the one that holds.
            answered = _parse_time(pick.answered_at)
            store = (
                SQLiteKnowledgeStore(conn, clock=lambda at=answered: at)
                if answered is not None
                else SQLiteKnowledgeStore(conn)
            )
            store.remember(
                question=pick.question,
                answer=pick.answer,
                caller=caller,
                basis=AnswerBasis.USER_CONFIRMED,
                source=pick.source,
                device_id=device_id,
                supersedes_id=supersedes,
                scope=scope,
                author_id=OWNER_ID,
            )
        stats.recorded += 1
        stats.superseded += supersedes is not None
        stats.recorded_picks.append(pick)


def load_offsets(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return {str(k): int(v) for k, v in files.items()} if isinstance(files, dict) else {}


def save_offsets(path: Path, offsets: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"files": offsets}, indent=1), encoding="utf-8")
    temporary.replace(path)


def capture(
    conn: sqlite3.Connection,
    options: dict[str, Any],
    *,
    offsets_path: Path,
    caller: Caller,
    device_id: str,
    apply: bool,
    limit: int = DEFAULT_LIMIT,
    home: Path | None = None,
) -> CaptureStats:
    """Read the transcripts' new lines and record the picks in them.

    Offsets advance only on ``apply``, and only past files whose picks were all
    handled, so a dry run changes nothing and a capped run resumes where it
    stopped.
    """
    stats = CaptureStats()
    offsets = load_offsets(offsets_path) if apply else {}
    for key, offset, picks in read_new_picks(transcript_files(options, home), offsets):
        stats.files_read += 1
        deferred = stats.deferred
        record_picks(
            conn, picks, caller=caller, device_id=device_id, stats=stats,
            apply=apply, limit=limit,
        )
        if apply and stats.deferred == deferred:
            offsets[key] = offset
    if apply:
        save_offsets(offsets_path, offsets)
    return stats


__all__ = [
    "DEFAULT_LIMIT",
    "SOURCE_PREFIX",
    "CaptureStats",
    "Pick",
    "capture",
    "picks_in_line",
    "read_new_picks",
    "record_picks",
    "transcript_files",
]

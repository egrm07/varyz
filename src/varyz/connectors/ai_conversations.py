"""AI conversation histories — Claude Code, Codex, OpenClaw, and Hermes.

The hero Personal Insight source. Everything the user has already explained to
an agent — what they are building, what they decided, what they refuse to do,
what they keep coming back to — is sitting in local JSONL transcripts that no
future agent can read. This connector turns those into evidence.

Two rules shape the whole module, and both exist because the Mirror is an
interpretation *of the user*:

**An agent's words are never the user's beliefs.** Only turns the person
actually typed carry ``authored_by_user``. That is not a formality — in every
one of these formats a transcript is mostly assistant prose by volume, so a
values section that counted words rather than authorship would describe the
model.

**A `role: "user"` line is not proof a user wrote it.** This is the trap. Claude
Code delivers tool results, hook output, command stdout, and injected system
reminders as ``role: "user"`` messages; Codex replays environment context the
same way. Taking the role field at face value would file a `git status` dump as
something Egor said. So a turn survives only if it still has real prose left
after the machine-generated wrappers are stripped, and tool traffic is dropped
structurally rather than filtered by pattern.

Tool calls, tool results, thinking blocks, and images are never ingested. They
are the bulk of the bytes (one local session is 117 MB), the least
interpretable evidence, and the likeliest place for a pasted credential or a
dumped file to be sitting.

Claude Code deletes transcripts after about a month but keeps every prompt the
user typed in ``~/.claude/history.jsonl``. For a session whose transcript was
never read, those prompts are ingested as its turns: the user's words only,
without pasted contents, slash commands or shell escapes. On the machine this
was built on that was 1686 of 1849 prompts, three months of the owner steering
agents that would otherwise be gone.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, ClassVar

from varyz.ai_text import clean_generated_context
from varyz.connectors.base import expand, register
from varyz.domain import ConversationMetadata, Cursor, Kind, NormalizedEntry, RawItem, TimePrecision

# A turn longer than this is a paste, not a thing somebody said. Kept rather
# than dropped — the beginning of a long paste is usually the user framing it —
# but bounded so one dumped log cannot become a hundred chunks.
MAX_TURN_CHARS = 12_000
_TRUNCATION_MARKER = "\n\n… truncated by varyz …"

# Below this a turn carries no text worth embedding: an empty line, a stray
# bracket. Deliberately *not* set high enough to drop "go", "ship it", or "no,
# revert" — brief instruction is characteristic evidence, and the Mirror's
# question about how someone works is partly answered by it.
MIN_TURN_CHARS = 2

# Short turns are real but weak evidence, so they stay searchable and stay out
# of the timeline's foreground.
_SHORT_TURN_CHARS = 24
_SIGNIFICANCE_AUTHORED = 0.62
_SIGNIFICANCE_AUTHORED_SHORT = 0.30
_SIGNIFICANCE_ASSISTANT = 0.20

# Wrappers the harness injects into a `role: "user"` message. None of it was
# typed by the person, and most of it is machine output that would otherwise be
# indexed as their words. Removed before the turn is judged empty, so a message
# that is *only* a wrapper disappears instead of arriving as a bare tag.
_INJECTED_TAGS = (
    r"system-reminder|local-command-caveat|local-command-stdout|local-command-stderr"
    r"|command-name|command-message|command-args|user-prompt-submit-hook"
    r"|environment_context|user_instructions|INSTRUCTIONS"
    # Reports the harness posts back into the conversation as if the person had
    # spoken: a finished background task, a command's output, a plugin
    # suggestion, an aborted turn. Measured on this machine, these opened 337 of
    # the 1,392 `ai_conversations` turns stored as his own words — 24% — and
    # `<task-notification>` alone accounted for 302 of them.
    r"|task-notification|bash-input|bash-stdout|bash-stderr"
    r"|recommended_plugins|turn_aborted|in-app-browser-context"
    # Codex automations wake a thread with a `<heartbeat>`, and a thread goal is
    # replayed on every continuation inside `<codex_internal_context>` - the
    # goal the person typed once, repeated 7 times on this machine as if said
    # again each time. The typed goal itself is ingested where it was typed.
    r"|heartbeat|codex_internal_context"
)
_INJECTED_BLOCKS = re.compile(
    # Attributes allowed: the harness writes `<in-app-browser-context
    # source="ambient-ui-state">`, and requiring a bare `<tag>` left that one
    # whole while removing its siblings.
    rf"<({_INJECTED_TAGS})(?:\s[^>]*)?>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
# The same tags when the harness emits them unclosed.
_INJECTED_OPEN = re.compile(
    rf"</?({_INJECTED_TAGS})(?:\s[^>]*)?>",
    re.IGNORECASE,
)

# A turn opening with one of those, where the tag is never closed, is machine
# output whole: there is no wrapper to strip, so stripping leaves the report
# behind as his sentence. Measured on this machine, 7 of the 302 task
# notifications arrived that way. Deliberately narrow — a *closed* opening is
# left to the strip above, because a reminder the harness prepends to something
# he genuinely typed also opens the turn, and dropping those would be the same
# defect inverted. Across every authored turn here, no closed opening had prose
# left after stripping; all 329 were machine output end to end.
_MACHINE_OPENING = re.compile(rf"^\s*<({_INJECTED_TAGS})(?:\s|>)", re.IGNORECASE)

# Content blocks that hold prose, by the several names these formats use for
# the same thing. Every other block type — tool_use, tool_result, thinking,
# image, redacted_thinking — is ignored on purpose.
_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text", "summary_text"}

# The opening line every one of these harnesses writes when it replays a
# context-compaction summary as a `role: "user"` message. Claude Code flags
# those lines (`isCompactSummary`) and they are dropped structurally; a Codex
# rollout carries no flag at all, so for that format the preamble is the only
# thing there is to recognise. Anchored at the start and never searched for
# mid-message, so a person writing *about* compaction keeps their sentence:
# measured across this machine's history, all 399 real occurrences sit at
# offset 0 and none elsewhere.
_REPLAYED_SUMMARY = "This session is being continued from a previous conversation"

# Bumped whenever a rule for what is not the person's speech changes, so a sync
# re-judges the turns stored under the older rules once (`withdrawn`).
AUTHORSHIP_RULES = 4

# Bumped whenever the rule for which folder names a project changes, so a sync
# reads every transcript once more and the natural-key upsert relabels the
# turns filed under the older rule. Version 1: a home folder, a scratch folder
# and a Codex chat folder name no project, and a turn carries the end of the
# agent's reply it answers.
PROJECT_RULES = 2

# How much of the agent's reply a person's turn carries: its end, where the
# agent says what it did or asks. "Spin it in all directions" means nothing
# without the spinner the agent just described.
_AFTER_CHARS = 300

# Lines the harness writes into the person's side of the conversation that are
# not wrapped in any tag. Measured in this machine's archive on 2026-09-26,
# before the rule existed: 68 interrupt markers and 30 bare Codex `AGENTS.md`
# headers (whose `<INSTRUCTIONS>` body is stripped above) were stored as his
# own words. Each must be the whole of what is left, so a sentence that quotes
# one is kept.
_HARNESS_LINE = re.compile(
    r"^(?:\[Request interrupted by user(?: for tool use)?\]"
    r"|# AGENTS\.md instructions for [^\n]*)$"
)

# A slash command that only drives the harness: a bare `/compact`, or a
# setting with its value (`/model fable`, `/effort ultracode`, `/cd SAN7.10`).
# Named, because the argument count alone cannot tell them apart: `/btw
# status?` is one word and a question he asked. On 2026-09-26 the archive held
# 85 of these as his words; `/btw`, `/goal` and `/plan` turns are kept.
_HARNESS_COMMAND_TURN = re.compile(
    r"^/(?:[\w:.-]+"
    r"|(?:model|effort|autocompact|compact|cd|usage|clear|fast|config|resume"
    r"|output-style|permissions|cost|status|theme|login|logout|exit|context"
    r"|rewind|add-dir|remote-control|mcp|hooks|agents|doctor|statusline|vim)"
    r"(?:\s+\S+){1,2})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Turn:
    """One thing somebody said, addressed to where it was said."""

    role: str  # 'user' | 'assistant'
    text: str
    turn_id: str
    session: str
    occurred: datetime | None
    project: str | None = None
    working_directory: str | None = None
    reply_to_id: str | None = None
    quote_target_id: str | None = None

    @property
    def authored_by_user(self) -> bool:
        return self.role == "user"


@dataclass(frozen=True)
class Adapter:
    """One agent's on-disk transcript format."""

    adapter_id: str
    label: str
    # Where the tool keeps histories by default. Globs, because OpenClaw stores
    # one session directory per agent and the set is not known in advance.
    root_globs: tuple[str, ...]
    file_glob: str = "**/*.jsonl"

    def roots(self, home: Path) -> list[Path]:
        found: list[Path] = []
        for pattern in self.root_globs:
            for path in sorted(home.glob(pattern)):
                if path.is_dir() and path not in found:
                    found.append(path)
        return found


ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        adapter_id="claude_code",
        label="Claude Code",
        root_globs=(".claude/projects",),
    ),
    Adapter(
        adapter_id="codex",
        label="Codex",
        root_globs=(".codex/sessions", ".codex/archived_sessions"),
        file_glob="**/rollout-*.jsonl",
    ),
    Adapter(
        adapter_id="openclaw",
        label="OpenClaw",
        root_globs=(
            ".openclaw/agents/*/sessions",
            ".config/openclaw/agents/*/sessions",
        ),
    ),
    Adapter(
        adapter_id="hermes",
        label="Hermes",
        root_globs=(
            ".hermes/sessions",
            ".hermes/conversations",
            ".config/hermes/sessions",
        ),
    ),
)

ADAPTERS_BY_ID = {adapter.adapter_id: adapter for adapter in ADAPTERS}


def detect_adapters(home: Path | None = None) -> list[tuple[Adapter, list[Path]]]:
    """Which agent histories this machine actually has, with their roots.

    Reads directory names only — never a transcript. Onboarding needs to offer
    the source before the user has approved reading any of it.
    """
    home = (home or Path.home()).expanduser()
    found = []
    for adapter in ADAPTERS:
        roots = adapter.roots(home)
        if roots:
            found.append((adapter, roots))
    return found


def _clean(text: str) -> str:
    """Strip the harness's wrappers, and discard the message entirely when the
    whole of it is machine output.

    Returning "" is how this module says *this was not speech*: every caller
    drops a turn with no prose left. A replayed compaction summary has no
    wrapper to strip — it is thousands of words of fluent first-person prose —
    so nothing less than dropping it works.
    """
    text = clean_generated_context(text)
    opening = _MACHINE_OPENING.match(text)
    if opening and not re.search(rf"</{re.escape(opening.group(1))}>", text, re.IGNORECASE):
        return ""
    text = _INJECTED_BLOCKS.sub(" ", text)
    text = _INJECTED_OPEN.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    if text.startswith(_REPLAYED_SUMMARY):
        return ""
    if _HARNESS_LINE.match(text) or _HARNESS_COMMAND_TURN.match(text):
        return ""
    return clean_generated_context(text)


def is_machine_text(text: str) -> bool:
    """True when a stored turn's words are harness output, not speech.

    The judgement `_clean` makes on the way in, for turns stored before one of
    its rules existed: `prune_machine_turns` finds them with it.
    """
    return not _clean(text)


def is_machine_entry(source: str, body: str | None) -> bool:
    """Recheck stored AI rows, including the connector's provenance header."""
    if source != "ai_conversations" or not body:
        return False
    header, separator, text = body.partition("\n\n")
    if not (separator and header.startswith("From: ") and "\nConversation: " in header):
        text = body
    return is_machine_text(text)


def _block_text(content: Any) -> str:
    """Prose from a message body, whatever shape it arrived in.

    A bare string is a plain turn. A list is content blocks, and only the text
    ones are read: a message whose blocks are all `tool_result` yields "" and
    is therefore dropped by the caller, which is the whole point.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _TEXT_BLOCK_TYPES and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 10_000_000_000:  # milliseconds
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _claude_code_turn(
    data: dict[str, Any], index: int, path: Path, context: dict[str, str]
) -> Turn | None:
    """One line of a Claude Code session file.

    `isMeta` marks harness-authored user lines (the slash-command caveat, the
    resumed-session banner). `isSidechain` marks a subagent transcript, where
    the "user" is the orchestrating model asking a worker to do something — a
    conversation the person never had, and one that would badly distort what
    they appear to care about.

    `isCompactSummary` is the one that reached the Mirror before it was caught.
    When a session runs out of context the harness writes the model's summary
    of it back as a `role: "user"` message — thousands of words of fluent
    first-person prose, no wrapper tags, nothing to strip. On this machine 75 of
    1,414 indexed turns were that (54 here, 21 more from Codex), and two of them
    arrived in `varyz mirror` as the owner's goals: the summary had quoted a
    test fixture, and the archive read the quote as something he wanted.
    `isVisibleInTranscriptOnly` is set alongside it on every one of the 200 in
    local history and never alone, so either flag is enough; both mean the line
    is display text, not speech. Formats that carry no such flag are caught by
    `_REPLAYED_SUMMARY` in `_clean` instead.
    """
    if data.get("type") not in {"user", "assistant"}:
        return None
    if data.get("isMeta") or data.get("isSidechain"):
        return None
    if data.get("isCompactSummary") or data.get("isVisibleInTranscriptOnly"):
        return None
    if data.get("entrypoint") in _PROGRAM_ENTRYPOINTS or context.get("machine"):
        return None
    if data.get("type") == "user" and _typed_by_program(data):
        if data.get("turnOrigin") == "sdk" and not context.get("opened"):
            context["machine"] = "1"
        return None
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = _clean(_block_text(message.get("content")))
    if not text:
        return None
    if role == "user":
        context["opened"] = "1"
    session = str(data.get("sessionId") or path.stem)
    cwd = data.get("cwd")
    return Turn(
        role=role,
        text=text,
        turn_id=str(data.get("uuid") or f"line{index}"),
        session=session,
        occurred=_timestamp(data.get("timestamp")),
        # A folder that names no project is not replaced by the transcript's folder.
        project=_project_name(cwd) if cwd else path.parent.name,
        working_directory=str(cwd) if cwd else None,
        reply_to_id=str(data["parentUuid"]) if data.get("parentUuid") else None,
    )


# `claude -p` and the Agent SDK stamp every line `entrypoint: "sdk-cli"`. The
# prompt in such a session was written by a program - a benchmark harness, a
# scheduled job, an agent driving another agent - not typed by the person, and
# on this machine the two found (2026-09-22) were benchmark prompts addressed
# *to* an advisor "acting for Egor". The interactive terminal writes "cli" and
# the desktop app "claude-desktop".
_PROGRAM_ENTRYPOINTS = frozenset({"sdk-cli", "sdk-ts", "sdk-py"})


def _typed_by_program(data: dict[str, Any]) -> bool:
    """A user line the person did not type, by what the line says of itself.

    `entrypoint` is inherited from the environment, so `claude -p` started from
    inside a desktop session - a script, a benchmark, an agent running another
    agent - writes "claude-desktop" on every line like the session around it.
    `turnOrigin` is set per prompt: "human" for one typed, "sdk" for one handed
    over by a program. Of 1,239 prompt lines on this machine (2026-09-26) four
    were "sdk" under "claude-desktop", all harness prompts, and none of the 147
    typed ones were. A session a program opened is dropped whole (the caller
    marks it); an "sdk" turn inside a typed session is dropped alone. `origin`
    names the rest: task notifications and goal auto-continuations.
    """
    if data.get("turnOrigin") == "sdk":
        return True
    origin = data.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind is not None and kind != "human"


# A prompt-history line that is a bare slash command ("/clear", "/model") or a
# shell escape ("! git status") drove the harness; it is not something said.
_HARNESS_COMMAND = re.compile(r"^(?:/[\w:.-]+\s*$|!)")


def _claude_code_history_turn(
    data: dict[str, Any], index: int, path: Path, context: dict[str, str]
) -> Turn | None:
    """One line of Claude Code's prompt history (`~/.claude/history.jsonl`).

    Claude Code deletes session transcripts after about 30 days, but keeps
    every prompt the person typed in this file: on this machine 1,686 of its
    1,849 prompts belonged to sessions whose transcripts were already gone.
    It holds only what was typed - `display`, with pastes reduced to a
    "[Pasted text #1]" placeholder - so it cannot carry tool traffic or a
    replayed summary. The pastes themselves (`pastedContents`) are never read.
    """
    session = data.get("sessionId")
    display = data.get("display")
    if not session or not isinstance(display, str):
        return None
    if _HARNESS_COMMAND.match(display.strip()):
        return None
    text = _clean(display)
    if not text:
        return None
    return Turn(
        role="user",
        text=text,
        turn_id=f"prompt-{data.get('timestamp', index)}",
        session=str(session),
        occurred=_timestamp(data.get("timestamp")),
        project=_project_name(data.get("project")),
    )


def _codex_turn(
    data: dict[str, Any], index: int, path: Path, context: dict[str, str]
) -> Turn | None:
    """One line of a Codex rollout file.

    Two generations of the format say the same thing twice: `response_item`
    carries the model-facing message, and `event_msg` carries the UI event for
    the same turn. Once a file has shown a `response_item`, its event stream is
    ignored for good — the fact is remembered in `context` and checkpointed,
    because a resume that begins between the two copies of one turn would
    otherwise ingest the second copy as a new memory. Files old enough to have
    only `event_msg` lines never set the flag and are read from it.

    Unlike Claude Code, a rollout states the working directory once — in the
    `session_meta` header — so the individual turns carry no clue about what
    the conversation was about. That header is remembered in `context` (and
    checkpointed, so a resumed file labels its turns the same way as a fresh
    scan) rather than every turn being labelled with a bare uuid.
    """
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = data
    kind = data.get("type") or payload.get("type")

    if kind == "session_meta":
        if payload.get("id"):
            context["session"] = str(payload["id"])
        if payload.get("cwd"):
            context["cwd"] = str(payload["cwd"])
            project = _project_name(payload["cwd"])
            if project:
                context["project"] = project
            else:
                context.pop("project", None)
        if _codex_program_session(payload):
            context["machine"] = "1"
        return None
    if context.get("machine"):
        return None

    session = str(
        payload.get("session_id")
        or data.get("session_id")
        or context.get("session")
        or _codex_session(path)
    )

    if payload.get("type") == "message" or kind == "response_item":
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            return None
        text = _clean(_block_text(payload.get("content")))
        context["canonical"] = "1"
    elif kind == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
        if context.get("canonical"):
            return None
        role = "user" if payload["type"] == "user_message" else "assistant"
        text = _clean(_block_text(payload.get("message")))
    else:
        return None
    if not text:
        return None
    return Turn(
        role=role,
        text=text,
        turn_id=str(payload.get("id") or data.get("id") or f"line{index}"),
        session=session,
        occurred=_timestamp(data.get("timestamp") or payload.get("timestamp")),
        project=_project_name(payload.get("cwd") or data.get("cwd")) or context.get("project"),
        working_directory=payload.get("cwd") or data.get("cwd") or context.get("cwd"),
        reply_to_id=(
            str(payload.get("reply_to_id") or data.get("reply_to_id"))
            if payload.get("reply_to_id") or data.get("reply_to_id")
            else None
        ),
    )


def _codex_program_session(meta: dict[str, Any]) -> bool:
    """A Codex thread a program started, whose "user" is not the person.

    `thread_source: "subagent"` (or a `source` naming one) is Codex's
    `isSidechain`: the guardian that reviews each approval request, and workers
    a thread spawned. Measured here on 2026-09-26: 112 of 229 rollouts, and the
    guardian's "The following is the Codex agent history..." preamble stood in
    the archive as 92 of his turns. `codex exec` is `claude -p`.
    """
    source = meta.get("source")
    if meta.get("thread_source") == "subagent":
        return True
    if isinstance(source, dict) and "subagent" in source:
        return True
    return source == "exec" or meta.get("originator") == "codex_exec"


def _program_threads(adapter_id: str, path: Path) -> set[str]:
    """The thread ids of a transcript a program wrote, read from its head.

    A Claude Code session says it on its first prompt line (`entrypoint`, or
    `turnOrigin` for a program run from inside the desktop app), and a Codex
    rollout says who started it in its first line; the first line that says is
    enough. Thread ids are what `normalize` files a turn under.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return set()
    with handle:
        for index, line in enumerate(handle):
            if index >= 50:
                break
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if adapter_id == "claude_code":
                if data.get("type") != "user" or data.get("isMeta"):
                    continue
                thread = {f"ai:claude_code:{data.get('sessionId') or path.stem}"}
                if data.get("entrypoint") in _PROGRAM_ENTRYPOINTS:
                    return thread
                return thread if data.get("turnOrigin") == "sdk" else set()
            if adapter_id == "codex":
                payload = data.get("payload")
                if data.get("type") != "session_meta" or not isinstance(payload, dict):
                    return set()
                if _codex_program_session(payload) and payload.get("id"):
                    return {f"ai:codex:{payload['id']}"}
                return set()
            return set()
    return set()


def _program_turns(path: Path) -> set[str]:
    """External ids of the prompt lines in a Claude Code file a program typed.

    For a program's turn inside a session the person typed - a task
    notification, a scheduled check-in - where the session itself stays. The
    whole file is read, but only lines that carry an origin are parsed.
    """
    found: set[str] = set()
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return found
    with handle:
        for index, line in enumerate(handle):
            if '"turnOrigin"' not in line and '"origin"' not in line:
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("type") == "user" and _typed_by_program(data):
                session = data.get("sessionId") or path.stem
                turn = data.get("uuid") or f"line{index}"
                found.add(f"claude_code:{session}:{turn}")
    return found


def _codex_session(path: Path) -> str:
    # rollout-2026-05-22T17-22-28-019e5047-….jsonl → the trailing uuid.
    stem = path.stem
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


def _generic_turn(
    data: dict[str, Any], index: int, path: Path, context: dict[str, str]
) -> Turn | None:
    """A chat transcript line in the shape most tools converge on.

    Used for OpenClaw and Hermes. Their stores were not available to read while
    this was written, so the reader is deliberately tolerant about spelling
    rather than precise about one schema: it accepts role/content at the top
    level or under `message`/`payload`, and it still refuses anything that has
    no prose left after cleaning. If either tool turns out to store something
    materially different, this returns nothing rather than guessing wrong.
    """
    for candidate in (data.get("message"), data.get("payload"), data):
        if not isinstance(candidate, dict):
            continue
        role = candidate.get("role") or candidate.get("author") or candidate.get("speaker")
        if role not in {"user", "assistant", "human", "ai", "model"}:
            continue
        raw = candidate.get("content")
        if raw is None:
            raw = candidate.get("text") or candidate.get("message")
        text = _clean(_block_text(raw))
        if not text:
            continue
        return Turn(
            role="user" if role in {"user", "human"} else "assistant",
            text=text,
            turn_id=str(candidate.get("id") or data.get("id") or f"line{index}"),
            session=str(
                data.get("session_id")
                or data.get("sessionId")
                or data.get("thread_id")
                or context.get("session")
                or path.stem
            ),
            occurred=_timestamp(
                data.get("timestamp")
                or data.get("ts")
                or data.get("created_at")
                or candidate.get("timestamp")
            ),
            project=(
                _project_name(data.get("cwd") or candidate.get("cwd"))
                if data.get("cwd") or candidate.get("cwd")
                else context.get("project") or path.parent.name
            ),
            reply_to_id=_native_reference(
                candidate,
                data,
                keys=("reply_to_id", "replyToId", "parent_id", "parentId"),
            ),
            quote_target_id=_native_reference(
                candidate,
                data,
                keys=("quote_target_id", "quoteTargetId", "quoted_message_id"),
            ),
        )
    return None


def _native_reference(
    primary: dict[str, Any], fallback: dict[str, Any], *, keys: tuple[str, ...]
) -> str | None:
    """Read a source-declared message reference, never infer one from text."""
    for container in (primary, fallback):
        for key in keys:
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value)
    return None


# The Codex app files a chat started outside any project under a folder of its
# own, named by the day and the first words typed:
# ~/Documents/Codex/2026-09-02/help-me-tailor-and-update-my. Measured here on
# 2026-09-29, 9 such folders had been read as projects ("my", "hel", "i-w").
_CHAT_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HOME_PARENTS = frozenset({"users", "home"})
_SCRATCH_FOLDERS = frozenset({"tmp", "temp"})


def _project_name(cwd: Any) -> str | None:
    """The project a session ran in: its folder's name, or None for no project.

    The folder the agent ran in, as the machine that wrote the transcript
    spelled it: a Windows path read on Linux or macOS (WSL, a copied archive)
    is one name to `Path`. PureWindowsPath splits on both separators. A home
    folder, a scratch folder and an app's folder for one chat (or anything
    under it) name no project.
    """
    if not cwd:
        return None
    parts = [
        part for part in PureWindowsPath(str(cwd).removeprefix("file://")).parts
        if part.strip("\\/") and not part.endswith((":", ":\\"))
    ]
    if not parts:
        return None
    folded = [part.casefold() for part in parts]
    if len(parts) == 2 and folded[0] in _HOME_PARENTS:
        return None
    if folded[-1] in _SCRATCH_FOLDERS:
        return None
    if folded[-1] in {"session-workspace", "cowork-recents", ".codex", ".claude"}:
        return None
    for index, part in enumerate(folded[:-1]):
        if part == "codex" and _CHAT_DAY.fullmatch(parts[index + 1]):
            return None
    # A session in deploy/site or src/mobile belongs to its enclosing checkout.
    # Only resolve paths native to this machine; copied foreign paths retain
    # their source label instead of being attributed to the reader's checkout.
    native = Path(str(cwd).removeprefix("file://"))
    if native.is_absolute() and native.is_dir():
        for folder in (native, *native.parents):
            if (folder / ".git").exists():
                return folder.name
    if any(part in {".codex", ".claude"} for part in folded):
        return None
    return parts[-1]


_PARSERS = {
    "claude_code": _claude_code_turn,
    "claude_code_history": _claude_code_history_turn,
    "codex": _codex_turn,
}


def parse_turn(
    adapter_id: str,
    data: dict[str, Any],
    index: int,
    path: Path,
    context: dict[str, str] | None = None,
) -> Turn | None:
    """Parse one transcript line.

    `context` carries what a file said about itself earlier — a header line, a
    session id — for formats that state it once instead of on every turn. It is
    written to as well as read, so an empty one still has to be the caller's
    own dict rather than a substitute made here.
    """
    if context is None:
        context = {}
    return _PARSERS.get(adapter_id, _generic_turn)(data, index, path, context)


def _unread_bytes(path: Path, files: dict[str, Any]) -> int:
    """What reading a session file costs this run: `_file_turns`'s own shortcut."""
    seen = files.get(str(path)) if isinstance(files.get(str(path)), dict) else {}
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    before = int(seen.get("size", 0))
    if not seen.get("lines") or size < before:
        return size
    return size - before


@register
class AIConversationsConnector:
    """Local agent transcripts, read where the agents already keep them."""

    connector_id: ClassVar[str] = "ai_conversations"

    def __init__(self) -> None:
        self.checkpoint: Cursor | None = None
        # Every transcript this discovery walked, for `withdrawn`.
        self._walked: list[tuple[str, Path]] = []
        # How far through its files this discovery is, for the sync's bar.
        self.read_fraction: float | None = None

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        if not options.get("approved", False):
            raise ValueError(
                "AI conversation history requires explicit approval. "
                "Run: varyz connect ai-conversations"
            )
        adapters = _selected_adapters(options)
        include_assistant = bool(options.get("include_assistant", False))
        max_chars = int(options.get("max_turn_chars", MAX_TURN_CHARS))
        raw_since = options.get("since")
        since = _timestamp(raw_since)
        if raw_since not in (None, "") and since is None:
            # A cut-off that cannot be parsed used to read back as None, and
            # the import that followed quietly covered the whole history --
            # the opposite of what the setting asked for, said nowhere.
            raise ValueError(
                f"connectors.ai_conversations.since is {raw_since!r}, which is not a "
                "date. Write it as YYYY-MM-DD, or remove it to import everything"
            )
        home = Path(str(options["_home"])).expanduser() if options.get("_home") else Path.home()
        # Configured paths *replace* the default locations rather than adding
        # to them. Anything else makes the connector's reach impossible to
        # state: somebody who points Varyz at one exported folder would still
        # be silently importing every session on the machine, and no test could
        # pin the behaviour down without depending on the developer's own home
        # directory — which is how this was found.
        configured = [expand(str(p)) for p in options.get("paths", []) if str(p).strip()]

        state: dict[str, Any] = dict(cursor.data) if cursor else {}
        files: dict[str, Any] = dict(state.get("files", {}))
        if state.get("project_rules") != PROJECT_RULES:
            # Read from the top again, keeping only what a file said of itself
            # that the project rule does not decide.
            files = {
                key: {field: value for field, value in seen.items()
                      if field not in {"lines", "size", "project"}}
                if isinstance(seen, dict) else {}
                for key, seen in files.items()
            }
            state = {**state, "project_rules": PROJECT_RULES}

        if configured:
            missing = [p for p in configured if not p.is_dir()]
            if len(missing) == len(configured):
                raise ValueError(
                    "none of the configured AI conversation paths exist: "
                    + ", ".join(str(p) for p in configured)
                )

        # Every file is listed before any is read, so the sync can say how far
        # through them it is: by the bytes each still holds past its checkpoint.
        plan: list[tuple[Adapter, Path, bool]] = []
        for adapter in adapters:
            roots = [p for p in configured if p.is_dir()] if configured else adapter.roots(home)
            for root in roots:
                for path in sorted(root.glob(adapter.file_glob)):
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    if adapter.adapter_id == "claude_code" and path.name.startswith("rollout-"):
                        continue  # a Codex file under a root pointed at both
                    plan.append((adapter, path, False))
                history = root.parent / "history.jsonl"
                if adapter.adapter_id == "claude_code" and root.name == "projects" and (
                    history.is_file()
                ):
                    plan.append((adapter, history, True))
        weights = [_unread_bytes(path, files) for _, path, _ in plan]
        total, done = sum(weights), 0

        scanned = 0
        for (adapter, path, is_history), weight in zip(plan, weights, strict=True):
            self.read_fraction = done / total if total else None
            done += weight
            scanned += 1
            if not is_history:
                self._walked.append((adapter.adapter_id, path))
                yield from self._file_turns(
                    adapter,
                    path,
                    files,
                    state,
                    include_assistant=include_assistant,
                    max_chars=max_chars,
                    since=since,
                )
                continue
            # Prompts from sessions whose transcripts were deleted. A session
            # any transcript was ever read for - on disk now, or checkpointed
            # by an earlier sync - already has its turns.
            read = {Path(key).stem for key in files if key != str(path)}
            yield from self._file_turns(
                adapter,
                path,
                files,
                state,
                include_assistant=False,
                max_chars=max_chars,
                since=since,
                parser="claude_code_history",
                skip_sessions=read,
            )

        if not scanned and not files:
            where = (
                ", ".join(str(p) for p in configured)
                if configured
                else "the default locations for "
                + ", ".join(a.label for a in adapters)
            )
            raise ValueError(
                f"No AI conversation history found in {where}. Point Varyz at yours "
                'with paths = ["~/path/to/sessions"] under '
                "[connectors.ai_conversations]."
            )
        # Every file's position, including those with nothing new: the cursors
        # yielded above only carry the files that produced a turn.
        self.checkpoint = Cursor(data={**state, "files": dict(files)})

    def withdrawn(self, conn: sqlite3.Connection) -> list[str]:
        """Stored turns the current authorship rules would never have read.

        Run by the sync after a complete discovery. A rule added to `_clean` or
        to the program-session checks applies to new lines at once, but the
        turns filed before it stay in the archive as the person's words - and
        rule mining and workflow learning read exactly those. So once per
        `AUTHORSHIP_RULES` version the stored turns are judged again: by their
        words, and by whether the file they came from was a program's session.
        Measured on 2026-09-26, version 2 found 316 of 3,004 stored turns: 187
        by their words, 129 from program sessions (26 of those were copies of
        his prompts that a Codex subagent replays from its parent thread).
        Version 3 adds single prompts a program typed inside a typed session
        (`turnOrigin` "sdk"): 3 more of 2,688 on its first run.
        """
        state = self.checkpoint.data if self.checkpoint else {}
        if state.get("authorship_rules") == AUTHORSHIP_RULES:
            return []
        doomed: list[str] = []
        rows = conn.execute(
            """SELECT id, external_id, body, thread_id FROM entries
               WHERE source = 'ai_conversations' AND authored_by_user = 1"""
        ).fetchall()
        program_threads = {
            thread
            for adapter_id, path in self._walked
            for thread in _program_threads(adapter_id, path)
        }
        program_turns = {
            turn
            for adapter_id, path in self._walked
            if adapter_id == "claude_code"
            for turn in _program_turns(path)
        }
        for entry_id, external_id, body, thread_id in rows:
            words = body.split("\n\n", 1)[1] if "\n\n" in body else body
            if (
                thread_id in program_threads
                or external_id in program_turns
                or is_machine_text(words)
            ):
                doomed.append(str(entry_id))
        self.checkpoint = Cursor(data={**state, "authorship_rules": AUTHORSHIP_RULES})
        return doomed

    def _file_turns(
        self,
        adapter: Adapter,
        path: Path,
        files: dict[str, Any],
        state: dict[str, Any],
        *,
        include_assistant: bool,
        max_chars: int,
        since: datetime | None,
        parser: str | None = None,
        skip_sessions: set[str] | frozenset[str] = frozenset(),
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        """Turns from one session file, resuming where the last run stopped.

        These files are append-only, so a line count is a valid checkpoint and
        an unchanged size means there is nothing new to read at all — which is
        what keeps a repeat scan of hundreds of sessions close to free. A file
        that got *smaller* was rotated or rewritten under the same name, so its
        line numbers no longer mean what they meant; it is re-read from the top
        and the natural-key upsert absorbs the repeats.

        Whatever a file said about itself in its header — the Codex session id
        and working directory — is checkpointed alongside the line count, so a
        resumed file that begins past its own header still labels its turns the
        way the first scan did.
        """
        key = str(path)
        seen = files.get(key) if isinstance(files.get(key), dict) else {}
        context = {
            field: str(seen[field])
            for field in ("session", "project", "canonical", "machine", "opened", "after")
            if isinstance(seen.get(field), str) and seen[field]
        }
        try:
            size = path.stat().st_size
        except OSError:
            return
        start_line = int(seen.get("lines", 0))
        if size == seen.get("size") and start_line:
            return
        if size < int(seen.get("size", 0)):
            start_line = 0

        emitted_in_file: set[tuple[str, str, str]] = set()
        line_number = 0
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number <= start_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                turn = parse_turn(parser or adapter.adapter_id, data, line_number, path, context)
                if turn is None or turn.session in skip_sessions:
                    continue
                after = context.pop("after", None)
                if not turn.authored_by_user:
                    context["after"] = _tail(turn.text)
                    if not include_assistant:
                        continue
                if len(turn.text) < MIN_TURN_CHARS:
                    continue
                if since and turn.occurred and turn.occurred < since:
                    continue
                marker = (turn.session, turn.role, turn.text[:200])
                if marker in emitted_in_file:
                    continue  # the same turn recorded twice by two log streams
                emitted_in_file.add(marker)

                files[key] = {"lines": line_number, "size": size, **context}
                state["files"] = files
                yield (
                    _item(adapter, turn, path, line_number, max_chars,
                          after if turn.authored_by_user else None),
                    Cursor(data={"files": dict(files)}),
                )

        # A file with no new turns still gets checkpointed, or every future run
        # re-reads every transcript that happens to be all tool traffic.
        files[key] = {"lines": max(line_number, start_line), "size": size, **context}
        state["files"] = files

    def normalize(self, item: RawItem) -> NormalizedEntry:
        rec = item.metadata or json.loads(item.payload_bytes().decode("utf-8"))
        authored = bool(rec["authored_by_user"])
        speaker = "me" if authored else f"{rec['adapter_label']} assistant"
        # A session in no project names none: its id is no project either.
        header = f"From: {speaker}\nConversation: {rec['adapter_label']}"
        if rec.get("project"):
            header += f" — {rec['project']}"
        occurred = _timestamp(rec.get("occurred"))
        length = len(rec["text"])
        if authored:
            significance = (
                _SIGNIFICANCE_AUTHORED_SHORT
                if length < _SHORT_TURN_CHARS
                else _SIGNIFICANCE_AUTHORED
            )
        else:
            significance = _SIGNIFICANCE_ASSISTANT
        return NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id=item.external_id,
            body=f"{header}\n\n{rec['text']}",
            occurred_start=occurred,
            captured_at=occurred,
            # A turn with no timestamp is placed by its file, not invented.
            time_precision=TimePrecision.EXACT if occurred else TimePrecision.UNKNOWN,
            thread_id=f"ai:{rec['adapter']}:{rec['session']}",
            conversation=ConversationMetadata(
                conversation_id=str(rec["session"]),
                working_directory=rec.get("working_directory"),
                participant_ids=("user", "assistant"),
                participant_labels=("me", f"{rec['adapter_label']} assistant"),
                participants_complete=True,
                sender_id=str(rec["role"]),
                sender_label=speaker,
                recipient_ids=("assistant" if authored else "user",),
                recipient_labels=(
                    f"{rec['adapter_label']} assistant" if authored else "me",
                ),
                addressee_ids=("assistant" if authored else "user",),
                addressee_labels=(
                    f"{rec['adapter_label']} assistant" if authored else "me",
                ),
                reply_to_id=_ai_reference(rec, "reply_to_id"),
                quote_target_id=_ai_reference(rec, "quote_target_id"),
                context_before=(rec.get("after") or None) if authored else None,
                source_order=rec.get("line"),
            ),
            authored_by_user=authored,
            significance=significance,
        )


def _tail(text: str) -> str:
    """The end of an agent's reply, on one line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= _AFTER_CHARS else "…" + flat[-_AFTER_CHARS:].lstrip()


def _item(adapter: Adapter, turn: Turn, path: Path, line: int, max_chars: int,
          after: str | None = None) -> RawItem:
    text = turn.text
    if len(text) > max_chars:
        text = text[:max_chars] + _TRUNCATION_MARKER
    record = {
        "adapter": adapter.adapter_id,
        "adapter_label": adapter.label,
        "session": turn.session,
        "project": turn.project,
        "working_directory": turn.working_directory,
        "role": turn.role,
        "authored_by_user": turn.authored_by_user,
        "occurred": turn.occurred.isoformat() if turn.occurred else None,
        "text": text,
        "reply_to_id": turn.reply_to_id,
        "quote_target_id": turn.quote_target_id,
        "after": after,
        # The source address: the exact file and line this came from, so a
        # Mirror conclusion can be traced back to the sentence behind it.
        "path": str(path),
        "line": line,
    }
    return RawItem(
        # Stable across re-scans and across a reconfigured root: the session id
        # comes from the transcript, not from where it happens to be stored.
        external_id=f"{adapter.adapter_id}:{turn.session}:{turn.turn_id}",
        payload=json.dumps(record, ensure_ascii=False, sort_keys=True),
        payload_ext=".json",
        metadata=record,
    )


def _ai_reference(record: dict[str, Any], field: str) -> str | None:
    value = record.get(field)
    if value is None or not str(value).strip():
        return None
    return f"{record['adapter']}:{record['session']}:{value}"


def _selected_adapters(options: dict[str, Any]) -> list[Adapter]:
    configured = options.get("adapters")
    if not configured:
        return list(ADAPTERS)
    selected = []
    for name in configured:
        adapter = ADAPTERS_BY_ID.get(str(name).strip().lower())
        if adapter is None:
            known = ", ".join(ADAPTERS_BY_ID)
            raise ValueError(f"Unknown AI conversation adapter '{name}'. Known: {known}")
        selected.append(adapter)
    return selected

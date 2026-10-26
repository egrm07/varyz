"""Which stored owner turns an agent wrote: what a rule reader must not learn from.

A coding agent's history stores every message sent *to* it as the user's. Most
are the owner's, but an orchestrator (Paseo, a parent Codex or Claude session)
also sends its workers briefs, contracts and status notes, and an account
failover replays a whole earlier session as one message. Read as the owner's,
those teach the owner's rules in an agent's voice: dozens of ways of saying
"the parent coordinates, workers implement".

Two checks. A turn is agent-written when it carries an orchestrator's envelope
or opens the way only an agent's message does (``agent_written``). A thread is
agent-started when its first real request is agent-written; a continuation is
judged by the request it carries (``agent_thread``). In an agent-started thread
the owner still steps in now and then ("resume", "pause that agent"), so there
only turns that read as typed by a person are kept (``typed_by_owner``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# An orchestrator's envelope around a message from another agent.
_ENVELOPE = re.compile(
    r'<paseo-system\b[^>]*\bkind="agent-|\A\s*<paseo-system\b', re.IGNORECASE
)
# Blocks only a relay writes: a handed-over history, an agent's reply.
_RELAY_BLOCK = re.compile(
    r"<(?:previous-session|chat-history-summary|agent-response)\b", re.IGNORECASE
)
# How a message opens when an agent or a failover wrote it, never the owner.
_AGENT_OPENING = re.compile(
    r"\A\s*(?:"
    r"This task is continuing from an earlier"
    r"|You were (?:stopped|interrupted) by a usage limit"
    r"|You are (?:a|the) (?:sub-?agent|worker|child agent)\b"
    r"|Reply exactly [A-Z_]{4,}"
    r"|State handoff from\b"
    r"|(?:Root|Parent) (?:coordination|follow-up|inspected|resumed|metrics|all|latest)\b"
    r"|Owner (?:confirms|found)\b"
    r"|Integration coordination\b"
    r"|Coordination check\b"
    r")",
    re.IGNORECASE,
)
# What a harness puts first in a session before the owner's request.
_INJECTED = re.compile(
    r"\A\s*(?:# AGENTS\.md instructions|<environment_context|<user_instructions"
    r"|# Files mentioned by the user)",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    r"\A\s*(?:This task is continuing from an earlier"
    r"|You were (?:stopped|interrupted) by a usage limit)",
    re.IGNORECASE,
)
# Where a continuation quotes the request it carries on.
_CARRIED_REQUEST = re.compile(r"\bUser(?: \(original request\))?:\s*")
# Where a carried request ends and the replay of the agent's work begins.
_REPLAY = re.compile(
    r"\n\s*Assistant:|\s\[ran `|\s\[(?:Read|Edit|Write|Bash|ToolSearch)\b|\s\[mcp__"
)
# The owner's reply to an agent's questions, relayed by Varyz or Paseo as typed.
_RELAYED_ANSWERS = re.compile(r"\A\s*Answers to your questions:", re.IGNORECASE)
# A short message is a person's nudge ("resume", "pause that agent").
SHORT_WORDS = 6


def agent_written(text: str) -> bool:
    """True when an agent or a failover relay wrote this message, not the owner."""
    return bool(
        _ENVELOPE.search(text) or _RELAY_BLOCK.search(text) or _AGENT_OPENING.match(text)
    )


def injected(text: str) -> bool:
    """True for what a harness puts before the owner's first request."""
    return bool(_INJECTED.match(text))


def carried_request(text: str) -> str:
    """The request a continuation carries on; the text itself for any other message.

    A continuation can carry another, so this unwraps a few levels. "" when a
    continuation quotes no request.
    """
    for _ in range(4):
        if not _CONTINUATION.match(text):
            return text
        found = _CARRIED_REQUEST.search(text)
        if found is None:
            return ""
        text = text[found.end():]
    return ""


def agent_thread(texts: Iterable[str]) -> bool:
    """True when a thread's first real request (its turns in order) was agent-written."""
    for text in texts:
        if not text.strip() or injected(text):
            continue
        request = carried_request(text)
        # Only the request's opening: after it a continuation replays the agent's work.
        return not request.strip() or agent_written(_opening(request))
    return False


def _opening(request: str) -> str:
    return _REPLAY.split(request, maxsplit=1)[0][:600]


def typed_by_owner(text: str) -> bool:
    """In an agent-started thread, whether a turn reads as typed by a person.

    Agents open with a capital and a sentence of status; the owner nudges,
    answers, or types in lower case. Losing a capitalised owner turn here costs
    little; learning from a worker's brief costs the profile its voice.
    """
    if agent_written(text) or injected(text):
        return False
    if _RELAYED_ANSWERS.match(text) or len(text.split()) <= SHORT_WORDS:
        return True
    first = next((char for char in text if char.isalpha()), "")
    return first.islower()


__all__ = ["SHORT_WORDS", "agent_thread", "agent_written", "carried_request", "injected",
           "typed_by_owner"]

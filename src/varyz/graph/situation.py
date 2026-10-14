"""What kind of room a sentence was said in.

The archive knows things about a turn that never survive into extraction. An
AI-conversation entry is a turn in a working session: somebody instructing a
coding assistant, inside a session keyed to a working directory. A WhatsApp
entry is a remark to a person. A commit message is a record with no addressee
at all. All three arrive at the extractor as a bare sentence, and a model asked
"what does this person value" will answer for every one of them.

That is the whole defect this module exists to close. "I want the exporter to
skip the empty rows" and "I want a record that holds the shape of my whole
life" are the same grammar with the same kind of object; the first is a ticket
and the second is a life. Nothing in the words separates them. The
room does.

Two rules keep this honest:

**Structural only.** Every field is read from what a connector wrote down —
`source`, `kind`, `thread_id`, and the header block it prepends to a body. This
module never inspects the message text. A filter that rejected sentences for
containing file paths or build verbs would be a different, worse thing wearing
this module's name, and it would delete the memoir line first.

**Describing, not judging.** A `Situation` says where a sentence was said. It
does not say whether the sentence is worth keeping. Extraction and the Mirror
decide that, with this as one input among several.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from varyz.domain import Entry, Kind


class Setting(StrEnum):
    """The kind of room."""

    # A working session with a software agent: a task thread, keyed to a
    # project directory, where the speaker's sentences are instructions.
    WORK_SESSION = "work_session"
    # People talking to each other.
    CONVERSATION = "conversation"
    # Something written down with no interlocutor: a commit, a document.
    RECORD = "record"


class Addressee(StrEnum):
    """Who the speaker was talking to."""

    ASSISTANT = "assistant"
    PERSON = "person"
    NONE = "none"


# The header block connectors prepend to a body: `From:`, `Chat:`,
# `Conversation:`. Parsed rather than pattern-matched against prose — this is a
# format Varyz writes itself.
_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z -]{0,30}):\s*(.*)$")

# `ai:<adapter>:<session>` — written by the AI-conversations connector.
_AI_THREAD_RE = re.compile(r"^ai:([^:]*):(.*)$", re.DOTALL)

# `Claude Code — memoir`: the assistant's surface, then the working directory
# the session was keyed to. The separator is the em dash the connector writes,
# so project names containing hyphens ("cowork-recents") stay whole.
_SURFACE_PROJECT_RE = re.compile(r"^(.*?)\s+—\s+(.*)$")

AI_SOURCES = frozenset({"ai_conversations"})

# Kinds that are one person addressing another. A journal or a note is
# authored by the user but spoken to nobody, which is a real difference: it is
# where people say things they would not say out loud.
CONVERSATIONAL_KINDS = frozenset({Kind.MESSAGE, Kind.EMAIL, Kind.TRANSCRIPT})


def header_block(body: str) -> dict[str, str]:
    """The connector's header lines, lowercased, or nothing.

    Mirrors `graph.extract._headers` deliberately rather than importing it:
    this module is read by the Mirror as well as by extraction, and it stays
    cheap to import by depending on nothing but the domain model.
    """
    head, separator, _ = body.partition("\n\n")
    if not separator:
        return {}
    parsed: dict[str, str] = {}
    for line in head.splitlines():
        match = _HEADER_RE.match(line)
        if match:
            parsed[match.group(1).casefold()] = match.group(2).strip()
    return parsed


@dataclass(frozen=True, slots=True)
class Situation:
    """Where a sentence was said, as far as the archive can tell."""

    setting: Setting = Setting.RECORD
    addressee: Addressee = Addressee.NONE
    # "Claude Code", "Codex" — empty outside a work session.
    surface: str = ""
    # The working directory a session was keyed to, when there is one.
    project: str = ""
    # The session or thread the turn belongs to.
    session: str = ""

    @property
    def is_work_session(self) -> bool:
        return self.setting is Setting.WORK_SESSION

    def describe(self) -> str:
        """One plain line naming the room, for a reader that only sees text."""
        if self.setting is Setting.WORK_SESSION:
            where = f" on the project '{self.project}'" if self.project else ""
            tool = self.surface or "a coding assistant"
            if self.addressee is Addressee.ASSISTANT:
                return f"working session: the speaker is instructing {tool}{where}"
            return f"working session: {tool} replying to the speaker{where}"
        if self.setting is Setting.CONVERSATION:
            return "conversation: the speaker is talking to another person"
        return "record: written down, addressed to nobody"


def classify(
    *,
    source: str,
    kind: str,
    thread_id: str | None = None,
    headers: dict[str, str] | None = None,
    authored_by_user: bool = False,
) -> Situation:
    """The room a turn was said in, from stored fields only.

    `headers` is the connector's header block (see `header_block`). It carries
    the assistant's surface and the project directory, which exist nowhere else
    once an entry is written.
    """
    headers = headers or {}
    thread = thread_id or ""
    ai_thread = _AI_THREAD_RE.match(thread)
    if source in AI_SOURCES or ai_thread:
        surface, project = _surface_and_project(headers.get("conversation", ""))
        if ai_thread:
            surface = surface or ai_thread.group(1)
            session = ai_thread.group(2)
        else:
            session = thread
        return Situation(
            setting=Setting.WORK_SESSION,
            # The user's own turns are instructions to the agent. The agent's
            # turns are addressed back at the user, who is a person — the
            # asymmetry is the point, and `authored_by_user` is the only field
            # that can tell them apart after ingestion.
            addressee=Addressee.ASSISTANT if authored_by_user else Addressee.PERSON,
            surface=surface,
            project=project,
            session=session,
        )
    if kind in CONVERSATIONAL_KINDS or "chat" in headers:
        return Situation(
            setting=Setting.CONVERSATION,
            addressee=Addressee.PERSON,
            session=thread,
        )
    return Situation(session=thread)


def situation_of(entry: Entry) -> Situation:
    """The room an entry was written in."""
    return classify(
        source=entry.source,
        kind=str(entry.kind),
        thread_id=entry.thread_id,
        headers=header_block(entry.body),
        authored_by_user=entry.authored_by_user,
    )


def _surface_and_project(value: str) -> tuple[str, str]:
    match = _SURFACE_PROJECT_RE.match(value.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return value.strip(), ""

"""Core domain models shared across connectors, pipeline, and retrieval."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from ulid import ULID


def new_id() -> str:
    return str(ULID())


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def local_day(value: str | datetime | None, *, empty: str = "undated") -> str:
    """The day a stored instant fell on where the person was.

    Every timestamp is stored in UTC and every table that showed one sliced
    the first ten characters off it, so a note captured at 00:25 in Berlin was
    filed under the day before and the search for what happened today did not
    list it.
    """
    if not value:
        return empty
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:10]
    return moment.astimezone().date().isoformat()


def local_stamp(value: str | datetime | None, *, empty: str = "") -> str:
    """A stored instant as the local wall clock that hour was read by."""
    if not value:
        return empty
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)[:19]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


class Kind(StrEnum):
    MESSAGE = "message"
    EMAIL = "email"
    JOURNAL = "journal"
    CALENDAR_EVENT = "calendar_event"
    PHOTO = "photo"
    NOTE = "note"
    DOCUMENT = "document"
    TRANSCRIPT = "transcript"


class TimePrecision(StrEnum):
    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    DECADE = "decade"
    UNKNOWN = "unknown"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    UNCLASSIFIED = "unclassified"

    @property
    def cloud_ok(self) -> bool:
        # Fail-closed: unclassified is treated as sensitive.
        return self in (Sensitivity.PUBLIC, Sensitivity.PERSONAL)


class ConversationEditState(StrEnum):
    """What the source explicitly says about a conversational item's version.

    ``UNKNOWN`` is deliberately distinct from ``ORIGINAL``.  Many exports do
    not expose edit history at all; absence in those formats is not evidence
    that a message was never edited.
    """

    UNKNOWN = "unknown"
    ORIGINAL = "original"
    EDITED = "edited"
    DELETED = "deleted"


class ConversationMetadata(BaseModel):
    """Source-native conversation structure preserved at normalization.

    IDs and labels are separate on purpose.  Some exports expose stable user
    identifiers, while others expose display names only.  A connector may
    preserve either, but must never turn a label into an invented identifier.
    Empty/``None`` fields mean the source did not establish that structure.
    """

    conversation_id: str | None = None
    working_directory: str | None = None
    participant_ids: tuple[str, ...] = ()
    participant_labels: tuple[str, ...] = ()
    participants_complete: bool | None = None
    sender_id: str | None = None
    sender_label: str | None = None
    recipient_ids: tuple[str, ...] = ()
    recipient_labels: tuple[str, ...] = ()
    addressee_ids: tuple[str, ...] = ()
    addressee_labels: tuple[str, ...] = ()
    reply_to_id: str | None = None
    quote_target_id: str | None = None
    # The end of what the other side said just before this message: context to
    # read it by, kept out of the body because it is not this sender's words.
    context_before: str | None = None
    source_order: int | str | None = None
    edit_state: ConversationEditState = ConversationEditState.UNKNOWN
    edited_at: datetime | None = None
    edited_at_literal: str | None = None

    @field_validator("edited_at")
    @classmethod
    def edited_at_requires_timezone(cls, value: datetime | None) -> datetime | None:
        """A source wall-clock string is not permission to invent UTC."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("edited_at must include a timezone")
        return value


class RawItem(BaseModel):
    """An artifact as discovered upstream, before normalization."""

    external_id: str
    payload: bytes | str
    payload_ext: str  # '.eml', '.jpg', '.md', ...
    metadata: dict[str, Any] = Field(default_factory=dict)

    def payload_bytes(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return self.payload.encode("utf-8")


class Cursor(BaseModel):
    """Opaque per-connector checkpoint, persisted as JSON in sync_state."""

    data: dict[str, Any] = Field(default_factory=dict)


class NormalizedEntry(BaseModel):
    """Canonical entry fields emitted by Connector.normalize()."""

    kind: Kind
    external_id: str
    title: str | None = None
    body: str = ""
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None
    tz: str | None = None
    time_precision: TimePrecision = TimePrecision.EXACT
    captured_at: datetime | None = None
    thread_id: str | None = None
    conversation: ConversationMetadata = Field(default_factory=ConversationMetadata)
    # Did the user write this, or did somebody else write it to them?
    #
    # Only set by connectors that can actually tell — an outgoing WhatsApp
    # message, a user turn in an agent transcript. False is not "written by
    # someone else", it is "not known to be the user's own words", which is why
    # the Mirror's authored-evidence gate can only ever count upwards from it.
    authored_by_user: bool = False
    lat: float | None = None
    lon: float | None = None
    # Connectors may lower intrinsically noisy artifacts (for example a
    # daily browser roll-up) without changing their searchable text.  The
    # timeline uses this value; search and Ask still index the entry.
    significance: float | None = Field(default=None, ge=0, le=1)


class Entry(BaseModel):
    """A stored entry row."""

    id: str
    kind: Kind
    source: str
    source_account: str = ""
    external_id: str
    content_hash: str
    title: str | None = None
    body: str = ""
    occurred_start: str | None = None
    occurred_end: str | None = None
    tz: str | None = None
    time_precision: TimePrecision = TimePrecision.EXACT
    captured_at: str | None = None
    ingested_at: str
    vault_ref: str | None = None
    thread_id: str | None = None
    conversation: ConversationMetadata = Field(default_factory=ConversationMetadata)
    authored_by_user: bool = False
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED
    enrich_version: int = 0


class IngestStats(BaseModel):
    connector: str
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    # Items deliberately excluded from Varyz by a durable local tombstone.
    # They still count as processed so cursor-based connectors advance.
    excluded: int = 0
    errors: int = 0
    # Entries dropped because the source no longer offers them. Deliberately
    # outside `total`: that counts what the source handed over this run, and a
    # deleted file hands over nothing.
    deleted: int = 0
    # Configured roots that are not on this machine. Deliberately not an error:
    # resolve_paths refuses only when *none* of them exist, because a laptop may
    # legitimately be configured for two machines' folders. But a run that
    # walked three of four folders and reported an unqualified success is how a
    # mistyped path survives -- so the ones it skipped are carried out.
    missing_paths: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.new + self.updated + self.unchanged + self.excluded + self.errors


class SearchHit(BaseModel):
    entry: Entry
    score: float
    snippet: str | None = None       # highlighted fragment, for search UI
    chunk: str | None = None         # best-matching chunk — the only part sent to the cloud
    # Explanations are retrieval evidence, never a verdict about the person.
    match_reasons: tuple[str, ...] = ()
    match_strength: str | None = None

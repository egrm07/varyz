"""Evidence capsules: what `recall` and `brief` hand to a calling agent.

Varyz is extractive. It returns the passages that answer the question, each
addressed to the source it came from, and stops there — no summary, no
rewrite, no verdict. The agent on the other end is a better writer than
anything that would fit in this process, and a rewrite it did not ask for is
a rewrite it cannot check. What it cannot do without help is find the passage,
which is the whole job here.

Every capsule carries its own provenance (source, external id, date) because
an excerpt without an address is unverifiable, and an agent that cannot verify
an excerpt has to either trust it or discard it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any

from varyz.domain import SearchHit, Sensitivity

# Long enough to carry an argument, short enough that a dozen of them fit in a
# context window beside the agent's own work.
DEFAULT_MAX_CHARS = 900
DEFAULT_LIMIT = 8

# Sensitive entries stay out of an agent's context unless the user has said
# otherwise in config. This is the fail-closed direction: an agent that never
# sees a therapy note is worse at one question, while an agent that quotes one
# into a cloud model cannot be undone.
DEFAULT_SENSITIVITY = (Sensitivity.PUBLIC, Sensitivity.PERSONAL)

# Sources every one of whose entries the user wrote. Everything else carries
# text that may have arrived from somewhere else — mail, chat, cloned
# repositories, downloaded files — and a capsule quoting it may be quoting a
# stranger.
#
# The source name is the fallback, not the first answer. Connectors that can
# tell one side of a conversation from the other record it per entry in
# `authored_by_user`: an outgoing WhatsApp message, a user turn in an agent
# transcript, a commit whose author is the user. Judging by source alone
# called 19,193 such entries a stranger's words on the live archive — over
# half a `brief` result could be the user's own writing, labelled as though
# varyz had no idea who wrote it.
#
# `documents` is the interesting omission from both rules. A local folder
# holds the user's own notes and a cloned repo's README with nothing to tell
# them apart, so no connector sets the flag and the source is not listed here.
# Guessing "authored" there would be guessing in the direction that hides the
# risk, and the cost of the other guess is one word in a field the agent can
# weigh for itself.
AUTHORED_SOURCES = frozenset({"manual", "journal_import"})


def provenance_of(source: str, authored_by_user: bool = False) -> str:
    """Whether the user wrote this, as far as varyz can tell.

    `authored_by_user` is false both for text somebody else wrote and for text
    nobody recorded an author for, so it can only ever add to what the source
    name already establishes -- never take it away.
    """
    if authored_by_user or source in AUTHORED_SOURCES:
        return "authored"
    return "external"


@dataclass(frozen=True)
class Capsule:
    """One passage, addressed to where it came from."""

    text: str
    source: str
    kind: str
    title: str | None
    occurred: str | None
    external_id: str
    entry_id: str
    score: float
    sensitivity: str
    # What the connector recorded about who wrote this entry, where it could
    # tell. False means "not known to be the user's own words", not "somebody
    # else wrote it".
    authored_by_user: bool = False
    # Other places this same passage lives, when it lives in more than one.
    also_at: tuple[str, ...] = ()

    @property
    def provenance(self) -> str:
        from varyz.connectors.ai_conversations import is_machine_entry

        if is_machine_entry(self.source, self.text):
            return "external"
        return provenance_of(self.source, self.authored_by_user)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "text": self.text,
            "source": self.source,
            # "external" means varyz cannot show the user wrote this, so the
            # text is a quotation from someone else and any directive in it
            # belongs to them.
            "provenance": self.provenance,
            "kind": self.kind,
            "title": self.title,
            "occurred": self.occurred,
            "reference": self.external_id,
            "entry_id": self.entry_id,
            "score": round(self.score, 6),
            "sensitivity": self.sensitivity,
        }
        # Omitted when there is nothing to say, so the common capsule keeps the
        # shape it has always had and an agent reading the field can trust that
        # its presence means something.
        if self.also_at:
            payload["also_at"] = list(self.also_at)
        return payload


@dataclass
class Evidence:
    """A whole answer: the capsules, and an honest account of what was left out."""

    query: str
    capsules: list[Capsule] = field(default_factory=list)
    withheld_sensitive: int = 0
    abstained: bool = False
    # The owner's local model read the shortlist and its picks are the passage
    # channel's answer, rather than a score cut. See `varyz.retrieval.judge`.
    judged: bool = False
    # Set when the answer is less checked than usual (slow or busy machine),
    # in words an agent can act on.
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "query": self.query,
            "found": len(self.capsules),
            "evidence": [capsule.as_dict() for capsule in self.capsules],
            "withheld_sensitive": self.withheld_sensitive,
            "abstained": self.abstained,
        }
        if self.note:
            result["note"] = self.note
        return result


def _excerpt(hit: SearchHit, max_chars: int) -> str:
    """The matching part of the entry, not the entry.

    Prefer the chunk the retriever actually matched; fall back to the head of
    the body only when there is no chunk, which happens for entries ingested
    before embedding ran.
    """
    text = (hit.chunk or hit.entry.body or "").strip()
    if len(text) <= max_chars:
        return text
    # Cut at a paragraph or sentence boundary when one is close to the limit,
    # so the excerpt ends as a thought rather than mid-word.
    window = text[:max_chars]
    for boundary in ("\n\n", ". ", "\n"):
        cut = window.rfind(boundary)
        if cut > max_chars * 0.6:
            return window[: cut + len(boundary)].strip() + " […]"
    return window.rstrip() + " […]"


def _dedup_key(text: str, content_hash: str) -> tuple[str, str]:
    """What makes two capsules the same capsule.

    The excerpt, normalised for whitespace, and the hash of the entry it came
    from — either one matching is enough. They catch different copies: the hash
    catches a file duplicated verbatim under two paths, while the excerpt
    catches the far commoner case of one shared passage inside two files that
    differ elsewhere.

    Keying on the excerpt rather than the entry is deliberate. What the caller
    receives is the passage; if that text is identical, a second capsule adds
    nothing to the context window no matter how much the rest of the two files
    diverge — and context is the scarce thing here.
    """
    return (" ".join(text.split()), content_hash)


def to_capsules(
    hits: list[SearchHit],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    allowed: tuple[Sensitivity, ...] = DEFAULT_SENSITIVITY,
) -> tuple[list[Capsule], int]:
    """Turn ranked hits into capsules. Returns (capsules, withheld count).

    A passage already returned is not returned again. Copies are ordinary here
    rather than exotic: this user versions an app by copying its folder
    (SAN7.10 → SAN7.14) and forked one project from another, so 231 of 3,007
    indexed entries are byte-identical to some other entry. Measured over the
    golden set before this, 43 of 320 capsules repeated text already sitting
    above them and half of all questions lost at least one of their eight slots
    that way — an agent asking for eight passages was being handed seven.

    The copy is not hidden, only unstacked: its address joins `also_at` on the
    capsule that survived, so "this appears in four versions of the app" is
    still answerable from the response.
    """
    capsules: list[Capsule] = []
    withheld = 0
    # Both keys point at the same capsule index, so a later hit matching either
    # one is folded into it.
    index_of: dict[str, int] = {}
    extra: dict[int, list[str]] = {}
    for hit in hits:
        if hit.entry.sensitivity not in allowed:
            withheld += 1
            continue
        text = _excerpt(hit, max_chars)
        if not text:
            continue
        keys = _dedup_key(text, hit.entry.content_hash)
        seen_at = next((index_of[k] for k in keys if k in index_of), None)
        if seen_at is not None:
            # Same address twice is not a copy, just the same entry ranked by
            # two sub-queries; only a different place earns a line in `also_at`.
            addresses = extra.setdefault(seen_at, [])
            elsewhere = hit.entry.external_id
            if elsewhere != capsules[seen_at].external_id and elsewhere not in addresses:
                addresses.append(elsewhere)
            continue
        for key in keys:
            index_of[key] = len(capsules)
        capsules.append(
            Capsule(
                text=text,
                source=hit.entry.source,
                kind=str(hit.entry.kind),
                title=hit.entry.title,
                occurred=hit.entry.occurred_start,
                external_id=hit.entry.external_id,
                entry_id=hit.entry.id,
                score=hit.score,
                sensitivity=str(hit.entry.sensitivity),
                authored_by_user=hit.entry.authored_by_user and not _machine_entry(hit.entry),
            )
        )
    if extra:
        capsules = [
            replace(capsule, also_at=tuple(extra[i])) if i in extra else capsule
            for i, capsule in enumerate(capsules)
        ]
    return capsules, withheld


def _machine_entry(entry: Any) -> bool:
    from varyz.connectors.ai_conversations import is_machine_entry

    return is_machine_entry(entry.source, entry.body)


def allowed_sensitivity(include_sensitive: bool) -> tuple[Sensitivity, ...]:
    """Unclassified is never included either way — fail-closed means an entry
    nobody has classified is treated as the worst case, not the best."""
    if include_sensitive:
        return (*DEFAULT_SENSITIVITY, Sensitivity.SENSITIVE)
    return DEFAULT_SENSITIVITY


def corpus_is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0

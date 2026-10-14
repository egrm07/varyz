"""Cut a message archive into the conversations it was actually held in.

A message entry is one turn. When varyz still read chat apps, the median one on
this machine was 47 characters, and 36,771 of 41,448 were under 80 -- which is why every layer built
on top of single messages underperforms on a real archive while looking healthy
on a synthetic one, whose records are authored and self-contained. "ok, tuesday
works" is not a retrievable unit, not an interpretable one, and not something a
useful conclusion can be drawn from on its own.

This module is deliberately structural. It reads timestamps, threads and
lengths, never text, and makes no judgement about content: the decision of what
a window *means*, and whether it may travel, belongs to the passes that read
this output. Determinism is the point -- the same archive and the same policy
produce byte-identical windows in any iteration order, so a window id is a
stable address that later passes can cache against.

The policy itself is borrowed rather than reinvented: `EpisodePolicy` already
states what a bounded conversation is for the personal-model compiler, and two
different answers to that question in one codebase is one too many. What this
module does not borrow is `EpisodeReconstructor`, which builds one episode per
*target* entry -- the right shape for annotating a specific message, and the
wrong one here, where the archive has to be covered once rather than 41,448
times.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from varyz.db.dao import row_to_entry
from varyz.domain import Entry, iso, utc_now
from varyz.personal_model.episodes import EpisodePolicy
from varyz.personal_model.models import conversation_episode_id

# Sources whose entries are messages in a running conversation: the owner's
# sessions with coding agents. varyz does not read chat apps. Mail is
# conversational too, but a mail thread is already made of self-contained
# documents, so it gains nothing here and is left to the per-entry path.
CONVERSATION_SOURCES: tuple[str, ...] = ("ai_conversations",)

_POLICY = EpisodePolicy()


def policy_version(policy: EpisodePolicy = _POLICY) -> str:
    """A version string that changes whenever the cut would change.

    Stored on every window and mixed into its id, so a policy edit produces a
    new set of windows instead of leaving the old ones in place wearing a
    description that no longer fits them.
    """
    material = json.dumps(
        {
            "declared": policy.boundary_policy_version,
            "max_gap_seconds": policy.max_time_gap.total_seconds(),
            "max_turns": policy.max_turns,
            "max_characters": policy.max_characters,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"w{policy.boundary_policy_version}+{digest}"


@dataclass(frozen=True, slots=True)
class ConversationWindow:
    """One bounded conversation, addressed by its membership."""

    id: str
    source: str
    thread_id: str | None
    entry_ids: tuple[str, ...]
    started_at: str
    ended_at: str
    characters: int
    policy_version: str
    content_hash: str

    @property
    def turns(self) -> int:
        return len(self.entry_ids)


def _timestamp(entry: Entry) -> datetime | None:
    """When the message happened, or None where the archive does not say.

    Only `occurred_start` counts. `ingested_at` is when varyz saw the message,
    which for an imported history is one instant for tens of thousands of them
    -- close enough to a timestamp to fool a gap rule, and wrong enough to weld
    four years of conversation into a single window.
    """
    raw = entry.occurred_start
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sort_key(entry: Entry) -> tuple[str, str, str]:
    # The id breaks ties. Two messages sharing a timestamp to the second is
    # ordinary in a chat export, and without a tiebreak the partition would
    # depend on the order sqlite happened to return rows in.
    return (entry.thread_id or "", entry.occurred_start or "", entry.id)


def partition(
    entries: Iterable[Entry], *, policy: EpisodePolicy = _POLICY
) -> list[ConversationWindow]:
    """Split messages into windows: one thread, one stretch of time, capped.

    A window ends when the thread changes, when the silence since the last
    message exceeds the policy gap, or when the turn or character cap is
    reached. Messages the archive cannot place in time are skipped rather than
    guessed at: a window with an unknown boundary is not a conversation, and
    quietly attaching one to its neighbour would corrupt the very windows the
    archive does know about.
    """
    version = policy_version(policy)
    ordered = sorted(
        (entry for entry in entries if _timestamp(entry) is not None), key=_sort_key
    )
    windows: list[ConversationWindow] = []
    batch: list[Entry] = []
    characters = 0

    def flush() -> None:
        nonlocal batch, characters
        if not batch:
            return
        ids = tuple(entry.id for entry in batch)
        windows.append(
            ConversationWindow(
                id=conversation_episode_id(ids, boundary_policy_version=version),
                source=batch[0].source,
                thread_id=batch[0].thread_id,
                entry_ids=ids,
                started_at=batch[0].occurred_start or "",
                ended_at=batch[-1].occurred_start or "",
                characters=characters,
                policy_version=version,
                content_hash=_membership_hash(batch),
            )
        )
        batch = []
        characters = 0

    for entry in ordered:
        if batch:
            previous = batch[-1]
            gap = (_timestamp(entry) - _timestamp(previous)).total_seconds()  # type: ignore[operator]
            size = len(entry.body or "")
            if (
                entry.thread_id != previous.thread_id
                or entry.source != previous.source
                or gap > policy.max_time_gap.total_seconds()
                or len(batch) >= policy.max_turns
                or characters + size > policy.max_characters
            ):
                flush()
        batch.append(entry)
        characters += len(entry.body or "")
    flush()
    return windows


def _membership_hash(entries: Sequence[Entry]) -> str:
    """Identify the window's *content*, so an edit upstream invalidates it.

    Over content hashes rather than bodies: the archive already computes one
    per entry on ingest, it is what changes when a message is edited or
    re-imported, and hashing it again keeps message text out of this module
    entirely.
    """
    material = json.dumps(
        [[entry.id, entry.content_hash] for entry in entries],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _chat_entries(
    conn: sqlite3.Connection, sources: Sequence[str]
) -> list[Entry]:
    placeholders = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"SELECT * FROM entries WHERE source IN ({placeholders})"  # noqa: S608 - placeholders only
        " AND occurred_start IS NOT NULL",
        tuple(sources),
    ).fetchall()
    return [row_to_entry(row) for row in rows]


def build_windows(
    conn: sqlite3.Connection,
    *,
    sources: Sequence[str] = CONVERSATION_SOURCES,
    policy: EpisodePolicy = _POLICY,
) -> dict[str, int]:
    """Refresh the stored partition. Idempotent, and safe to interrupt.

    Windows whose id and content hash are already stored are left exactly as
    they are, so everything keyed to them -- labels, conclusions, the record of
    a window already read -- survives a re-run. A window that no longer exists
    under the current partition is deleted, and the cascade takes its
    derivatives with it, because a conclusion about a conversation that has
    been re-cut is a conclusion about nothing.
    """
    windows = partition(_chat_entries(conn, sources), policy=policy)
    now = iso(utc_now())
    stored = {
        row["id"]: row["content_hash"]
        for row in conn.execute("SELECT id, content_hash FROM conversation_windows")
    }
    # Which windows the search index already holds. An archive partitioned
    # before the index existed has none of them, and re-cutting the partition
    # would not notice: an unchanged window is skipped, index or no index. So
    # the absence is checked rather than assumed, which makes a re-run the
    # backfill and leaves nothing that has to be remembered separately.
    indexed = {
        row["window_id"] for row in conn.execute("SELECT window_id FROM conversation_windows_fts")
    }
    wanted = {window.id: window for window in windows}

    added = changed = backfilled = 0
    with conn:
        for window in windows:
            existing = stored.get(window.id)
            if existing == window.content_hash:
                if window.id not in indexed:
                    _index(conn, window.id)
                    backfilled += 1
                continue
            if existing is not None:
                # Same membership, different content: the text under it moved.
                # Replacing the row and its members keeps the cascade honest.
                conn.execute("DELETE FROM conversation_windows WHERE id = ?", (window.id,))
                changed += 1
            else:
                added += 1
            conn.execute(
                "INSERT INTO conversation_windows"
                " (id, source, thread_id, started_at, ended_at, turns, characters,"
                "  policy_version, content_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    window.id,
                    window.source,
                    window.thread_id,
                    window.started_at,
                    window.ended_at,
                    window.turns,
                    window.characters,
                    window.policy_version,
                    window.content_hash,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO conversation_window_entries (window_id, entry_id, seq)"
                " VALUES (?, ?, ?)",
                [(window.id, entry_id, seq) for seq, entry_id in enumerate(window.entry_ids)],
            )
            _index(conn, window.id)
        removed = [window_id for window_id in stored if window_id not in wanted]
        conn.executemany(
            "DELETE FROM conversation_windows WHERE id = ?",
            [(window_id,) for window_id in removed],
        )
        conn.executemany(
            "DELETE FROM conversation_windows_fts WHERE window_id = ?",
            [(window_id,) for window_id in removed],
        )

    return {
        "windows": len(windows),
        "added": added,
        "changed": changed,
        "removed": len(removed),
        "indexed": backfilled,
    }


def _index(conn: sqlite3.Connection, window_id: str) -> None:
    """Put the rendered conversation in the search index, replacing any prior copy.

    The delete is unconditional because a changed window is stored by deleting
    and re-inserting the row, and this table has no foreign key to cascade
    from: an fts5 virtual table cannot carry one. Doing both here, in the same
    transaction as the window itself, is what keeps the index from outliving
    what it describes.
    """
    conn.execute("DELETE FROM conversation_windows_fts WHERE window_id = ?", (window_id,))
    conn.execute(
        "INSERT INTO conversation_windows_fts (window_id, text) VALUES (?, ?)",
        (window_id, window_text(conn, window_id)),
    )


def window_text(conn: sqlite3.Connection, window_id: str) -> str:
    """The conversation as a model should read it: in order, one turn per line.

    Speaker is given as `you` or `them` rather than a name. A name in the
    prompt is a name the model can put in a conclusion, and a conclusion is the
    thing that travels; who said it is already recorded structurally against
    the entries, where it can be resolved without ever being generated.
    """
    rows = conn.execute(
        "SELECT e.body, e.authored_by_user FROM conversation_window_entries w"
        " JOIN entries e ON e.id = w.entry_id"
        " WHERE w.window_id = ? ORDER BY w.seq",
        (window_id,),
    ).fetchall()
    return "\n".join(
        f"{'you' if row['authored_by_user'] else 'them'}: {row['body'] or ''}" for row in rows
    )

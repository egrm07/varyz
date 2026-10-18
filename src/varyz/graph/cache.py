"""Reuse of per-entry extraction across graph generations.

A generation is named after the whole archive, so appending one message names a
new generation. The work, though, is per entry: an extractor asked the same
question about the same text under the same rules returns the same answer. This
module remembers that answer so a rebuild pays only for what actually changed.

Two things make a cached answer valid, and both are folded into the key. The
*scope* carries everything about the reader — extractor, version, model,
prompt, the privacy policy and mode the broker enforced, and the batching limits
that decide how much of a conversation the model sees. The per-entry part
carries everything about what was read — the entry's content hash, its
sensitivity label (which decides whether a cloud model may see it at all), and,
for a context-aware extractor, a fingerprint of the conversation window it is
allowed to read around the entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import timedelta

from pydantic import ValidationError

from varyz.domain import Entry, Kind, iso, utc_now
from varyz.graph.models import ExtractionBatch
from varyz.llm.refusal import read_failure_detail

log = logging.getLogger(__name__)

# The window fingerprint of an entry no extractor reads context around. It is a
# constant rather than an empty string so a bug that loses a real fingerprint
# cannot silently look like "this entry stands alone".
SELF_ONLY = "-"

# How long a scope nothing has read is kept. A rebuild refreshes the scope it
# used, so this only ever reaches abandoned readers: old extractor versions,
# retired prompts, a provider switched away from.
RETENTION_DAYS = 30

# How many extractions to hold before writing them. Each flush is one
# transaction; a per-entry commit would add 45,000 fsyncs to a full build. A
# crash loses at most this many cached answers, which are recomputed — the
# run's own checkpoint is committed separately, entry by entry, and is
# unaffected.
FLUSH_EVERY = 256


def cache_scope(
    *,
    extractor_name: str,
    extractor_version: str,
    model: str | None,
    prompt_version: str | None,
    parameters: Mapping[str, object] | None = None,
) -> str:
    """Identity of the reader: everything outside an entry that changes the answer."""
    material = json.dumps(
        {
            "extractor": extractor_name,
            "version": extractor_version,
            "model": model,
            "prompt_version": prompt_version,
            "parameters": dict(parameters or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def window_bucket(entry: Entry) -> tuple[str, str, str, str] | None:
    """The conversation window a context-aware extractor may read around `entry`.

    This mirrors `ConversationWindowReader.session_around`, which selects the
    same source, account, thread and calendar day and nothing else. The two are
    held together by a test, because a window wider than this fingerprint would
    let a changed neighbour hide behind a cache hit.
    """
    if entry.kind != Kind.MESSAGE or not entry.thread_id or not entry.occurred_start:
        return None
    return (
        entry.source,
        entry.source_account or "",
        entry.thread_id,
        entry.occurred_start[:10],
    )


class WindowFingerprints:
    """A digest per conversation window, built in one pass on first use."""

    def __init__(
        self, conn: sqlite3.Connection, *, excluded_sources: tuple[str, ...] = ()
    ):
        self._conn = conn
        self._excluded_sources = tuple(
            sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
        )
        self._digests: dict[tuple[str, str, str, str], str] | None = None

    def of(self, entry: Entry) -> str:
        bucket = window_bucket(entry)
        if bucket is None:
            return SELF_ONLY
        if self._digests is None:
            self._digests = self._load()
        return self._digests.get(bucket, SELF_ONLY)

    def _load(self) -> dict[tuple[str, str, str, str], str]:
        digests: dict[tuple[str, str, str, str], str] = {}
        current: tuple[str, str, str, str] | None = None
        digest = hashlib.sha256()
        placeholders = ",".join("?" for _ in self._excluded_sources)
        exclusion = (
            f" AND lower(source) NOT IN ({placeholders})" if self._excluded_sources else ""
        )
        rows = self._conn.execute(
            """SELECT source, source_account, thread_id,
                      substr(occurred_start, 1, 10) AS day,
                      id, content_hash, sensitivity
               FROM entries
               WHERE kind='message' AND thread_id IS NOT NULL AND thread_id <> ''
                 AND occurred_start IS NOT NULL
            """ + exclusion + " ORDER BY source, source_account, thread_id, day, id",
            self._excluded_sources,
        )
        for row in rows:
            bucket = (
                row["source"],
                row["source_account"] or "",
                row["thread_id"],
                row["day"],
            )
            if bucket != current:
                if current is not None:
                    digests[current] = digest.hexdigest()
                current, digest = bucket, hashlib.sha256()
            # Sensitivity is in the digest because it decides which neighbours a
            # given broker may show the model, and it can change without the
            # text changing.
            digest.update(
                f"{row['id']}\0{row['content_hash']}\0{row['sensitivity']}\n".encode()
            )
        if current is not None:
            digests[current] = digest.hexdigest()
        return digests


class ExtractionCache:
    """What an extractor concluded about one entry, reusable by later generations."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        windows: WindowFingerprints | None = None,
    ):
        self.conn = conn
        self.scope = scope
        self.windows = windows
        self.hits = 0
        self.misses = 0
        self._pending: list[tuple[str, str, str, str, str, str, str, str]] = []

    def key(self, entry: Entry) -> str:
        window = self.windows.of(entry) if self.windows is not None else SELF_ONLY
        material = "\0".join(
            (
                self.scope,
                entry.id,
                entry.content_hash,
                str(entry.sensitivity),
                window,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, entry: Entry) -> ExtractionBatch | None:
        key = self.key(entry)
        row = self.conn.execute(
            "SELECT batch_json FROM graph_extraction_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
        if row is None:
            self.misses += 1
            return None
        try:
            batch = ExtractionBatch.model_validate_json(row["batch_json"])
        except (ValidationError, ValueError) as exc:
            # A stored shape this code cannot read is a miss, not a failure:
            # drop it and let the extractor answer again.
            # Named, not rendered: the row is a model's reply to a prompt made
            # of the owner's entries, and a rendered ValidationError repeats it.
            log.warning(
                "unreadable extraction cache row for %s: %s",
                entry.id,
                read_failure_detail(exc),
            )
            with self.conn:
                self.conn.execute(
                    "DELETE FROM graph_extraction_cache WHERE cache_key=?", (key,)
                )
            self.misses += 1
            return None
        self.hits += 1
        return batch

    def put(self, entry: Entry, batch: ExtractionBatch) -> None:
        now = iso(utc_now()) or ""
        self._pending.append(
            (
                self.key(entry),
                entry.id,
                self.scope,
                entry.content_hash,
                self.windows.of(entry) if self.windows is not None else SELF_ONLY,
                batch.model_dump_json(),
                now,
                now,
            )
        )
        if len(self._pending) >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        with self.conn:
            self.conn.executemany(
                """INSERT INTO graph_extraction_cache
                   (cache_key, entry_id, scope, content_hash, window_fingerprint,
                    batch_json, created_at, last_used_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     batch_json=excluded.batch_json,
                     last_used_at=excluded.last_used_at""",
                pending,
            )

    def sweep(self, *, retention_days: int = RETENTION_DAYS) -> int:
        """Keep the scope that just ran, drop scopes nothing has read in a month.

        Refreshing the whole scope in one statement rather than touching each
        row on the way past keeps a cache hit free: reading costs one indexed
        lookup and no write.
        """
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "UPDATE graph_extraction_cache SET last_used_at=? WHERE scope=?",
                (iso(now), self.scope),
            )
            cursor = self.conn.execute(
                "DELETE FROM graph_extraction_cache WHERE last_used_at < ?",
                (iso(now - timedelta(days=retention_days)),),
            )
        return int(cursor.rowcount or 0)

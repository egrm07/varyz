"""Read-only access to canonical entries and their chunks."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from varyz.db.dao import get_entry, row_to_entry
from varyz.domain import Entry


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    entry_id: str
    seq: int
    text: str


@dataclass(frozen=True)
class CorpusFingerprint:
    """What the archive currently is, without reading what it says."""

    entries: int
    content_hash: str


class EvidenceReader(Protocol):
    """The graph sees evidence through this deliberately read-only port."""

    def get_entry(self, entry_id: str) -> Entry | None: ...

    def iter_entries(self, *, after_id: str | None = None) -> Iterator[Entry]: ...

    def corpus_fingerprint(self) -> CorpusFingerprint: ...

    def chunks_for(self, entry_id: str) -> list[EvidenceChunk]: ...


class SQLiteEvidenceReader:
    def __init__(
        self, conn: sqlite3.Connection, *, excluded_sources: tuple[str, ...] = ()
    ):
        self._conn = conn
        self._excluded_sources = tuple(
            sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
        )

    def _source_clause(self) -> tuple[str, tuple[str, ...]]:
        if not self._excluded_sources:
            return "", ()
        placeholders = ",".join("?" for _ in self._excluded_sources)
        return f"lower(source) NOT IN ({placeholders})", self._excluded_sources

    def get_entry(self, entry_id: str) -> Entry | None:
        entry = get_entry(self._conn, entry_id)
        if entry is None:
            return None
        if entry.source.casefold() in self._excluded_sources:
            return None
        return entry

    def iter_entries(self, *, after_id: str | None = None) -> Iterator[Entry]:
        clauses: list[str] = []
        params: list[str] = []
        source_clause, source_params = self._source_clause()
        if source_clause:
            clauses.append(source_clause)
            params.extend(source_params)
        if after_id:
            clauses.append("id > ?")
            params.append(after_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM entries{where} ORDER BY id",  # noqa: S608 - placeholders only
            tuple(params),
        )
        for row in rows:
            yield row_to_entry(row)

    def corpus_fingerprint(self) -> CorpusFingerprint:
        """Name the archive from two columns, not from 45,866 parsed entries.

        The graph identifies a generation by what the archive contained when it
        was built. Computing that used to mean materialising every `Entry` —
        13.5 MB of body text plus pydantic overhead — to read one field off
        each and throw the rest away. Two columns and a running digest say the
        same thing at constant memory.
        """
        digest = hashlib.sha256()
        entries = 0
        source_clause, params = self._source_clause()
        where = f" WHERE {source_clause}" if source_clause else ""
        for entry_id, content_hash in self._conn.execute(
            f"SELECT id, content_hash FROM entries{where} ORDER BY id",  # noqa: S608
            params,
        ):
            if entries:
                digest.update(b"\n")
            digest.update(f"{entry_id}:{content_hash}".encode())
            entries += 1
        return CorpusFingerprint(entries=entries, content_hash=digest.hexdigest())

    def chunks_for(self, entry_id: str) -> list[EvidenceChunk]:
        rows = self._conn.execute(
            "SELECT id, entry_id, seq, text FROM chunks WHERE entry_id=? ORDER BY seq",
            (entry_id,),
        ).fetchall()
        return [EvidenceChunk(**dict(row)) for row in rows]

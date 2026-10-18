"""What became of each answer an agent was given: the append-only outcome ledger.

An outcome is a signal about one ``resolution_ledger`` row: the agent's work
``adopted`` it, a later owner answer ``overridden`` it, or the owner ``corrected``
it. Rows are device-stamped and ordered by the per-device sequence like every
other resolution write; a later signal is a new row, never an edit.

Only unambiguous signals are recorded. Today that is supersession: when the owner
replaces a knowledge entry, every earlier resolution that entry answered is
``overridden`` (see :func:`record_supersession`).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from varyz.domain import new_id, utc_now
from varyz.resolution.knowledge import (
    KNOWLEDGE_GENERATION,
    _datetime,
    _iso,
    _next_monotonic_seq,
    _write_transaction,
)

OUTCOMES = frozenset({"adopted", "overridden", "corrected"})
SUPERSESSION_SOURCE = "supersession"


@dataclass(frozen=True)
class ResolutionOutcome:
    id: str
    ledger_id: str
    outcome: str
    source: str
    knowledge_id: str | None
    device_id: str
    monotonic_seq: int
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ledger_id": self.ledger_id,
            "outcome": self.outcome,
            "source": self.source,
            "knowledge_id": self.knowledge_id,
            "device_id": self.device_id,
            "monotonic_seq": self.monotonic_seq,
            "recorded_at": _iso(self.recorded_at),
        }


def append_outcome(
    conn: sqlite3.Connection,
    *,
    ledger_id: str,
    outcome: str,
    source: str,
    device_id: str,
    recorded_at: datetime,
    knowledge_id: str | None = None,
) -> str:
    """Insert one outcome row. The caller owns the write transaction."""
    source = source.strip()
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    if not source:
        raise ValueError("outcome source cannot be blank")
    if conn.execute(
        "SELECT 1 FROM resolution_ledger WHERE id = ?", (ledger_id,)
    ).fetchone() is None:
        raise KeyError(f"unknown resolution: {ledger_id}")
    outcome_id = new_id()
    seq = _next_monotonic_seq(conn, device_id)
    conn.execute(
        """INSERT INTO resolution_outcomes
           (id, ledger_id, outcome, source, knowledge_id, device_id,
            monotonic_seq, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (outcome_id, ledger_id, outcome, source, knowledge_id, device_id, seq,
         _iso(recorded_at)),
    )
    return outcome_id


def ledger_ids_answered_by(conn: sqlite3.Connection, knowledge_id: str) -> tuple[str, ...]:
    """Resolutions a knowledge entry answered, oldest first.

    Tier one cites the entry it answered from in the ledger row's citations. Rows
    written before that citation existed carry none and are never guessed at.
    """
    rows = conn.execute(
        """SELECT DISTINCT ledger.id, ledger.resolved_at, ledger.device_id,
                  ledger.monotonic_seq
           FROM resolution_ledger AS ledger, json_each(ledger.citations_json) AS citation
           WHERE ledger.tier = 1 AND ledger.answer IS NOT NULL
             AND json_extract(citation.value, '$.generation_id') = ?
             AND json_extract(citation.value, '$.record_id') = ?
           ORDER BY ledger.resolved_at, ledger.device_id, ledger.monotonic_seq""",
        (KNOWLEDGE_GENERATION, knowledge_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def record_supersession(
    conn: sqlite3.Connection,
    superseded_id: str,
    *,
    knowledge_id: str,
    device_id: str,
    recorded_at: datetime,
) -> tuple[str, ...]:
    """Mark every resolution the superseded entry answered as ``overridden``.

    Called inside the transaction that appends the superseding entry, so the
    correction and its outcomes land together or not at all.
    """
    return tuple(
        append_outcome(
            conn,
            ledger_id=ledger_id,
            outcome="overridden",
            source=SUPERSESSION_SOURCE,
            device_id=device_id,
            recorded_at=recorded_at,
            knowledge_id=knowledge_id,
        )
        for ledger_id in ledger_ids_answered_by(conn, superseded_id)
    )


class SQLiteOutcomeLedger:
    """Record and read outcomes of resolutions; there is no update or delete."""

    def __init__(
        self, conn: sqlite3.Connection, *, device_id: str = "local", clock: Any = utc_now
    ) -> None:
        self._conn = conn
        self._device_id = device_id
        self._clock = clock
        self._lock = threading.RLock()

    def record(
        self,
        ledger_id: str,
        outcome: str,
        *,
        source: str,
        knowledge_id: str | None = None,
        device_id: str | None = None,
    ) -> str:
        with _write_transaction(self._conn, self._lock):
            return append_outcome(
                self._conn,
                ledger_id=ledger_id,
                outcome=outcome,
                source=source,
                device_id=device_id or self._device_id,
                recorded_at=self._clock(),
                knowledge_id=knowledge_id,
            )

    def for_resolution(self, ledger_id: str) -> tuple[ResolutionOutcome, ...]:
        """Every outcome recorded for one resolution, in the order recorded."""
        return self.query(ledger_id=ledger_id)

    def query(
        self,
        *,
        ledger_id: str | None = None,
        outcome: str | None = None,
        source: str | None = None,
        knowledge_id: str | None = None,
    ) -> tuple[ResolutionOutcome, ...]:
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
        clauses: list[str] = []
        values: list[str] = []
        for column, value in (
            ("ledger_id", ledger_id),
            ("outcome", outcome),
            ("source", source),
            ("knowledge_id", knowledge_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""SELECT * FROM resolution_outcomes {where}
                ORDER BY recorded_at, device_id, monotonic_seq""",
            values,
        ).fetchall()
        return tuple(_outcome(row) for row in rows)


def _outcome(row: sqlite3.Row) -> ResolutionOutcome:
    return ResolutionOutcome(
        id=row["id"],
        ledger_id=row["ledger_id"],
        outcome=row["outcome"],
        source=row["source"],
        knowledge_id=row["knowledge_id"],
        device_id=row["device_id"],
        monotonic_seq=int(row["monotonic_seq"]),
        recorded_at=_datetime(row["recorded_at"]),
    )


__all__ = [
    "OUTCOMES",
    "SUPERSESSION_SOURCE",
    "ResolutionOutcome",
    "SQLiteOutcomeLedger",
    "append_outcome",
    "ledger_ids_answered_by",
    "record_supersession",
]

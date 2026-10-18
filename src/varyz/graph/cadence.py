"""When a rebuild is worth doing, as distinct from when ingestion runs.

Autosync polls every fifteen minutes because ingestion is incremental: a cycle
that finds nothing new costs a few API calls and writes nothing. A rebuild is
not incremental in the same way. Even with every entry's extraction served from
cache, three new messages restage all 45,866 entries into a whole new
generation — 88 seconds of writes and some 27,000 alias rows that the next
prune then deletes again.

So the two cadences are separated. Ingest keeps its fifteen minutes; the graph
rebuilds once enough has landed to be worth a generation, or once the live one
is old enough that "enough" has stopped being the right question. `varyz graph
rebuild` asks neither question — an operator saying rebuild means rebuild.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from varyz.domain import utc_now


@dataclass(frozen=True)
class RebuildDecision:
    """Whether to rebuild now, and the number that decided it."""

    due: bool
    reason: str
    new_entries: int = 0
    age_minutes: float | None = None


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def rebuild_due(
    conn: sqlite3.Connection,
    *,
    min_new_entries: int,
    max_age_minutes: int,
    excluded_sources: tuple[str, ...] = (),
    now: datetime | None = None,
) -> RebuildDecision:
    """Decide from two cheap reads: how many entries and how old the graph is.

    Deliberately not a corpus hash comparison. Knowing *exactly* whether the
    archive changed means hashing every row, and the answer would still be yes
    for a single edited character — which is the cost this gate exists to
    avoid. Counting rows misses entries that were revised rather than added;
    the age bound is what catches those, which is why it is not optional.
    """
    excluded = tuple(
        sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
    )
    placeholders = ",".join("?" for _ in excluded)
    where = f" WHERE lower(source) NOT IN ({placeholders})" if excluded else ""
    entries = int(
        conn.execute(
            f"SELECT COUNT(*) FROM entries{where}",  # noqa: S608 - placeholders only
            excluded,
        ).fetchone()[0]
    )
    active = conn.execute(
        "SELECT entries_total, activated_at FROM graph_runs WHERE state='active'"
    ).fetchone()
    if active is None:
        return RebuildDecision(True, "no active generation", new_entries=entries)

    new_entries = max(entries - int(active["entries_total"]), 0)
    activated = _parse(active["activated_at"])
    age_minutes = None
    if activated is not None:
        moment = now or utc_now()
        if activated.tzinfo is None:
            activated = activated.replace(tzinfo=moment.tzinfo)
        age_minutes = max((moment - activated).total_seconds() / 60, 0.0)

    if new_entries >= min_new_entries:
        return RebuildDecision(
            True,
            f"{new_entries} new entries since the live graph",
            new_entries=new_entries,
            age_minutes=age_minutes,
        )
    if age_minutes is None:
        # An active generation with no activation timestamp predates the field
        # or was written by hand; the age bound cannot fire, so rebuild.
        return RebuildDecision(
            True, "live graph has no activation time", new_entries=new_entries
        )
    if age_minutes >= max_age_minutes:
        return RebuildDecision(
            True,
            f"live graph is {age_minutes:.0f} minutes old",
            new_entries=new_entries,
            age_minutes=age_minutes,
        )
    return RebuildDecision(
        False,
        f"{new_entries} new entries, live graph {age_minutes:.0f} minutes old",
        new_entries=new_entries,
        age_minutes=age_minutes,
    )

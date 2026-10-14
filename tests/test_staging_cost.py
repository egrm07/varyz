"""What a rebuild pays per entry once the extraction cache has done its job.

The cache takes the model out of the loop, which left staging as the whole
cost: one lookup in `entity_aliases` that could only use `entity_id`, on an
archive where a single entity — the user — owned 290,968 of 467,134 alias rows.
These tests pin the index that made that lookup selective, the per-entry rows
staging no longer writes, and the reason the daemon now notices a migration at
all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from varyz.autosync import code_fingerprint
from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.evidence import SQLiteEvidenceReader
from varyz.graph.extract import DeterministicExtractor, GraphBuildService
from varyz.graph.identity import self_entity_id
from varyz.graph.repository import GraphRepository

STAGING_LOOKUP = """SELECT id FROM entity_aliases
   WHERE entity_id=? AND graph_generation_id=? AND source_entry_id=?"""


def test_the_staging_lookup_uses_all_three_of_its_columns(conn):
    plan = conn.execute(
        "EXPLAIN QUERY PLAN " + STAGING_LOOKUP, ("entity", "generation", "entry")
    ).fetchall()
    detail = " ".join(row["detail"] for row in plan)

    assert "idx_entity_alias_staging" in detail
    # Narrowing on entity_id alone is what cost 139 ms an entry: the entity for
    # "me" accumulates an alias row for every entry that mentions the user.
    assert "graph_generation_id=?" in detail
    assert "source_entry_id=?" in detail


def test_the_index_is_ordered_so_a_generation_sweep_can_use_it(conn):
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM entity_aliases WHERE entity_id=? AND graph_generation_id=?",
        ("entity", "generation"),
    ).fetchall()

    assert "idx_entity_alias_staging" in " ".join(row["detail"] for row in plan)


def _entry(conn, external_id: str, body: str, *, day: int = 1) -> None:
    with conn:
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE,
                external_id=external_id,
                body=body,
                occurred_start=datetime(2026, 4, day, 8, 0, tzinfo=UTC),
                captured_at=datetime(2026, 4, day, 8, 0, tzinfo=UTC),
            ),
            source="manual",
            sensitivity=Sensitivity.PERSONAL,
        )


def _rebuild(conn):
    return GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild()


def _self_aliases(conn) -> list[tuple[str, str | None]]:
    return [
        (row["graph_generation_id"], row["source_entry_id"])
        for row in conn.execute(
            "SELECT graph_generation_id, source_entry_id FROM entity_aliases"
            " WHERE namespace='system' AND alias_value='self'"
        )
    ]


def test_an_entry_that_says_nothing_about_me_costs_no_owner_alias(conn):
    """The row every extractor emitted per entry, whether or not it was used."""
    _entry(conn, "weather", "The tram to the office was late again.", day=1)
    _entry(conn, "more-weather", "Rain all week, apparently.", day=2)

    _rebuild(conn)

    # One seeded row for the generation, and not one per entry looked at.
    assert len(_self_aliases(conn)) == 1


def test_the_owner_identity_exists_even_with_nothing_to_attach_it_to(conn):
    """`self_entity_id` is what the whole Mirror hangs off; it may not vanish."""
    _entry(conn, "weather", "The tram to the office was late again.", day=1)

    _rebuild(conn)

    generation, source_entry_id = _self_aliases(conn)[0]
    assert self_entity_id(conn) is not None
    # Seeded by the run, so it is attributed to the generation, not to whichever
    # entry happened to be processed first.
    assert source_entry_id is None
    assert generation


def test_an_owner_claim_still_resolves_to_the_owner(conn):
    _entry(conn, "berlin", "I moved to Berlin in 2019.", day=1)

    _rebuild(conn)

    owner = self_entity_id(conn)
    assert owner is not None
    subjects = {
        row[0] for row in conn.execute("SELECT subject_entity_id FROM claims")
    }
    assert owner in subjects


def test_a_new_migration_changes_the_code_fingerprint(tmp_path, monkeypatch):
    """A daemon holding an old schema is the thing the fingerprint exists for."""
    import varyz.autosync as autosync

    root = tmp_path / "varyz"
    (root / "db" / "migrations").mkdir(parents=True)
    (root / "autosync.py").write_text("# source\n", encoding="utf-8")
    monkeypatch.setattr(autosync, "__file__", str(root / "autosync.py"))
    before = code_fingerprint()

    (root / "db" / "migrations" / "0099_new.sql").write_text(
        "CREATE INDEX whatever ON entries (id);\n", encoding="utf-8"
    )

    assert code_fingerprint() != before

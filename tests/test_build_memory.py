"""A rebuild names the archive without holding it, and survives a bad entry.

Two costs the build loop used to pay whether or not it had to. It materialised
every `Entry` — 13.5 MB of body text plus pydantic overhead on this archive —
solely to compute a corpus hash, and it discarded hours of staged work when a
single model call timed out.
"""

from __future__ import annotations

import hashlib

import pytest

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry
from varyz.evidence import SQLiteEvidenceReader
from varyz.graph.extract import (
    DeterministicExtractor,
    ExtractionUnavailable,
    GraphBuildService,
)
from varyz.graph.repository import GraphRepository


def _entry(conn, external_id: str, body: str):
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(kind=Kind.NOTE, external_id=external_id, body=body),
            source="manual",
            sensitivity="personal",
        )
    return dao.get_entry(conn, entry_id)


def _service(conn, extractor=None, **kwargs):
    return GraphBuildService(
        GraphRepository(conn),
        SQLiteEvidenceReader(conn),
        extractor or DeterministicExtractor(),
        **kwargs,
    )


# --- what a claim's object is allowed to swallow ---------------------------


def test_a_place_is_the_place_and_not_the_rest_of_the_sentence(conn):
    """The object patterns run to the end of the sentence.

    "I moved to Lisbon in March 2025 and I have been renting in Alfama since"
    filed a place called "Lisbon in March 2025 and I have been renting in
    Alfama since": an entity no second sentence can ever match again, carrying
    a date the temporal parser had already read correctly off the same
    sentence.
    """
    entry = _entry(
        conn,
        "clause-tail",
        "I moved to Lisbon in March 2025 and I have been renting in Alfama since.",
    )

    batch = DeterministicExtractor().extract(entry)

    moves = [claim for claim in batch.claims if claim.predicate == "moved_to"]
    assert len(moves) == 1
    assert moves[0].object_surface == "Lisbon"
    # The date is still read; it just belongs to the claim, not to the place.
    assert moves[0].valid_from is not None
    assert (moves[0].valid_from.year, moves[0].valid_from.month) == (2025, 3)
    assert [alias["label"] for alias in batch.aliases if alias["entity_type"] == "place"] == [
        "Lisbon"
    ]


def test_a_place_whose_name_holds_and_keeps_it(conn):
    """Trinidad and Tobago is one place; the cut only fires on a new subject."""
    entry = _entry(conn, "and-name", "I moved to Trinidad and Tobago.")

    batch = DeterministicExtractor().extract(entry)

    moves = [claim for claim in batch.claims if claim.predicate == "moved_to"]
    assert [claim.object_surface for claim in moves] == ["Trinidad and Tobago"]


# --- naming the archive without reading it --------------------------------


def test_the_streamed_fingerprint_is_the_hash_the_old_build_computed(conn):
    """Generation keys are derived from this. Changing it would orphan every
    generation on disk and force a full re-extraction of the whole archive."""
    for index in range(5):
        _entry(conn, f"fp-{index}", f"I moved to Berlin in 201{index}.")
    reader = SQLiteEvidenceReader(conn)
    entries = list(reader.iter_entries())

    fingerprint = reader.corpus_fingerprint()

    assert fingerprint.entries == 5
    assert (
        fingerprint.content_hash
        == hashlib.sha256(
            "\n".join(f"{entry.id}:{entry.content_hash}" for entry in entries).encode()
        ).hexdigest()
    )


def test_an_empty_archive_still_has_a_name(conn):
    fingerprint = SQLiteEvidenceReader(conn).corpus_fingerprint()

    assert fingerprint.entries == 0
    assert fingerprint.content_hash == hashlib.sha256(b"").hexdigest()


def test_excluded_sources_do_not_enter_the_graph_corpus(conn):
    kept = _entry(conn, "kept", "I decided to keep the local model.")
    with conn:
        social_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(kind=Kind.MESSAGE, external_id="social", body="old chat"),
            source="WhatsApp",
            sensitivity="sensitive",
        )

    reader = SQLiteEvidenceReader(conn, excluded_sources=(" WhatsApp ", "telegram"))

    assert [entry.id for entry in reader.iter_entries()] == [kept.id]
    assert reader.corpus_fingerprint().entries == 1
    assert reader.get_entry(social_id) is None


def test_editing_one_entry_changes_the_fingerprint(conn):
    _entry(conn, "edited", "I moved to Berlin.")
    reader = SQLiteEvidenceReader(conn)
    before = reader.corpus_fingerprint()

    _entry(conn, "edited", "I moved to Porto.")

    assert reader.corpus_fingerprint().content_hash != before.content_hash


def test_a_rebuild_never_materialises_the_archive(conn):
    """The walk resumes in SQL. Reading every entry to skip most of them is
    what the checkpoint exists to avoid, and what the corpus hash forced."""
    entries = [
        _entry(conn, f"walk-{index}", f"I lived in Berlin in 201{index}.")
        for index in range(4)
    ]
    ordered = sorted(entry.id for entry in entries)

    class WatchedReader(SQLiteEvidenceReader):
        def __init__(self, conn):
            super().__init__(conn)
            self.after_ids: list[str | None] = []

        def iter_entries(self, *, after_id=None):
            self.after_ids.append(after_id)
            yield from super().iter_entries(after_id=after_id)

    reader = WatchedReader(conn)
    service = GraphBuildService(GraphRepository(conn), reader, DeterministicExtractor())
    checks: list[int] = []

    def should_stop() -> bool:
        checks.append(len(checks))
        return len(checks) > 2

    first = service.rebuild(should_stop=should_stop)
    assert first.stopped and first.entries_processed == 2

    service.rebuild()

    # One walk per rebuild, and the second one starts at the checkpoint rather
    # than reading from the beginning and throwing the first half away.
    assert reader.after_ids == [None, ordered[1]]


# --- surviving one bad entry ----------------------------------------------


class Flaky:
    """Fails a named entry a fixed number of times, then answers normally."""

    name = "flaky-fixture"
    version = "1"
    model = None
    prompt_version = None

    def __init__(self, bad_id: str, failures: int):
        self.bad_id = bad_id
        self.failures = failures
        self.calls: list[str] = []
        self.delegate = DeterministicExtractor()

    def extract(self, entry):
        self.calls.append(entry.id)
        if entry.id == self.bad_id and self.failures:
            self.failures -= 1
            raise TimeoutError("read timeout")
        return self.delegate.extract(entry)


def test_a_prompt_that_answers_on_the_retry_costs_nothing(conn):
    first = _entry(conn, "retry-a", "I moved to Berlin.")
    second = _entry(conn, "retry-b", "I worked on Varyz.")
    slow = min(first.id, second.id)
    extractor = Flaky(slow, failures=1)

    result = _service(conn, extractor).rebuild()

    assert result.entries_processed == 2
    assert result.entries_failed == 0
    assert extractor.calls.count(slow) == 2
    assert GraphRepository(conn).active_run().id == result.run_id


def test_one_unreadable_entry_is_quarantined_and_the_rest_are_built(conn):
    first = _entry(conn, "quarantine-a", "I moved to Berlin.")
    second = _entry(conn, "quarantine-b", "I moved to Porto.")
    doomed = min(first.id, second.id)

    result = _service(conn, Flaky(doomed, failures=99)).rebuild()

    assert result.entries_processed == 2
    assert result.entries_failed == 1
    assert GraphRepository(conn).active_run().id == result.run_id
    rows = conn.execute(
        """SELECT entry_id, reason_code, candidate_json FROM predicate_quarantine
           WHERE graph_generation_id=?""",
        (result.run_id,),
    ).fetchall()
    assert [(row["entry_id"], row["reason_code"]) for row in rows] == [
        (doomed, "extraction_failed")
    ]
    # The other entry's claim is in the graph, so this is a real generation and
    # not an activated husk.
    assert conn.execute(
        "SELECT COUNT(*) FROM claims WHERE graph_generation_id=?", (result.run_id,)
    ).fetchone()[0] >= 1


def test_a_quarantined_failure_records_no_archive_text(conn):
    entry = _entry(conn, "private", "I moved to Berlin with Anna.")

    class Leaky:
        name = "leaky"
        version = "1"
        model = None
        prompt_version = None

        def extract(self, target):
            raise ValueError(f"invalid schema near {target.body!r}")

    result = _service(conn, Leaky()).rebuild()

    stored = conn.execute(
        "SELECT candidate_json FROM predicate_quarantine WHERE entry_id=?", (entry.id,)
    ).fetchone()["candidate_json"]
    assert "Berlin" not in stored and "Anna" not in stored
    assert "ValueError" in stored
    assert result.entries_failed == 1


def test_a_run_of_failures_stops_the_rebuild_rather_than_emptying_the_graph(conn):
    for index in range(6):
        _entry(conn, f"dead-{index}", f"I lived in Berlin in 201{index}.")

    class Dead:
        name = "dead"
        version = "1"
        model = None
        prompt_version = None

        def extract(self, entry):
            raise TimeoutError("read timeout")

    with pytest.raises(ExtractionUnavailable):
        _service(conn, Dead(), max_consecutive_failures=3).rebuild()

    assert GraphRepository(conn).active_run() is None
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE state='failed'"
    ).fetchone()[0] == 1


def test_a_success_forgives_the_failures_before_it(conn):
    """Two bad entries scattered through an archive are not a dead model."""
    entries = [
        _entry(conn, f"mixed-{index}", f"I lived in Berlin in 201{index}.")
        for index in range(6)
    ]
    ordered = sorted(entry.id for entry in entries)
    doomed = {ordered[1], ordered[4]}

    class Scattered:
        name = "scattered"
        version = "1"
        model = None
        prompt_version = None

        def __init__(self):
            self.delegate = DeterministicExtractor()

        def extract(self, entry):
            if entry.id in doomed:
                raise TimeoutError("read timeout")
            return self.delegate.extract(entry)

    result = _service(conn, Scattered(), max_consecutive_failures=2).rebuild()

    assert result.entries_processed == 6
    assert result.entries_failed == 2
    assert GraphRepository(conn).active_run().id == result.run_id

"""Deterministic graph infrastructure, temporal semantics, and overrides."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from varyz.retrieval.understand import understand

from varyz.db import dao
from varyz.deletion import forget_entry
from varyz.domain import Kind, NormalizedEntry, Sensitivity, TimePrecision
from varyz.evidence import SQLiteEvidenceReader
from varyz.graph.doctor import diagnose
from varyz.graph.extract import (
    DeterministicExtractor,
    ExtractionUnavailable,
    GraphBuildService,
)
from varyz.graph.models import (
    ClaimCandidate,
    ClaimModality,
    EvidenceCandidate,
    ExtractionBatch,
    ObjectType,
)
from varyz.graph.overrides import append_override
from varyz.graph.repository import GraphRepository
from varyz.graph.temporal import parse_time_expression, parse_time_interval
from varyz.privacy.labels import resource_label
from varyz.retrieval.graph_search import GraphQueryService
from varyz.retrieval.planner import plan_query


def _entry(
    conn,
    external_id: str,
    body: str,
    *,
    sensitivity=Sensitivity.PERSONAL,
    captured_at: datetime | None = None,
):
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE,
                external_id=external_id,
                body=body,
                captured_at=captured_at,
            ),
            source="manual",
            sensitivity=sensitivity,
        )
    return dao.get_entry(conn, entry_id)


def _rebuild(conn):
    return GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild()


def test_context_platform_migration_is_additive(conn):
    for table in (
        "entities",
        "claims",
        "claim_evidence",
        "graph_overrides",
        "jobs",
        "internal_events",
        "context_requests",
        "egress_receipts",
    ):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM predicate_registry").fetchone()[0] >= 10
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_deterministic_claims_preserve_modality_time_and_evidence(conn):
    _entry(conn, "fact", "I moved to Berlin on 2021-03-02.")
    _entry(conn, "plan", "I may move to Berlin next month.")
    _entry(conn, "dream", "I dreamed I moved to Berlin.")
    result = _rebuild(conn)

    assert result.claims == 3
    rows = conn.execute(
        """SELECT modality, valid_from, temporal_inferred FROM claims
           ORDER BY modality"""
    ).fetchall()
    assert {row["modality"] for row in rows} == {"dream", "fact", "plan"}
    fact = next(row for row in rows if row["modality"] == "fact")
    assert fact["valid_from"].startswith("2021-03-02")
    assert conn.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == 3
    assert not [issue for issue in diagnose(conn) if issue.severity == "error"]


def test_context_dependent_affirmations_do_not_become_beliefs(conn):
    for index, body in enumerate(
        ("I think so", "I think so too.", "I believe so but idk.")
    ):
        _entry(conn, f"affirmation-{index}", body)
    _entry(conn, "substantive-belief", "I think software should stay local.")

    result = _rebuild(conn)

    assert result.claims == 1
    claim = conn.execute(
        "SELECT predicate, object_surface, modality FROM claims"
    ).fetchone()
    assert dict(claim) == {
        "predicate": "believed",
        "object_surface": "software should stay local",
        "modality": "belief",
    }


def test_rebuild_is_idempotent_and_activation_is_single_generation(conn):
    _entry(conn, "one", "I lived in Madrid in 2024.")
    first = _rebuild(conn)
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("graph_runs", "claims", "claim_evidence")
    }
    second = _rebuild(conn)
    assert second.no_op is True
    assert first.run_id == second.run_id
    assert counts == {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in counts
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE state='active'"
    ).fetchone()[0] == 1


def test_display_names_become_candidates_not_automatic_merges(conn):
    _entry(conn, "a", "From: Anna\nChat: one\n\nI moved to Lisbon.")
    _entry(conn, "b", "From: Anna\nChat: two\n\nI lived in Rome.")
    _rebuild(conn)

    annas = conn.execute(
        """SELECT DISTINCT entity_id FROM entity_aliases
           WHERE namespace='display_name' AND normalized_value='anna'"""
    ).fetchall()
    assert len(annas) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_candidates WHERE state='pending'"
    ).fetchone()[0] >= 1


def test_conflicting_claims_are_linked_and_preserved(conn):
    _entry(conn, "a", "I lived in Berlin in 2021.")
    _entry(conn, "b", "I lived in Lisbon in 2021.")
    _rebuild(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM claims WHERE lifecycle_state='active'"
    ).fetchone()[0] == 2
    assert conn.execute(
        """SELECT COUNT(*) FROM claim_relations
           WHERE relation_type='contradicts'"""
    ).fetchone()[0] == 1


def test_two_different_beliefs_are_two_beliefs_and_not_a_disagreement(conn):
    """A person may hold any number of convictions at once.

    Living in Berlin rules out living in Lisbon; believing one thing rules out
    nothing. Until this was separated, every belief in a cluster was linked as
    contradicting every other, and the Mirror showed a person's own supporting
    sentence back to them under "evidence against" — one real conclusion arrived
    with three such items and a single supporting one.
    """
    _entry(conn, "a", "I believe software should stay local.")
    _entry(conn, "b", "I believe measurement matters more than guessing.")
    _rebuild(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM claims WHERE lifecycle_state='active'"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM claim_relations WHERE relation_type='contradicts'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "denial",
    [
        "I believe software should not stay local.",
        "I don't believe software should stay local.",
        "I do not believe software should stay local.",
        "I no longer believe software should stay local.",
    ],
)
def test_the_same_belief_asserted_and_denied_still_contradicts(conn, denial):
    """However the denial is spelled, it has to meet the assertion.

    The contracted forms are the ones people actually use, and until they were
    read as negation "I don't believe X" matched no claim pattern at all and
    scored as a positive assertion of X — so the only version of a conviction
    the archive could hold was the one its owner had argued against.
    """
    _entry(conn, "a", "I believe software should stay local.")
    _entry(conn, "b", denial)
    _rebuild(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM claim_relations WHERE relation_type='contradicts'"
    ).fetchone()[0] == 1


def test_claim_override_replays_after_changed_corpus(conn):
    entry = _entry(conn, "a", "I moved to Berlin in 2021.")
    _rebuild(conn)
    fingerprint = conn.execute("SELECT fingerprint FROM claims").fetchone()[0]
    append_override(conn, "claim", fingerprint, "reject", reason="wrong memory")
    assert conn.execute(
        "SELECT lifecycle_state FROM claims WHERE fingerprint=?", (fingerprint,)
    ).fetchone()[0] == "rejected"

    # An unrelated upstream edit creates a new generation with the same claim fingerprint.
    with conn:
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.NOTE,
                external_id=entry.external_id,
                body="I moved to Berlin in 2021. Unrelated footnote.",
            ),
            source="manual",
            sensitivity=Sensitivity.PERSONAL,
        )
    _rebuild(conn)
    active = GraphRepository(conn).active_run()
    assert active
    state = conn.execute(
        """SELECT lifecycle_state FROM claims
           WHERE graph_generation_id=? AND fingerprint=?""",
        (active.id, fingerprint),
    ).fetchone()[0]
    assert state == "rejected"


def test_sensitivity_override_relabels_regenerated_claim(conn):
    _entry(conn, "sensitivity-override", "I moved to Berlin.")
    _rebuild(conn)
    original = conn.execute(
        """SELECT id, fingerprint FROM claims
           WHERE lifecycle_state='active'"""
    ).fetchone()

    append_override(
        conn,
        "claim",
        original["fingerprint"],
        "change_sensitivity",
        {"sensitivity": "sensitive"},
    )
    assert resource_label(conn, "claim", original["id"])[0] == Sensitivity.SENSITIVE

    GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
        # A version of its own, so the same corpus builds a second generation
        # instead of no-opping. Derived from the extractor's rather than
        # hardcoded, so bumping the extractor does not silently turn this into
        # a downgrade test.
    ).rebuild(version=f"{DeterministicExtractor.version}.1")
    regenerated = conn.execute(
        """SELECT c.id, c.sensitivity FROM claims c
           JOIN graph_runs g ON g.id=c.graph_generation_id
           WHERE g.state='active' AND c.fingerprint=?""",
        (original["fingerprint"],),
    ).fetchone()
    assert regenerated["sensitivity"] == "sensitive"
    label, reasons = resource_label(conn, "claim", regenerated["id"])
    assert label == Sensitivity.SENSITIVE
    assert "graph_override" in reasons


def test_time_override_normalizes_precision_and_rejects_invalid_values(conn):
    _entry(conn, "time-override", "I lived in Berlin.")
    _rebuild(conn)
    fingerprint = conn.execute("SELECT fingerprint FROM claims").fetchone()[0]

    with pytest.raises(ValueError, match="invalid ISO"):
        append_override(
            conn,
            "claim",
            fingerprint,
            "correct_time",
            {
                "valid_from": "not-a-date",
                "valid_to": None,
                "temporal_precision": "day",
            },
        )
    append_override(
        conn,
        "claim",
        fingerprint,
        "correct_time",
        {
            "valid_from": "2019",
            "valid_to": "2021",
            "temporal_precision": "year",
        },
    )
    corrected = conn.execute(
        """SELECT valid_from, valid_to, temporal_precision
           FROM claims WHERE fingerprint=?""",
        (fingerprint,),
    ).fetchone()
    assert corrected["valid_from"].startswith("2019-01-01T00:00:00")
    assert corrected["valid_to"].startswith("2021-12-31T23:59:59")
    assert corrected["temporal_precision"] == "year"


def test_merge_and_split_overrides_are_reversible(conn):
    _entry(conn, "a", "From: Anna\nChat: one\n\nI moved to Lisbon.")
    _entry(conn, "b", "From: Anna\nChat: two\n\nI lived in Rome.")
    _rebuild(conn)
    entities = [
        row["entity_id"]
        for row in conn.execute(
            """SELECT DISTINCT entity_id FROM entity_aliases
               WHERE namespace='display_name' AND normalized_value='anna'"""
        )
    ]
    assert len(entities) == 2
    append_override(
        conn,
        "entity",
        entities[1],
        "merge",
        {"merged_into_id": entities[0]},
    )
    merged = conn.execute(
        "SELECT status, merged_into_id FROM entities WHERE id=?", (entities[1],)
    ).fetchone()
    assert tuple(merged) == ("merged", entities[0])
    append_override(conn, "entity", entities[1], "split")
    restored = conn.execute(
        "SELECT status, merged_into_id FROM entities WHERE id=?", (entities[1],)
    ).fetchone()
    assert tuple(restored) == ("active", None)


def test_a_merge_into_an_entity_that_is_gone_is_replayed_as_a_no_op(conn):
    """merged_into_id is a foreign key, and overrides outlive their targets.

    Every override is replayed onto every rebuild. An entity the surviving
    side of a merge named can stop being produced -- the entry it came from
    was forgotten, the extractor changed -- and writing the merge anyway
    raised IntegrityError: FOREIGN KEY constraint failed. That aborted the
    rebuild, so the archive could never rebuild its graph again.
    """
    _entry(conn, "a", "From: Anna\nChat: one\n\nI moved to Lisbon.")
    _rebuild(conn)
    entity = conn.execute("SELECT id FROM entities LIMIT 1").fetchone()["id"]

    append_override(conn, "entity", entity, "merge", {"merged_into_id": "person-gone"})

    survivor = conn.execute(
        "SELECT status, merged_into_id FROM entities WHERE id=?", (entity,)
    ).fetchone()
    assert tuple(survivor) == ("active", None)
    # And the rebuild that replays it still finishes.
    _rebuild(conn)


def test_deletion_invalidates_dependent_claim_and_evidence(conn, vault):
    entry = _entry(conn, "a", "I moved to Berlin in 2021.")
    _rebuild(conn)
    assert forget_entry(conn, vault, entry.id)
    assert conn.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == 0
    assert conn.execute("SELECT lifecycle_state FROM claims").fetchone()[0] == "withdrawn"
    assert not [
        issue for issue in diagnose(conn) if issue.code == "claim_without_active_evidence"
    ]


def test_relative_time_requires_observation_anchor():
    unknown = parse_time_expression("next month", None)
    assert unknown.value is None and unknown.inferred is True
    anchored = parse_time_expression(
        "next month", datetime(2024, 12, 15, tzinfo=UTC)
    )
    assert anchored.value == datetime(2025, 1, 1, tzinfo=UTC)
    assert anchored.inferred is True
    decade = parse_time_expression("during the 1990s", None)
    assert decade.value == datetime(1990, 1, 1, tzinfo=UTC)
    assert str(decade.precision) == "decade"
    exact = parse_time_expression("at 2025-02-03T10:15:00+01:00", None)
    assert exact.value == datetime(2025, 2, 3, 9, 15, tzinfo=UTC)
    assert str(exact.precision) == "exact"


def test_extraction_uses_source_capture_time_as_relative_time_anchor(conn):
    captured = datetime(2025, 1, 18, 14, 30, tzinfo=UTC)
    entry = _entry(
        conn,
        "captured-relative",
        "I may move to Lisbon next month.",
        captured_at=captured,
    )

    claim = DeterministicExtractor().extract(entry).claims[0]

    assert claim.observed_at == captured
    assert claim.valid_from == datetime(2025, 2, 1, tzinfo=UTC)
    assert claim.temporal_inferred is True
    assert claim.original_time_text == "next month"


def test_explicit_date_range_closes_claim_interval(conn):
    entry = _entry(
        conn,
        "closed-interval",
        "I lived in Berlin from 2019-09 to 2021-04.",
    )

    claim = DeterministicExtractor().extract(entry).claims[0]
    open_start = parse_time_interval("I lived there until 2022", None)

    assert claim.object_surface == "Berlin"
    assert claim.valid_from == datetime(2019, 9, 1, tzinfo=UTC)
    assert claim.valid_to == datetime(
        2021, 4, 30, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert str(claim.temporal_precision) == "month"
    assert claim.original_time_text == "from 2019-09 to 2021-04"
    assert open_start.valid_from is None
    assert open_start.valid_to == datetime(
        2022, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )

    named_months = parse_time_interval(
        "from March 2021 through April 2021", None
    )
    assert named_months.valid_from == datetime(2021, 3, 1, tzinfo=UTC)
    assert named_months.valid_to == datetime(
        2021, 4, 30, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert named_months.precision == TimePrecision.MONTH


def test_invalid_calendar_tokens_fail_closed_and_short_year_ranges_work(conn):
    impossible_month = parse_time_expression("met in 2023-24", None)
    impossible_day = parse_time_expression("met on 2023-02-31", None)
    shorthand = parse_time_interval("me and sam in 2023-24", None)

    assert impossible_month.value is None
    assert impossible_month.precision == TimePrecision.UNKNOWN
    assert impossible_day.value is None
    assert impossible_day.precision == TimePrecision.UNKNOWN
    assert shorthand.valid_from == datetime(2023, 1, 1, tzinfo=UTC)
    assert shorthand.valid_to == datetime(
        2024, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert shorthand.precision == TimePrecision.YEAR

    _entry(conn, "invalid-month-regression", "me and sam in 2023-24")
    result = _rebuild(conn)
    assert result.entries_processed == 1


def test_graph_search_expands_claims_to_original_entries(conn):
    entry = _entry(conn, "a", "I moved to Berlin on 2021-03-02.")
    _rebuild(conn)
    service = GraphQueryService(conn)
    plan = plan_query("When did I move to Berlin?")
    graph_hits = service.search("When did I move to Berlin?", plan)
    expanded = service.expand_to_entries(graph_hits)
    assert graph_hits
    assert expanded[0].entry.id == entry.id
    assert "moved to Berlin" in expanded[0].chunk


def test_invalid_model_evidence_is_quarantined(conn):
    entry = _entry(conn, "a", "I moved to Berlin.")

    class BadExtractor:
        name = "bad"
        version = "1"
        model = "fixture"
        prompt_version = "1"

        def extract(self, source):
            return ExtractionBatch(
                aliases=(
                    {
                        "key": "self",
                        "entity_type": "person",
                        "label": "Me",
                        "namespace": "system",
                        "value": "self",
                        "source_entry_id": source.id,
                        "verification_state": "verified",
                    },
                ),
                claims=(
                    ClaimCandidate(
                        subject_key="self",
                        predicate="moved_to",
                        object_type=ObjectType.TEXT,
                        object_value="Berlin",
                        object_surface="Berlin",
                        modality=ClaimModality.FACT,
                        evidence=(
                            EvidenceCandidate(
                                entry_id=source.id, quote="text that is not there"
                            ),
                        ),
                    ),
                ),
            )

    result = GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), BadExtractor()
    ).rebuild()
    assert result.quarantined == 1
    assert result.claims == 0
    assert entry.body == dao.get_entry(conn, entry.id).body


def test_failed_rebuild_does_not_replace_active_generation(conn):
    _entry(conn, "a", "I moved to Berlin.")
    active = _rebuild(conn).run_id

    class FailingExtractor:
        name = "fail"
        version = "1"
        model = None
        prompt_version = None

        def extract(self, entry):
            raise RuntimeError("fixture failure")

    with pytest.raises(ExtractionUnavailable):
        GraphBuildService(
            GraphRepository(conn),
            SQLiteEvidenceReader(conn),
            FailingExtractor(),
            max_consecutive_failures=1,
        ).rebuild()
    assert GraphRepository(conn).active_run().id == active
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE state='failed'"
    ).fetchone()[0] == 1


def test_partial_rebuild_resumes_from_committed_entry_checkpoint(conn):
    first = _entry(conn, "resume-a", "I moved to Berlin.")
    second = _entry(conn, "resume-b", "I worked on Varyz.")
    ordered = sorted((first, second), key=lambda entry: entry.id)

    class BrokenUntilFixed:
        name = "resume-fixture"
        version = "1"
        model = None
        prompt_version = None

        def __init__(self):
            self.fixed = False
            self.calls = []
            self.delegate = DeterministicExtractor()

        def extract(self, entry):
            self.calls.append(entry.id)
            if entry.id == ordered[1].id and not self.fixed:
                raise RuntimeError("simulated crash after checkpoint")
            return self.delegate.extract(entry)

    extractor = BrokenUntilFixed()
    service = GraphBuildService(
        GraphRepository(conn),
        SQLiteEvidenceReader(conn),
        extractor,
        max_consecutive_failures=1,
    )
    with pytest.raises(ExtractionUnavailable):
        service.rebuild()
    failed = conn.execute(
        "SELECT * FROM graph_runs WHERE extractor_name='resume-fixture'"
    ).fetchone()
    assert failed["state"] == "failed"
    assert failed["checkpoint_entry_id"] == ordered[0].id
    assert failed["entries_processed"] == 1

    extractor.fixed = True
    result = service.rebuild()

    assert result.entries_processed == 2
    assert extractor.calls.count(ordered[0].id) == 1
    assert GraphRepository(conn).active_run().id == result.run_id


def test_stop_request_leaves_the_run_staged_and_resumable(conn):
    """The kill switch has to land mid-rebuild, and land without losing work."""
    for index in range(3):
        _entry(conn, f"stop-{index}", f"I lived in Berlin in 201{index}.")
    service = GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
    )
    checks: list[int] = []

    def stop_after_two() -> bool:
        checks.append(len(checks))
        return len(checks) > 2

    stopped = service.rebuild(should_stop=stop_after_two)

    assert stopped.stopped is True
    assert stopped.entries_processed == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE state='active'"
    ).fetchone()[0] == 0

    resumed = service.rebuild()

    assert resumed.stopped is False
    assert resumed.entries_processed == 3
    assert GraphRepository(conn).active_run().id == resumed.run_id


def test_an_older_extractor_generation_cannot_supersede_a_newer_graph(conn):
    """A daemon holding last week's code must not overwrite this week's graph."""
    _entry(conn, "downgrade", "I lived in Berlin.")
    service = GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
    )
    newer = service.rebuild(version="5")

    with pytest.raises(RuntimeError, match="refusing to supersede"):
        service.rebuild(version="4")

    assert GraphRepository(conn).active_run().id == newer.run_id

    rolled_back = service.rebuild(version="4", allow_downgrade=True)

    assert GraphRepository(conn).active_run().id == rolled_back.run_id


def test_same_input_coverage_loss_cannot_activate_silently(conn):
    _entry(conn, "coverage", "I lived in Berlin.")
    repository = GraphRepository(conn)
    active_result = GraphBuildService(
        repository, SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild(version="5")
    active = repository.active_run()
    assert active is not None
    replacement, _ = repository.begin_run(
        generation_key="coverage-loss-fixture",
        extractor_name=active.extractor_name,
        extractor_version="6",
        model=active.model,
        prompt_version=active.prompt_version,
        input_content_hash=active.input_content_hash,
        entries_total=active.entries_total,
    )

    with pytest.raises(RuntimeError, match="same-input coverage"):
        repository.activate(replacement.id)

    assert repository.active_run().id == active_result.run_id


def test_a_previously_approved_graph_can_be_rolled_back_without_rebuilding(conn):
    _entry(conn, "first", "I lived in Berlin.")
    repository = GraphRepository(conn)
    first = GraphBuildService(
        repository, SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild(version="5")
    _entry(conn, "second", "I moved to Paris.")
    second = GraphBuildService(
        repository, SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild(version="6")

    repository.rollback(first.run_id)

    assert repository.active_run().id == first.run_id
    assert conn.execute(
        "SELECT state FROM graph_runs WHERE id=?", (second.run_id,)
    ).fetchone()[0] == "superseded"
    event = conn.execute(
        """SELECT event_type FROM internal_events
            WHERE aggregate_id=? ORDER BY rowid DESC LIMIT 1""",
        (first.run_id,),
    ).fetchone()
    assert event[0] == "graph_generation_rolled_back"


def test_service_treats_an_explicit_version_as_permission_to_roll_back(conn, cfg):
    """Naming a version is the operator choosing; a stale default is not."""
    from varyz.graph.service import rebuild_graph

    _entry(conn, "service-downgrade", "I lived in Berlin.")
    cfg.graph.extractor = "deterministic"
    cfg.graph.extractor_version = "5"
    newer = rebuild_graph(conn, cfg)

    cfg.graph.extractor_version = "4"
    with pytest.raises(RuntimeError, match="refusing to supersede"):
        rebuild_graph(conn, cfg)
    assert GraphRepository(conn).active_run().id == newer.run_id

    older = rebuild_graph(conn, cfg, version="4")

    assert GraphRepository(conn).active_run().id == older.run_id


def test_a_question_that_names_no_subject_is_asking_for_the_whole_class(conn):
    """The other half of the precision guard, and the half that was missing.

    "What have I promised and not delivered?" names no person, no project and
    no topic. The archive held twenty-nine promises and answered with none of
    them: the question matched the predicate, every other word matched nothing,
    and the guard against answering one subject with another -- correct for a
    question that has a subject -- threw the class away for one that does not.
    """

    _entry(conn, "a", "I promised to send Dana the revised contract.")
    _entry(conn, "b", "I promised to call the landlord about the boiler.")
    _entry(conn, "c", "I decided to use Postgres for the ingest path.")
    _rebuild(conn)
    service = GraphQueryService(conn)

    question = "What have I promised someone and not delivered?"
    hits = service.search(question, plan_query(question))
    assert hits
    assert {hit.claim.predicate for hit in hits} == {"promised_to"}

    # Newest first: the promise made last is the one still owed.
    question = "What have I decided?"
    hits = service.search(question, plan_query(question))
    assert {hit.claim.predicate for hit in hits} == {"decided_to"}

    # And one word from outside the class still names a subject, so a subject
    # the archive has never heard of is still answered with nothing.
    question = "What did I decide about database indexing performance?"
    hits = service.search(question, plan_query(question))
    assert not hits, [hit.claim.object_surface for hit in hits]


def test_the_same_promise_said_twice_takes_one_slot_and_brings_both_quotes(conn):
    """Half the archive's promises are in a duplicate pair.

    Ten slots spent on six distinct statements is a thinner answer to "what
    have I promised" than the archive can give, and the graph already knew
    which of them were restatements -- it had written the `duplicates` relation
    and the search handed it back as decoration while letting the restatement
    take a slot of its own.
    """

    _entry(conn, "a", "I promised to send Dana the revised contract.")
    _entry(conn, "b", "I promised to send Dana the revised contract.")
    _entry(conn, "c", "I promised to call the landlord about the boiler.")
    _rebuild(conn)

    question = "What have I promised someone and not delivered?"
    hits = GraphQueryService(conn).search(question, plan_query(question))

    assert len(hits) == 2, [hit.claim.object_surface for hit in hits]
    assert len({hit.claim.object_surface for hit in hits}) == 2
    restated = [hit for hit in hits if hit.restated_by]
    assert len(restated) == 1
    # The occasion it was said again is evidence for the same promise, not a
    # second promise: one hit, both quotes.
    assert len(restated[0].evidence) == 2
    assert len(restated[0].restated_by) == 1


def test_an_answer_says_when_each_claim_was_said(conn):
    """`observed_at` is the only date most of the archive has.

    Every promise in the real graph carries one and none of them carries an
    asserted_at, a valid_from or a valid_to, so a payload that emitted only
    those three answered "what have I promised" with undated statements -- not
    orderable, not ageable, last week's indistinguishable from 2025's.
    """

    _entry(
        conn,
        "a",
        "I promised to send Dana the revised contract.",
        captured_at=datetime(2025, 3, 3, 9, 0, tzinfo=UTC),
    )
    _rebuild(conn)

    payload = understand(conn, "What have I promised?", limit=5, max_chars=200)

    assert payload["found"] == 1
    insight = payload["insights"][0]
    assert insight["observed_at"]
    assert insight["restated_by"] == []


def test_graph_search_will_not_answer_one_subject_with_another(conn):
    """The assertion whose absence let a whole class of nonsense ship.

    Every graph search test before this one asked a question the fixture could
    answer and checked the answer came back. None asked a question it could not
    answer, so nothing noticed that the search returned its most confident
    claims regardless of subject: a question's own grammar -- the stop list held
    fourteen words and none of the common ones -- and its verb, which matched
    the predicate of every claim in the same class, were doing the selecting.
    """

    _entry(conn, "a", "I moved to Berlin on 2021-03-02.")
    _entry(conn, "b", "I decided to use Postgres for the ingest path.")
    _rebuild(conn)
    service = GraphQueryService(conn)

    question = "When did I move to Lisbon?"
    hits = service.search(question, plan_query(question))
    assert [hit.claim.object_surface for hit in hits] != ["Berlin"]

    question = "What did I decide about database indexing performance?"
    hits = service.search(question, plan_query(question))
    assert not hits, [hit.claim.object_surface for hit in hits]

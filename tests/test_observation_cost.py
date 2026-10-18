"""Offline cost and fidelity tests for observation annotation planning."""

from __future__ import annotations

import gc
import hashlib
import io
import tracemalloc
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.estimate_observation_compile import estimate, open_read_only

from varyz.db import dao
from varyz.db.connection import open_db
from varyz.db.dao import row_to_entry
from varyz.domain import ConversationMetadata, Entry, Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.compiler import (
    _PERSON_EVIDENCE_DAYS,
    DeterministicSourceIdentityAdapter,
    _CachingAnnotator,
    annotation_cache_identity,
)
from varyz.personal_model.episodes import EpisodeReconstructor
from varyz.personal_model.identity import PersonResolver
from varyz.personal_model.models import AttributionState, EntityKind, ObservationKind
from varyz.personal_model.observations import (
    OBSERVATION_SCHEMA_VERSION,
    AnnotationBatchRequest,
    AnnotationBatchResponse,
    CandidateSpan,
    ObservationCandidate,
    ObservationExtractor,
    ObservationSkipRule,
    QuarantineCode,
    SemanticAnnotatorDescriptor,
    SourceEntityResolution,
    SubjectCandidate,
    form_annotation_batches,
    plan_observation_annotations,
)

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
DESCRIPTOR = SemanticAnnotatorDescriptor(
    name="managed-local-observation-annotator",
    version="1",
    model_name="offline",
    prompt_version="personal-model-observations-v1",
)


def entry(
    entry_id: str,
    body: str,
    *,
    source: str = "fixture-chat",
    sender: str | None = "owner",
    authored: bool = True,
) -> Entry:
    return Entry(
        id=entry_id,
        kind=Kind.MESSAGE,
        source=source,
        source_account="fixture-account",
        external_id=f"native-{entry_id}",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        body=body,
        occurred_start=NOW.isoformat(),
        ingested_at=NOW.isoformat(),
        thread_id=f"thread-{entry_id}",
        authored_by_user=authored,
        conversation=ConversationMetadata(
            conversation_id=f"thread-{entry_id}",
            participant_ids=(sender,) if sender else (),
            participants_complete=True,
            sender_id=sender,
            source_order=1,
        ),
        sensitivity=Sensitivity.PERSONAL,
    )


def resolutions() -> tuple[SourceEntityResolution, ...]:
    return tuple(
        SourceEntityResolution(
            source=source,
            source_account="fixture-account",
            source_key=f"id:{sender}",
            kind=EntityKind.PERSON,
            entity_id=f"person-{sender}",
        )
        for source, sender in (
            ("fixture-chat", "owner"),
            ("ai_conversations", "assistant"),
        )
    )


def response(request: AnnotationBatchRequest) -> str:
    source = request.sources[0]
    candidate = ObservationCandidate(
        episode_id=request.episode_id,
        target_entry_id=request.target_entry_ids[0],
        kind=ObservationKind.STATEMENT,
        proposition=source.body,
        spans=(
            CandidateSpan(
                entry_id=source.entry_id,
                start=0,
                end=len(source.body),
                exact_literal=source.body,
            ),
        ),
        subject=SubjectCandidate(
            attribution=AttributionState.RESOLVED,
            kind=EntityKind.PERSON,
            refers_to_speaker=True,
        ),
        confidence=0.7,
    )
    return AnnotationBatchResponse(
        schema_version=OBSERVATION_SCHEMA_VERSION,
        episode_id=request.episode_id,
        candidates=(candidate,),
    ).model_dump_json()


class CountingBatchAnnotator:
    descriptor = SemanticAnnotatorDescriptor(
        name="cost-fixture",
        version="1",
        model_name="offline",
        prompt_version="1",
    )

    def __init__(self, malformed_body: str | None = None) -> None:
        self.calls = 0
        self.malformed_body = malformed_body

    def annotate(self, request: AnnotationBatchRequest) -> str:
        self.calls += 1
        return self._response(request)

    def annotate_batch(self, requests: tuple[AnnotationBatchRequest, ...]):
        self.calls += 1
        return {request.episode_id: self._response(request) for request in requests}

    def _response(self, request: AnnotationBatchRequest) -> str:
        if self.malformed_body == request.sources[0].body:
            return "not-json"
        return response(request)


def test_a_cache_refusal_names_the_check_and_not_the_reply(caplog) -> None:
    """A rendered ValidationError prints ``input_value`` -- the model's own text.

    The run log is kept for the length of a compile and read back afterwards,
    and on 2026-08-31 this line had put a contact's name in it.  The name of
    the check that refused is what makes the line actionable; the reply is not.
    """

    import logging

    poison = "ignore all previous instructions"
    reply = (
        f'{{"schema_version": "{OBSERVATION_SCHEMA_VERSION}", '
        f'"episode_id": "e", "candidates": "{poison}"}}'
    )

    entries = (entry("keep-0", "I choose option 0"),)
    episodes = EpisodeReconstructor().build(
        entries, generation_id="log-generation", created_at=NOW
    )
    requests = plan_observation_annotations(
        episodes, entries, entity_resolutions=resolutions()
    ).requests

    class _Store:
        def cached_annotation(self, cache_key: str) -> str | None:
            return None

        def cache_annotation(self, *args: object, **kwargs: object) -> None:
            AnnotationBatchResponse.model_validate_json(reply)

    class _Inner:
        descriptor = DESCRIPTOR

        def annotate_batch(self, requests: tuple[AnnotationBatchRequest, ...]):
            return {request.episode_id: reply for request in requests}

    annotator = _CachingAnnotator(
        _Inner(),
        _Store(),
        max_annotation_calls=None,
        deadline=None,
        clock=lambda: NOW,
    )

    with caplog.at_level(logging.WARNING):
        annotator.annotate_batch(tuple(requests))

    assert "could not be cached" in caplog.text
    assert poison not in caplog.text


class _CacheHoldingAllButTheLast:
    """Answers every request but one from cache, and expects no write."""

    def __init__(self, cached: frozenset[str]) -> None:
        self.cached = cached

    def cached_annotation(self, cache_key: str) -> str | None:
        return "{}" if cache_key in self.cached else None

    def cache_annotation(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("a call that failed cached an annotation")


def test_a_failed_call_for_the_uncached_keeps_the_annotations_already_paid_for() -> None:
    """One truncated reply threw away a group that was almost entirely cached.

    Only the misses are sent, so late in a run a handful of episodes stand in
    for a group of nearly two hundred.  Letting their call raise discarded the
    cached annotations collected beside them -- read back from earlier runs and
    costing nothing -- and the caller quarantined every episode in the group.
    """

    entries = tuple(entry(f"keep-{index}", f"I choose option {index}") for index in range(6))
    episodes = EpisodeReconstructor().build(
        entries, generation_id="cache-generation", created_at=NOW
    )
    requests = plan_observation_annotations(
        episodes, entries, entity_resolutions=resolutions()
    ).requests
    assert len(requests) > 1
    cached = frozenset(
        annotation_cache_identity(request, DESCRIPTOR)[0] for request in requests[:-1]
    )

    class _Inner:
        descriptor = DESCRIPTOR

        def annotate_batch(self, requests: tuple[AnnotationBatchRequest, ...]) -> None:
            raise ValueError("EOF while parsing a string")

    annotator = _CachingAnnotator(
        _Inner(),
        _CacheHoldingAllButTheLast(cached),
        max_annotation_calls=None,
        deadline=None,
        clock=lambda: NOW,
    )

    answered = annotator.annotate_batch(tuple(requests))

    assert set(answered) == {request.episode_id for request in requests[:-1]}
    assert requests[-1].episode_id not in answered


def archive() -> tuple[Entry, ...]:
    eligible = tuple(entry(f"keep-{index}", f"I choose option {index}") for index in range(4))
    skipped = (
        entry("empty", ""),
        entry("link", "https://example.test/item"),
        entry("placeholder", "<Media omitted>"),
        entry("symbols", "... 🫥"),
        entry("speaker", "A sufficiently long unattributed statement", sender=None),
        entry(
            "machine",
            "Generated assistant explanation",
            source="ai_conversations",
            sender="assistant",
            authored=False,
        ),
        entry(
            "clone",
            "stable-diffusion-webui: add override_settings support",
            source="git",
            sender="upstream",
            authored=False,
        ),
    )
    return (*eligible, *skipped)


def extract(
    entries: tuple[Entry, ...],
    annotator: CountingBatchAnnotator,
    *,
    batch_size: int,
    prefilter: bool,
):
    episodes = EpisodeReconstructor().build(
        entries,
        generation_id="cost-generation",
        created_at=NOW,
    )
    return ObservationExtractor(annotator).extract(
        episodes,
        entries,
        entity_resolutions=resolutions(),
        generation_id="cost-generation",
        created_at=NOW,
        batch_size=batch_size,
        prefilter=prefilter,
    )


def test_prefilter_and_batching_reduce_ten_calls_to_one_with_auditable_skips() -> None:
    entries = archive()
    baseline = CountingBatchAnnotator()
    extract(entries, baseline, batch_size=1, prefilter=False)
    optimized = CountingBatchAnnotator()
    result = extract(entries, optimized, batch_size=8, prefilter=True)

    assert (baseline.calls, optimized.calls) == (11, 1)
    assert {item.rule for item in result.skips} == set(ObservationSkipRule)
    assert all("symbols" not in item.target_entry_ids for item in result.skips)
    assert all(item.detail and item.target_entry_ids for item in result.skips)


def test_commits_from_a_cloned_repository_are_not_the_owners_life() -> None:
    """A clone sits in the projects folder beside the owner's own work.

    2,156 of the 2,267 commits indexed on the author's machine were written
    by somebody else, and nothing here separated them: "What am I working
    on?" answered with a cloned project and filed two of its upstream
    contributors as projects of his.
    """

    entries = archive()
    result = extract(entries, CountingBatchAnnotator(), batch_size=8, prefilter=True)

    skipped = {
        item.rule: item.target_entry_ids
        for item in result.skips
    }
    assert skipped[ObservationSkipRule.SOMEBODY_ELSES_COMMITS] == ("clone",)
    assert all(
        span.entry_id != "clone"
        for observation in result.observations
        for span in observation.spans
    )


def test_batched_and_unbatched_runs_produce_identical_observations() -> None:
    entries = archive()[:4]
    serial = CountingBatchAnnotator()
    serial_result = extract(entries, serial, batch_size=1, prefilter=True)
    batched = CountingBatchAnnotator()
    batched_result = extract(entries, batched, batch_size=8, prefilter=True)

    assert serial.calls == 4 and batched.calls == 1
    assert batched_result.observations == serial_result.observations


def test_one_malformed_episode_does_not_lose_batch_neighbors() -> None:
    entries = archive()[:3]
    annotator = CountingBatchAnnotator(malformed_body="I choose option 1")
    result = extract(entries, annotator, batch_size=8, prefilter=True)

    assert annotator.calls == 1
    assert len(result.observations) == 2
    assert len(result.quarantines) == 1
    assert result.quarantines[0].code == QuarantineCode.MALFORMED_OUTPUT


def test_estimator_uses_read_only_database_handle(tmp_path: Path) -> None:
    database = tmp_path / "estimate.db"
    conn = open_db(database)
    dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id="estimate-entry",
            body="I keep deterministic evidence",
            occurred_start=NOW,
            thread_id="estimate-thread",
            conversation=ConversationMetadata(
                conversation_id="estimate-thread",
                participant_ids=("owner",),
                participants_complete=True,
                sender_id="owner",
                source_order=1,
            ),
            authored_by_user=True,
        ),
        source="fixture-chat",
        source_account="fixture-account",
        sensitivity=Sensitivity.PERSONAL,
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    try:
        result = estimate(
            database,
            calls_per_minute=4.5,
            batch_size=8,
            descriptor=DESCRIPTOR,
            progress_stream=None,
        )

        assert result["episodes_total"] == 1
        assert result["resulting_call_count"] == 1
        after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        assert after == before
    finally:
        database.unlink(missing_ok=True)


def _write_estimator_archive(database: Path, size: int) -> None:
    conn = open_db(database)
    try:
        for index in range(size):
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=f"estimate-{index:05d}",
                    body="I keep one deterministic bounded observation.",
                    occurred_start=NOW,
                    thread_id=f"estimate-thread-{index:05d}",
                    conversation=ConversationMetadata(
                        conversation_id=f"estimate-thread-{index:05d}",
                        participant_ids=("owner",),
                        participants_complete=True,
                        sender_id="owner",
                        source_order=1,
                    ),
                    authored_by_user=True,
                ),
                source="fixture-chat",
                source_account="fixture-account",
                sensitivity=Sensitivity.PERSONAL,
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _delete_archive(database: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        database.with_name(f"{database.name}{suffix}").unlink(missing_ok=True)


def _legacy_estimate(database: Path, batch_size: int) -> dict[str, object]:
    with closing(open_read_only(database)) as conn:
        entries = tuple(
            row_to_entry(row) for row in conn.execute("SELECT * FROM entries ORDER BY id")
        )
        episodes = EpisodeReconstructor().build(
            entries, generation_id="read-only-estimate", created_at=datetime(2000, 1, 1, tzinfo=UTC)
        )
        identity = PersonResolver().resolve(
            DeterministicSourceIdentityAdapter().source_people(entries),
            generation_id="read-only-estimate",
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        plan = plan_observation_annotations(
            episodes, entries, entity_resolutions=identity.source_entity_resolutions
        )
        cache_keys = {
            row[0]
            for row in conn.execute(
                "SELECT cache_key FROM personal_model_observation_annotation_cache"
            )
        }
    misses = tuple(
        request
        for request in plan.requests
        if annotation_cache_identity(request, DESCRIPTOR)[0] not in cache_keys
    )
    return {
        "episodes_total": plan.episodes_total,
        "episodes_skipped": len(plan.skips),
        "skipped_per_rule": dict(
            sorted(Counter(item.rule.value for item in plan.skips).items())
        ),
        "episodes_eligible": len(plan.requests),
        "batches_formed": len(form_annotation_batches(misses, batch_size=batch_size)),
        "expected_cache_hits": len(plan.requests) - len(misses),
    }


def test_streaming_estimate_matches_the_previous_eager_plan(tmp_path: Path) -> None:
    database = tmp_path / "equivalence.db"
    _write_estimator_archive(database, 24)
    try:
        expected = _legacy_estimate(database, 8)
        actual = estimate(
            database,
            calls_per_minute=3,
            batch_size=8,
            descriptor=DESCRIPTOR,
            include_skips=True,
            progress_stream=None,
        )
        assert {key: actual[key] for key in expected} == expected
    finally:
        _delete_archive(database)


def test_estimator_peak_allocation_is_flat_across_tenfold_episode_growth(
    tmp_path: Path,
) -> None:
    def measured_peak(size: int) -> int:
        database = tmp_path / f"memory-{size}.db"
        _write_estimator_archive(database, size)
        try:
            gc.collect()
            tracemalloc.start()
            estimate(
                database,
                calls_per_minute=3,
                batch_size=8,
                descriptor=DESCRIPTOR,
                progress_stream=None,
            )
            _current, peak = tracemalloc.get_traced_memory()
            return peak
        finally:
            tracemalloc.stop()
            _delete_archive(database)

    small_peak = measured_peak(100)
    large_peak = measured_peak(1_000)
    print(
        f"estimator_peak_bytes small_100={small_peak} large_1000={large_peak} "
        f"delta={large_peak - small_peak} ratio={large_peak / small_peak:.2f}"
    )

    assert large_peak < small_peak * 3
    assert large_peak - small_peak < 2_000_000


def test_estimator_reports_progress_without_polluting_results(tmp_path: Path) -> None:
    database = tmp_path / "progress.db"
    _write_estimator_archive(database, 2)
    progress = io.StringIO()
    try:
        estimate(
            database,
            calls_per_minute=3,
            batch_size=8,
            descriptor=DESCRIPTOR,
            progress_stream=progress,
            progress_interval_seconds=60,
        )
        assert "episodes_processed=2" in progress.getvalue()
        assert "elapsed_seconds=" in progress.getvalue()
    finally:
        _delete_archive(database)


def _witnessed_on(days: int) -> tuple[Entry, ...]:
    """One entry a day for `days` days, all naming the same person."""

    built = []
    for offset in range(days):
        when = NOW.replace(day=1) + timedelta(days=offset)
        item = entry(f"e{offset}", f"note {offset}", sender="rhea", authored=False)
        built.append(
            item.model_copy(
                update={
                    "occurred_start": when.isoformat(),
                    "ingested_at": when.isoformat(),
                }
            )
        )
    return tuple(built)


def test_a_source_person_is_dated_by_more_than_the_first_entry() -> None:
    """One evidence entry a person could not say they were known over time.

    Everything else about a source person accumulates across every entry that
    witnesses them; only the evidence was cut to the first one.  The compiled
    model of 2026-09-07 showed what that costs: 376 people carrying 379
    evidence rows between them, exactly one of whom could be shown on two
    different days, which failed Alpha acceptance criterion 2 outright.
    """

    people = DeterministicSourceIdentityAdapter().source_people(_witnessed_on(3))

    rhea = next(item for item in people if item.source_key == "id:rhea")
    (alias,) = rhea.aliases
    assert alias.evidence_entry_ids == ("e0", "e1", "e2")


def test_a_person_witnessed_for_years_is_dated_from_both_ends() -> None:
    """The span is what the record has to be able to say, so both ends are kept.

    Bounded, because a person in a decade of chat witnesses tens of thousands
    of entries and the record is not a copy of the archive.
    """

    people = DeterministicSourceIdentityAdapter().source_people(_witnessed_on(20))

    rhea = next(item for item in people if item.source_key == "id:rhea")
    (alias,) = rhea.aliases
    assert len(alias.evidence_entry_ids) == _PERSON_EVIDENCE_DAYS
    assert alias.evidence_entry_ids == ("e0", "e1", "e2", "e3", "e16", "e17", "e18", "e19")


def test_the_word_a_connector_writes_for_a_deleted_account_is_not_a_name() -> None:
    """Four different Telegram accounts were four cards all called `unknown`."""

    def deleted(entry_id: str, account: str) -> Entry:
        item = entry(entry_id, "a message", sender=account, authored=False)
        return item.model_copy(
            update={
                "conversation": item.conversation.model_copy(
                    update={"sender_label": "unknown"}
                )
            }
        )

    people = DeterministicSourceIdentityAdapter().source_people(
        (deleted("one", "user6000000001"), deleted("two", "user6000000002"))
    )

    assert {item.display_name for item in people} == {
        "user6000000001",
        "user6000000002",
    }


def test_a_number_a_connector_already_shortened_is_not_traded_for_a_raw_jid() -> None:
    """The name column holding an account id is not the same as it holding a word.

    The WhatsApp connector writes `79000000001`, having taken the
    `@s.whatsapp.net` off itself; preferring the sender id wherever the label
    did not name somebody put it straight back on, for every person in this
    archive known by a number and no name.
    """

    def texted(entry_id: str, number: str) -> Entry:
        item = entry(entry_id, "a message", sender=f"{number}@s.whatsapp.net", authored=False)
        return item.model_copy(
            update={
                "conversation": item.conversation.model_copy(
                    update={"sender_label": number, "participant_ids": ()}
                )
            }
        )

    people = DeterministicSourceIdentityAdapter().source_people(
        (texted("one", "79000000001"), texted("two", "79000000002"))
    )

    assert {item.display_name for item in people} == {"79000000001", "79000000002"}


def _commit(entry_id: str, repo: str, author: str, *, owner: bool) -> Entry:
    item = entry(entry_id, f"{repo}: a change", source="git", sender=author, authored=owner)
    return item.model_copy(update={"external_id": f"/home/dev/repos/{repo}@{entry_id}"})


def test_contributors_to_a_repository_you_cloned_are_not_people_in_your_life() -> None:
    """Half the compiled model's people had never met the person it models.

    175 of its 376 people were contributors to one open-source project on the
    machine, cloned and never written a line of.  A repository the owner does
    commit to is the opposite case -- the people in it are people he works
    with -- so the test is the repository, not the commit.
    """

    entries = (
        _commit("clone-1", "stable-diffusion-webui", "upstream", owner=False),
        _commit("mine-1", "varyz", "owner", owner=True),
        _commit("mine-2", "varyz", "colleague", owner=False),
    )

    people = DeterministicSourceIdentityAdapter().source_people(entries)

    keys = {item.source_key for item in people}
    assert "id:upstream" not in keys
    assert {"id:owner", "id:colleague"} <= keys


def test_entries_on_one_day_are_one_witness() -> None:
    """A person seen forty times in an afternoon has been seen once, over time."""

    same_day = tuple(
        entry(f"same{index}", "hello", sender="rhea", authored=False) for index in range(5)
    )

    people = DeterministicSourceIdentityAdapter().source_people(same_day)

    rhea = next(item for item in people if item.source_key == "id:rhea")
    (alias,) = rhea.aliases
    assert alias.evidence_entry_ids == ("same0",)

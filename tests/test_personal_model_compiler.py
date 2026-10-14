"""Integration tests for the ordered personal-model compiler boundary."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from varyz.db import dao
from varyz.domain import ConversationMetadata, Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.compiler import (
    CompilerCandidateBatch,
    CompileRequest,
    CompilerPolicy,
    CompileStatus,
    CorrectionDisposition,
    ExecutionLocality,
    GraphSnapshot,
    OfferedQuestionProposal,
    PersonalModelCompiler,
    SQLiteActiveGraphReader,
    SQLiteCompilerStateStore,
    _quarantines_by_code,
    build_correction_application_service,
    build_personal_model_compiler,
)
from varyz.personal_model.models import (
    CandidateAssemblyError,
    CountersearchState,
    DerivationKind,
    EntityKind,
    EvidenceRole,
    Fact,
    FactStatus,
    FactValueKind,
    Interpretation,
    InterpretationEvidence,
    InterpretationKind,
    InterpretationScope,
    InterpretationVerdict,
    Person,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Project,
    Readiness,
    RelationshipSnapshot,
    ScopeKind,
    SubjectReference,
    TextFactValue,
)
from varyz.personal_model.observations import (
    OBSERVATION_SCHEMA_VERSION,
    AnnotationBatchRequest,
    AnnotationBatchResponse,
    AnnotationSource,
    AttributionState,
    CandidateSpan,
    DialogueContext,
    ObservationBudgetExhausted,
    ObservationCandidate,
    ObservationExtractionResult,
    ObservationKind,
    ObservationModality,
    ObservationQuarantine,
    Polarity,
    QuarantineCode,
    SemanticAnnotatorDescriptor,
    SubjectCandidate,
    _request_dependency_fingerprint,
)
from varyz.personal_model.projects import ProjectEvidenceCandidate
from varyz.personal_model.query import (
    EntitySelector,
    LifeModelQueryService,
    LifeQuery,
    LifeRelation,
    QueryDisposition,
)
from varyz.personal_model.readiness import GATE_KEYS, GateVerdict
from varyz.personal_model.relationships import RelationshipHistory
from varyz.personal_model.repository import (
    ModelOverride,
    OverrideAction,
    OverrideTargetKind,
    PersonalModelRepository,
    model_record_dependency,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


class GraphReader:
    def __init__(self, snapshot: GraphSnapshot):
        self.snapshot = snapshot

    def read_active(self) -> GraphSnapshot:
        return self.snapshot


class EntryReader:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def read_entries(self, snapshot: GraphSnapshot):
        rows = self.conn.execute("SELECT id FROM entries ORDER BY id").fetchall()
        return tuple(dao.get_entry(self.conn, row["id"]) for row in rows)


class EmptyAnnotator:
    execution_locality = ExecutionLocality.OFFLINE
    descriptor = SemanticAnnotatorDescriptor(
        name="local-fixture",
        version="1",
        model_name="offline-fixture",
        prompt_version="literal-v1",
    )

    def __init__(self, *, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def annotate(self, request: AnnotationBatchRequest) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("private provider diagnostic must not escape")
        return AnnotationBatchResponse(
            schema_version=OBSERVATION_SCHEMA_VERSION,
            episode_id=request.episode_id,
        ).model_dump_json()


class MutableClock:
    def __init__(self, current: datetime):
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class AdvancingAnnotator(EmptyAnnotator):
    def __init__(self, clock: MutableClock, advance: timedelta):
        super().__init__()
        self.clock = clock
        self.advance = advance

    def annotate(self, request: AnnotationBatchRequest) -> str:
        response = super().annotate(request)
        self.clock.current += self.advance
        return response


class ExternalCandidateAdapter:
    name = "cloud-fake"
    version = "1"
    model_name = "external-model"
    prompt_version = "1"
    execution_locality = ExecutionLocality.EXTERNAL

    def __init__(self):
        self.calls = 0

    def propose(self, **_kwargs):
        self.calls += 1
        raise AssertionError("external adapter must never be invoked")


class MalformedCandidateAdapter:
    name = "malformed-local"
    version = "1"
    model_name = "local-fixture"
    prompt_version = "1"
    execution_locality = ExecutionLocality.OFFLINE

    def propose(self, **_kwargs):
        raise ValueError("raw malformed payload must not escape")


class NamedRefusalCandidateAdapter(MalformedCandidateAdapter):
    """An adapter refusing through its own check rather than through the model."""

    name = "named-refusal-local"

    def propose(self, **_kwargs):
        raise CandidateAssemblyError("goal proposal references an unknown project key")


def add_graph(conn: sqlite3.Connection) -> GraphSnapshot:
    stamp = NOW.isoformat()
    conn.execute(
        """INSERT INTO graph_runs
           (id,generation_key,extractor_name,extractor_version,model,prompt_version,
            predicate_version,identity_version,input_content_hash,state,started_at,
            completed_at,activated_at)
           VALUES ('graph-compiler','graph-compiler-key','fixture','1',NULL,NULL,
                   '1','1','graph-input','active',?,?,?)""",
        (stamp, stamp, stamp),
    )
    conn.commit()
    return GraphSnapshot(
        generation_id="graph-compiler",
        content_hash=hashlib.sha256(b"graph-compiler").hexdigest(),
        extractor_name="fixture",
        extractor_version="1",
        policy_version="graph-policy-1",
    )


def test_runtime_compiler_reader_excludes_configured_social_sources(conn):
    graph = add_graph(conn)
    with conn:
        for source, external_id in (
            ("ai_conversations", "kept-ai-chat"),
            ("WhatsApp", "excluded-whatsapp"),
            ("TELEGRAM", "excluded-telegram"),
        ):
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=external_id,
                    body=f"source fixture: {source}",
                ),
                source=source,
                sensitivity=Sensitivity.PERSONAL,
            )

    cfg = SimpleNamespace(
        graph=SimpleNamespace(excluded_sources=[" WhatsApp ", "telegram"]),
        data_dir=None,
    )
    compiler = build_personal_model_compiler(conn, cfg)

    entries = compiler.evidence_reader.read_entries(graph)

    assert [(entry.source, entry.external_id) for entry in entries] == [
        ("ai_conversations", "kept-ai-chat")
    ]


def add_turn(
    conn: sqlite3.Connection,
    external_id: str,
    body: str,
    *,
    order: int,
    sender: str,
    reply_to: str | None = None,
    thread: str = "compiler-thread",
) -> str:
    entry_id, _ = dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id=external_id,
            body=body,
            occurred_start=NOW + timedelta(minutes=order),
            thread_id=thread,
            conversation=ConversationMetadata(
                conversation_id=thread,
                participant_ids=("owner", "anna"),
                participants_complete=True,
                sender_id=sender,
                sender_label="Egor" if sender == "owner" else "Anna",
                addressee_ids=("anna" if sender == "owner" else "owner",),
                reply_to_id=reply_to,
                source_order=order,
            ),
            authored_by_user=sender == "owner",
        ),
        source="fixture-chat",
        source_account="owner-account",
        sensitivity=Sensitivity.PERSONAL,
    )
    conn.commit()
    return entry_id


def pin_graph_to_current_entries(conn: sqlite3.Connection) -> None:
    digest = hashlib.sha256()
    rows = conn.execute("SELECT id,content_hash FROM entries ORDER BY id").fetchall()
    for ordinal, row in enumerate(rows):
        if ordinal:
            digest.update(b"\n")
        digest.update(f"{row['id']}:{row['content_hash']}".encode())
    conn.execute(
        """UPDATE graph_runs SET input_content_hash=?,entries_total=?
           WHERE id='graph-compiler'""",
        (digest.hexdigest(), len(rows)),
    )
    conn.commit()


def compiler(
    conn: sqlite3.Connection,
    snapshot: GraphSnapshot,
    *,
    annotator=None,
    policy: CompilerPolicy | None = None,
    clock=None,
) -> PersonalModelCompiler:
    return PersonalModelCompiler(
        graph_reader=GraphReader(snapshot),
        evidence_reader=EntryReader(conn),
        repository=PersonalModelRepository(conn),
        state_store=SQLiteCompilerStateStore(conn),
        annotator=annotator,
        clock=clock,
        policy=policy
        or CompilerPolicy(
            require_observation_annotator=False,
            allow_literal_only_activation=True,
        ),
    )


def add_isolated_turns(conn: sqlite3.Connection, count: int) -> tuple[str, ...]:
    return tuple(
        add_turn(
            conn,
            f"isolated-{ordinal}",
            f"Isolated message {ordinal}",
            order=1,
            sender="owner",
            thread=f"isolated-thread-{ordinal}",
        )
        for ordinal in range(count)
    )


def test_literal_compile_activates_one_episode_for_two_targets_and_typed_snapshots(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    first = add_turn(conn, "question", "What flowers do you like?", order=1, sender="owner")
    second = add_turn(
        conn,
        "answer",
        "Peonies",
        order=2,
        sender="anna",
        reply_to="question",
    )

    result = compiler(conn, snapshot).compile(
        CompileRequest(generation_id="model-compiler-1", created_at=NOW)
    )

    assert result.activated and not result.failed
    assert result.active_generation_id == "model-compiler-1"
    assert result.record_counts == {"conversation_episode": 1, "person": 2}
    assert result.readiness is not None
    assert tuple(item.gate for item in result.readiness.gates) == GATE_KEYS
    assert any(item.verdict != GateVerdict.READY for item in result.readiness.gates)
    metric_names = {
        row[0]
        for row in conn.execute(
            """SELECT metric_name FROM personal_model_run_metrics
               WHERE generation_id=? AND stage='readiness'""",
            (result.generation_id,),
        ).fetchall()
    }
    assert metric_names == {*GATE_KEYS, "fact_records"}
    readiness_checkpoint = conn.execute(
        """SELECT detail_json FROM personal_model_compiler_checkpoints
           WHERE generation_id=? AND stage='readiness'""",
        (result.generation_id,),
    ).fetchone()
    assert readiness_checkpoint is not None
    assert "episode_context_completeness" in readiness_checkpoint[0]
    coverage = conn.execute(
        """SELECT target_entry_id, episode_id
           FROM personal_model_target_episode_coverage
           WHERE generation_id=? ORDER BY target_entry_id""",
        (result.generation_id,),
    ).fetchall()
    assert {row["target_entry_id"] for row in coverage} == {first, second}
    assert len({row["episode_id"] for row in coverage}) == 1
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM personal_model_source_identity_memberships
           WHERE generation_id=?""",
            (result.generation_id,),
        ).fetchone()[0]
        == 2
    )
    checkpoint_detail = conn.execute(
        """SELECT detail_json FROM personal_model_compiler_checkpoints
           WHERE generation_id=? AND stage='activate'""",
        (result.generation_id,),
    ).fetchone()[0]
    assert "Peonies" not in checkpoint_detail
    assert "Anna" not in checkpoint_detail


def test_activation_that_never_happened_is_not_checkpointed_as_complete(
    conn: sqlite3.Connection,
) -> None:
    """The activate checkpoint used to be written before the activation ran.

    A run killed in that window left "activate: complete" on the record while
    personal_model_active_generation stayed empty and the generation stayed
    staged -- and there is no ``varyz model activate`` to finish the job by
    hand, so the record was the only thing saying the model was live.
    """

    snapshot = add_graph(conn)
    add_turn(conn, "question", "What flowers do you like?", order=1, sender="owner")
    add_turn(conn, "answer", "Peonies", order=2, sender="anna", reply_to="question")

    built = compiler(conn, snapshot)

    def refuse(generation_id: str, expected_input_hash: str):
        raise RuntimeError("killed between the checkpoint and the switch")

    built._activate_generation = refuse  # type: ignore[method-assign]
    result = built.compile(CompileRequest(generation_id="activate-killed", created_at=NOW))

    assert result.failed and not result.activated
    assert result.active_generation_id is None
    state = conn.execute(
        """SELECT state FROM personal_model_compiler_checkpoints
           WHERE generation_id=? AND stage='activate'""",
        (result.generation_id,),
    ).fetchone()[0]
    assert state == "failed"


def test_the_compiled_generation_is_released_before_the_pointer_swap(
    conn: sqlite3.Connection,
) -> None:
    """Activation asks for a second copy of the archive; the first must be gone.

    ``_assert_inputs_current`` re-reads, re-sorts and re-hashes every entry to
    prove the inputs did not move under the compile, and it runs at the end of
    the run, when the generation, its episodes, its observations and the entries
    read at the top are all still on the frame.  On 2026-09-08 that pairing cost
    a finished run its result: the compiler wrote ``activate: complete`` at
    02:16:18Z holding a 15 GB heap, Windows answered the next allocation by
    trimming its working set to a few hundred megabytes, and three hours later
    it was still faulting that heap back in at nine thousand reads a second,
    never having reached the swap.  The generation had to be activated by hand.

    Asserted on the frame rather than through a weak reference because the claim
    is about what the compiler is still holding, which is what the frame says.
    """

    snapshot = add_graph(conn)
    add_turn(conn, "question", "What flowers do you like?", order=1, sender="owner")
    add_turn(conn, "answer", "Peonies", order=2, sender="anna", reply_to="question")

    built = compiler(conn, snapshot)
    activate = built._activate_generation
    held: set[str] = set()

    def observe(generation_id: str, expected_input_hash: str):
        held.update(sys._getframe(1).f_locals)
        return activate(generation_id, expected_input_hash)

    built._activate_generation = observe  # type: ignore[method-assign]
    result = built.compile(CompileRequest(generation_id="released", created_at=NOW))

    assert result.activated and not result.failed
    assert result.record_counts
    assert held, "the activation was never reached"
    assert not held & {
        "records",
        "entries",
        "snapshot",
        "episodes",
        "observation_result",
        "identity",
        "source_people",
        "projects",
        "candidates",
        "atomic_claims",
    }


_ANNOTATOR = SemanticAnnotatorDescriptor(
    name="fixture-annotator",
    version="fixture-v1",
    model_name="no-network-fixture",
    prompt_version="literal-v1",
)


def _annotation_request() -> AnnotationBatchRequest:
    sources = (
        AnnotationSource(
            entry_id="entry-1",
            content_hash="0" * 64,
            body="Peonies, especially pink",
            source="fixture-chat",
            source_account="owner@example.test",
        ),
    )
    return AnnotationBatchRequest(
        episode_id="episode-1",
        target_entry_ids=("entry-1",),
        context_complete=False,
        sources=sources,
        allowed_subjects=(),
        dependency_fingerprint=_request_dependency_fingerprint(
            episode_id="episode-1",
            target_entry_ids=("entry-1",),
            context_complete=False,
            sources=sources,
            allowed_subjects=(),
        ),
    )


def _reply(*candidates: dict) -> str:
    return json.dumps(
        {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "episode_id": "episode-1",
            "candidates": list(candidates),
        }
    )


def _good_candidate() -> dict:
    return {
        "episode_id": "episode-1",
        "target_entry_id": "entry-1",
        "kind": ObservationKind.STATEMENT.value,
        "proposition": "Peonies",
        "spans": [
            {
                "entry_id": "entry-1",
                "start": 0,
                "end": 7,
                "exact_literal": "Peonies",
            }
        ],
        "subject": {
            "attribution": AttributionState.RESOLVED.value,
            "kind": "person",
            "refers_to_speaker": True,
        },
        "dialogue_context": DialogueContext.DIRECT_ANSWER.value,
        "polarity": Polarity.POSITIVE.value,
        "modality": ObservationModality.ASSERTED.value,
        "confidence": 0.7,
    }


def _candidate_the_reader_refuses() -> dict:
    # An unresolved subject that names one anyway. The annotator returns this
    # shape often enough to matter: it knows it cannot say who the subject is
    # and offers the surface it saw instead, in a field the schema reserves for
    # a decision.
    bad = _good_candidate()
    bad["subject"] = {
        "attribution": AttributionState.UNKNOWN.value,
        "source_key": "label:me",
    }
    return bad


def test_one_candidate_the_reader_refuses_does_not_keep_the_episode_out_of_the_cache(
    conn,
) -> None:
    """The cache has to hold what the reader accepts, and nothing stricter.

    It validated the whole batch while ``ObservationExtractor`` falls back to
    reading candidates one at a time.  So an episode with one malformed
    candidate was annotated, used, and paid for, and then never marked done --
    which means it came back on the next run, and was bought again, forever.
    Ten in the first five minutes of the 2026-08-31 compile.
    """

    store = SQLiteCompilerStateStore(conn)
    request = _annotation_request()
    reply = _reply(_good_candidate(), _candidate_the_reader_refuses())

    store.cache_annotation("key-1", request, _ANNOTATOR, "r" * 64, reply)

    assert store.cached_annotation("key-1") == reply


def test_a_reply_with_no_envelope_at_all_is_still_not_a_cache_hit(conn) -> None:
    store = SQLiteCompilerStateStore(conn)

    with pytest.raises((ValueError, TypeError)):
        store.cache_annotation(
            "key-2", _annotation_request(), _ANNOTATOR, "r" * 64, "not json"
        )

    assert store.cached_annotation("key-2") is None


def test_the_observation_checkpoint_says_how_the_stage_lost_what_it_lost() -> None:
    """Two numbers cannot show that one reason is nearly all of the loss.

    The checkpoint recorded kept and lost, and on the owner's archive they read
    1428 and 21924 -- a stage failing, with no way to see how.  Establishing
    that a single code accounted for almost all of it meant replaying the
    annotation cache offline.  A histogram costs nothing and puts the same
    answer where every run already writes.
    """

    def refused(code: QuarantineCode, detail: str) -> ObservationQuarantine:
        return ObservationQuarantine(code=code, detail=detail)

    result = ObservationExtractionResult(
        annotator_version="fixture",
        quarantines=(
            refused(QuarantineCode.INCOMPLETE_CONTEXT_HIGH_CONFIDENCE, "Peonies"),
            refused(QuarantineCode.FABRICATED_SPAN, "Anna said so"),
            refused(QuarantineCode.INCOMPLETE_CONTEXT_HIGH_CONFIDENCE, "Utrecht"),
            refused(QuarantineCode.INCOMPLETE_CONTEXT_HIGH_CONFIDENCE, "the lease"),
        ),
    )

    counted = _quarantines_by_code(result)

    # Largest first, so the reason that matters is the one read first.
    assert list(counted) == ["incomplete_context_high_confidence", "fabricated_span"]
    assert counted == {"incomplete_context_high_confidence": 3, "fabricated_span": 1}
    # Codes only: the details are the annotator's own text.
    rendered = json.dumps(counted)
    assert not any(
        detail in rendered for detail in ("Peonies", "Anna said so", "Utrecht", "the lease")
    )


def test_a_stage_that_lost_nothing_reports_an_empty_histogram() -> None:
    assert _quarantines_by_code(ObservationExtractionResult(annotator_version="fixture")) == {}


def test_cache_reuse_and_metadata_only_change_invalidates_records(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_turn(conn, "question", "Question", order=1, sender="owner")
    answer_id = add_turn(
        conn,
        "answer",
        "Answer",
        order=2,
        sender="anna",
        reply_to="question",
    )
    annotator = EmptyAnnotator()
    runner = compiler(conn, snapshot, annotator=annotator)

    first = runner.compile(CompileRequest(generation_id="reuse-1", created_at=NOW))
    second = runner.compile(CompileRequest(generation_id="reuse-2", created_at=NOW))
    assert not first.failed and not second.failed
    assert annotator.calls == 1
    assert second.reused_record_count == sum(second.record_counts.values())

    dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id="unrelated",
            body="Unrelated archive item",
            occurred_start=NOW + timedelta(days=1),
            thread_id="unrelated-thread",
            conversation=ConversationMetadata(
                conversation_id="unrelated-thread",
                participant_ids=("bob",),
                participants_complete=True,
                sender_id="bob",
                sender_label="Bob",
                source_order=1,
            ),
        ),
        source="unrelated-source",
        source_account="other",
        sensitivity=Sensitivity.PERSONAL,
    )
    conn.commit()
    unrelated = runner.compile(CompileRequest(generation_id="reuse-unrelated", created_at=NOW))
    assert not unrelated.failed
    assert unrelated.reused_record_count >= sum(second.record_counts.values())
    assert annotator.calls == 2

    entry = dao.get_entry(conn, answer_id)
    assert entry is not None
    changed = entry.conversation.model_copy(update={"source_order": 20})
    conn.execute(
        "UPDATE entries SET conversation_metadata_json=? WHERE id=?",
        (changed.model_dump_json(exclude_defaults=True, exclude_none=True), answer_id),
    )
    conn.commit()
    pin_graph_to_current_entries(conn)
    third = runner.compile(CompileRequest(generation_id="reuse-3", created_at=NOW))
    assert not third.failed
    assert annotator.calls == 3
    assert third.reused_record_count < sum(third.record_counts.values())


def test_call_budget_checkpoints_partial_without_replacing_active_generation(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_isolated_turns(conn, 3)
    stable = compiler(conn, snapshot).compile(
        CompileRequest(generation_id="budget-stable", created_at=NOW)
    )
    assert stable.activated
    annotator = EmptyAnnotator()

    partial = compiler(conn, snapshot, annotator=annotator).compile(
        CompileRequest(
            generation_id="budget-partial",
            max_annotation_calls=2,
            created_at=NOW,
        )
    )

    assert partial.status == CompileStatus.PARTIAL
    assert not partial.failed and not partial.activated
    assert partial.active_generation_id == "budget-stable"
    assert annotator.calls == 2
    assert partial.observation_progress is not None
    assert partial.observation_progress.episodes_annotated == 2
    assert partial.observation_progress.episodes_remaining == 1
    assert partial.observation_progress.entries_reached == 2
    generation = PersonalModelRepository(conn).get_generation("budget-partial")
    assert generation.state.value == "staged" and generation.record_count == 0
    row = conn.execute(
        """SELECT state,episodes_annotated,episodes_remaining,entries_reached,
                  annotation_calls,budget_exhausted_by
             FROM personal_model_observation_progress WHERE generation_id=?""",
        (partial.generation_id,),
    ).fetchone()
    assert tuple(row) == ("partial", 2, 1, 2, 2, "call_budget")
    checkpoint = conn.execute(
        """SELECT state,detail_json FROM personal_model_compiler_checkpoints
           WHERE generation_id=? AND stage='observations'""",
        (partial.generation_id,),
    ).fetchone()
    assert checkpoint["state"] == "running"
    assert json.loads(checkpoint["detail_json"])["status"] == "partial"


def test_wall_clock_deadline_stops_before_starting_another_annotation(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_isolated_turns(conn, 3)
    clock = MutableClock(NOW)
    annotator = AdvancingAnnotator(clock, timedelta(seconds=10))

    partial = compiler(conn, snapshot, annotator=annotator, clock=clock).compile(
        CompileRequest(
            generation_id="deadline-partial",
            observation_deadline=NOW + timedelta(seconds=5),
            created_at=NOW,
        )
    )

    assert partial.status == CompileStatus.PARTIAL
    assert annotator.calls == 1
    assert partial.observation_progress is not None
    assert partial.observation_progress.episodes_annotated == 1
    assert partial.observation_progress.episodes_remaining == 2
    assert partial.observation_progress.budget_exhausted_by == "deadline"


class VanishingAnnotator(EmptyAnnotator):
    """An annotator that answers once and then goes away, as a dead server does."""

    def __init__(self, *, answers: int):
        super().__init__()
        self.answers = answers

    def annotate(self, request: AnnotationBatchRequest) -> str:
        if self.calls >= self.answers:
            raise ObservationBudgetExhausted("provider_unavailable")
        return super().annotate(request)


def test_an_annotator_that_goes_away_stops_the_run_and_says_so(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_isolated_turns(conn, 3)
    annotator = VanishingAnnotator(answers=1)

    partial = compiler(conn, snapshot, annotator=annotator).compile(
        CompileRequest(generation_id="provider-partial", created_at=NOW)
    )

    assert partial.status == CompileStatus.PARTIAL
    assert not partial.activated
    assert partial.observation_progress is not None
    assert partial.observation_progress.episodes_annotated == 1
    assert partial.observation_progress.budget_exhausted_by == "provider_unavailable"
    row = conn.execute(
        """SELECT state,budget_exhausted_by
             FROM personal_model_observation_progress WHERE generation_id=?""",
        (partial.generation_id,),
    ).fetchone()
    assert tuple(row) == ("partial", "provider_unavailable")
    # The one answer it did give is kept, so the next run does not pay for it again.
    assert conn.execute(
        "SELECT COUNT(*) FROM personal_model_observation_annotation_cache"
    ).fetchone()[0] == 1


def test_three_bounded_generations_make_deterministic_progress_and_converge(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_isolated_turns(conn, 6)
    annotator = EmptyAnnotator()
    runner = compiler(conn, snapshot, annotator=annotator)

    first = runner.compile(
        CompileRequest(generation_id="converge-1", max_annotation_calls=4, created_at=NOW)
    )
    second = runner.compile(
        CompileRequest(generation_id="converge-2", max_annotation_calls=1, created_at=NOW)
    )
    third = runner.compile(
        CompileRequest(generation_id="converge-3", max_annotation_calls=4, created_at=NOW)
    )

    assert [item.status for item in (first, second, third)] == [
        CompileStatus.PARTIAL,
        CompileStatus.PARTIAL,
        CompileStatus.COMPLETE,
    ]
    progress = [item.observation_progress for item in (first, second, third)]
    assert all(item is not None for item in progress)
    assert [item.episodes_annotated for item in progress if item is not None] == [4, 5, 6]
    assert [item.annotation_calls for item in progress if item is not None] == [4, 1, 1]
    assert [item.cache_hits for item in progress if item is not None] == [0, 4, 5]
    covered = []
    for generation_id in (first.generation_id, second.generation_id, third.generation_id):
        row = conn.execute(
            """SELECT annotated_episode_ids_json
               FROM personal_model_observation_progress WHERE generation_id=?""",
            (generation_id,),
        ).fetchone()
        covered.append(set(json.loads(row[0])))
    assert covered[0] < covered[1] < covered[2]
    assert annotator.calls == 6
    assert third.activated and third.active_generation_id == "converge-3"
    assert conn.execute(
        "SELECT COUNT(*) FROM personal_model_observation_annotation_cache"
    ).fetchone()[0] == 6


def test_provider_failure_and_conflicting_override_keep_previous_active(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_turn(conn, "one", "Hello", order=1, sender="owner")
    good = compiler(conn, snapshot).compile(
        CompileRequest(generation_id="stable-active", created_at=NOW)
    )
    assert good.activated

    failed_provider = compiler(
        conn,
        snapshot,
        annotator=EmptyAnnotator(fail=True),
        policy=CompilerPolicy(
            allow_literal_only_activation=True,
            fail_on_quarantine=True,
        ),
    ).compile(CompileRequest(generation_id="failed-provider", created_at=NOW))
    assert failed_provider.failed
    assert failed_provider.active_generation_id == "stable-active"
    assert "private provider diagnostic" not in (failed_provider.error or "")

    malformed = PersonalModelCompiler(
        graph_reader=GraphReader(snapshot),
        evidence_reader=EntryReader(conn),
        repository=PersonalModelRepository(conn),
        state_store=SQLiteCompilerStateStore(conn),
        candidate_adapter=MalformedCandidateAdapter(),
        policy=CompilerPolicy(
            require_observation_annotator=False,
            allow_literal_only_activation=True,
        ),
    ).compile(CompileRequest(generation_id="malformed-candidate", created_at=NOW))
    assert malformed.failed and malformed.active_generation_id == "stable-active"
    assert "raw malformed payload" not in (malformed.error or "")
    quarantine = conn.execute(
        """SELECT reason_code FROM personal_model_quarantines
           WHERE generation_id='malformed-candidate' AND stage='candidates'"""
    ).fetchone()
    assert quarantine["reason_code"] == "malformed_candidate_response"
    assert conn.execute(
        """SELECT state FROM personal_model_compiler_checkpoints
           WHERE generation_id='malformed-candidate' AND stage='memory'"""
    ).fetchone()[0] == "stale"

    person_id = conn.execute(
        """SELECT record_id FROM personal_model_records
           WHERE generation_id='stable-active' AND record_kind='person' LIMIT 1"""
    ).fetchone()[0]
    repo = PersonalModelRepository(conn)
    repo.append_override(
        target_kind=OverrideTargetKind.PERSON,
        target_record_id=person_id,
        action=OverrideAction.SPLIT,
        payload={"resulting_record_ids": ["left", "right"]},
    )
    conflict = compiler(conn, snapshot).compile(
        CompileRequest(generation_id="conflict", created_at=NOW)
    )
    assert conflict.failed
    assert conflict.active_generation_id == "stable-active"
    assert "conflicting personal-model override" in (conflict.error or "")

    external = ExternalCandidateAdapter()
    blocked = PersonalModelCompiler(
        graph_reader=GraphReader(snapshot),
        evidence_reader=EntryReader(conn),
        repository=PersonalModelRepository(conn),
        state_store=SQLiteCompilerStateStore(conn),
        candidate_adapter=external,
        policy=CompilerPolicy(allow_literal_only_activation=True),
    ).compile(CompileRequest(generation_id="blocked-cloud", created_at=NOW))
    assert blocked.failed
    assert blocked.active_generation_id == "stable-active"
    assert external.calls == 0
    assert "not declared managed-local/offline" in (blocked.error or "")

    user_present_without_authorization = PersonalModelCompiler(
        graph_reader=GraphReader(snapshot),
        evidence_reader=EntryReader(conn),
        repository=PersonalModelRepository(conn),
        state_store=SQLiteCompilerStateStore(conn),
        candidate_adapter=external,
        policy=CompilerPolicy(
            user_present=True,
            allow_cloud_egress=False,
            allow_literal_only_activation=True,
        ),
    ).compile(CompileRequest(generation_id="unapproved-cloud", created_at=NOW))
    assert user_present_without_authorization.failed
    assert external.calls == 0
    assert "explicit cloud-egress authorization" in (
        user_present_without_authorization.error or ""
    )


def test_compiler_generation_scoped_rows_are_immutable_after_activation(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    add_turn(conn, "one", "Hello", order=1, sender="owner")
    result = compiler(conn, snapshot).compile(
        CompileRequest(generation_id="immutable", created_at=NOW)
    )
    assert result.activated

    try:
        conn.execute(
            """UPDATE personal_model_compiler_checkpoints SET state='failed'
               WHERE generation_id='immutable' AND stage='identity'"""
        )
    except sqlite3.IntegrityError as exc:
        assert "immutable" in str(exc)
    else:  # pragma: no cover - trigger is the assertion
        raise AssertionError("activated compiler checkpoints were mutable")


def test_default_sqlite_offline_path_compiles_explicit_birthday_and_query_reads_it(
    conn: sqlite3.Connection,
) -> None:
    add_graph(conn)
    add_turn(conn, "owner-context", "Owner context", order=0, sender="owner")
    entry_id = add_turn(
        conn,
        "birthday",
        "My birthday is 14 May",
        order=1,
        sender="anna",
    )
    conn.execute(
        """INSERT INTO entities
           (id,entity_type,canonical_label,status,created_at,updated_at,
            graph_generation_id,generation_version)
           VALUES ('graph-anna','person','Anna','active',?,?, 'graph-compiler','1')""",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """INSERT OR IGNORE INTO predicate_registry
           (predicate,version,object_types,description,inference_only,active)
           VALUES ('birthday','1','["date"]','explicit birthday',0,1)"""
    )
    conn.execute(
        """INSERT INTO claims
           (id,subject_entity_id,predicate,predicate_version,object_type,
            object_value_json,object_surface,polarity,modality,asserted_by_entity_id,
            asserted_at,temporal_precision,confidence,sensitivity,extractor_name,
            graph_generation_id,fingerprint,lifecycle_state,origin,created_at,updated_at)
           VALUES ('claim-birthday','graph-anna','birthday','1','date',?,?,'positive',
                   'fact','graph-anna',?,'day',1.0,'personal','fixture','graph-compiler',
                   'fp-birthday','active','deterministic',?,?)""",
        ('{"month":5,"day":14}', "14 May", NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """INSERT INTO claim_evidence
           (id,claim_id,entry_id,quote,speaker_entity_id,evidence_role,confidence,
            char_start,char_end,sensitivity,graph_generation_id,fingerprint,created_at)
           VALUES ('evidence-birthday','claim-birthday',?,'My birthday is 14 May',
                   'graph-anna','supports',1.0,0,21,'personal','graph-compiler',
                   'fp-evidence-birthday',?)""",
        (entry_id, NOW.isoformat()),
    )
    conn.commit()
    pin_graph_to_current_entries(conn)

    result = build_personal_model_compiler(conn).compile(
        CompileRequest(generation_id="offline-explicit", created_at=NOW)
    )
    assert result.activated and not result.failed
    assert result.record_counts["fact"] == 1
    fact = PersonalModelRepository(conn).list_records(kind="fact")[0]
    assert fact.predicate == "person.birth_date"
    assert fact.readiness.value == "ready"
    assert result.readiness is not None
    fact_verdict = next(
        item for item in result.readiness.fact_records if item.record_id == fact.id
    )
    assert fact_verdict.verdict == GateVerdict.READY
    anna_record = PersonalModelRepository(conn).get_record(fact.subject.id)
    assert anna_record.readiness == Readiness.LIMITED

    answer = LifeModelQueryService(conn).query(
        LifeQuery(
            entities=(EntitySelector(value="Anna"),),
            relation=LifeRelation.FACT,
            predicate="person.birth_date",
        )
    )
    assert answer.disposition == QueryDisposition.ANSWERED
    assert answer.answers[0].display_value == "05-14"
    natural = LifeModelQueryService(conn).ask_text("when is Anna's birthday")
    assert natural.disposition == QueryDisposition.ANSWERED
    assert natural.answers[0].display_value == "05-14"

    corrected = build_correction_application_service(conn).apply(
        target_kind=OverrideTargetKind.FACT,
        target_record_id=fact.id,
        action=OverrideAction.REJECT,
        payload={"reason": "owner says this imported date is wrong"},
        generation_id="offline-corrected",
        created_at=NOW,
    )
    assert corrected.disposition == CorrectionDisposition.APPLIED
    assert corrected.active_generation_id == "offline-corrected"
    assert (
        LifeModelQueryService(conn).ask_text("when is Anna's birthday").disposition
        == QueryDisposition.NOT_KNOWN
    )

    rebuilt = build_personal_model_compiler(conn).compile(
        CompileRequest(generation_id="offline-rebuilt", created_at=NOW)
    )
    assert rebuilt.activated and not rebuilt.failed
    replayed = PersonalModelRepository(conn).get_record(fact.id)
    assert replayed.status.value == "rejected"

    # A literal-only correction runtime must never replace a richer active
    # generation with a snapshot that silently drops unrelated semantic data.
    repo = PersonalModelRepository(conn)
    source = repo.active_generation()
    assert source is not None
    repo.create_generation(
        source.graph_generation_id,
        compiler_version=source.compiler_version,
        policy_version=source.policy_version,
        input_content_hash="rich-active-input",
        generation_id="offline-rich-active",
    )
    kind_order = {
        "conversation_episode": 0,
        "person": 1,
        "observation": 2,
        "project": 3,
        "goal": 4,
        "fact": 5,
        "preference": 6,
        "event": 7,
        "relationship_snapshot": 8,
        "interpretation_candidate": 9,
        "interpretation": 10,
    }
    rows = conn.execute(
        """SELECT record_id,record_kind,dependency_hash
           FROM personal_model_records WHERE generation_id=?""",
        (source.id,),
    ).fetchall()
    for row in sorted(rows, key=lambda item: (kind_order[item["record_kind"]], item["record_id"])):
        assert repo.clone_record_if_unchanged(
            source_generation_id=source.id,
            target_generation_id="offline-rich-active",
            record_id=row["record_id"],
            expected_dependency_hash=row["dependency_hash"],
        )
    owner = next(
        item
        for item in repo.list_records(generation_id="offline-rich-active", kind="person")
        if item.is_archive_owner
    )
    owner_hash = conn.execute(
        """SELECT dependency_hash FROM personal_model_records
           WHERE generation_id='offline-rich-active' AND record_id=?""",
        (owner.id,),
    ).fetchone()[0]
    rich_project = Project(
        id="project-rich-semantic",
        generation_id="offline-rich-active",
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.USER_CONFIRMED,
        producer=ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1"),
        readiness=Readiness.LIMITED,
        created_at=NOW,
        canonical_key="project:rich-semantic",
        display_name="Rich semantic project",
        participant_person_ids=(owner.id,),
        purpose="Must survive an unrelated birthday correction",
    )
    repo.stage_record(
        rich_project,
        dependencies=(model_record_dependency("person", owner.id, owner_hash),),
    )
    for prior_override in repo.overrides_for_generation("offline-rich-active"):
        repo.record_override_replay(
            "offline-rich-active",
            prior_override.id,
            outcome="applied",
            matched_record_id=prior_override.target_record_id,
        )
    repo.activate_generation("offline-rich-active")
    preserved = build_correction_application_service(conn).apply(
        target_kind=OverrideTargetKind.FACT,
        target_record_id=fact.id,
        action=OverrideAction.CONFIRM,
        payload={"value": fact.value.model_dump(mode="json")},
        generation_id="offline-would-drop-rich",
        created_at=NOW,
    )
    assert preserved.disposition == CorrectionDisposition.FAILED
    assert preserved.active_generation_id == "offline-rich-active"
    assert "discard existing canonical records" in (preserved.error or "")
    assert repo.get_record(rich_project.id).display_name == "Rich semantic project"

    anna = repo.get_record(fact.subject.id)
    assert isinstance(anna, Person)
    before_failed_correction = PersonalModelRepository(conn).active_generation_id()
    unresolved = build_correction_application_service(conn).apply(
        target_kind=OverrideTargetKind.PERSON,
        target_record_id=anna.id,
        action=OverrideAction.SPLIT,
        payload={"resulting_record_ids": ["anna-work", "anna-personal"]},
        generation_id="offline-unresolved-split",
        created_at=NOW,
    )
    assert unresolved.disposition == CorrectionDisposition.REBUILD_NEEDED
    assert unresolved.active_generation_id == before_failed_correction


def test_default_sqlite_path_fails_closed_when_archive_outgrows_active_graph(
    conn: sqlite3.Connection,
) -> None:
    add_graph(conn)
    add_turn(conn, "before-graph", "Covered by graph", order=1, sender="owner")
    pin_graph_to_current_entries(conn)
    stable = build_personal_model_compiler(conn).compile(
        CompileRequest(generation_id="pinned-stable", created_at=NOW)
    )
    assert stable.activated

    add_turn(conn, "after-graph", "Not covered by graph", order=2, sender="anna")
    stale = build_personal_model_compiler(conn).compile(
        CompileRequest(generation_id="pinned-stale", created_at=NOW)
    )
    assert stale.failed
    assert stale.active_generation_id == "pinned-stable"
    assert PersonalModelRepository(conn).get_generation("pinned-stale").state.value == "failed"
    assert "rebuild the graph" in (stale.error or "")


def test_preflight_rejects_stale_graph_without_creating_a_generation(
    conn: sqlite3.Connection,
) -> None:
    add_graph(conn)
    add_turn(conn, "before-graph", "Covered by graph", order=1, sender="owner")
    pin_graph_to_current_entries(conn)
    compiler = build_personal_model_compiler(conn)
    compiler.preflight()

    add_turn(conn, "after-graph", "Not covered by graph", order=2, sender="anna")

    with pytest.raises(RuntimeError, match="rebuild the graph"):
        compiler.preflight()
    assert conn.execute("SELECT COUNT(*) FROM personal_model_generations").fetchone()[0] == 0


def test_sqlite_graph_reader_accepts_legacy_minimal_graph_schema() -> None:
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    legacy.execute("CREATE TABLE graph_runs (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
    legacy.execute("INSERT INTO graph_runs VALUES ('legacy-active','active')")

    snapshot = SQLiteActiveGraphReader(legacy).read_active()

    assert snapshot.generation_id == "legacy-active"
    assert snapshot.extractor_name == "legacy-sqlite-graph"
    assert snapshot.extractor_version == "legacy-unknown"
    assert snapshot.policy_version == "predicate:legacy-unknown;identity:legacy-unknown"
    assert snapshot.model_name is None and snapshot.prompt_version is None
    assert snapshot.explicit_claims == ()


def test_relationship_history_projection_pins_same_generation_dependencies(
    conn: sqlite3.Connection,
) -> None:
    add_graph(conn)
    repo = PersonalModelRepository(conn)
    repo.create_generation(
        "graph-compiler",
        compiler_version="test-compiler",
        policy_version="test-policy",
        input_content_hash="relationship-input",
        generation_id="relationship-history-generation",
    )
    base = {
        "generation_id": "relationship-history-generation",
        "confidence": 1.0,
        "sensitivity": Sensitivity.PERSONAL,
        "derivation": DerivationKind.USER_CONFIRMED,
        "producer": ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1"),
        "created_at": NOW,
    }
    owner = Person(
        **base,
        id="person-owner",
        canonical_key="person:owner",
        display_name="Egor",
        is_archive_owner=True,
        readiness=Readiness.LIMITED,
    )
    anna = Person(
        **base,
        id="person-anna",
        canonical_key="person:anna",
        display_name="Anna",
        readiness=Readiness.LIMITED,
    )
    fact = Fact(
        **base,
        id="fact-anna-birthday",
        subject=SubjectReference(kind=EntityKind.PERSON, id=anna.id),
        predicate="person.birth_date",
        value=TextFactValue(
            kind=FactValueKind.TEXT,
            exact_literal="14 May",
            normalized="05-14",
        ),
        readiness=Readiness.READY,
    )
    relationship = RelationshipSnapshot(
        **base,
        id="relationship-anna",
        archive_owner_person_id=owner.id,
        other_person_id=anna.id,
        period_start=NOW - timedelta(days=30),
        period_end=NOW,
        readiness=Readiness.LIMITED,
    )
    dependency_hashes = {
        owner.id: repo.stage_record(owner),
        anna.id: repo.stage_record(anna),
    }
    dependency_hashes[fact.id] = repo.stage_record(
        fact,
        dependencies=(
            model_record_dependency("person", anna.id, dependency_hashes[anna.id]),
        ),
    )
    dependency_hashes[relationship.id] = repo.stage_record(
        relationship,
        dependencies=(
            model_record_dependency("person", owner.id, dependency_hashes[owner.id]),
            model_record_dependency("person", anna.id, dependency_hashes[anna.id]),
        ),
    )
    SQLiteCompilerStateStore(conn).store_relationship_histories(
        "relationship-history-generation",
        (
            RelationshipHistory(
                snapshot_id=relationship.id,
                archive_owner_person_id=owner.id,
                other_person_id=anna.id,
                period_start=relationship.period_start,
                period_end=relationship.period_end,
                fact_ids=(fact.id,),
            ),
        ),
    )
    row = conn.execute(
        """SELECT d.record_kind,d.record_id,d.dependency_hash
           FROM personal_model_relationship_history_dependencies d
           WHERE d.generation_id=? AND d.snapshot_id=?""",
        ("relationship-history-generation", relationship.id),
    ).fetchone()
    assert tuple(row) == ("fact", fact.id, dependency_hashes[fact.id])
    repo.activate_generation("relationship-history-generation")
    with conn:
        try:
            conn.execute(
                """UPDATE personal_model_relationship_histories SET period_end=?
                   WHERE generation_id=? AND snapshot_id=?""",
                (
                    (NOW + timedelta(days=1)).isoformat(),
                    "relationship-history-generation",
                    relationship.id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:  # pragma: no cover - trigger is the assertion
            raise AssertionError("activated relationship history was mutable")


def test_supersede_override_adds_reverse_fact_and_preference_history_links(
    conn: sqlite3.Connection,
) -> None:
    snapshot = add_graph(conn)
    runner = compiler(conn, snapshot)
    base = {
        "generation_id": "supersession-generation",
        "confidence": 1.0,
        "sensitivity": Sensitivity.PERSONAL,
        "derivation": DerivationKind.USER_CONFIRMED,
        "producer": ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1"),
        "readiness": Readiness.READY,
        "created_at": NOW,
    }
    subject = SubjectReference(kind=EntityKind.PERSON, id="person-anna")
    old_fact = Fact(
        **base,
        id="fact-old",
        subject=subject,
        predicate="person.current_location",
        value=TextFactValue(kind=FactValueKind.TEXT, exact_literal="London"),
    )
    new_fact = Fact(
        **base,
        id="fact-new",
        subject=subject,
        predicate="person.current_location",
        value=TextFactValue(kind=FactValueKind.TEXT, exact_literal="Paris"),
    )
    old_preference = Preference(
        **base,
        id="preference-old",
        subject=subject,
        predicate="preference.food",
        object=TextFactValue(kind=FactValueKind.TEXT, exact_literal="ramen"),
        valence=PreferenceValence.LIKE,
    )
    new_preference = Preference(
        **base,
        id="preference-new",
        subject=subject,
        predicate="preference.food",
        object=TextFactValue(kind=FactValueKind.TEXT, exact_literal="sushi"),
        valence=PreferenceValence.LIKE,
    )
    overrides = (
        ModelOverride(
            sequence=1,
            id="override-fact",
            target_kind=OverrideTargetKind.FACT,
            target_record_id=old_fact.id,
            action=OverrideAction.SUPERSEDE,
            payload={"replacement_record_id": new_fact.id},
            struck_value=None,
            supersedes_override_id=None,
            actor="owner",
            created_at=NOW.isoformat(),
        ),
        ModelOverride(
            sequence=2,
            id="override-preference",
            target_kind=OverrideTargetKind.PREFERENCE,
            target_record_id=old_preference.id,
            action=OverrideAction.SUPERSEDE,
            payload={"replacement_record_id": new_preference.id},
            struck_value=None,
            supersedes_override_id=None,
            actor="owner",
            created_at=NOW.isoformat(),
        ),
    )

    records, matches, conflicts = runner._apply_record_overrides(  # noqa: SLF001
        (old_fact, new_fact, old_preference, new_preference),
        overrides,
    )
    by_id = {item.id: item for item in records}
    assert not conflicts
    assert matches == {"override-fact": old_fact.id, "override-preference": old_preference.id}
    assert by_id[old_fact.id].status == FactStatus.SUPERSEDED
    assert old_fact.id in by_id[new_fact.id].supersedes_fact_ids
    assert by_id[old_preference.id].status == FactStatus.SUPERSEDED
    assert old_preference.id in by_id[new_preference.id].supersedes_preference_ids


def test_offered_questions_only_materialize_for_validated_visible_interpretations() -> None:
    proposal = OfferedQuestionProposal(
        question_id="question-pattern",
        interpretation_candidate_id="candidate-pattern",
        display_text="Does this pattern still fit?",
        normalized_text="does this pattern still fit?",
        offered_at=NOW,
    )
    assert PersonalModelCompiler._accepted_offered_questions((), (proposal,)) == ()  # noqa: SLF001

    accepted = Interpretation.model_construct(
        id="interpretation-pattern",
        candidate_id="candidate-pattern",
        readiness=Readiness.LIMITED,
    )
    offered = PersonalModelCompiler._accepted_offered_questions(  # noqa: SLF001
        (accepted,),
        (proposal,),
    )
    assert len(offered) == 1
    assert offered[0].interpretation_id == accepted.id
    confirmed = accepted.model_copy(
        update={"owner_verdict": InterpretationVerdict.CONFIRM}
    )
    withdrawn = PersonalModelCompiler._accepted_offered_questions(  # noqa: SLF001
        (confirmed,), (proposal,)
    )
    assert withdrawn[0].state == "withdrawn"


def _verdict_interpretation(
    thesis: str = "The owner protects chosen commitments.",
    generation_id: str = "verdict-generation",
) -> Interpretation:
    return Interpretation(
        id="interpretation-pattern",
        generation_id=generation_id,
        confidence=0.731,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.USER_CONFIRMED,
        producer=ProducerMetadata(kind=ProducerKind.USER, name="fixture", version="1"),
        readiness=Readiness.LIMITED,
        created_at=NOW,
        concept_key="protects_commitments",
        thesis=thesis,
        kind=InterpretationKind.DECISION_PATTERN,
        scope=InterpretationScope(kind=ScopeKind.GLOBAL),
        evidence_assignments=(
            InterpretationEvidence(
                episode_id="episode-1",
                observation_ids=("observation-1",),
                role=EvidenceRole.SUPPORTS,
                independence_group_id="decision-1",
            ),
        ),
        alternatives=(),
        counterevidence_summary="No contradiction was selected.",
        countersearch_state=CountersearchState.NOT_STARTED,
        derivation_summary="Evidence-only fixture derivation.",
        candidate_id="candidate-pattern",
    )


def test_partial_verdict_keeps_exact_part_and_rejected_verdict_is_not_offered(conn) -> None:
    runner = compiler(conn, add_graph(conn))
    original = _verdict_interpretation()
    partial = ModelOverride(
        sequence=1,
        id="partial-verdict",
        target_kind=OverrideTargetKind.INTERPRETATION,
        target_record_id=original.id,
        action=OverrideAction.VERDICT,
        payload={"verdict": "partial", "kept_part": "protects chosen commitments"},
        struck_value={"thesis": original.thesis},
        supersedes_override_id=None,
        actor="owner",
        created_at=NOW.isoformat(),
    )
    records, matches, conflicts = runner._apply_record_overrides(  # noqa: SLF001
        (original,), (partial,)
    )
    kept = records[0]
    assert matches == {partial.id: original.id}
    assert not conflicts
    assert kept.thesis == "protects chosen commitments"
    assert kept.owner_kept_part == "protects chosen commitments"
    assert kept.owner_verdict == InterpretationVerdict.PARTIAL
    assert kept.confidence == original.confidence
    rebuilt = _verdict_interpretation(generation_id="verdict-rebuild")
    rebuilt_kept = runner._apply_record_overrides(  # noqa: SLF001
        (rebuilt,), (partial,)
    )[0][0]
    assert rebuilt_kept.owner_verdict == InterpretationVerdict.PARTIAL
    assert rebuilt_kept.thesis == kept.thesis
    assert rebuilt_kept.confidence == original.confidence

    rejected_override = replace(
        partial,
        id="reject-verdict",
        payload={"verdict": "reject", "kept_part": None},
    )
    rejected = runner._apply_record_overrides(  # noqa: SLF001
        (original,), (rejected_override,)
    )[0][0]
    proposal = OfferedQuestionProposal(
        question_id="question-pattern",
        interpretation_candidate_id=original.candidate_id,
        display_text="Does this pattern still fit?",
        normalized_text="does this pattern still fit?",
        offered_at=NOW,
    )
    assert rejected.readiness == Readiness.REJECTED
    assert rejected.owner_verdict == InterpretationVerdict.REJECT
    assert PersonalModelCompiler._accepted_offered_questions(  # noqa: SLF001
        (rejected,), (proposal,)
    ) == ()


def test_reworded_interpretation_orphans_verdict_instead_of_misattaching(conn) -> None:
    runner = compiler(conn, add_graph(conn))
    repo = runner.repository
    override = repo.append_override(
        target_kind="interpretation",
        target_record_id="interpretation-pattern",
        action="verdict",
        payload={"verdict": "confirm", "kept_part": None},
        struck_value={"thesis": "The owner protects chosen commitments."},
        override_id="reword-verdict",
    )
    repo.create_generation(
        "graph-compiler",
        compiler_version="test",
        policy_version="test",
        input_content_hash="verdict-input",
        generation_id="verdict-generation",
    )
    conn.execute(
        """INSERT INTO personal_model_records
           (generation_id,record_id,record_kind,record_json,canonical_json_hash,
            dependency_hash,readiness,sensitivity,created_at)
           VALUES ('verdict-generation','interpretation-pattern','interpretation',
                   '{}','hash','dependency','limited','personal',?)""",
        (NOW.isoformat(),),
    )
    reworded = _verdict_interpretation("The owner abandons chosen commitments.")
    records, matches, conflicts = runner._apply_record_overrides(  # noqa: SLF001
        (reworded,), (override,)
    )
    assert records[0].owner_verdict is None
    assert matches == {}
    runner._replay_overrides(  # noqa: SLF001
        "verdict-generation",
        (override,),
        records,
        matches,
        conflicts,
        SimpleNamespace(source_identity_to_person_id={}, applied_override_ids=()),
        SimpleNamespace(
            applied_override_ids=(),
            project_candidate_to_project_id={},
            goal_candidate_to_goal_id={},
        ),
    )
    replay = conn.execute(
        """SELECT outcome,detail FROM personal_model_override_replays
           WHERE generation_id='verdict-generation' AND override_id=?""",
        (override.id,),
    ).fetchone()
    assert replay["outcome"] == "orphaned"
    assert "thesis changed" in replay["detail"]


class QuotingAnnotator(EmptyAnnotator):
    """One observation per episode, quoting the answering message whole."""

    def annotate(self, request: AnnotationBatchRequest) -> str:
        self.calls += 1
        target = request.target_entry_ids[0]
        source = next((item for item in request.sources if item.entry_id == target), None)
        if source is None or not source.body:
            return super().annotate(request)
        return AnnotationBatchResponse(
            schema_version=OBSERVATION_SCHEMA_VERSION,
            episode_id=request.episode_id,
            candidates=(
                ObservationCandidate(
                    episode_id=request.episode_id,
                    target_entry_id=target,
                    kind=ObservationKind.STATEMENT,
                    proposition=source.body,
                    spans=(
                        CandidateSpan(
                            entry_id=target,
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
                    dialogue_context=DialogueContext.DIRECT_ANSWER,
                    polarity=Polarity.POSITIVE,
                    modality=ObservationModality.ASSERTED,
                    confidence=0.7,
                ),
            ),
        ).model_dump_json()


class OneProjectAdapter:
    """Name one project over every observation the compile produced."""

    name = "one-project-fixture"
    version = "1"
    model_name = "offline-fixture"
    prompt_version = "1"
    execution_locality = ExecutionLocality.OFFLINE

    def propose(self, *, snapshot, entries, identity, observations):
        return CompilerCandidateBatch(
            projects=(
                ProjectEvidenceCandidate(
                    candidate_id="project-candidate",
                    canonical_value_key="varyz",
                    display_name="Varyz",
                    explicit_self_reference=True,
                    observation_ids=tuple(sorted(item.id for item in observations)),
                ),
            )
        )


def test_a_generation_that_found_a_project_activates_with_its_evidence_pinned(
    conn,
) -> None:
    """A project's evidence names episodes and observations; activation checks them.

    The compiler wrote model dependencies from its own list of what a record
    references and the repository validated them against a second, longer list.
    Only interpretations had their evidence walked, so a project or a goal
    reached ``activate`` owing a dependency row per episode and per observation
    it cited and carrying none: ``lacks model dependency membership``, at the
    last stage, after the whole archive had been annotated.  Nothing had ever
    caught it because no test compiled a project all the way to activation.
    """

    snapshot = add_graph(conn)
    for ordinal in range(3):
        thread = f"project-thread-{ordinal}"
        add_turn(
            conn,
            f"question-{ordinal}",
            "What are you working on?",
            order=1,
            sender="anna",
            thread=thread,
        )
        add_turn(
            conn,
            f"answer-{ordinal}",
            f"I keep coming back to Varyz, and this is the {ordinal} time I say so.",
            order=2,
            sender="owner",
            reply_to=f"question-{ordinal}",
            thread=thread,
        )

    engine = PersonalModelCompiler(
        graph_reader=GraphReader(snapshot),
        evidence_reader=EntryReader(conn),
        repository=PersonalModelRepository(conn),
        state_store=SQLiteCompilerStateStore(conn),
        annotator=QuotingAnnotator(),
        candidate_adapter=OneProjectAdapter(),
        policy=CompilerPolicy(
            require_observation_annotator=True,
            allow_literal_only_activation=True,
        ),
    )
    result = engine.compile(CompileRequest(generation_id="project-generation", created_at=NOW))

    assert result.error is None, result.error
    assert result.activated and not result.failed
    assert result.record_counts.get("project") == 1
    dependencies = {
        (row["dependency_kind"], row["dependency_id"])
        for row in conn.execute(
            """SELECT d.dependency_kind, d.dependency_id
                 FROM personal_model_record_dependencies d
                 JOIN personal_model_records r
                   ON r.generation_id=d.generation_id AND r.record_id=d.record_id
                WHERE d.generation_id='project-generation' AND r.record_kind='project'
                  AND d.target_generation_id IS NOT NULL"""
        )
    }
    # Every episode and observation the project cites, pinned by the record itself.
    assert {kind for kind, _ in dependencies} >= {"conversation_episode", "observation"}
    assert sum(1 for kind, _ in dependencies if kind == "observation") == 3


def test_a_named_refusal_reaches_the_operator_and_an_unnamed_one_does_not(
    conn: sqlite3.Connection,
) -> None:
    """Which check refused is reportable; what it read is not.

    The compiler used to say "invalid candidates" and nothing else, which is
    the same sentence whether one proposal in sixty thousand named a project
    that does not exist or the provider was down.  An operator watching an
    eight-hour compile die on it has a dead generation and nothing to act on.

    It now repeats the adapter's own named check, which is a fixed string, and
    still refuses to repeat anything else an adapter raised, because a plain
    ValueError from that boundary carries the model's output.
    """

    snapshot = add_graph(conn)
    add_turn(conn, "one", "Hello", order=1, sender="owner")
    check = "goal proposal references an unknown project key"

    def compile_with(adapter: object, generation_id: str):
        return PersonalModelCompiler(
            graph_reader=GraphReader(snapshot),
            evidence_reader=EntryReader(conn),
            repository=PersonalModelRepository(conn),
            state_store=SQLiteCompilerStateStore(conn),
            candidate_adapter=adapter,
            policy=CompilerPolicy(
                require_observation_annotator=False,
                allow_literal_only_activation=True,
            ),
        ).compile(CompileRequest(generation_id=generation_id, created_at=NOW))

    named = compile_with(NamedRefusalCandidateAdapter(), "named-refusal")
    assert named.failed
    assert check in (named.error or "")
    detail = conn.execute(
        """SELECT detail_json FROM personal_model_quarantines
           WHERE generation_id='named-refusal' AND stage='candidates'"""
    ).fetchone()[0]
    assert check in detail

    unnamed = compile_with(MalformedCandidateAdapter(), "unnamed-refusal")
    assert unnamed.failed
    assert "raw malformed payload" not in (unnamed.error or "")
    assert "without naming a check" in (unnamed.error or "")

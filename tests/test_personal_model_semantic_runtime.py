"""Managed-local semantic runtime composition and safety tests."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest

from varyz.config import Config
from varyz.domain import ConversationMetadata, Entry, Kind, Sensitivity
from varyz.personal_model import semantic_runtime
from varyz.personal_model.compiler import ExecutionLocality, GraphSnapshot
from varyz.personal_model.episodes import EpisodeReconstructor
from varyz.personal_model.identity import PersonResolver, SourceAlias, SourcePerson
from varyz.personal_model.models import (
    AliasKind,
    AttributionState,
    DerivationKind,
    DialogueContext,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    Observation,
    ObservationKind,
    ObservationModality,
    Polarity,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    TemporalScope,
    TextSpan,
)
from varyz.personal_model.observations import (
    OBSERVATION_SCHEMA_VERSION,
    ObservationExtractor,
    QuarantineCode,
)
from varyz.personal_model.projects import ProjectHierarchyCompiler
from varyz.personal_model.relationships import RelationshipHistoryCompiler
from varyz.personal_model.semantic_runtime import (
    SEMANTIC_CANDIDATE_SCHEMA_VERSION,
    LocalModelUnavailable,
    SemanticCandidateResponse,
    _CandidateSchemaVersion,
    _compact_schema,
    build_local_semantic_compiler,
    build_local_semantic_runtime,
    read_candidate_reply,
)
from varyz.personal_model.synthesis import (
    SYNTHESIS_SCHEMA_VERSION,
    AlternativeResponse,
    EvidenceSearchResponse,
    HypothesisProposalResponse,
)
from varyz.personal_model.validation import InterpretationValidator, ValidationPolicy

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
PRODUCER = ProducerMetadata(
    kind=ProducerKind.DETERMINISTIC,
    name="runtime-fixture",
    version="1",
    rule_version="1",
)


class FakeProvider:
    def __init__(
        self,
        schema_title: str,
        callback: Callable[[str, str, str], str],
        calls: list[tuple[str, str, str]],
        *,
        available: bool = True,
    ) -> None:
        self.model = "managed-local-fixture"
        self.schema_title = schema_title
        self.callback = callback
        self.calls = calls
        self._available = available

    def available(self) -> bool:
        return self._available

    def complete(self, system: str, user: str) -> str:
        self.calls.append((self.schema_title, system, user))
        return self.callback(self.schema_title, system, user)


class ProviderBuilder:
    def __init__(
        self,
        callback: Callable[[str, str, str], str],
        *,
        available: bool = True,
    ) -> None:
        self.callback = callback
        self.available = available
        self.calls: list[tuple[str, str, str]] = []
        self.builds: list[dict[str, Any]] = []

    def __call__(self, _cfg: Config, **kwargs: Any) -> FakeProvider:
        self.builds.append(kwargs)
        title = kwargs["format_schema"]["title"]
        return FakeProvider(title, self.callback, self.calls, available=self.available)


def entry(
    entry_id: str,
    body: str,
    *,
    order: int,
    sender: str,
    addressee: str,
    thread: str = "thread",
) -> Entry:
    return Entry(
        id=entry_id,
        kind=Kind.MESSAGE,
        source="fixture-chat",
        source_account="owner-account",
        external_id=f"native-{entry_id}",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        body=body,
        occurred_start=(NOW + timedelta(minutes=order)).isoformat(),
        ingested_at=NOW.isoformat(),
        thread_id=thread,
        authored_by_user=sender == "owner",
        conversation=ConversationMetadata(
            conversation_id=thread,
            participant_ids=("owner", "anna"),
            participants_complete=True,
            sender_id=sender,
            sender_label="Egor" if sender == "owner" else "Anna",
            addressee_ids=(addressee,),
            source_order=order,
        ),
        sensitivity=Sensitivity.PERSONAL,
    )


def empty_snapshot() -> GraphSnapshot:
    """A pinned graph carrying no allowlisted explicit claims.

    The literal half of the candidate adapter reads this contract even when the
    model half does all the proposing, so the runtime tests supply the real
    record rather than a stand-in that answers nothing.
    """

    return GraphSnapshot(
        generation_id="graph-generation",
        content_hash="0" * 64,
        extractor_name="fixture",
        extractor_version="1",
        policy_version="1",
    )


def identity(entries: tuple[Entry, ...]):
    people = tuple(
        SourcePerson(
            source="fixture-chat",
            source_account="owner-account",
            source_key=f"id:{key}",
            display_name=name,
            aliases=(
                SourceAlias(
                    kind=AliasKind.SOURCE_ACCOUNT_ID,
                    exact_value=key,
                    evidence_entry_ids=(entries[0].id,),
                    verified_durable=True,
                    identifier_scope="fixture-chat:owner-account",
                ),
            ),
            is_archive_owner=key == "owner",
            source_key_is_durable=True,
            sensitivity=Sensitivity.PERSONAL,
        )
        for key, name in (("owner", "Egor"), ("anna", "Anna"))
    )
    return PersonResolver().resolve(people, generation_id="generation", created_at=NOW)


def observation(
    index: int,
    *,
    owner_id: str,
    anna_id: str,
    proposition: str,
    kind: ObservationKind = ObservationKind.CHOICE,
) -> Observation:
    entry_id = f"evidence-{index}"
    episode_id = f"episode-{index}"
    at = NOW + timedelta(days=index * 40)
    span = TextSpan(
        entry_id=entry_id,
        start=0,
        end=len(proposition),
        exact_literal=proposition,
        source_revision=f"revision-{index}",
    )
    evidence = EvidenceReference(
        entry_id=entry_id,
        role=EvidenceRole.SUPPORTS,
        episode_id=episode_id,
        span=span,
        sensitivity=Sensitivity.PERSONAL,
    )
    return Observation(
        id=f"observation-{index}",
        generation_id="generation",
        evidence=(evidence.model_copy(update={"observation_id": f"observation-{index}"}),),
        temporal=TemporalScope(stated_at=at),
        confidence=0.95,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.OBSERVED,
        producer=PRODUCER,
        readiness=Readiness.STAGED,
        created_at=NOW,
        episode_id=episode_id,
        kind=kind,
        proposition=proposition,
        spans=(span,),
        speaker_person_id=owner_id,
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=EntityKind.PERSON,
        subject_id=owner_id,
        subject_attribution=AttributionState.RESOLVED,
        addressee_person_ids=(anna_id,),
        dialogue_context=DialogueContext.VOLUNTEERED_REASONING,
        polarity=Polarity.POSITIVE,
        modality=ObservationModality.ASSERTED,
        cost_or_stakes_literal=proposition,
    )


def test_runtime_builds_only_managed_local_schema_pinned_ports() -> None:
    builder = ProviderBuilder(lambda *_: "{}")
    cfg = Config()
    cfg.llm.provider = "claude"
    runtime = build_local_semantic_runtime(cfg, provider_builder=builder)

    assert runtime.model_name == "managed-local-fixture"
    assert len(builder.builds) == 5
    assert all(item["think"] is False for item in builder.builds)
    assert all(item["format_schema"] for item in builder.builds)
    assert runtime.annotator.execution_locality.value == "managed_local"
    assert runtime.candidate_adapter.execution_locality.value == "managed_local"
    assert runtime.synthesis_adapter.execution_locality.value == "managed_local"


def test_semantic_compiler_composition_wires_all_three_ports(conn, cfg: Config) -> None:
    builder = ProviderBuilder(lambda *_: "{}")

    composition = build_local_semantic_compiler(conn, cfg, provider_builder=builder)
    compiler = composition.compiler

    assert compiler.annotator is not None
    assert compiler.annotator.execution_locality == ExecutionLocality.MANAGED_LOCAL
    assert compiler.candidate_adapter.execution_locality == ExecutionLocality.MANAGED_LOCAL
    assert compiler.synthesis_adapter is not None
    assert compiler.synthesis_adapter.execution_locality == ExecutionLocality.MANAGED_LOCAL
    assert composition.model_name == "managed-local-fixture"


def test_correction_service_composition_keeps_semantic_ports(conn, cfg: Config) -> None:
    from varyz.personal_model.compiler import build_correction_application_service

    builder = ProviderBuilder(lambda *_: "{}")

    service = build_correction_application_service(conn, cfg, provider_builder=builder)

    assert service.compiler.annotator is not None
    assert service.compiler.annotator.execution_locality == ExecutionLocality.MANAGED_LOCAL
    assert service.compiler.candidate_adapter.execution_locality == ExecutionLocality.MANAGED_LOCAL
    assert service.compiler.synthesis_adapter is not None
    assert service.compiler.synthesis_adapter.execution_locality == ExecutionLocality.MANAGED_LOCAL


def test_unavailable_local_model_is_reported_before_any_semantic_call() -> None:
    builder = ProviderBuilder(lambda *_: "{}", available=False)
    try:
        build_local_semantic_runtime(Config(), provider_builder=builder)
    except LocalModelUnavailable as exc:
        assert "observation" in str(exc) and "candidate" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("unavailable managed model was accepted")
    assert builder.calls == []


def test_context_dependent_observation_reads_whole_episode_and_exact_span() -> None:
    turns = (
        entry("question", "Do you want sushi or pizza?", order=1, sender="anna", addressee="owner"),
        entry("answer", "Sushi, please", order=2, sender="owner", addressee="anna"),
    )
    resolved = identity(turns)

    def callback(title: str, _system: str, user: str) -> str:
        assert title == "AnnotationBatchResponse"
        request = json.loads(user)
        assert [item["body"] for item in request["sources"]] == [
            "Do you want sushi or pizza?",
            "Sushi, please",
        ]
        episode_id = request["episode_id"]
        target = request["target_entry_ids"][-1]
        return json.dumps(
            {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "episode_id": episode_id,
                "candidates": [
                    {
                        "episode_id": episode_id,
                        "target_entry_id": target,
                        "kind": "choice",
                        "proposition": "Sushi, please",
                        "spans": [
                            {
                                "entry_id": "answer",
                                "start": 0,
                                "end": 13,
                                "exact_literal": "Sushi, please",
                            }
                        ],
                        "subject": {
                            "attribution": "resolved",
                            "kind": "person",
                            "refers_to_speaker": True,
                        },
                        "dialogue_context": "direct_answer",
                        "polarity": "positive",
                        "modality": "asserted",
                        "confidence": 0.91,
                    }
                ],
            }
        )

    runtime = build_local_semantic_runtime(Config(), provider_builder=ProviderBuilder(callback))
    episodes = EpisodeReconstructor().build(
        turns,
        generation_id="generation",
        created_at=NOW,
        target_entry_ids=("answer",),
    )
    result = ObservationExtractor(runtime.annotator).extract(
        episodes,
        turns,
        entity_resolutions=resolved.source_entity_resolutions,
        generation_id="generation",
        created_at=NOW,
    )
    assert len(result.observations) == 1
    assert result.observations[0].proposition == "Sushi, please"
    assert result.observations[0].dialogue_context == DialogueContext.DIRECT_ANSWER


def test_malformed_observation_output_is_quarantined_without_provider_text() -> None:
    turns = (
        entry("question", "Which one?", order=1, sender="anna", addressee="owner"),
        entry("answer", "This one", order=2, sender="owner", addressee="anna"),
    )
    resolved = identity(turns)
    runtime = build_local_semantic_runtime(
        Config(), provider_builder=ProviderBuilder(lambda *_: "private malformed output")
    )
    episodes = EpisodeReconstructor().build(
        turns,
        generation_id="generation",
        created_at=NOW,
        target_entry_ids=("answer",),
    )
    result = ObservationExtractor(runtime.annotator).extract(
        episodes,
        turns,
        entity_resolutions=resolved.source_entity_resolutions,
        generation_id="generation",
        created_at=NOW,
    )
    assert result.observations == ()
    assert result.quarantines[0].code == QuarantineCode.MALFORMED_OUTPUT
    assert "private malformed output" not in result.quarantines[0].detail


def test_candidate_runtime_consolidates_project_rejects_errand_and_wires_relationship() -> None:
    turns = (
        entry(
            "one",
            "Varyz helps me understand my life",
            order=1,
            sender="owner",
            addressee="anna",
        ),
        entry("two", "I am still building Varyz", order=2, sender="owner", addressee="anna"),
    )
    resolved = identity(turns)
    owner_id = next(item.id for item in resolved.persons if item.is_archive_owner)
    anna_id = next(item.id for item in resolved.persons if not item.is_archive_owner)
    observations = (
        observation(
            1,
            owner_id=owner_id,
            anna_id=anna_id,
            proposition="Varyz helps me understand my life",
        ),
        observation(
            2,
            owner_id=owner_id,
            anna_id=anna_id,
            proposition="I am still building Varyz",
            kind=ObservationKind.COMMITMENT,
        ),
    )

    def callback(title: str, _system: str, user: str) -> str:
        if title != "SemanticCandidateResponse":
            return "{}"
        ids = [item["id"] for item in json.loads(user)["observations"]]
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": [ids[0]],
                    },
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": [ids[1]],
                    },
                ],
                "goals": [
                    {
                        "canonical_value_key": "buy_milk",
                        "title": "Buy milk",
                        "node_kind": "errand",
                        "status": "unknown",
                        "observation_ids": [ids[1]],
                    }
                ],
                "relationship_links": [
                    {
                        "observation_id": ids[1],
                        "other_person_ids": [anna_id],
                        "significant": True,
                    }
                ],
            }
        )

    runtime = build_local_semantic_runtime(Config(), provider_builder=ProviderBuilder(callback))
    batch = runtime.candidate_adapter.propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )
    projects = ProjectHierarchyCompiler().compile(
        generation_id="generation",
        observations=observations,
        atomic_claims=(),
        project_candidates=batch.projects,
        goal_candidates=batch.goals,
        created_at=NOW,
    )
    assert len(projects.projects) == 1
    assert len(projects.rejections) == 1
    relationships = RelationshipHistoryCompiler().compile(
        generation_id="generation",
        archive_owner_person_id=owner_id,
        significant_person_ids=batch.significant_person_ids,
        periods=batch.relationship_periods,
        people=resolved.persons,
        observations=observations,
        approvals=batch.relationship_approvals,
        observation_contexts=batch.relationship_observation_contexts,
        correlations=batch.relationship_correlations,
        created_at=NOW,
    )
    assert len(relationships.snapshots) == len(relationships.histories) == 1
    assert relationships.histories[0].commitment_observation_ids == ("observation-2",)


def test_local_synthesis_runs_symmetric_countersearch_and_only_offers_validated_question() -> None:
    turns = (
        entry("one", "first", order=1, sender="owner", addressee="anna"),
        entry("two", "second", order=2, sender="owner", addressee="anna"),
    )
    resolved = identity(turns)
    owner_id = next(item.id for item in resolved.persons if item.is_archive_owner)
    anna_id = next(item.id for item in resolved.persons if not item.is_archive_owner)
    observations = tuple(
        observation(
            index,
            owner_id=owner_id,
            anna_id=anna_id,
            proposition=f"Choice {index} protected autonomy at a real cost",
        )
        for index in range(1, 5)
    )
    episode_result = EpisodeReconstructor().build(
        tuple(
            entry(
                f"evidence-{index}",
                item.proposition,
                order=index,
                sender="owner",
                addressee="anna",
                thread=f"thread-{index}",
            )
            for index, item in enumerate(observations, start=1)
        ),
        generation_id="generation",
        created_at=NOW,
    )
    # Reconstructor ids are canonical, so align observations with its episode ids —
    # by the turn each observation actually quotes, since episode order is the
    # reconstructor's business and not the order the entries were written in.
    episode_of_entry = {
        turn.entry_id: episode.id
        for episode in episode_result.episodes
        for turn in episode.turns
    }
    observations = tuple(
        item.model_copy(update={"episode_id": episode_of_entry[item.spans[0].entry_id]})
        for item in observations
    )
    correlations = tuple(
        __import__(
            "varyz.personal_model.synthesis", fromlist=["EpisodeCorrelation"]
        ).EpisodeCorrelation(
            episode_id=item.episode_id,
            independence_group_id=f"decision-{index}",
            domain="work" if index % 2 else "relationships",
        )
        for index, item in enumerate(observations, start=1)
    )
    evidence_lanes: list[str] = []

    def callback(title: str, _system: str, user: str) -> str:
        payload = json.loads(user)
        if title == "HypothesisProposalResponse":
            ids = [item["id"] for item in payload["observations"]]
            return json.dumps(
                {
                    "schema_version": SYNTHESIS_SCHEMA_VERSION,
                    "proposals": [
                        {
                            "concept": "Protected autonomy",
                            "hypothesis": (
                                "Costly choices across contexts suggest protected autonomy."
                            ),
                            "kind": "value",
                            "scope": {"kind": "global"},
                            "observation_ids_explained": ids[:3],
                            "alternatives": [
                                {
                                    "alternative": "A temporary deadline explains it.",
                                    "distinguishing_evidence": (
                                        "Compare choices without deadlines."
                                    ),
                                }
                            ],
                        }
                    ],
                }
            )
        if title == "EvidenceSearchResponse":
            evidence_lanes.append(payload["lane"])
            ids = [item["id"] for item in payload["observations"]]
            selected = ids[:3] if payload["lane"] == "supports" else ids[3:]
            return json.dumps(
                {
                    "schema_version": SYNTHESIS_SCHEMA_VERSION,
                    "candidate_observation_ids": ids,
                    "selected_observation_ids": selected,
                }
            )
        if title == "AlternativeResponse":
            return json.dumps(
                {
                    "schema_version": SYNTHESIS_SCHEMA_VERSION,
                    "alternatives": [
                        {
                            "thesis": "This could reflect deadline pressure.",
                            "distinguishing_evidence": "Compare choices outside deadlines.",
                            "evidence_ids": [observations[3].id],
                        }
                    ],
                    "missing_evidence": ["A similar leisure choice."],
                }
            )
        return "{}"

    runtime = build_local_semantic_runtime(Config(), provider_builder=ProviderBuilder(callback))
    synthesis = runtime.synthesis_adapter.synthesize(
        generation_id="generation",
        observations=observations,
        episodes=episode_result,
        correlations=correlations,
        people=resolved.persons,
        projects=(),
        source_scopes=("fixture-chat:owner-account",),
        created_at=NOW,
    )
    assert evidence_lanes == ["supports", "contradicts"]
    assert len(synthesis.candidates) == 1
    candidate = synthesis.candidates[0]
    validation = InterpretationValidator(
        ValidationPolicy(calibration_manifest_hash="a" * 64)
    ).validate(
        candidate,
        generation_id="generation",
        observations=observations,
        episodes=episode_result.episodes,
        correlations=correlations,
        people=resolved.persons,
        validated_at=NOW,
    )
    assert validation.interpretation is not None, validation.issues
    questions = runtime.synthesis_adapter.offered_question_proposals(
        synthesis=synthesis,
        interpretations=(validation.interpretation,),
    )
    assert len(questions) == 1
    assert questions[0].interpretation_candidate_id == candidate.id
    assert runtime.synthesis_adapter.offered_question_proposals(
        synthesis=synthesis, interpretations=()
    ) == ()


def test_located_spans_take_their_offsets_from_the_entry_they_cite():
    """The quotation is the evidence; the offsets are arithmetic done for it."""

    from varyz.personal_model.semantic_runtime import _locate_spans

    body = "she said the deadline moved to Friday"
    candidate = {
        "spans": [
            {"entry_id": "entry-1", "exact_literal": "deadline moved", "start": 0, "end": 3}
        ]
    }
    located = _locate_spans(candidate, {"entry-1": body})
    span = located["spans"][0]
    assert body[span["start"] : span["end"]] == "deadline moved"
    assert span["end"] - span["start"] == len("deadline moved")


def test_a_literal_absent_from_its_entry_is_not_relocated():
    """Searching must not turn a quotation that was never there into a valid one."""

    from varyz.personal_model.semantic_runtime import _locate_spans

    candidate = {
        "spans": [
            {"entry_id": "entry-1", "exact_literal": "never written", "start": 0, "end": 13}
        ]
    }
    located = _locate_spans(candidate, {"entry-1": "something else entirely"})
    assert located["spans"][0] == candidate["spans"][0]


def test_a_literal_is_never_sought_outside_the_entry_the_candidate_names():
    from varyz.personal_model.semantic_runtime import _locate_spans

    candidate = {
        "spans": [{"entry_id": "entry-1", "exact_literal": "elsewhere", "start": 0, "end": 9}]
    }
    located = _locate_spans(
        candidate, {"entry-1": "no match here", "entry-2": "elsewhere"}
    )
    assert located["spans"][0]["start"] == 0
    assert located["spans"][0]["end"] == 9


def _breaker_annotator(max_consecutive_failures: int):
    from varyz.personal_model.semantic_runtime import LocalSemanticAnnotator

    class _Call:
        model_name = "fixture-model"

    return LocalSemanticAnnotator(
        _Call(),
        lambda count: _Call(),
        max_consecutive_failures=max_consecutive_failures,
    )


def test_one_unreadable_chunk_does_not_lose_the_rest_of_a_single_slot_group() -> None:
    """The one-slot path raised, and the caller wrote off every episode with it.

    The caching annotator sends only the episodes it has no cached annotation
    for, so late in a run one chunk is often the only chunk -- and the whole
    group it stands in for is discarded on a single truncated reply.  Measured
    on compile-20260830-230408: two replies a run ended mid-string, 384
    episodes written off each time, nearly all of them already annotated.
    """

    import types

    from varyz.personal_model.annotation_wire import WireGroupResponse
    from varyz.personal_model.semantic_runtime import LocalSemanticAnnotator

    class _Call:
        model_name = "fixture-model"

    try:
        WireGroupResponse.model_validate_json('{"r":[{"n":1,"c":[{"t":1')
    except Exception as exc:  # noqa: BLE001 - the reply this test is about
        truncated = exc

    annotator = LocalSemanticAnnotator(
        _Call(), lambda count: _Call(), concurrency=1, max_consecutive_failures=1000
    )
    requests = tuple(types.SimpleNamespace(episode_id=f"episode-{index}") for index in range(24))
    doomed = "episode-9"

    def chunk(self, group, call=None):
        if any(item.episode_id == doomed for item in group):
            raise truncated
        return {item.episode_id: "{}" for item in group}

    annotator._annotate_chunk = types.MethodType(chunk, annotator)

    answered = annotator.annotate_batch(requests)

    # Halving the chunk narrows the loss to the one episode that cannot be
    # read, rather than to the eight it was sent with.
    assert set(answered) == {item.episode_id for item in requests} - {doomed}


def test_a_run_of_failed_groups_stops_the_run_rather_than_reading_on(monkeypatch) -> None:
    from varyz.personal_model.observations import ObservationBudgetExhausted

    annotator = _breaker_annotator(3)
    monkeypatch.setattr(
        type(annotator),
        "_annotate_chunk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("server is gone")),
    )
    group = ("first", "second")

    assert annotator._annotate_chunk_or_none(group) is None
    assert annotator._annotate_chunk_or_none(group) is None
    try:
        annotator._annotate_chunk_or_none(group)
    except ObservationBudgetExhausted as exc:
        assert exc.reason == "provider_unavailable"
    else:  # pragma: no cover - the assertion below reports the failure
        raise AssertionError("a dead provider annotated the rest of the archive in silence")


def test_an_occasional_failed_group_does_not_stop_the_run(monkeypatch) -> None:
    annotator = _breaker_annotator(3)
    outcomes = iter([False, True, False, False, True, False, False, True])

    def chunk(_self, requests, call=None):
        if next(outcomes):
            raise ConnectionError("one bad reply")
        return {"episode": "{}"}

    monkeypatch.setattr(type(annotator), "_annotate_chunk", chunk)
    group = ("first", "second")
    results = [annotator._annotate_chunk_or_none(group) for _ in range(8)]

    assert [item is None for item in results] == [
        False,
        True,
        False,
        False,
        True,
        False,
        False,
        True,
    ]


def test_a_group_whose_reply_ran_out_of_room_is_asked_again_in_halves(monkeypatch) -> None:
    """A truncated reply is about the prompt's length, not about the episodes.

    The server stops a slot at its context limit and the JSON ends mid-string,
    which the strict parse cannot read; the whole group used to be written off,
    up to eight episodes at a time.  Half a group is half a prompt, so the
    halves have the room the whole did not.
    """

    annotator = _breaker_annotator(3)
    asked: list[int] = []

    def chunk(_self, requests, call=None):
        asked.append(len(requests))
        if len(requests) > 4:
            raise json.JSONDecodeError("EOF while parsing a string", '{"r":[', 4946)
        return {episode: "{}" for episode in requests}

    monkeypatch.setattr(type(annotator), "_annotate_chunk", chunk)
    group = tuple(f"episode-{index}" for index in range(8))

    recovered = annotator._annotate_chunk_or_none(group)

    assert recovered is not None
    assert sorted(recovered) == sorted(group)
    assert asked == [8, 4, 4]
    # The halves answered, so the run is not one step nearer being stopped.
    assert annotator._consecutive_failures == 0


class _Refusal(Exception):
    """A transport error carrying the server's status, as httpx raises one."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.response = type("_Response", (), {"status_code": status})()


def test_a_group_too_long_to_send_is_asked_again_in_halves(monkeypatch) -> None:
    """An over-context 400 is about this request's size, not about the server.

    The server refuses a prompt that overruns its context before generating a
    token, so nothing comes back to fail a parse -- the transport raises. That
    put the one failure halving was written for down the branch that does not
    halve, and seven groups of eight were written off identically in every one
    of four attempts of a real compile.
    """

    annotator = _breaker_annotator(3)
    asked: list[int] = []

    def chunk(_self, requests, call=None):
        asked.append(len(requests))
        if len(requests) > 2:
            raise _Refusal(400, "request (24239 tokens) exceeds the available context size")
        return {episode: "{}" for episode in requests}

    monkeypatch.setattr(type(annotator), "_annotate_chunk", chunk)
    group = tuple(f"episode-{index}" for index in range(8))

    recovered = annotator._annotate_chunk_or_none(group)

    assert recovered is not None
    assert sorted(recovered) == sorted(group)
    assert asked == [8, 4, 2, 2, 4, 2, 2]


def test_individually_oversized_requests_do_not_trip_the_provider_breaker(
    monkeypatch,
) -> None:
    """Request-specific refusals must not masquerade as provider downtime."""

    annotator = _breaker_annotator(3)

    def chunk(_self, requests, call=None):
        raise _Refusal(400, "request (9408 tokens) exceeds the available context size")

    monkeypatch.setattr(type(annotator), "_annotate_chunk", chunk)

    for index in range(12):
        assert annotator._annotate_chunk_or_none((f"oversized-{index}",)) is None

    assert annotator._consecutive_failures == 0


def test_a_server_that_failed_on_its_own_side_is_not_asked_twice_more(monkeypatch) -> None:
    """A 5xx says nothing about this request, so halving it is three calls for one.

    Only a 4xx is a statement about what was sent.
    """

    annotator = _breaker_annotator(99)
    asked: list[int] = []

    def chunk(_self, requests, call=None):
        asked.append(len(requests))
        raise _Refusal(503, "model is loading")

    monkeypatch.setattr(type(annotator), "_annotate_chunk", chunk)

    assert annotator._annotate_chunk_or_none(tuple(f"episode-{i}" for i in range(8))) is None
    assert asked == [8]


def test_a_provider_that_never_answered_is_not_asked_twice_more(monkeypatch) -> None:
    """Splitting is for a reply that arrived unreadable, not for silence.

    A server that is gone is gone for both halves, and retrying there would ask
    a dead provider three times for every group instead of once.
    """

    annotator = _breaker_annotator(99)
    asked: list[int] = []

    def chunk(_self, requests, call=None):
        asked.append(len(requests))
        raise ConnectionError("server is gone")

    monkeypatch.setattr(type(annotator), "_annotate_chunk", chunk)

    assert annotator._annotate_chunk_or_none(tuple(f"episode-{i}" for i in range(8))) is None
    assert asked == [8]

class _CandidateCall:
    """A candidate port that records what each window was asked."""

    model_name = "fixture-model"

    def __init__(self, reply) -> None:
        self.reply = reply
        self.systems: list[str] = []
        self.payloads: list[dict] = []

    def complete(self, *, system, payload) -> str:
        self.systems.append(system)
        self.payloads.append(json.loads(payload.model_dump_json()))
        return self.reply(len(self.payloads), self.payloads[-1])


def _candidate_fixture(count: int):
    turns = tuple(
        entry(f"turn-{index}", f"line {index}", order=index, sender="owner", addressee="anna")
        for index in range(1, count + 1)
    )
    resolved = identity(turns)
    owner_id = next(item.id for item in resolved.persons if item.is_archive_owner)
    anna_id = next(item.id for item in resolved.persons if not item.is_archive_owner)
    observations = tuple(
        observation(index, owner_id=owner_id, anna_id=anna_id, proposition=f"Varyz line {index}")
        for index in range(1, count + 1)
    )
    return turns, resolved, owner_id, anna_id, observations


def _candidate_adapter(
    call,
    *,
    observations_per_call: int,
    concurrency: int = 1,
    cache=None,
    max_consecutive_failures: int | None = None,
):
    from varyz.personal_model.semantic_runtime import (
        MAX_CONSECUTIVE_WINDOW_FAILURES,
        LocalSemanticCandidateAdapter,
    )

    return LocalSemanticCandidateAdapter(
        call,
        observations_per_call=observations_per_call,
        concurrency=concurrency,
        cache=cache,
        max_consecutive_failures=(
            MAX_CONSECUTIVE_WINDOW_FAILURES
            if max_consecutive_failures is None
            else max_consecutive_failures
        ),
    )


class _WindowCache:
    """The candidate-window cache, without a database under it."""

    def __init__(self) -> None:
        self.stored: dict[str, str] = {}
        self.provenance: list[dict[str, str]] = []

    def cached_candidate_windows(self, cache_keys):
        return {key: self.stored[key] for key in cache_keys if key in self.stored}

    def cache_candidate_windows(self, entries, **provenance):
        self.provenance.append(dict(provenance))
        for key, response in entries:
            self.stored.setdefault(key, response)


def _one_project_per_window():
    def reply(_index: int, payload: dict) -> str:
        ids = [item["id"] for item in payload["observations"]]
        anna_id = next(
            item["id"] for item in payload["people"] if not item["is_archive_owner"]
        )
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": ids,
                    }
                ],
                "relationship_links": [
                    {"observation_id": ids[0], "other_person_ids": [anna_id], "significant": True}
                ],
            }
        )

    return reply


def test_a_candidate_set_too_large_for_one_call_is_proposed_over_windows() -> None:
    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(3)
    call = _CandidateCall(_one_project_per_window())
    batch = _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert [len(item["observations"]) for item in call.payloads] == [1, 1, 1]
    # Numbered for the wire, so which observation went where is read off the
    # proposition -- every window's own id is "1".
    seen = [item["observations"][0]["proposition"] for item in call.payloads]
    assert sorted(seen) == sorted(item.proposition for item in observations)
    assert {item["observations"][0]["id"] for item in call.payloads} == {"1"}
    # One canonical key across three windows is still one project, because the
    # windows are pooled before ``_projects`` groups them.
    assert len(batch.projects) == 1
    assert len(batch.relationship_observation_contexts) == 3
    assert len(batch.episode_correlations) == 3


def test_a_candidate_window_read_once_is_not_read_again(monkeypatch) -> None:
    """Three attempts of one run each bought all 324 windows and threw them away."""

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(3)
    cache = _WindowCache()

    first_call = _CandidateCall(_one_project_per_window())
    first = _candidate_adapter(first_call, observations_per_call=1, cache=cache).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )
    assert len(first_call.payloads) == 3
    assert len(cache.stored) == 3

    second_call = _CandidateCall(_one_project_per_window())
    second = _candidate_adapter(second_call, observations_per_call=1, cache=cache).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert second_call.payloads == []
    assert len(second.projects) == len(first.projects)
    assert len(second.episode_correlations) == len(first.episode_correlations)
    assert cache.provenance[0]["prompt_version"] == "personal-model-candidates-v5"
    assert cache.provenance[0]["model_name"] == "fixture-model"


def test_a_window_asked_with_different_project_keys_is_not_the_same_window() -> None:
    """The instruction carries the keys earlier windows coined, so it is part of the key."""

    _turns, _resolved, _owner_id, _anna_id, observations = _candidate_fixture(1)
    adapter = _candidate_adapter(
        _CandidateCall(lambda _index, _payload: ""), observations_per_call=1
    )
    request, _numbering = adapter._request(observations, {})
    bare = adapter._cache_key(adapter._candidate_system({}), request)
    carried = adapter._cache_key(adapter._candidate_system({"varyz": "Varyz"}), request)

    assert bare != carried


def test_a_candidate_reply_that_could_not_be_read_is_not_stored() -> None:
    """A cache hit has to be a reply that crossed the contract, or it is a way in."""

    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(1)
    cache = _WindowCache()
    call = _CandidateCall(lambda _index, _payload: "{ this is not a reply")

    with pytest.raises(ValueError):
        _candidate_adapter(call, observations_per_call=1, cache=cache).propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=observations,
        )

    assert cache.stored == {}


def test_the_candidate_window_cache_survives_the_process_that_wrote_it(conn) -> None:
    """The point is a later attempt, which is a later connection to the same file."""

    from varyz.personal_model.compiler import SQLiteCompilerStateStore

    store = SQLiteCompilerStateStore(conn)
    assert store.cached_candidate_windows(["absent"]) == {}
    store.cache_candidate_windows(
        [("key-1", '{"schema_version":"1"}')],
        adapter_name="fixture-adapter",
        adapter_version="1",
        model_name="fixture-model",
        prompt_version="fixture-prompt",
        schema_version="1",
    )

    assert store.cached_candidate_windows(["key-1", "absent"]) == {
        "key-1": '{"schema_version":"1"}'
    }


def test_an_episode_is_never_split_across_candidate_windows() -> None:
    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(2)
    shared = observations[1].model_copy(update={"episode_id": observations[0].episode_id})
    call = _CandidateCall(_one_project_per_window())
    _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=(observations[0], shared),
    )

    assert [len(item["observations"]) for item in call.payloads] == [2]


def _project_naming(anna_id: str, bad_windows: set[int], bad_links: set[int] | None = None):
    """A project per window, citing an observation that does not exist in some.

    ``bad_links`` names windows whose relationship link points at a person the
    identity does not hold, which is the other half of what an assembly leaves
    out.
    """

    def reply(index: int, payload: dict) -> str:
        ids = [item["id"] for item in payload["observations"]]
        cited = ["observation-that-was-never-proposed"] if index in bad_windows else ids
        linked = "person-that-was-never-resolved" if index in (bad_links or set()) else anna_id
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": cited,
                    }
                ],
                "relationship_links": [
                    {"observation_id": ids[0], "other_person_ids": [linked], "significant": True}
                ],
            }
        )

    return reply


def test_one_unresolvable_proposal_does_not_void_the_windows_that_were_read() -> None:
    """A window loop that tolerates a lost window, followed by an assembly that did not.

    Sixty windows of a real archive were read, and one proposal among them
    naming an observation that does not exist ended the whole compile with
    nothing to show for eight hours.  The proposal is left out instead.
    """

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(10)
    call = _CandidateCall(_project_naming(anna_id, {3}))

    batch = _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert len(call.payloads) == 10
    assert len(batch.projects) == 1
    # Nine windows' evidence survives; the tenth proposal is the one left out.
    assert len(batch.projects[0].observation_ids) == 9
    assert len(batch.relationship_observation_contexts) == 10


def test_a_batch_that_is_mostly_unresolvable_is_still_refused() -> None:
    """Tolerance is for a stray proposal, not for a model answering in another shape.

    A run where most proposals will not resolve has lost its id space or its
    schema, and a model built from what is left would be hollow rather than
    merely smaller.  That still ends the stage, and says how much it dropped.
    """

    from varyz.personal_model.models import CandidateAssemblyError

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(10)
    call = _CandidateCall(_project_naming(anna_id, set(range(1, 11)), bad_links={1}))

    try:
        _candidate_adapter(call, observations_per_call=1).propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=observations,
        )
    except CandidateAssemblyError as refused:
        assert "11 of 20" in refused.reason
        assert "unknown observations" in refused.reason
    else:  # pragma: no cover - the assertion below reports the failure
        raise AssertionError("a batch of unresolvable proposals compiled into a model")


def test_a_quarter_of_the_proposals_left_out_is_a_smaller_model_not_a_refusal() -> None:
    """The rate three compiles of the live archive actually produced.

    607 of 2319 and 562 of 2320 -- a quarter each time, and each time the
    windows had already been read.  A fifth was the line, so all three ended
    with nothing.  A quarter is a stage that worked less well than it should;
    the compile keeps what resolved and says what it left out.
    """

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(10)
    call = _CandidateCall(_project_naming(anna_id, {1, 2, 3, 4, 5}))

    batch = _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    # Five windows of project evidence lost, ten relationships kept whole.
    assert len(batch.projects) == 1
    assert len(batch.projects[0].observation_ids) == 5
    assert len(batch.relationship_observation_contexts) == 10


def test_a_person_the_model_was_shown_is_not_counted_as_one_it_invented() -> None:
    """The largest drop in three failed compiles, and it had one name for two faults.

    An observation about a place carries that place's id, and the window the
    model reads shows it.  Naming it back is this code offering something and
    then refusing it -- nothing like the model coining an id nobody has ever
    held, and not fixable by the same change.
    """

    from varyz.personal_model.models import CandidateAssemblyError

    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(2)
    elsewhere = observations[0].model_copy(
        update={"subject_kind": EntityKind.PLACE, "subject_id": "place-leiden"}
    )

    def reply(index: int, payload: dict) -> str:
        ids = [item["id"] for item in payload["observations"]]
        named = "place-leiden" if index == 1 else "person-that-was-never-resolved"
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "relationship_links": [
                    {"observation_id": ids[0], "other_person_ids": [named], "significant": True}
                ],
            }
        )

    with pytest.raises(CandidateAssemblyError) as refused:
        _candidate_adapter(_CandidateCall(reply), observations_per_call=1).propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=(elsewhere, observations[1]),
        )

    assert "names a person shown to it that is not in the identity (1)" in refused.value.reason
    assert "references an unknown person (1)" in refused.value.reason


def test_a_proposal_that_names_one_observation_twice_is_read_not_refused() -> None:
    """Repeating a citation was refused beside inventing one, and they differ.

    An id that is not in the window is evidence the model made up.  An id given
    twice cites nothing that is not there, and every reader downstream already
    merges these ids through a set before building anything from them.
    """

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(2)

    def reply(index: int, payload: dict) -> str:
        ids = [item["id"] for item in payload["observations"]]
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": ids + ids,
                    }
                ],
                "relationship_links": [
                    {"observation_id": ids[0], "other_person_ids": [anna_id], "significant": True}
                ],
            }
        )

    batch = _candidate_adapter(_CandidateCall(reply), observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert len(batch.projects) == 1
    cited = batch.projects[0].observation_ids
    assert len(cited) == len(set(cited)) == 2


def test_a_proposal_citing_an_episode_is_told_from_one_citing_nothing() -> None:
    """An observation id and an episode id are both sixty-four hex characters.

    Every observation a window carries has one of each, so a model that answers
    with the wrong one of the two is refused for inventing evidence -- which is
    a different fault with a different repair, and the run log named neither.
    """

    from varyz.personal_model.models import CandidateAssemblyError

    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(10)

    def reply(index: int, payload: dict) -> str:
        item = payload["observations"][0]
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": f"project-{index}",
                        "display_name": f"Project {index}",
                        "explicit_self_reference": True,
                        "observation_ids": [item["episode_id"]],
                    }
                ],
            }
        )

    try:
        _candidate_adapter(_CandidateCall(reply), observations_per_call=1).propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=observations,
        )
    except CandidateAssemblyError as refused:
        assert "cites episodes where observations belong" in refused.reason
        assert "unknown observations" not in refused.reason
    else:  # pragma: no cover - the assertion below reports the failure
        raise AssertionError("a proposal citing an episode resolved to an observation")


def test_a_relationship_said_twice_is_not_a_proposal_that_would_not_resolve() -> None:
    """One observation carries one relationship record, so the second is the first.

    Counting it as unresolvable spent the one-fifth allowance that exists to
    catch a model answering in a shape this code cannot read.
    """

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(1)

    def reply(index: int, payload: dict) -> str:
        ids = [item["id"] for item in payload["observations"]]
        link = {"observation_id": ids[0], "other_person_ids": [anna_id], "significant": True}
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": ids,
                    }
                ],
                "relationship_links": [link, dict(link)],
            }
        )

    batch = _candidate_adapter(_CandidateCall(reply), observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert len(batch.relationship_observation_contexts) == 1


def test_a_candidate_window_the_model_could_not_read_does_not_lose_the_others() -> None:
    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(3)
    answer = _one_project_per_window()

    def reply(index: int, payload: dict) -> str:
        return "not json at all" if index == 2 else answer(index, payload)

    call = _CandidateCall(reply)
    batch = _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert len(call.payloads) == 3
    assert len(batch.projects) == 1
    assert len(batch.relationship_observation_contexts) == 2


def test_a_window_the_model_looped_on_is_asked_again_in_halves() -> None:
    """A reply cut off mid-string is a window that ran to its token ceiling.

    Two or three a run arrive that way, their tails the same few characters
    over and over, and each one is a stretch of the archive never read.  Half a
    window is half as much to write about.
    """

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(4)
    whole = _one_project_per_window()

    def reply(index: int, payload: dict) -> str:
        if len(payload["observations"]) > 1:
            return '{"schema_version": "literal", "projects": [{"canonical_value'
        return whole(index, payload)

    call = _CandidateCall(reply)
    batch = _candidate_adapter(call, observations_per_call=2).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    # Every window of two was refused; every half of one was read.
    assert len(batch.relationship_observation_contexts) == 4
    assert len(batch.projects) == 1
    assert len(batch.projects[0].observation_ids) == 4


def test_every_candidate_window_failing_is_raised_as_it_always_was() -> None:
    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(2)
    call = _CandidateCall(lambda _index, _payload: "not json at all")
    with pytest.raises(ValueError):
        _candidate_adapter(call, observations_per_call=1).propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=observations,
        )


def test_a_candidate_window_numbers_its_observations_and_nothing_else() -> None:
    """The observations are numbered; the people and the episodes are not.

    Asked on 2026-09-06 to echo sixty-four-character keys, the live 12B
    returned 32% of them corrupted, so the ids it must copy were replaced with
    positions.  Doing that to all three namespaces made it worse, not better:
    over the same fifty windows, numbering everything gave 2.36 proposals a
    window against 3.10 for the archive's own ids and produced no projects and
    no episode correlations at all, while numbering the observations alone gave
    4.96.  An observation id points back at a line the prompt already quotes;
    a person and an episode are what the window is *about*, and anonymising
    them left the model with nothing to say.
    """

    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(3)
    call = _CandidateCall(_one_project_per_window())
    _candidate_adapter(call, observations_per_call=3).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    payload = call.payloads[0]
    assert [item["id"] for item in payload["observations"]] == ["1", "2", "3"]

    episodes = {item.episode_id for item in observations[:3]}
    assert {item["episode_id"] for item in payload["observations"]} <= episodes
    named = {item.speaker_person_id for item in observations[:3]}
    people = {person["id"] for person in payload["people"]}
    assert people and named <= people
    assert all(
        value is None or value in people
        for item in payload["observations"]
        for value in (item["speaker_person_id"], item["subject_id"])
    )


def test_a_window_is_only_shown_the_people_its_observations_name() -> None:
    turns, resolved, owner_id, anna_id, observations = _candidate_fixture(2)
    alone = observations[1].model_copy(update={"addressee_person_ids": ()})

    def reply(_index: int, payload: dict) -> str:
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": [item["id"] for item in payload["observations"]],
                    }
                ],
            }
        )

    call = _CandidateCall(reply)
    _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=(observations[0], alone),
    )

    shown = [
        {item["display_name"] for item in payload["people"]} for payload in call.payloads
    ]
    owner = next(item.display_name for item in resolved.persons if item.id == owner_id)
    anna = next(item.display_name for item in resolved.persons if item.id == anna_id)
    assert {owner, anna} in shown
    # The window whose observation addresses nobody is not shown Anna at all.
    assert {owner} in shown


def test_keys_coined_in_an_earlier_candidate_window_are_offered_to_the_next() -> None:
    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(2)
    call = _CandidateCall(_one_project_per_window())
    _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert "varyz" not in call.systems[0]
    assert "varyz (Varyz)" in call.systems[1]

# A managed-local slot is 16,384 tokens and the candidate reply is allowed 4,096
# of them, so the prompt has around 12,000 tokens to live in. At the 3.5
# characters a token this archive averages that is near 42,000 characters; the
# guard below is set under it, because a prompt that only just fits is a prompt
# that stops fitting the first time an observation is longer than the mean.
_CANDIDATE_PROMPT_CHARACTER_BUDGET = 40_000


def _crowded_identity(count: int, entries: tuple[Entry, ...]):
    """One owner, one correspondent who is actually spoken to, and a crowd."""

    people = tuple(
        SourcePerson(
            source="fixture-chat",
            source_account="owner-account",
            source_key=f"id:{key}",
            display_name=name,
            aliases=(
                SourceAlias(
                    kind=AliasKind.SOURCE_ACCOUNT_ID,
                    exact_value=key,
                    evidence_entry_ids=(entries[0].id,),
                    verified_durable=True,
                    identifier_scope="fixture-chat:owner-account",
                ),
            ),
            is_archive_owner=key == "owner",
            source_key_is_durable=True,
            sensitivity=Sensitivity.PERSONAL,
        )
        for key, name in (
            ("owner", "Egor"),
            ("anna", "Anna"),
            *((f"other-{index}", f"Person Number {index}") for index in range(count)),
        )
    )
    return PersonResolver().resolve(people, generation_id="generation", created_at=NOW)


def test_no_candidate_call_is_larger_than_the_slot_it_is_sent_to() -> None:
    """The shape that killed a generation on 2026-08-15, at the scale that did it.

    That compile died with ``400 Bad Request ... request (52593 tokens) exceeds
    the av``: every observation and all 375 resolved people went into one call.
    The compiler reads that as a malformed candidate response and raises, so the
    cost was the whole run, after its annotation had already been paid for.
    """

    turns = tuple(
        entry(f"turn-{index}", f"line {index}", order=index, sender="owner", addressee="anna")
        for index in range(1, 201)
    )
    resolved = _crowded_identity(373, turns)
    owner_id = next(item.id for item in resolved.persons if item.is_archive_owner)
    anna_id = next(item.id for item in resolved.persons if item.display_name == "Anna")
    observations = tuple(
        observation(
            index,
            owner_id=owner_id,
            anna_id=anna_id,
            proposition=f"Varyz is the thing I keep coming back to, line {index}",
        )
        for index in range(1, 201)
    )
    assert len(resolved.persons) == 375

    def callback(title: str, _system: str, user: str) -> str:
        if title != "SemanticCandidateResponse":
            return "{}"
        ids = [item["id"] for item in json.loads(user)["observations"]]
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": ids,
                    }
                ],
            }
        )

    builder = ProviderBuilder(callback)
    runtime = build_local_semantic_runtime(Config(), provider_builder=builder)
    batch = runtime.candidate_adapter.propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    sizes = [len(system) + len(user) for _title, system, user in builder.calls]
    assert sizes, "the candidate stage made no call at all"
    assert max(sizes) <= _CANDIDATE_PROMPT_CHARACTER_BUDGET, max(sizes)
    # Every observation is still read, in more than one call, and the 373 people
    # no observation names are never sent.
    assert len(builder.calls) > 1
    seen = {
        item["proposition"]
        for _title, _system, user in builder.calls
        for item in json.loads(user)["observations"]
    }
    assert seen == {item.proposition for item in observations}
    named = {
        item["display_name"]
        for _title, _system, user in builder.calls
        for item in json.loads(user)["people"]
    }
    assert named == {
        item.display_name for item in resolved.persons if item.id in {owner_id, anna_id}
    }
    assert len(batch.projects) == 1

def test_one_goal_named_in_two_windows_is_one_goal_and_not_two_ids_in_conflict() -> None:
    """The same goal recurs window after window; it used to break the compile.

    ``_goals`` built its ids in a comprehension keyed by canonical key, so two
    proposals carrying one key shared whichever id came last while keeping
    their own evidence. ``ProjectHierarchyCompiler`` rejects that as a
    conflicting duplicate candidate id, the compiler reads the rejection as a
    malformed candidate response, and the generation is lost.
    """

    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(2)

    def reply(_index: int, payload: dict) -> str:
        ids = [item["id"] for item in payload["observations"]]
        return json.dumps(
            {
                "schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION,
                "projects": [
                    {
                        "canonical_value_key": "varyz",
                        "display_name": "Varyz",
                        "explicit_self_reference": True,
                        "observation_ids": ids,
                    }
                ],
                "goals": [
                    {
                        "canonical_value_key": "ship_the_alpha",
                        "title": "Ship the alpha",
                        "node_kind": "goal",
                        "status": "unknown",
                        "project_keys": ["varyz"],
                        "observation_ids": ids,
                    }
                ],
            }
        )

    call = _CandidateCall(reply)
    batch = _candidate_adapter(call, observations_per_call=1).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert len(call.payloads) == 2
    assert len(batch.goals) == 1
    goal = batch.goals[0]
    # Both windows' evidence, under one id.
    assert set(goal.observation_ids) == {item.id for item in observations}
    assert goal.project_candidate_ids == (batch.projects[0].candidate_id,)
    # The stage that used to raise on this now compiles it.
    hierarchy = ProjectHierarchyCompiler().compile(
        generation_id="generation",
        observations=observations,
        atomic_claims=(),
        project_candidates=batch.projects,
        goal_candidates=batch.goals,
        created_at=NOW,
    )
    assert len(hierarchy.projects) == 1


def _candidate_reply(**lists) -> str:
    payload = {"schema_version": SEMANTIC_CANDIDATE_SCHEMA_VERSION}
    payload.update(lists)
    return json.dumps(payload)


def _good_goal() -> dict:
    return {
        "canonical_value_key": "ship-varyz",
        "title": "Ship Varyz",
        "node_kind": "goal",
        "observation_ids": ["obs-1"],
    }


def test_one_proposal_the_schema_refuses_does_not_cost_the_window() -> None:
    """A grammar cannot express a Python validator, so the model cannot obey one.

    ``GoalProposal.status_has_literal`` is exactly that: a rule about two
    fields together, invisible to the decoder that is constraining the reply.
    Twelve windows across the runs of 2026-08-30 were refused whole over rules
    of that kind, asked again in halves, and then lost -- a stretch of the
    archive never read, for one bad item among dozens of good ones.
    """

    unknown_status_with_a_literal = _good_goal() | {"status_literal": "done"}
    raw = _candidate_reply(goals=[_good_goal(), unknown_status_with_a_literal])

    response, dropped = read_candidate_reply(raw)

    assert dropped == 1
    assert [goal.title for goal in response.goals] == ["Ship Varyz"]


def test_a_key_that_is_not_a_key_drops_only_itself() -> None:
    raw = _candidate_reply(
        projects=[
            {
                "canonical_value_key": "Not A Key",
                "display_name": "Varyz",
                "observation_ids": ["obs-1"],
            },
            {
                "canonical_value_key": "varyz",
                "display_name": "Varyz",
                "observation_ids": ["obs-1"],
            },
        ],
        goals=[_good_goal()],
    )

    response, dropped = read_candidate_reply(raw)

    assert dropped == 1
    assert [item.canonical_value_key for item in response.projects] == ["varyz"]
    # The other lists are untouched by a failure in this one.
    assert len(response.goals) == 1


def test_a_reply_with_nothing_wrong_with_it_is_not_rebuilt() -> None:
    raw = _candidate_reply(goals=[_good_goal()])

    response, dropped = read_candidate_reply(raw)

    assert dropped == 0
    assert response.schema_version == SEMANTIC_CANDIDATE_SCHEMA_VERSION


def test_a_reply_that_was_cut_off_still_raises_so_the_window_is_halved() -> None:
    """Truncation is the one failure halving fixes, and it must still reach it."""

    with pytest.raises(json.JSONDecodeError):
        read_candidate_reply('{"schema_version": "personal-model-candidates-v1", "goa')


class _ConcurrentCandidateCall:
    """A candidate port that records overlap, and is safe to call from threads."""

    model_name = "fixture-model"

    def __init__(self, reply, *, hold: int = 0) -> None:
        self.reply = reply
        self.payloads: list[dict] = []
        self.systems: list[str] = []
        self.high_water = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self._arrived = threading.Barrier(hold) if hold > 1 else None

    def complete(self, *, system, payload) -> str:
        body = json.loads(payload.model_dump_json())
        with self._lock:
            self._in_flight += 1
            self.high_water = max(self.high_water, self._in_flight)
            self.systems.append(system)
            self.payloads.append(body)
            index = len(self.payloads)
        if self._arrived is not None:
            # Blocks until the whole wave has arrived, so a sequential reader
            # deadlocks here rather than passing by accident.
            self._arrived.wait(timeout=30)
        try:
            return self.reply(index, body)
        finally:
            with self._lock:
                self._in_flight -= 1


def test_a_wave_puts_every_slot_to_work_at_once() -> None:
    """One window at a time left five of six slots idle for the whole stage.

    Measured on compile-20260831-141645: a window is 18 to 41 seconds and
    nothing else was in flight for any of them.
    """

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(6)
    call = _ConcurrentCandidateCall(_one_project_per_window(), hold=3)

    batch = _candidate_adapter(call, observations_per_call=1, concurrency=3).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert call.high_water == 3, call.high_water
    assert len(call.payloads) == 6
    assert len(batch.projects) == 1


def test_reading_in_waves_gives_the_same_answer_as_reading_one_at_a_time() -> None:
    """Threads may finish in any order; the fold happens in window order."""

    def propose(*, concurrency: int, hold: int = 0):
        turns, resolved, _owner, anna_id, observations = _candidate_fixture(6)
        call = _ConcurrentCandidateCall(_one_project_per_window(), hold=hold)
        batch = _candidate_adapter(
            call, observations_per_call=1, concurrency=concurrency
        ).propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=observations,
        )
        return batch, call

    one, sequential = propose(concurrency=1)
    # Six windows in waves of two, each wave held until both of it arrive, so
    # the two really are in flight together rather than passing by being quick.
    many, waved = propose(concurrency=2, hold=2)

    assert sequential.high_water == 1
    assert waved.high_water == 2
    assert [item.canonical_value_key for item in one.projects] == [
        item.canonical_value_key for item in many.projects
    ]
    assert len(one.relationship_observation_contexts) == len(
        many.relationship_observation_contexts
    )
    assert len(one.episode_correlations) == len(many.episode_correlations)
    # Every window is asked, whichever way round they are read.
    assert sorted(
        item["observations"][0]["id"] for item in waved.payloads
    ) == sorted(item["observations"][0]["id"] for item in sequential.payloads)


def test_a_wave_carries_the_keys_coined_by_the_waves_before_it() -> None:
    """The consolidation this stage gets is the keys it hands the next window."""

    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(4)
    call = _ConcurrentCandidateCall(_one_project_per_window())

    _candidate_adapter(call, observations_per_call=1, concurrency=2).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    # Four windows, two waves. The first wave is asked with nothing known; the
    # second is asked with the key the first coined.
    assert sum("varyz" in system for system in call.systems) == 2, call.systems


def test_a_provider_that_fails_every_window_stops_the_compile() -> None:
    """Losing one window is a stretch unread; losing all of them is a lie."""

    from varyz.personal_model.semantic_runtime import LocalModelUnavailable

    turns, resolved, _owner_id, _anna_id, observations = _candidate_fixture(6)

    def always_fails(_index: int, _payload: dict) -> str:
        raise TimeoutError("the request never came back")

    call = _CandidateCall(always_fails)
    adapter = _candidate_adapter(call, observations_per_call=1, max_consecutive_failures=3)

    with pytest.raises(LocalModelUnavailable, match="came back"):
        adapter.propose(
            snapshot=empty_snapshot(),
            entries=turns,
            identity=resolved,
            observations=observations,
        )

    # Stopped at the third, rather than reading the archive to the end and
    # persisting whatever the first two windows happened not to say.
    assert len(call.payloads) == 3


def test_one_window_failing_is_still_only_that_window() -> None:
    turns, resolved, _owner_id, anna_id, observations = _candidate_fixture(4)
    good = _one_project_per_window()

    def fails_once(index: int, payload: dict) -> str:
        if index == 2:
            raise TimeoutError("the request never came back")
        return good(index, payload)

    call = _CandidateCall(fails_once)
    batch = _candidate_adapter(
        call, observations_per_call=1, max_consecutive_failures=3
    ).propose(
        snapshot=empty_snapshot(),
        entries=turns,
        identity=resolved,
        observations=observations,
    )

    assert len(call.payloads) == 4
    # Three windows answered, so the count never reached the threshold and the
    # run finished. What the fourth would have said is simply missing: one
    # project still, pooled from three windows rather than four, and its
    # evidence covers three of the four observations.
    assert len(batch.relationship_observation_contexts) == 3
    assert len(batch.projects) == 1
    assert len(batch.projects[0].observation_ids) == 3



def _required_but_absent(node: object, path: str = "root") -> list[tuple[str, list[str]]]:
    """Every object in a schema whose ``required`` names a property it lacks."""

    found: list[tuple[str, list[str]]] = []
    if isinstance(node, dict):
        properties = node.get("properties")
        required = node.get("required")
        if isinstance(properties, dict) and isinstance(required, list):
            missing = sorted(set(required) - set(properties))
            if missing:
                found.append((path, missing))
        for key, value in node.items():
            found += _required_but_absent(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _required_but_absent(value, f"{path}[{index}]")
    return found


@pytest.mark.parametrize(
    "model",
    [
        semantic_runtime.SemanticCandidateResponse,
        semantic_runtime.AnnotationBatchResponse,
        HypothesisProposalResponse,
        EvidenceSearchResponse,
        AlternativeResponse,
    ],
    ids=lambda model: model.__name__,
)
def test_every_required_property_is_one_the_model_may_write(model: type) -> None:
    """A reply nobody can write is a stage that silently produces nothing.

    The schema is sent with ``additionalProperties: false`` and decoded under a
    grammar built from it, so a property missing from ``properties`` cannot
    appear in a reply -- and a property in ``required`` must. One in both
    states is a proposal that can never validate, dropped on the way back and
    counted only as a proposal that "would not read".

    ``GoalProposal.title`` was in exactly that state for the life of this
    stage: the compaction that strips Pydantic's ``"title"`` metadata was
    stripping the field named ``title`` with it. The archive compiled 908
    candidate windows and 0 goals.
    """

    assert _required_but_absent(semantic_runtime._compact_schema(model)) == []


def test_compaction_still_drops_the_metadata_it_is_for() -> None:
    schema = semantic_runtime._compact_schema(semantic_runtime.SemanticCandidateResponse)
    goal = schema["$defs"]["GoalProposal"]
    # The field survives; the object's own Pydantic title does not.
    assert "title" in goal["properties"]
    assert "title" not in goal
    assert "title" not in goal["properties"]["canonical_value_key"]


def test_the_schema_version_is_a_const_the_grammar_writes() -> None:
    """The model cannot mistype what the grammar spells for it.

    A candidate window on 2026-09-06 came back declaring its schema version as
    ``":"`` and was thrown away whole -- a stretch of the archive read, paid
    for, and then never used, because a field the caller already knew the
    answer to was left for the model to copy.
    """

    assert get_args(_CandidateSchemaVersion) == (SEMANTIC_CANDIDATE_SCHEMA_VERSION,)
    sent = _compact_schema(SemanticCandidateResponse)
    assert sent["properties"]["schema_version"]["const"] == SEMANTIC_CANDIDATE_SCHEMA_VERSION

"""MCP projections of the active personal model are bounded and SELECT-only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from varyz.cli import app
from varyz.config import Config
from varyz.db.connection import open_db
from varyz.domain import Sensitivity, TimePrecision
from varyz.evidence import SQLiteEvidenceReader
from varyz.mcp_server import MAX_PERSONAL_MODEL_REQUEST_BYTES, VaryzServer
from varyz.personal_model.models import (
    AlternativeExplanation,
    CountersearchLane,
    CountersearchManifest,
    CountersearchState,
    DerivationKind,
    EntityKind,
    Event,
    EvidenceRole,
    Fact,
    FactStatus,
    Interpretation,
    InterpretationEvidence,
    InterpretationKind,
    InterpretationScope,
    InterpretationVerdict,
    MonthDayFactValue,
    Person,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Project,
    Readiness,
    ScopeKind,
    SubjectReference,
    TemporalScope,
    TextFactValue,
)
from varyz.personal_model.query import LifeModelQueryService
from varyz.privacy.models import ConsentGrant
from varyz.privacy.receipts import ReceiptStore

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
PRODUCER = ProducerMetadata(kind=ProducerKind.USER, name="owner", version="1")


class _OfflineLocalProvider:
    model = "offline-semantic-fixture"

    def available(self) -> bool:
        return True

    def complete(self, _system: str, _user: str) -> str:
        raise AssertionError("the empty-entry fixture must not request semantic completion")


def _offline_local_provider(*_args, **_kwargs) -> _OfflineLocalProvider:
    return _OfflineLocalProvider()


def _base(generation: str, record_id: str, **updates):
    return {
        "id": record_id,
        "generation_id": generation,
        "confidence": 0.9,
        "sensitivity": Sensitivity.PERSONAL,
        "derivation": DerivationKind.USER_CONFIRMED,
        "producer": PRODUCER,
        "readiness": Readiness.READY,
        "created_at": NOW,
        **updates,
    }


def _insert_generation(conn: sqlite3.Connection, generation: str) -> None:
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at) VALUES (?,'graph-test','test','test',?,'staged',?)""",
        (generation, f"input-{generation}", NOW.isoformat()),
    )


def _kind(record) -> str:
    return {
        Person: "person",
        Preference: "preference",
        Fact: "fact",
        Event: "event",
        Project: "project",
        Interpretation: "interpretation",
    }[type(record)]


def _insert_record(conn: sqlite3.Connection, record) -> None:
    payload = record.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO personal_model_records
           (generation_id,record_id,record_kind,record_json,canonical_json_hash,
            dependency_hash,normalized_predicate,normalized_concept,readiness,
            sensitivity,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record.generation_id,
            record.id,
            _kind(record),
            encoded,
            hashlib.sha256(encoded.encode()).hexdigest(),
            f"dependency-{record.id}",
            getattr(record, "predicate", None),
            getattr(record, "concept_key", None),
            str(record.readiness),
            str(record.sensitivity),
            NOW.isoformat(),
        ),
    )


def _offer(
    conn: sqlite3.Connection,
    generation: str,
    question_id: str,
    interpretation_id: str,
    text: str,
) -> None:
    conn.execute(
        """INSERT INTO personal_model_offered_questions
           (generation_id,question_id,interpretation_id,display_text,normalized_text,
            offered_at,state) VALUES (?,?,?,?,?,?,'active')""",
        (generation, question_id, interpretation_id, text, " ".join(text.casefold().split()),
         NOW.isoformat()),
    )


def _manifest() -> CountersearchManifest:
    lane = {
        "query_variants": 2,
        "max_candidates": 20,
        "candidates_considered": 4,
        "retrieval_version": "1",
    }
    return CountersearchManifest(
        support=CountersearchLane(
            query_fingerprint="support-query",
            candidate_pool_hash="support-pool",
            result_hash="support-result",
            **lane,
        ),
        counter=CountersearchLane(
            query_fingerprint="counter-query",
            candidate_pool_hash="counter-pool",
            result_hash="counter-result",
            **lane,
        ),
        source_scopes=("approved",),
        started_at=NOW,
        completed_at=NOW,
        completed=True,
    )


def _records(generation: str, flower: str = "peonies"):
    owner = Person(
        **_base(generation, "person-owner"),
        canonical_key="person:owner",
        display_name="Owner",
        is_archive_owner=True,
    )
    anna = Person(
        **_base(generation, "person-anna"),
        canonical_key="person:anna",
        display_name="Anna",
    )
    preference = Preference(
        **_base(generation, "pref-flower"),
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
        predicate="preference.flower",
        object=TextFactValue(kind="text", exact_literal=flower, normalized=flower),
        valence=PreferenceValence.LIKE,
        status=FactStatus.ACTIVE,
    )
    birthday = Fact(
        **_base(generation, "fact-birthday"),
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
        predicate="person.birth_date",
        value=MonthDayFactValue(kind="month_day", exact_literal="14 May", month=5, day=14),
        status=FactStatus.ACTIVE,
    )
    project = Project(
        **_base(generation, "project-varyz"),
        canonical_key="project:varyz",
        display_name="Varyz",
        participant_person_ids=("person-anna",),
        purpose="Build a searchable life model",
    )
    interpretation = Interpretation(
        **_base(generation, "interpretation-mutual-care"),
        concept_key="mutual_care",
        thesis="Care is expressed through practical help.",
        kind=InterpretationKind.RELATIONSHIP,
        scope=InterpretationScope(
            kind=ScopeKind.RELATIONSHIP,
            relationship_person_ids=("person-owner", "person-anna"),
        ),
        evidence_assignments=(
            InterpretationEvidence(
                episode_id="episode-1",
                observation_ids=("observation-1",),
                role=EvidenceRole.SUPPORTS,
                independence_group_id="decision-1",
            ),
        ),
        alternatives=(
            AlternativeExplanation(
                thesis="It may reflect temporary circumstances.",
                distinguishing_evidence="Compare practical help across other periods.",
            ),
        ),
        missing_evidence=("More independent time periods.",),
        counterevidence_summary="No strong counterexample was found.",
        countersearch_state=CountersearchState.COMPLETE,
        countersearch_manifest=_manifest(),
        derivation_summary="A relationship-scoped pattern across stored observations.",
        candidate_id="candidate-mutual-care",
    )
    return owner, anna, preference, birthday, project, interpretation


def _activate(conn: sqlite3.Connection, generation: str) -> None:
    current = conn.execute(
        "SELECT generation_id FROM personal_model_active_generation WHERE singleton=1"
    ).fetchone()
    if current:
        conn.execute("DELETE FROM personal_model_active_generation WHERE singleton=1")
        conn.execute(
            "UPDATE personal_model_generations SET state='superseded' WHERE id=?",
            (current[0],),
        )
    conn.execute(
        "UPDATE personal_model_generations SET state='current',activated_at=? WHERE id=?",
        (NOW.isoformat(), generation),
    )
    conn.execute(
        """INSERT INTO personal_model_active_generation(singleton,generation_id,activated_at)
           VALUES (1,?,?)""",
        (generation, NOW.isoformat()),
    )
    conn.commit()


@pytest.fixture
def model_server(tmp_path: Path):
    cfg = Config(base_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    fingerprint = SQLiteEvidenceReader(conn).corpus_fingerprint()
    conn.execute(
        """INSERT INTO graph_runs
           (id,generation_key,extractor_name,extractor_version,predicate_version,
            identity_version,input_content_hash,state,entries_total,entries_processed,
            started_at,completed_at,activated_at)
           VALUES ('graph-test','test','test','1','1','1',?,'active',?,?,?,?,?)""",
        (
            fingerprint.content_hash,
            fingerprint.entries,
            fingerprint.entries,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    for generation, flower in (("model-1", "peonies"), ("model-2", "roses")):
        _insert_generation(conn, generation)
        for record in _records(generation, flower):
            _insert_record(conn, record)
        conn.execute(
            """INSERT INTO personal_model_action_handoffs
               (generation_id,candidate_id,title,node_kind,target_layer,
                observation_ids_json,claim_ids_json,source_entry_ids_json,
                project_ids_json,parent_goal_ids_json)
               VALUES (?,?,'Review typed lookup','action',
                       'observation_commitment_action','[]','[]','[]',?, '[]')""",
            (
                generation,
                f"action-{generation}",
                json.dumps(["project-varyz"]),
            ),
        )
        _offer(
            conn,
            generation,
            "offered-mutual-care",
            "interpretation-mutual-care",
            "How is mutual care expressed here?",
        )
    _activate(conn, "model-1")
    conn.close()
    server = VaryzServer()
    server._cfg = cfg
    yield server
    server.close()


@pytest.mark.parametrize("tool", ["lookup", "interpret"])
def test_a_build_without_the_compiler_does_not_read_an_old_model(
    model_server, monkeypatch, tool
):
    """The alpha leaves the compiler out; a model an older version left is not read."""
    monkeypatch.setattr("varyz.mcp_server._MODEL_SHIPPED", False)

    with pytest.raises(KeyError):
        model_server.call(tool, {"text": "What does Anna like?"})


def test_lookup_minimizes_third_party_records_without_changing_local_lookup(model_server):
    direct = LifeModelQueryService(model_server.conn).ask_text("What does Anna like?")
    via_mcp = model_server.call("lookup", {"text": "What does Anna like?"})
    assert direct.answers[0].display_value == "peonies"
    assert not via_mcp["answers"]
    assert via_mcp["withheld_records"][0]["reason"] == (
        "third_party_relationship_necessity_not_established"
    )
    assert via_mcp["withheld_records"][0]["rule_version"] == "1"
    assert via_mcp["withheld_records"][0]["predicate_or_concept"] == "preference.flower"

    birthday = model_server.call(
        "lookup",
        {
            "query": {
                "entities": [{"value": "Anna", "kind": "person"}],
                "relation": "fact",
                "predicate": "birthday",
            }
        },
    )
    assert not birthday["answers"]
    assert birthday["withheld_records"][0]["predicate_or_concept"] == "person.birth_date"
    project = model_server.call("lookup", {"text": "What projects does Anna have?"})
    assert not project["answers"]
    assert project["withheld_records"][0]["record_kind"] == "project"

    action = model_server.call(
        "lookup",
        {
            "query": {
                "entities": [{"value": "project-varyz", "kind": "project"}],
                "relation": "action_handoff",
                "limit": 1,
            }
        },
    )
    assert action["generation_id"] == "model-1"
    assert action["answers"][0]["record_kind"] == "action_handoff"
    assert action["answers"][0]["display_value"] == "Review typed lookup"


def test_lookup_does_not_turn_arbitrary_text_into_synthesis(model_server):
    result = model_server.call("lookup", {"text": "Why does Anna fear abandonment?"})
    assert result["disposition"] == "clarification"
    assert not result["answers"]


def test_a_question_lookup_cannot_answer_is_not_reported_as_an_uncompiled_archive(
    model_server,
):
    """Two different states arrived at this payload as one null generation.

    One is an archive with nothing compiled. The other is a question this tool
    does not answer -- "who do I talk to most?", "what is my blood type?" --
    which comes back as a clarification naming the relations it does answer,
    and no generation because none was consulted. Reading the second as the
    first told the owner of a compiled archive that nothing had been compiled,
    in the same session as an answer drawn from the model it said did not
    exist, and sent them off to spend hours on a `model compile` that would not
    have changed the answer, over the clarification that would have.
    """
    answered = model_server.call("lookup", {"text": "What does Anna like?"})
    assert answered["generation_id"] is not None

    payload = model_server.call("lookup", {"text": "Who do I talk to most?"})
    assert payload["disposition"] == "clarification"
    assert payload["clarification"]
    assert "error" not in payload
    assert "hint" not in payload


def test_lookup_on_a_fresh_archive_names_what_has_not_been_built(tmp_path: Path):
    """A bare `not_known` is a report about the archive, and an agent relays it
    as one -- so a new install answers a question about its owner by denying it
    knows them. Every other tool on this surface names its precondition when it
    has none: understand says there is no active graph, mirror says to generate
    one, interpret says the question was never offered.
    """
    cfg = Config(base_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    open_db(cfg.db_path).close()
    server = VaryzServer()
    server._cfg = cfg
    try:
        payload = server.call("lookup", {"text": "What am I working on?"})
    finally:
        server.close()

    assert payload["disposition"] == "not_known"
    assert payload["generation_id"] is None
    assert payload["error"] == "no_personal_model"
    assert "varyz model compile" in payload["hint"]


def test_interpret_on_an_uncompiled_archive_says_why_nothing_was_offered(tmp_path):
    """The hint names the missing model, not a list to go and look through.

    "Select a question Varyz offered in a trusted local surface" is advice for
    an archive that has questions. On a fresh install there are none and cannot
    be, and the caller was sent looking for a list that does not exist.
    """
    cfg = Config(base_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    open_db(cfg.db_path).close()
    server = VaryzServer()
    server._cfg = cfg
    try:
        payload = server.call("interpret", {"question_id": "anything-at-all"})
    finally:
        server.close()

    assert payload["error"] == "question_not_offered"
    assert payload["generation_id"] is None
    assert "varyz model compile" in payload["hint"]


def test_lookup_reports_ambiguity_and_not_known_without_guessing(model_server):
    unknown = model_server.call("lookup", {"text": "Who is Nobody?"})
    assert unknown["disposition"] == "not_known"
    assert unknown["not_known"]

    _insert_generation(model_server.conn, "model-ambiguous")
    for record in _records("model-ambiguous"):
        _insert_record(model_server.conn, record)
    _insert_record(
        model_server.conn,
        Person(
            **_base("model-ambiguous", "person-anna-other"),
            canonical_key="person:anna-other",
            display_name="Anna",
        ),
    )
    _activate(model_server.conn, "model-ambiguous")
    ambiguous = model_server.call("lookup", {"text": "Who is Anna?"})
    assert ambiguous["disposition"] == "ambiguous"
    assert {item["entity_id"] for item in ambiguous["ambiguity_choices"]} == {
        "person-anna",
        "person-anna-other",
    }


def test_interpret_requires_an_offered_active_record_and_returns_bounded_drilldown(model_server):
    rejected = model_server.call("interpret", {"question_id": "invent-a-diagnosis"})
    assert rejected["error"] == "question_not_offered"
    assert "offered_question_ids" not in rejected
    assert "How is mutual care" not in json.dumps(rejected)

    ready_but_not_offered = model_server.call(
        "interpret", {"question_id": "interpretation-mutual-care"}
    )
    assert ready_but_not_offered["error"] == "question_not_offered"

    result = model_server.call("interpret", {"question_id": "offered-mutual-care"})
    assert result["question"] == "How is mutual care expressed here?"
    assert list(result)[3:9] == [
        "answer_contract",
        "evidence",
        "patterns",
        "counterevidence",
        "missing_evidence",
        "alternatives",
    ]
    assert result["judgment"]["scope"]["kind"] == "relationship"
    assert result["provenance"]["observation_ids"] == ["observation-1"]
    assert "reasoning" not in json.dumps(result).casefold()


def test_interpret_reflects_confirmed_verdict_after_question_is_withdrawn(model_server):
    generation = "model-confirmed"
    _insert_generation(model_server.conn, generation)
    records = _records(generation)
    confidence_before = json.dumps(records[-1].confidence, separators=(",", ":")).encode()
    for record in records[:-1]:
        _insert_record(model_server.conn, record)
    _insert_record(
        model_server.conn,
        records[-1].model_copy(update={"owner_verdict": InterpretationVerdict.CONFIRM}),
    )
    _offer(
        model_server.conn,
        generation,
        "offered-mutual-care",
        "interpretation-mutual-care",
        "How is mutual care expressed here?",
    )
    model_server.conn.execute(
        """UPDATE personal_model_offered_questions SET state='withdrawn'
           WHERE generation_id=? AND question_id='offered-mutual-care'""",
        (generation,),
    )
    _activate(model_server.conn, generation)

    result = model_server.call("interpret", {"question_id": "offered-mutual-care"})
    assert result["judgment"]["owner_verdict"] == "confirm"
    assert json.dumps(
        result["judgment"]["confidence"], separators=(",", ":")
    ).encode() == confidence_before


def test_rejected_interpretation_is_neither_offered_nor_usable(model_server):
    generation = "model-rejected"
    _insert_generation(model_server.conn, generation)
    records = _records(generation)
    for record in records[:-1]:
        _insert_record(model_server.conn, record)
    _insert_record(
        model_server.conn,
        records[-1].model_copy(
            update={
                "owner_verdict": InterpretationVerdict.REJECT,
                "readiness": Readiness.REJECTED,
            }
        ),
    )
    _offer(
        model_server.conn,
        generation,
        "offered-mutual-care",
        "interpretation-mutual-care",
        "How is mutual care expressed here?",
    )
    _activate(model_server.conn, generation)

    offered = model_server.call("interpret", {"question_id": "offered-mutual-care"})
    lookup = model_server.call(
        "lookup",
        {"query": {"relation": "interpretation", "concept": "mutual_care"}},
    )
    assert offered["error"] == "question_not_offered"
    assert lookup["answers"] == []


def test_active_switch_changes_lookup_without_tool_state(model_server):
    first = model_server.call("lookup", {"text": "What does Anna like?"})
    assert first["generation_id"] == "model-1"
    assert first["withheld_records"][0]["predicate_or_concept"] == "preference.flower"
    _activate(model_server.conn, "model-2")
    switched = model_server.call("lookup", {"text": "What does Anna like?"})
    assert switched["generation_id"] == "model-2"
    assert not switched["answers"]
    # A question mapped only in the old generation can never select a new record.
    _insert_generation(model_server.conn, "model-without-offer")
    for record in _records("model-without-offer", "roses"):
        _insert_record(model_server.conn, record)
    _activate(model_server.conn, "model-without-offer")
    old_mapping = model_server.call("interpret", {"question_id": "offered-mutual-care"})
    assert old_mapping["error"] == "question_not_offered"


def test_personal_model_calls_are_select_only_and_reject_bad_or_oversized_input(model_server):
    before = model_server.conn.total_changes
    model_server.call("lookup", {"text": "What does Anna like?"})
    model_server.call("interpret", {"question_id": "offered-mutual-care"})
    assert model_server.conn.total_changes == before

    with pytest.raises(ValueError, match="not both"):
        model_server.call("lookup", {"text": "Anna", "query": {"relation": "person"}})
    with pytest.raises(ValueError, match="too large"):
        model_server.call(
            "lookup", {"text": "x" * (MAX_PERSONAL_MODEL_REQUEST_BYTES + 1)}
        )
    with pytest.raises(ValueError, match="invalid life query"):
        model_server.call("lookup", {"query": {"relation": "diagnose"}})


def test_sensitive_lookup_and_unclassified_interpretation_are_withheld(model_server):
    # The service carries the withheld signal but never gives MCP a capability.
    _insert_generation(model_server.conn, "model-private")
    for record in _records("model-private"):
        _insert_record(model_server.conn, record)
    secret = Fact(
        **_base("model-private", "fact-secret", sensitivity=Sensitivity.SENSITIVE),
        subject=SubjectReference(kind=EntityKind.PERSON, id="person-anna"),
        predicate="person.constraint",
        value=TextFactValue(kind="text", exact_literal="private detail", normalized="private"),
    )
    hidden = _records("model-private")[-1].model_copy(
        update={"id": "interpretation-hidden", "sensitivity": Sensitivity.UNCLASSIFIED}
    )
    sensitive = _records("model-private")[-1].model_copy(
        update={
            "id": "interpretation-sensitive",
            "sensitivity": Sensitivity.SENSITIVE,
            "thesis": "SECRET-THESIS-917",
            "derivation_summary": "SECRET-DERIVATION-917",
            "counterevidence_summary": "SECRET-COUNTER-917",
            "strongest_counterexample": "SECRET-EXACT-QUOTE-917",
            "missing_evidence": ("SECRET-MISSING-917",),
            "alternatives": (
                AlternativeExplanation(
                    thesis="SECRET-ALTERNATIVE-917",
                    distinguishing_evidence="SECRET-DISTINGUISHER-917",
                    evidence_ids=("SECRET-ALTERNATIVE-EVIDENCE-ID-917",),
                ),
            ),
            "scope": InterpretationScope(
                kind=ScopeKind.RELATIONSHIP,
                relationship_person_ids=(
                    "person-owner",
                    "person-anna",
                ),
            ),
        }
    )
    _insert_record(model_server.conn, secret)
    _insert_record(model_server.conn, hidden)
    _insert_record(model_server.conn, sensitive)
    _offer(
        model_server.conn,
        "model-private",
        "offered-hidden",
        "interpretation-hidden",
        "Hidden question?",
    )
    _offer(
        model_server.conn,
        "model-private",
        "offered-sensitive",
        "interpretation-sensitive",
        "SECRET-QUESTION-917?",
    )
    _activate(model_server.conn, "model-private")

    lookup = model_server.call(
        "lookup",
        {
            "query": {
                "entities": [{"value": "Anna"}],
                "relation": "fact",
                "predicate": "person.constraint",
            }
        },
    )
    assert lookup["not_known"]
    assert lookup["sensitive_records_withheld"]
    refused = model_server.call("interpret", {"question_id": "offered-hidden"})
    assert refused["error"] == "question_not_offered"
    allowed = model_server.call("interpret", {"question_id": "offered-sensitive"})
    assert allowed["judgment"]["text"]
    assert allowed["judgment"]["exact_quote_wording"] == "withheld"
    assert allowed["evidence"] == []
    assert allowed["alternatives"] == []
    assert allowed["judgment"]["scope"] is None
    assert allowed["judgment"]["scope_withheld"]
    assert allowed["provenance"]["record_id"] is None
    assert allowed["provenance"]["observation_ids"] == []
    assert "SECRET-" not in json.dumps(allowed)


def _grant(
    model_server,
    grant_id: str,
    *,
    request_id: str | None,
    resource_id: str,
) -> None:
    ReceiptStore(model_server.conn).create_grant(
        ConsentGrant(
            id=grant_id,
            actor="local_user",
            destination="cloud:claude",
            purpose="answer_question",
            request_id=request_id,
            resource_type="summary",
            resource_id=resource_id,
        )
    )


def test_lookup_requires_one_time_permission_for_relationship_necessary_category(
    model_server,
):
    generation = "model-medical-event"
    _insert_generation(model_server.conn, generation)
    for record in _records(generation):
        _insert_record(model_server.conn, record)
    event = Event(
        **_base(generation, "event-therapy"),
        canonical_key="event:therapy",
        title="Anna discussed a therapy session with the owner",
        event_kind="meeting",
        participant_ids=("person-owner", "person-anna"),
        temporal=TemporalScope(observed_at=NOW, time_precision=TimePrecision.EXACT),
    )
    _insert_record(model_server.conn, event)
    _activate(model_server.conn, generation)

    query = {
        "query": {
            "entities": [{"value": "Anna", "kind": "person"}],
            "relation": "event",
        },
        "request_id": "lookup-medical-request",
    }
    blocked = model_server.call("lookup", query)
    assert not blocked["answers"]
    marker = blocked["withheld_records"][0]
    assert marker["record_kind"] == "event"
    assert marker["categories"] == ["medical"]
    assert marker["reason"] == "third_party_sensitive_requires_one_time_permission"
    assert "confidential_category_requires_confirmation" in marker["policy_reason_codes"]

    _grant(
        model_server,
        "standing-medical-grant",
        request_id=None,
        resource_id="event-therapy",
    )
    standing = model_server.call(
        "lookup",
        {
            **query,
            "request_id": "lookup-standing-request",
            "consent_grant_id": "standing-medical-grant",
        },
    )
    assert not standing["answers"]
    assert "consent_requires_one_time" in standing["withheld_records"][0][
        "policy_reason_codes"
    ]

    _grant(
        model_server,
        "one-time-medical-grant",
        request_id="lookup-medical-request",
        resource_id="event-therapy",
    )
    allowed = model_server.call(
        "lookup", {**query, "consent_grant_id": "one-time-medical-grant"}
    )
    assert allowed["answers"][0]["display_value"] == event.title
    assert not allowed["withheld_records"]


def test_interpret_requires_one_time_permission_for_third_party_category(model_server):
    generation = "model-medical-interpretation"
    _insert_generation(model_server.conn, generation)
    records = _records(generation)
    for record in records:
        _insert_record(model_server.conn, record)
    source = records[-1]
    medical = source.model_copy(
        update={
            "id": "interpretation-therapy",
            "thesis": "Anna's therapy session changed how practical care was offered.",
        }
    )
    _insert_record(model_server.conn, medical)
    _offer(
        model_server.conn,
        generation,
        "offered-therapy",
        medical.id,
        "How did care change around Anna's therapy session?",
    )
    _activate(model_server.conn, generation)
    arguments = {
        "question_id": "offered-therapy",
        "request_id": "interpret-medical-request",
    }
    blocked = model_server.call("interpret", arguments)
    assert blocked["error"] == "withheld"
    assert blocked["withheld_records"][0]["categories"] == ["medical"]
    assert blocked["withheld_records"][0]["reason"] == (
        "third_party_sensitive_requires_one_time_permission"
    )

    _grant(
        model_server,
        "one-time-interpret-grant",
        request_id="interpret-medical-request",
        resource_id=medical.id,
    )
    allowed = model_server.call(
        "interpret", {**arguments, "consent_grant_id": "one-time-interpret-grant"}
    )
    assert allowed["judgment"]["text"] == medical.thesis
    assert not allowed["withheld_records"]


def test_open_ended_third_party_profile_requests_return_reasons_not_a_dossier(
    model_server,
):
    facts = model_server.call(
        "lookup",
        {
            "query": {
                "entities": [{"value": "Anna", "kind": "person"}],
                "relation": "fact",
            }
        },
    )
    assert facts["not_known"]
    assert not facts["answers"]
    assert facts["withheld_records"]
    assert {item["reason"] for item in facts["withheld_records"]} == {
        "third_party_relationship_necessity_not_established"
    }

    person = model_server.call("lookup", {"text": "Who is Anna?"})
    assert person["not_known"]
    assert not person["answers"]
    assert person["withheld_records"][0]["record_kind"] == "person"


def test_cli_interpret_reads_an_offered_question_without_the_agent_boundary(
    model_server, monkeypatch
):
    """The CLI is the owner's terminal, not a destination.

    interpret reaches into the stored-interpretation path, which requires an
    egress gate because the MCP surface hands records to an agent. The CLI
    passed none and died on a TypeError before reading anything; passing the
    agent's own gate instead would minimize the owner's records against the
    owner.
    """
    import varyz.cli as cli

    monkeypatch.setattr(cli, "_cfg", lambda _path: model_server.cfg)

    result = CliRunner().invoke(app, ["model", "interpret", "offered-mutual-care"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "question_not_offered" not in result.output
    assert "offered-mutual-care" in result.output
    assert "model-1" in result.output


def test_cli_lookup_status_and_compile_dry_run_share_the_active_model(
    model_server, monkeypatch
):
    import varyz.cli as cli

    monkeypatch.setattr(cli, "_cfg", lambda _path: model_server.cfg)
    monkeypatch.setattr(cli, "_db", lambda _cfg: model_server.conn)
    monkeypatch.setattr("varyz.llm.local.build_local_provider", _offline_local_provider)
    runner = CliRunner()

    lookup = runner.invoke(app, ["model", "lookup", "What does Anna like?"])
    assert lookup.exit_code == 0, lookup.output
    assert "peonies" in lookup.output
    assert "model-1" in lookup.output

    status = runner.invoke(app, ["model", "status"])
    assert status.exit_code == 0, status.output
    assert "active generation: model-1" in status.output

    generations_before = model_server.conn.execute(
        "SELECT COUNT(*) FROM personal_model_generations"
    ).fetchone()[0]
    compile_plan = runner.invoke(app, ["model", "compile", "--dry-run"])
    assert compile_plan.exit_code == 0, compile_plan.output
    assert "Dry run" in compile_plan.output
    assert model_server.conn.execute(
        "SELECT COUNT(*) FROM personal_model_generations"
    ).fetchone()[0] == generations_before


def test_cli_correction_is_a_strict_append_only_repository_write(model_server, monkeypatch):
    import varyz.cli as cli

    monkeypatch.setattr(cli, "_cfg", lambda _path: model_server.cfg)
    monkeypatch.setattr(cli, "_db", lambda _cfg: model_server.conn)
    monkeypatch.setattr("varyz.llm.local.build_local_provider", _offline_local_provider)
    payload = json.dumps(
        {"value": {"kind": "month_day", "exact_literal": "15 May", "month": 5, "day": 15}}
    )
    result = CliRunner().invoke(
        app,
        [
            "model",
            "correct",
            "fact",
            "fact-birthday",
            "edit",
            "--payload",
            payload,
        ],
    )
    # The fixture has no attributable source entries, so compilation honestly
    # fails its owner/readiness gates; the ledger append still survives and the
    # prior active snapshot remains live.
    assert result.exit_code == 1, result.output
    assert "previous active model remains live" in result.output
    row = model_server.conn.execute(
        """SELECT target_kind,target_record_id,action,payload_json,actor
             FROM personal_model_overrides ORDER BY sequence DESC LIMIT 1"""
    ).fetchone()
    assert tuple(row)[:3] == ("fact", "fact-birthday", "edit")
    assert json.loads(row["payload_json"])["value"]["day"] == 15
    assert row["actor"] == "owner"
    # Appending a correction never edits or replaces the active snapshot in place.
    active_id = model_server.conn.execute(
        "SELECT id FROM personal_model_generations WHERE state='current'"
    ).fetchone()[0]
    assert active_id == "model-1"
    unchanged = model_server.conn.execute(
        """SELECT record_json FROM personal_model_records
             WHERE generation_id=? AND record_id='fact-birthday'""",
        (active_id,),
    ).fetchone()[0]
    assert json.loads(unchanged)["value"]["day"] == 14

    malformed = CliRunner().invoke(
        app,
        ["model", "correct", "fact", "fact-birthday", "edit", "--payload", "[]"],
    )
    assert malformed.exit_code == 2
    assert model_server.conn.execute(
        "SELECT COUNT(*) FROM personal_model_overrides"
    ).fetchone()[0] == 1
def test_a_malformed_personal_model_call_says_which_argument_is_wrong(model_server):
    """The refusal is the whole answer an agent gets, so it has to be the fix.

    lookup states its either/or in oneOf, which a caller reading only the
    schema's `required` list never sees, so the message has to carry it.
    interpret answered a call with no arguments at all by saying it "requires
    only question_id" -- a sentence about what it will not take, to a caller
    that gave it nothing -- and named no key when one was wrong.
    """
    with pytest.raises(ValueError, match="lookup requires text or query"):
        model_server.call("lookup", {})
    with pytest.raises(ValueError, match="not both"):
        model_server.call("lookup", {"text": "anything", "query": {"relation": "fact"}})
    with pytest.raises(ValueError, match="lookup does not take limit"):
        model_server.call("lookup", {"text": "anything", "limit": 3})
    with pytest.raises(ValueError, match="question_id is required"):
        model_server.call("interpret", {})
    with pytest.raises(ValueError, match="interpret does not take question"):
        model_server.call("interpret", {"question_id": "q", "question": "why"})


class _NoManagedModel:
    """A build that never installed the managed model."""

    model = "never-installed"

    def available(self) -> bool:
        return False

    def complete(self, _system: str, _user: str) -> str:
        raise AssertionError("a build without the managed model must not call it")


def test_a_build_without_the_model_installer_names_a_compile_it_can_run(
    tmp_path: Path, monkeypatch
):
    """The alpha never installs the managed model, and a semantic compile
    needs it, so `varyz model compile --run` fails there. The hints name the
    literal-only compile, which runs.
    """
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)
    cfg = Config(base_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    open_db(cfg.db_path).close()
    server = VaryzServer()
    server._cfg = cfg
    try:
        lookup = server.call("lookup", {"text": "What am I working on?"})
        interpret = server.call("interpret", {"question_id": "anything-at-all"})
    finally:
        server.close()

    assert "varyz model compile --literal-only --run" in lookup["hint"]
    assert "varyz model compile --literal-only --run" in interpret["hint"]


def test_a_correction_without_the_model_installer_recompiles_the_explicit_claims(
    model_server, monkeypatch
):
    """Every correction recompiled with the managed model, so in a build that
    never installs it every `varyz model correct` was refused. It recompiles
    the explicit claims instead, the one compile that build can run.
    """
    import varyz.cli as cli

    monkeypatch.setattr(cli, "_cfg", lambda _path: model_server.cfg)
    monkeypatch.setattr(cli, "_db", lambda _cfg: model_server.conn)
    monkeypatch.setattr("varyz.llm.local.build_local_provider", lambda *_a, **_k: _NoManagedModel())
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)
    payload = json.dumps(
        {"value": {"kind": "month_day", "exact_literal": "15 May", "month": 5, "day": 15}}
    )
    result = CliRunner().invoke(
        app, ["model", "correct", "fact", "fact-birthday", "edit", "--payload", payload]
    )

    assert "could not recompile the semantic model" not in result.output
    assert "Correction recorded" in result.output
    # The same honest readiness failure as with the model: the fixture has no
    # attributable source entries, so the previous snapshot stays live.
    assert "previous active model remains live" in result.output
    row = model_server.conn.execute(
        "SELECT target_record_id,action FROM personal_model_overrides"
    ).fetchone()
    assert tuple(row) == ("fact-birthday", "edit")


def test_a_semantic_compile_without_the_model_installer_names_literal_only(
    model_server, monkeypatch
):
    import varyz.cli as cli

    monkeypatch.setattr(cli, "_cfg", lambda _path: model_server.cfg)
    monkeypatch.setattr(cli, "_db", lambda _cfg: model_server.conn)
    monkeypatch.setattr("varyz.llm.local.build_local_provider", lambda *_a, **_k: _NoManagedModel())
    monkeypatch.setattr("varyz.llm.local_runtime.installer_is_shipped", lambda: False)

    result = CliRunner().invoke(app, ["model", "compile", "--dry-run"])

    assert result.exit_code == 1, result.output
    assert "Start the configured local model" not in result.output
    assert "does not install the managed model" in result.output
    assert "--literal-only" in result.output

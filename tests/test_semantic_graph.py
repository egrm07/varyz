"""Context-aware semantic facet extraction and privacy boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from tests.conftest import FakeLLM
from varyz.config import Config
from varyz.db import dao
from varyz.destinations.llm import LLMDestinationAdapter
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.graph.repository import GraphRepository
from varyz.graph.semantic import (
    PROMPT_VERSION,
    ContextualFacetExtractor,
    has_semantic_cue,
)
from varyz.graph.service import facet_max_tokens, rebuild_graph
from varyz.llm.refusal import ReplyTruncated
from varyz.privacy.broker import EgressBroker
from varyz.privacy.policy import PolicyEngine
from varyz.privacy.receipts import ReceiptStore


def test_default_semantic_extractor_is_managed_local_gemma():
    cfg = Config()

    assert cfg.graph.extractor == "contextual_facets"
    assert cfg.graph.semantic_provider == "local"
    # Bumped to "9" when a working session's own statements started being
    # kept, and the owner's commits started being read. Both change what
    # extraction produces and so have to change the cache scope with it,
    # or a rebuild reuses answers taken under the old reading.
    assert cfg.graph.extractor_version == "9"
    assert cfg.local_model.backend == "managed"
    assert cfg.local_model.model == "gemma4:12b-it-q4_K_M"


def _message(
    conn,
    external_id: str,
    text: str,
    *,
    sender: str,
    when: datetime,
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
):
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=external_id,
                body=f"From: {sender}\nChat: Test\n\n{text}",
                occurred_start=when,
                thread_id="wa:test",
            ),
            source="whatsapp",
            sensitivity=sensitivity,
        )
    return dao.get_entry(conn, entry_id)


def _extractor(conn, provider, *, min_confidence: float = 0.80):
    broker = EgressBroker(
        ReceiptStore(conn),
        policy=PolicyEngine(),
        adapters=(LLMDestinationAdapter("cloud:fake", provider, stream=False),),
    )
    return ContextualFacetExtractor(
        conn,
        broker,
        destination="cloud:fake",
        model="fixture",
        min_confidence=min_confidence,
    )


def test_context_resolves_short_affirmation_with_two_evidence_links(conn):
    now = datetime(2026, 1, 2, 10, tzinfo=UTC)
    context = _message(
        conn,
        "context",
        "Privacy should matter more than convenience, don't you think?",
        sender="Anna",
        when=now,
    )
    target = _message(
        conn,
        "target",
        "I think so.",
        sender="me",
        when=now + timedelta(minutes=1),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "belief",
                    "statement": "Privacy should matter more than convenience.",
                    "evidence_quote": "I think so.",
                    "supporting_context_ids": [context.id],
                    "confidence": 0.94,
                }
            ]
        }
    )
    provider = FakeLLM(reply)

    batch = _extractor(conn, provider).extract(target)

    assert len(batch.claims) == 1
    claim = batch.claims[0]
    assert claim.predicate == "believed"
    assert claim.object_surface == "Privacy should matter more than convenience."
    assert {evidence.entry_id for evidence in claim.evidence} == {
        context.id,
        target.id,
    }
    assert provider.calls
    assert "Privacy should matter" in provider.calls[0][1]

    repository = GraphRepository(conn)
    corpus_hash = hashlib.sha256(b"semantic-fixture").hexdigest()
    run, _ = repository.begin_run(
        generation_key=corpus_hash,
        extractor_name="contextual_facets",
        extractor_version="3",
        model="fixture",
        prompt_version="fixture",
        input_content_hash=corpus_hash,
        entries_total=1,
    )
    repository.stage_batch(run.id, target, batch)
    stored = conn.execute(
        """SELECT ce.entry_id, ce.quote
           FROM claim_evidence ce JOIN claims c ON c.id=ce.claim_id
           WHERE c.graph_generation_id=? ORDER BY ce.entry_id""",
        (run.id,),
    ).fetchall()
    assert {row["entry_id"] for row in stored} == {context.id, target.id}


class _CutOnce:
    """A model whose first reply runs out of room and whose second does not."""

    def __init__(self, reply: str, *, cuts: int = 1):
        self.reply = reply
        self.cuts = cuts
        self.calls: list[tuple[str, str]] = []

    def stream(self, system: str, user: str):
        self.calls.append((system, user))
        if len(self.calls) <= self.cuts:
            raise ReplyTruncated(produced=2600, cap=2600, prompt=5724)
        yield self.reply

    def complete(self, system: str, user: str) -> str:
        return "".join(self.stream(system, user))


def _cut_reply_setup(conn):
    now = datetime(2026, 1, 4, 10, tzinfo=UTC)
    context = _message(
        conn,
        "cut-context",
        "Privacy should matter more than convenience, don't you think?",
        sender="Anna",
        when=now,
    )
    target = _message(
        conn,
        "cut-target",
        "I think so.",
        sender="me",
        when=now + timedelta(minutes=1),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "belief",
                    "statement": "Privacy should matter more than convenience.",
                    "evidence_quote": "I think so.",
                    "supporting_context_ids": [context.id],
                    "confidence": 0.94,
                }
            ]
        }
    )
    return target, reply


def _batched_extractor(conn, provider, *, batch_max_targets: int):
    broker = EgressBroker(
        ReceiptStore(conn),
        policy=PolicyEngine(),
        adapters=(LLMDestinationAdapter("cloud:fake", provider, stream=False),),
    )
    return ContextualFacetExtractor(
        conn,
        broker,
        destination="cloud:fake",
        model="fixture",
        batch_max_targets=batch_max_targets,
    )


def test_a_reply_cut_short_asks_for_half_the_batch_rather_than_dropping_it(conn):
    """A reply that ran out of room is not a reply that will not parse.

    The whole batch used to be written off together and cached as an error, so
    one cut answer cost every target in the window its semantic reading.
    """
    target, reply = _cut_reply_setup(conn)
    provider = _CutOnce(reply)

    batch = _batched_extractor(conn, provider, batch_max_targets=8).extract(target)

    assert len(provider.calls) == 2, "the batch was written off instead of halved"
    assert [claim.object_surface for claim in batch.claims] == [
        "Privacy should matter more than convenience."
    ]


def test_halving_stops_at_one_target_rather_than_asking_forever(conn):
    """One target is as small as a batch gets; below that there is nothing to try."""
    target, reply = _cut_reply_setup(conn)
    provider = _CutOnce(reply, cuts=99)

    batch = _batched_extractor(conn, provider, batch_max_targets=4).extract(target)

    # 4 -> 2 -> 1, and then the batch is quarantined rather than asked again.
    assert len(provider.calls) == 3
    assert not [claim for claim in batch.claims if claim.predicate == "believed"]


def test_explicit_contextual_endorsement_has_high_precision_recovery(conn):
    now = datetime(2026, 1, 3, 10, tzinfo=UTC)
    context = _message(
        conn,
        "endorsement-context",
        "Children deserve privacy from their parents as they get older. Do you agree?",
        sender="Anna",
        when=now,
    )
    target = _message(
        conn,
        "endorsement-target",
        "I agree.",
        sender="me",
        when=now + timedelta(minutes=1),
    )

    batch = _extractor(conn, FakeLLM('{"facets":[]}')).extract(target)

    assert len(batch.claims) == 1
    assert batch.claims[0].predicate == "believed"
    assert "Children deserve privacy" in batch.claims[0].object_surface
    assert {evidence.entry_id for evidence in batch.claims[0].evidence} == {
        context.id,
        target.id,
    }


def test_transient_contextual_guess_is_not_recovered_as_belief(conn):
    now = datetime(2026, 1, 4, 10, tzinfo=UTC)
    _message(
        conn,
        "transient-context",
        "Do you think this train is running ten minutes late?",
        sender="Anna",
        when=now,
    )
    target = _message(
        conn,
        "transient-target",
        "I think so.",
        sender="me",
        when=now + timedelta(minutes=1),
    )

    batch = _extractor(conn, FakeLLM('{"facets":[]}')).extract(target)

    assert batch.claims == ()


def test_consequential_request_has_high_precision_recovery(conn):
    target = _message(
        conn,
        "private-request",
        "Would you keep my pregnancy private until I speak to the team?",
        sender="me",
        when=datetime(2026, 1, 5, 10, tzinfo=UTC),
    )

    batch = _extractor(conn, FakeLLM('{"facets":[]}')).extract(target)

    assert len(batch.claims) == 1
    assert batch.claims[0].predicate == "requested"


def test_routine_request_is_not_recovered(conn):
    target = _message(
        conn,
        "routine-request",
        "Could you close the window?",
        sender="me",
        when=datetime(2026, 1, 6, 10, tzinfo=UTC),
    )

    batch = _extractor(conn, FakeLLM('{"facets":[]}')).extract(target)

    assert batch.claims == ()


def test_model_can_reclassify_think_as_plan_instead_of_belief(conn):
    target = _message(
        conn,
        "plan",
        "I think I'll apply for the program next month.",
        sender="me",
        when=datetime(2026, 2, 1, 9, tzinfo=UTC),
    )
    provider = FakeLLM(
        json.dumps(
            {
                "facets": [
                    {
                        "target_id": target.id,
                        "kind": "plan",
                        "statement": "Apply for the program next month.",
                        "evidence_quote": "I think I'll apply for the program next month.",
                        "supporting_context_ids": [],
                        "confidence": 0.91,
                    }
                ]
            }
        )
    )

    claim = _extractor(conn, provider).extract(target).claims[0]

    assert claim.predicate == "planned_to"
    assert str(claim.modality) == "plan"
    assert claim.object_surface != "I'll apply for the program next month"


def test_empty_semantic_result_drops_regex_false_positive(conn):
    target = _message(
        conn,
        "ui-feedback",
        "I think the settings box is too tall.",
        sender="me",
        when=datetime(2026, 2, 2, 9, tzinfo=UTC),
    )
    provider = FakeLLM('{"facets":[]}')

    batch = _extractor(conn, provider).extract(target)

    assert batch.claims == ()
    assert provider.calls


def test_sensitive_target_never_reaches_cloud_and_uses_local_fallback(conn):
    target = _message(
        conn,
        "sensitive",
        "I believe privacy should always come first.",
        sender="me",
        when=datetime(2026, 2, 3, 9, tzinfo=UTC),
        sensitivity=Sensitivity.SENSITIVE,
    )
    provider = FakeLLM('{"facets":[]}')

    batch = _extractor(conn, provider).extract(target)

    assert provider.calls == []
    assert len(batch.claims) == 1
    assert batch.claims[0].predicate == "believed"
    assert batch.claims[0].sensitivity == Sensitivity.SENSITIVE


def test_sensitive_neighbor_is_withheld_from_cloud_context(conn):
    now = datetime(2026, 2, 4, 9, tzinfo=UTC)
    _message(
        conn,
        "sensitive-context",
        "therapy secret sentinel",
        sender="Anna",
        when=now,
        sensitivity=Sensitivity.SENSITIVE,
    )
    target = _message(
        conn,
        "personal-target",
        "I think friendship matters.",
        sender="me",
        when=now + timedelta(minutes=1),
    )
    provider = FakeLLM('{"facets":[]}')

    _extractor(conn, provider).extract(target)

    assert len(provider.calls) == 1
    assert "friendship matters" in provider.calls[0][1]
    assert "therapy secret sentinel" not in provider.calls[0][1]


def test_oversized_target_stays_local(conn):
    target = _message(
        conn,
        "oversized",
        "I believe privacy matters. " + ("context " * 2000),
        sender="me",
        when=datetime(2026, 2, 4, 10, tzinfo=UTC),
    )
    provider = FakeLLM('{"facets":[]}')
    extractor = _extractor(conn, provider)
    extractor.batch_max_chars = 1000

    batch = extractor.extract(target)

    assert provider.calls == []
    assert batch.claims[0].predicate == "believed"


def test_context_window_stops_at_configured_time_gap(conn):
    now = datetime(2026, 2, 5, 9, tzinfo=UTC)
    _message(
        conn,
        "stale-context",
        "stale antecedent sentinel",
        sender="Anna",
        when=now - timedelta(hours=13),
    )
    target = _message(
        conn,
        "fresh-target",
        "I think this matters.",
        sender="me",
        when=now,
    )
    provider = FakeLLM('{"facets":[]}')

    _extractor(conn, provider).extract(target)

    assert "stale antecedent sentinel" not in provider.calls[0][1]


def test_same_session_targets_share_one_model_call(conn):
    now = datetime(2026, 2, 6, 9, tzinfo=UTC)
    first = _message(
        conn,
        "batched-first",
        "I believe privacy matters.",
        sender="me",
        when=now,
    )
    second = _message(
        conn,
        "batched-second",
        "I plan to study medicine next year.",
        sender="me",
        when=now + timedelta(minutes=1),
    )
    provider = FakeLLM(
        json.dumps(
            {
                "facets": [
                    {
                        "target_id": first.id,
                        "kind": "belief",
                        "statement": "Privacy matters.",
                        "evidence_quote": "I believe privacy matters.",
                        "supporting_context_ids": [],
                        "confidence": 0.95,
                    },
                    {
                        "target_id": second.id,
                        "kind": "plan",
                        "statement": "Study medicine next year.",
                        "evidence_quote": "I plan to study medicine next year.",
                        "supporting_context_ids": [],
                        "confidence": 0.95,
                    },
                ]
            }
        )
    )
    extractor = _extractor(conn, provider)

    assert extractor.extract(first).claims[0].predicate == "believed"
    assert extractor.extract(second).claims[0].predicate == "planned_to"
    assert len(provider.calls) == 1
    # And nothing is left holding the window's records. Each cached answer has
    # exactly one reader; kept past it, the map grew for the length of a run
    # and every batch's `list[Entry]` of context stayed live inside it.
    assert extractor._facet_cache == {}


def test_one_malformed_facet_does_not_destroy_its_batch(conn, caplog):
    target = _message(
        conn,
        "partly-valid-batch",
        "I believe privacy matters, and I plan to volunteer at the library.",
        sender="me",
        when=datetime(2026, 2, 6, 10, tzinfo=UTC),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "belief",
                    "scope": "life",
                    "statement": "I believe privacy matters.",
                    "evidence_quote": "I believe privacy matters",
                    "confidence": 0.95,
                },
                {
                    "target_id": target.id,
                    "kind": "work",
                    "scope": "work",
                    "statement": "This malformed facet is discarded.",
                    "evidence_quote": "I plan to volunteer at the library",
                    "confidence": 0.90,
                },
                {
                    "target_id": target.id,
                    "kind": "plan",
                    "scope": "life",
                    "statement": "I plan to volunteer at the library.",
                    "evidence_quote": "I plan to volunteer at the library",
                    "confidence": 0.94,
                },
            ]
        }
    )

    batch = _extractor(conn, FakeLLM(reply)).extract(target)

    assert [claim.predicate for claim in batch.claims] == ["believed", "planned_to"]
    assert batch.quarantined == ()
    warnings = [message for message in caplog.messages if "dropped" in message]
    assert len(warnings) == 1
    assert "dropped 1 of 3 facets" in warnings[0]
    assert "Input should be" in warnings[0]


def test_a_batch_with_only_malformed_facets_is_empty_but_not_quarantined(conn):
    target = _message(
        conn,
        "wholly-invalid-batch",
        "I plan to swim tomorrow.",
        sender="me",
        when=datetime(2026, 2, 6, 11, tzinfo=UTC),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "work",
                    "scope": "work",
                    "statement": "I plan to swim tomorrow.",
                    "evidence_quote": "I plan to swim tomorrow",
                    "confidence": 0.90,
                },
                {
                    "target_id": target.id,
                    "kind": "plan",
                    "scope": "life",
                    "statement": "I plan to swim tomorrow.",
                    "evidence_quote": "I plan to swim tomorrow",
                    "confidence": 1.5,
                },
            ]
        }
    )

    batch = _extractor(conn, FakeLLM(reply)).extract(target)

    assert batch.claims == ()
    assert batch.quarantined == ()


def test_unparseable_outer_json_still_records_the_batch_error(conn):
    target = _message(
        conn,
        "unparseable-batch",
        "I plan to swim tomorrow.",
        sender="me",
        when=datetime(2026, 2, 6, 12, tzinfo=UTC),
    )

    batch = _extractor(conn, FakeLLM("not json")).extract(target)

    assert batch.quarantined == (
        {
            "predicate": "semantic_facet",
            "reason_code": "invalid_model_output",
            "entry_id": target.id,
            "error": "ValueError: model reply contained no JSON object",
        },
    )


def test_confidence_gate_rejects_ambiguous_model_guess(conn):
    target = _message(
        conn,
        "low-confidence",
        "I think so.",
        sender="me",
        when=datetime(2026, 2, 7, 9, tzinfo=UTC),
    )
    provider = FakeLLM(
        json.dumps(
            {
                "facets": [
                    {
                        "target_id": target.id,
                        "kind": "belief",
                        "statement": "The unclear proposition is true.",
                        "evidence_quote": "I think so.",
                        "supporting_context_ids": [],
                        "confidence": 0.79,
                    }
                ]
            }
        )
    )

    assert _extractor(conn, provider).extract(target).claims == ()


def test_router_covers_implicit_and_multilingual_semantic_cues(conn):
    when = datetime(2026, 2, 8, 9, tzinfo=UTC)
    entries = [
        _message(conn, "implicit", "I was heartbroken.", sender="me", when=when),
        _message(
            conn,
            "spanish",
            "Creo que la libertad importa.",
            sender="me",
            when=when + timedelta(minutes=1),
        ),
        _message(
            conn,
            "russian",
            "Я считаю, что честность важнее.",
            sender="me",
            when=when + timedelta(minutes=2),
        ),
    ]

    assert all(has_semantic_cue(entry) for entry in entries)


def test_configured_contextual_service_builds_with_injected_provider(conn, cfg):
    _message(
        conn,
        "service",
        "I think the button is too tall.",
        sender="me",
        when=datetime(2026, 2, 9, 9, tzinfo=UTC),
    )
    cfg.graph.extractor = "contextual_facets"
    cfg.graph.extractor_version = "3"
    provider = FakeLLM('{"facets":[]}')

    result = rebuild_graph(conn, cfg, provider=provider)

    assert result.claims == 0
    assert provider.calls
    active = GraphRepository(conn).active_run()
    assert active is not None
    assert active.extractor_name == "contextual_facets"
    # The run has to record the prompt that actually ran, or a cache keyed on
    # the version silently serves answers from a prompt that no longer exists.
    assert active.prompt_version == PROMPT_VERSION


def test_configured_local_service_uses_no_cloud_destination(conn, cfg):
    entry = _message(
        conn,
        "local-service",
        "I think honesty matters more than convenience.",
        sender="me",
        when=datetime(2026, 2, 10, 9, tzinfo=UTC),
    )
    cfg.graph.extractor = "contextual_facets"
    cfg.graph.semantic_provider = "local"
    provider = FakeLLM(
        json.dumps(
            {
                "facets": [
                    {
                        "target_id": entry.id,
                        "kind": "belief",
                        "statement": "Honesty matters more than convenience.",
                        "evidence_quote": (
                            "I think honesty matters more than convenience."
                        ),
                        "supporting_context_ids": [],
                        "confidence": 0.95,
                    }
                ]
            }
        )
    )

    result = rebuild_graph(conn, cfg, provider=provider)

    assert result.claims == 1
    destinations = {
        row["destination"]
        for row in conn.execute("SELECT destination FROM context_requests").fetchall()
    }
    assert destinations == {"local"}


def test_local_service_extracts_from_sensitive_entries(conn, cfg):
    """Messages are sensitive by default, so they are the whole personal corpus.

    In local mode no facet leaves the machine, so a sensitive entry must reach
    the model rather than degrade to the deterministic fallback.
    """
    entry = _message(
        conn,
        "local-sensitive",
        "I think honesty matters more than convenience.",
        sender="me",
        when=datetime(2026, 2, 10, 9, tzinfo=UTC),
        sensitivity=Sensitivity.SENSITIVE,
    )
    cfg.graph.extractor = "contextual_facets"
    cfg.graph.semantic_provider = "local"
    provider = FakeLLM(
        json.dumps(
            {
                "facets": [
                    {
                        "target_id": entry.id,
                        "kind": "belief",
                        "statement": "Honesty matters more than convenience.",
                        "evidence_quote": (
                            "I think honesty matters more than convenience."
                        ),
                        "supporting_context_ids": [],
                        "confidence": 0.95,
                    }
                ]
            }
        )
    )

    result = rebuild_graph(conn, cfg, provider=provider)

    assert result.claims == 1
    destinations = {
        row["destination"]
        for row in conn.execute("SELECT destination FROM context_requests").fetchall()
    }
    assert destinations == {"local"}


def test_facet_ceiling_scales_with_batch_size():
    """A 20-target batch needs room for 20 facets, or it truncates mid-list."""
    assert facet_max_tokens(1) == 1200
    assert facet_max_tokens(20) == 2600
    assert facet_max_tokens(20) > facet_max_tokens(5)
    # Bounded so prompt plus output stays inside the 8192-token context.
    assert facet_max_tokens(1000) == 4096


def _document(conn, external_id: str, body: str, *, source: str = "documents"):
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=external_id,
                body=body,
                captured_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
            ),
            source=source,
            sensitivity=Sensitivity.PERSONAL,
        )
    return dao.get_entry(conn, entry_id)


def test_a_document_is_never_read_as_first_person_testimony(conn, cfg):
    """A saved article's prose is not the reader's own belief.

    Documents and commit messages carry no From line, so `_headers` finds no
    sender and every facet the model returned was attributed to `self`. They
    are also the most expensive thing in the model path: on the live archive
    243 of 3,301 routed entries but 45% of the prompt text, each one alone in
    its own call because a document has no thread to batch with.
    """
    _document(conn, "essay", "The author decided to leave the industry entirely.")
    cfg.graph.extractor = "contextual_facets"
    provider = FakeLLM('{"facets":[]}')

    rebuild_graph(conn, cfg, version="1", provider=provider)

    assert provider.calls == []
    assert not conn.execute("SELECT 1 FROM context_requests").fetchall()


def test_a_document_keeps_the_deterministic_reading_it_licenses(conn, cfg):
    """Not routing it to a model is not the same as not reading it."""
    _document(conn, "notes", "I decided to learn Portuguese.")
    cfg.graph.extractor = "contextual_facets"
    provider = FakeLLM('{"facets":[]}')

    result = rebuild_graph(conn, cfg, version="1", provider=provider)

    assert provider.calls == []
    predicates = {
        row["predicate"]
        for row in conn.execute("SELECT predicate FROM claims").fetchall()
    }
    assert result.claims >= 1
    assert "decided_to" in predicates


def test_an_entry_with_a_speaker_still_reaches_the_model(conn, cfg):
    """The rule is about attribution, not about which connector wrote the row."""
    _message(
        conn,
        "spoken",
        "I think privacy matters more than convenience.",
        sender="me",
        when=datetime(2026, 6, 2, 9, tzinfo=UTC),
    )
    cfg.graph.extractor = "contextual_facets"
    provider = FakeLLM('{"facets":[]}')

    rebuild_graph(conn, cfg, version="1", provider=provider)

    assert provider.calls


def _session_turn(
    conn,
    external_id: str,
    text: str,
    *,
    when: datetime,
    authored: bool = True,
    project: str = "notes-app",
):
    """One turn of a working session with a coding agent, as the connector writes it."""
    speaker = "me" if authored else "Claude Code assistant"
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=external_id,
                body=f"From: {speaker}\nConversation: Claude Code — {project}\n\n{text}",
                occurred_start=when,
                thread_id="ai:claude_code:s1",
                authored_by_user=authored,
            ),
            source="ai_conversations",
            sensitivity=Sensitivity.PERSONAL,
        )
    return dao.get_entry(conn, entry_id)


def test_the_model_is_told_which_room_a_turn_was_said_in(conn):
    """The situation the archive already knows has to reach the prompt.

    Without it the model receives a bare sentence and is asked what this
    person values, so it answers — including for a build requirement dictated
    to a tool.
    """
    target = _session_turn(
        conn,
        "turn",
        "I want the export screen to only show the last thirty days.",
        when=datetime(2026, 3, 4, 11, tzinfo=UTC),
    )
    provider = FakeLLM('{"facets":[]}')

    _extractor(conn, provider).extract(target)

    prompt = provider.calls[0][1]
    assert '"setting":"work_session"' in prompt
    assert '"addressee":"assistant"' in prompt
    assert '"project":"notes-app"' in prompt


def test_a_facet_the_model_calls_work_is_kept_and_labelled_work(conn):
    target = _session_turn(
        conn,
        "turn",
        "I want the export screen to only show the last thirty days.",
        when=datetime(2026, 3, 4, 11, tzinfo=UTC),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "plan",
                    "scope": "work",
                    "statement": "Limit the export screen to the last thirty days.",
                    "evidence_quote": "I want the export screen",
                    "confidence": 0.95,
                }
            ]
        }
    )

    batch = _extractor(conn, FakeLLM(reply)).extract(target)

    # Kept, and labelled. It used to be dropped here, which cost the archive
    # every working session it had; the refusal to call it a value now lives in
    # the mirror, which is the one surface that was ever at risk from it.
    assert [claim.scope for claim in batch.claims] == ["work"]
    assert batch.quarantined == ()


def _commit(conn, external_id: str, message: str, *, authored: bool, when: datetime):
    """A commit as the git connector writes it: a document, no From line."""
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.DOCUMENT,
                external_id=external_id,
                body=message,
                occurred_start=when,
                thread_id="git:varyz",
                authored_by_user=authored,
            ),
            source="git",
            sensitivity=Sensitivity.PERSONAL,
        )
    return dao.get_entry(conn, entry_id)


def test_the_owners_own_commits_are_read_and_a_cloned_repos_are_not(conn):
    """Where four of the owner's questions lived, and the graph held one claim.

    The kind gate was written for documents, whose author is not on record, and
    it caught commits too: 2,280 of them produced one claim between them. A
    commit the owner wrote has an author, and the ingest already knows it.
    """
    when = datetime(2026, 3, 4, 11, tzinfo=UTC)
    mine = _commit(
        conn,
        "mine",
        "I decided to drop the live Telegram connector for a Desktop export.",
        authored=True,
        when=when,
    )
    theirs = _commit(
        conn,
        "theirs",
        "I decided to drop the live Telegram connector for a Desktop export.",
        authored=False,
        when=when,
    )

    def reply(entry):
        return json.dumps(
            {
                "facets": [
                    {
                        "target_id": entry.id,
                        "kind": "decision",
                        "statement": "Drop the live Telegram connector for a Desktop export.",
                        "evidence_quote": "I decided to drop the live Telegram connector",
                        "confidence": 0.95,
                    }
                ]
            }
        )

    from_mine = _extractor(conn, FakeLLM(reply(mine))).extract(mine)
    from_theirs = _extractor(conn, FakeLLM(reply(theirs))).extract(theirs)

    read = [claim for claim in from_mine.claims if claim.origin != "deterministic"]
    assert [claim.predicate for claim in read] == ["decided_to"]
    # Work, whatever the model called it, so the mirror never reads it.
    assert [claim.scope for claim in read] == ["work"]
    # The cloned repository's identical commit is not read by any model.
    assert not [claim for claim in from_theirs.claims if claim.origin != "deterministic"]


def test_the_owner_is_recognised_when_a_connector_spells_their_name_out(conn):
    """Whose statement it is cannot depend on how a connector writes "me".

    WhatsApp writes "From: me"; Telegram writes the owner's display name. The
    stored authorship flag is the same in both, so it decides, and a claim from
    the owner's own message is filed as the owner's rather than a stranger's.
    """
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id="own-telegram-turn",
                body="From: Egor Menshikov\nChat: Mama\n\nI decided to develop the app.",
                occurred_start=datetime(2026, 3, 4, 11, tzinfo=UTC),
                thread_id="tg:test",
                authored_by_user=True,
            ),
            source="telegram",
            sensitivity=Sensitivity.PERSONAL,
        )
    target = dao.get_entry(conn, entry_id)
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "decision",
                    "scope": "life",
                    "statement": "I decided to develop the app.",
                    "evidence_quote": "I decided to develop the app.",
                    "confidence": 0.95,
                }
            ]
        }
    )

    batch = _extractor(conn, FakeLLM(reply)).extract(target)

    assert [claim.subject_key for claim in batch.claims] == ["self"]


def test_work_shaped_talk_with_a_person_is_still_part_of_that_person_s_life(conn):
    """The work/life line is a line about the room, not about the subject.

    Said to an agent, "we should build the drone first" is a ticket. Said to a
    friend, it is a side project somebody is spending their year on. Dropping
    the second cost 45 real statements on the first live run of this rule --
    a relocation thread among them -- so the drop is scoped to work sessions
    and everywhere else a work-flavoured facet is kept.
    """
    target = _message(
        conn,
        "chat",
        "I think we should build the drone before the plane.",
        sender="me",
        when=datetime(2026, 3, 4, 11, tzinfo=UTC),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "plan",
                    "scope": "work",
                    "statement": "Build the drone before the plane.",
                    "evidence_quote": "we should build the drone before the plane",
                    "confidence": 0.95,
                }
            ]
        }
    )

    batch = _extractor(conn, FakeLLM(reply)).extract(target)

    assert [claim.predicate for claim in batch.claims] == ["planned_to"]


def test_a_life_scoped_facet_survives_being_said_in_a_working_session(conn):
    """People say true things about themselves in the middle of building."""
    target = _session_turn(
        conn,
        "turn",
        "I want something that remembers my whole life for me.",
        when=datetime(2026, 3, 4, 11, tzinfo=UTC),
    )
    reply = json.dumps(
        {
            "facets": [
                {
                    "target_id": target.id,
                    "kind": "plan",
                    "scope": "life",
                    "statement": "I want something that remembers my whole life for me.",
                    "evidence_quote": "I want something that remembers my whole life",
                    "confidence": 0.95,
                }
            ]
        }
    )

    batch = _extractor(conn, FakeLLM(reply)).extract(target)

    assert [claim.predicate for claim in batch.claims] == ["planned_to"]


def test_an_unlabelled_facet_defaults_to_the_room_it_was_said_in(conn):
    """A missing scope must not take the batch down, and must not become a value.

    Nine in ten stated wants in this archive's AI sessions are about software,
    so an unanswered question in that room is answered "work". The same facet
    from a conversation is unaffected — that is the old behaviour, kept.
    """
    when = datetime(2026, 3, 4, 11, tzinfo=UTC)
    session = _session_turn(conn, "turn", "I want the retries capped at three.", when=when)
    chat = _message(
        conn,
        "chat",
        "I want to spend more time outdoors.",
        sender="me",
        when=when,
    )

    def reply_for(entry, statement):
        return json.dumps(
            {
                "facets": [
                    {
                        "target_id": entry.id,
                        "kind": "plan",
                        "statement": statement,
                        "evidence_quote": statement,
                        "confidence": 0.95,
                    }
                ]
            }
        )

    from_session = _extractor(
        conn, FakeLLM(reply_for(session, "I want the retries capped at three."))
    ).extract(session)
    from_chat = _extractor(
        conn, FakeLLM(reply_for(chat, "I want to spend more time outdoors."))
    ).extract(chat)

    assert [claim.scope for claim in from_session.claims] == ["work"]
    assert [claim.predicate for claim in from_chat.claims] == ["planned_to"]
    assert [claim.scope for claim in from_chat.claims] == ["life"]


def test_the_keyless_fallback_reads_nothing_out_of_a_working_session(conn):
    """The one path with no model judging it is the one that must not guess.

    "I want X" is how a person talks to a build agent all day, and a cue-word
    match has nothing to weigh that against.
    """
    when = datetime(2026, 3, 4, 11, tzinfo=UTC)
    session = _session_turn(conn, "turn", "I decided to use the queue library.", when=when)
    chat = _message(
        conn,
        "chat",
        "I decided to move back to Lisbon.",
        sender="me",
        when=when,
    )
    extractor = ContextualFacetExtractor(
        conn,
        EgressBroker(ReceiptStore(conn), policy=PolicyEngine(), adapters=()),
        destination="cloud:missing",
        model="fixture",
    )

    assert extractor.extract(session).claims == ()
    assert [claim.predicate for claim in extractor.extract(chat).claims] == ["decided_to"]

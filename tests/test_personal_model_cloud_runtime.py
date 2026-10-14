"""The frontier observation path stays inside the privacy boundary."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from tests.conftest import FakeLLM
from varyz.config import Config
from varyz.db import dao
from varyz.destinations.llm import LLMDestinationAdapter
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.cloud_runtime import (
    BrokerSemanticAnnotator,
    HybridSemanticAnnotator,
    build_hybrid_semantic_compiler,
)
from varyz.personal_model.compiler import CompilerPolicy, ExecutionLocality
from varyz.personal_model.observations import (
    OBSERVATION_SCHEMA_VERSION,
    AnnotationBatchRequest,
    AnnotationSource,
    SemanticAnnotatorDescriptor,
    _request_dependency_fingerprint,
)
from varyz.privacy.broker import EgressBroker
from varyz.privacy.receipts import ReceiptStore


def _request(
    conn,
    episode_id: str,
    body: str,
    *,
    sensitivity: Sensitivity,
    source: str = "fixture-chat",
) -> AnnotationBatchRequest:
    with conn:
        entry_id, _ = dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=f"cloud-runtime:{episode_id}",
                body=body,
                occurred_start=datetime(2026, 8, 23, 12, tzinfo=UTC),
                thread_id=f"thread:{episode_id}",
                authored_by_user=True,
            ),
            source=source,
            source_account="owner",
            sensitivity=sensitivity,
        )
    entry = dao.get_entry(conn, entry_id)
    assert entry is not None
    source = AnnotationSource(
        entry_id=entry.id,
        content_hash=entry.content_hash,
        body=entry.body,
        source=entry.source,
        source_account=entry.source_account,
        speaker_source_key="person:owner",
    )
    fields = {
        "episode_id": episode_id,
        "target_entry_ids": (entry.id,),
        "context_complete": True,
        "sources": (source,),
        "allowed_subjects": (),
    }
    return AnnotationBatchRequest(
        **fields,
        dependency_fingerprint=_request_dependency_fingerprint(**fields),
    )


def _wire_reply(text: str) -> str:
    return json.dumps(
        {
            "r": [
                {
                    "n": 1,
                    "c": [
                        {
                            "t": 1,
                            "k": "reason",
                            "e": [{"m": 1, "q": text}],
                            "b": "A",
                            "d": "volunteered_reasoning",
                            "o": "asserted",
                            "s": None,
                            "c": 0.95,
                        }
                    ],
                }
            ]
        }
    )


def _cloud(conn, provider, **kwargs) -> BrokerSemanticAnnotator:
    broker = EgressBroker(
        ReceiptStore(conn),
        adapters=(LLMDestinationAdapter("cloud:fixture", provider, stream=False),),
    )
    return BrokerSemanticAnnotator(
        conn,
        broker,
        destination="cloud:fixture",
        model_name="frontier-fixture",
        **kwargs,
    )


class _LocalAnnotator:
    execution_locality = ExecutionLocality.OFFLINE

    def __init__(self) -> None:
        self.calls: list[tuple[AnnotationBatchRequest, ...]] = []
        self.descriptor = SemanticAnnotatorDescriptor(
            name="fixture-local",
            version="1",
            model_name="local-fixture",
            prompt_version="fixture-v1",
        )

    def annotate_batch(self, requests):
        batch = tuple(requests)
        self.calls.append(batch)
        return {
            request.episode_id: json.dumps(
                {
                    "schema_version": OBSERVATION_SCHEMA_VERSION,
                    "episode_id": request.episode_id,
                    "candidates": [],
                }
            )
            for request in batch
        }


class _EmptyGroupProvider:
    model = "frontier-fixture"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        count = len(json.loads(user)["episodes"])
        return json.dumps({"r": [{"n": index, "c": []} for index in range(1, count + 1)]})


class _LocalProvider:
    model = "managed-local-fixture"

    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str) -> str:
        return "{}"


def _local_provider_builder(cfg, **kwargs):
    return _LocalProvider()


def test_personal_episode_crosses_only_through_broker_and_restores_ids(conn):
    statement = "I chose the slower route because privacy mattered."
    request = _request(conn, "eligible", statement, sensitivity=Sensitivity.PERSONAL)
    provider = FakeLLM(_wire_reply(statement))
    annotator = _cloud(conn, provider)

    payload = json.loads(annotator.annotate(request))

    assert payload["episode_id"] == "eligible"
    assert payload["candidates"][0]["target_entry_id"] == request.target_entry_ids[0]
    assert payload["candidates"][0]["spans"][0]["exact_literal"] == statement
    assert len(provider.calls) == 1
    sent = provider.calls[0][1]
    assert statement in sent
    assert request.episode_id not in sent
    assert request.target_entry_ids[0] not in sent
    assert conn.execute("SELECT COUNT(*) FROM egress_receipts").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM egress_executions WHERE state='succeeded'").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("sensitivity", (Sensitivity.SENSITIVE, Sensitivity.UNCLASSIFIED))
def test_sensitive_or_unclassified_episode_never_reaches_provider(conn, sensitivity):
    secret = f"SECRET-{sensitivity.value}-MUST-STAY-LOCAL"
    request = _request(conn, sensitivity.value, secret, sensitivity=sensitivity)
    provider = FakeLLM(_wire_reply(secret))
    local = _LocalAnnotator()
    hybrid = HybridSemanticAnnotator(_cloud(conn, provider), local)

    hybrid.annotate(request)

    assert provider.calls == []
    assert local.calls == [(request,)]
    assert hybrid.routes == {"local_sensitive": 1}
    assert conn.execute("SELECT COUNT(*) FROM egress_receipts").fetchone()[0] == 0


def test_a_document_episode_stays_local_whatever_its_label(conn):
    text = "DOCUMENT-BODY-MUST-STAY-LOCAL"
    request = _request(conn, "doc", text, sensitivity=Sensitivity.PERSONAL, source="documents")
    provider = FakeLLM(_wire_reply(text))
    local = _LocalAnnotator()
    hybrid = HybridSemanticAnnotator(_cloud(conn, provider), local)

    hybrid.annotate(request)

    assert provider.calls == []
    assert local.calls == [(request,)]
    assert conn.execute("SELECT COUNT(*) FROM egress_receipts").fetchone()[0] == 0


def test_exhausted_spend_cap_falls_back_before_transport(conn):
    statement = "I value control over convenience."
    request = _request(conn, "spend", statement, sensitivity=Sensitivity.PERSONAL)
    provider = FakeLLM(_wire_reply(statement))
    local = _LocalAnnotator()
    hybrid = HybridSemanticAnnotator(
        _cloud(
            conn,
            provider,
            estimated_spend_usd=1.01,
            spend_remaining_usd=1.00,
        ),
        local,
    )

    hybrid.annotate(request)

    assert provider.calls == []
    assert local.calls == [(request,)]
    assert hybrid.routes == {"local_fallback": 1}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM egress_executions WHERE result_category='policy_blocked'"
        ).fetchone()[0]
        == 1
    )


def test_quota_failure_falls_back_but_bad_request_does_not(conn):
    request = _request(
        conn,
        "provider-errors",
        "I changed course because evidence changed.",
        sensitivity=Sensitivity.PERSONAL,
    )
    local = _LocalAnnotator()

    quota = FakeLLM()
    quota.complete = lambda system, user: (_ for _ in ()).throw(RuntimeError("429 quota"))
    HybridSemanticAnnotator(_cloud(conn, quota), local).annotate(request)
    assert local.calls == [(request,)]

    bad_request = FakeLLM()
    bad_request.complete = lambda system, user: (_ for _ in ()).throw(
        RuntimeError("400 bad request")
    )
    with pytest.raises(RuntimeError, match="400 bad request"):
        HybridSemanticAnnotator(_cloud(conn, bad_request), local).annotate(request)
    assert local.calls == [(request,)]


def test_malformed_frontier_output_is_loud_and_never_cached_as_local(conn):
    request = _request(
        conn,
        "malformed",
        "I protected the deadline by cutting scope.",
        sensitivity=Sensitivity.PUBLIC,
    )
    provider = FakeLLM("not json")
    local = _LocalAnnotator()

    with pytest.raises(ValueError, match="no JSON object"):
        HybridSemanticAnnotator(_cloud(conn, provider), local).annotate(request)

    assert local.calls == []
    assert len(provider.calls) == 1


def test_one_unanswered_episode_does_not_throw_away_the_group_it_was_bought_with(
    conn, caplog
):
    """A group is paid for once, and a partial reply is still most of what it bought.

    The local path has always kept what a batch answered.  Here the whole group
    crossed to another company's model and was billed before a word came back,
    so discarding every answer because one episode went unaddressed meant
    buying the rest a second time on the next run.
    """

    kept = "I chose the slower route because privacy mattered."
    requests = (
        _request(conn, "answered", kept, sensitivity=Sensitivity.PERSONAL),
        _request(
            conn, "skipped", "I moved the meeting to Friday.", sensitivity=Sensitivity.PERSONAL
        ),
    )
    provider = FakeLLM(_wire_reply(kept))

    with caplog.at_level(logging.WARNING):
        payloads = _cloud(conn, provider).annotate_batch(requests)

    assert len(provider.calls) == 1
    assert set(payloads) == {"answered"}
    assert json.loads(payloads["answered"])["candidates"][0]["spans"][0]["exact_literal"] == kept
    assert "returned 1 of 2 episodes" in caplog.text
    # The reply itself is never repeated: it came back from outside.
    assert kept not in caplog.text


def test_a_group_answered_not_at_all_is_still_an_error(conn):
    """Tolerating a partial reply is not tolerating no reply.

    Nothing came back that names any episode asked about, which is the provider
    failing rather than one episode going unaddressed, and the caller must not
    cache emptiness as an answer.
    """

    requests = (
        _request(conn, "none-a", "I renewed the lease.", sensitivity=Sensitivity.PERSONAL),
        _request(conn, "none-b", "I cancelled the trip.", sensitivity=Sensitivity.PERSONAL),
    )
    provider = FakeLLM('{"r":[{"n":9,"c":[]}]}')

    with pytest.raises(ValueError, match="answered none of 2"):
        _cloud(conn, provider).annotate_batch(requests)


def test_hybrid_declares_external_locality_so_background_compile_refuses_it(conn):
    provider = FakeLLM('{"r":[]}')
    hybrid = HybridSemanticAnnotator(_cloud(conn, provider), _LocalAnnotator())

    assert hybrid.execution_locality == ExecutionLocality.EXTERNAL


def test_cloud_groups_are_bounded_and_cumulative_spend_only_falls_back_the_tail(conn):
    requests = tuple(
        _request(
            conn,
            f"bounded-{index}",
            f"I made bounded choice {index} deliberately.",
            sensitivity=Sensitivity.PERSONAL,
        )
        for index in range(9)
    )
    provider = _EmptyGroupProvider()
    local = _LocalAnnotator()
    cloud = _cloud(
        conn,
        provider,
        estimated_spend_usd=0.60,
        spend_remaining_usd=0.60,
        episodes_per_call=8,
    )
    hybrid = HybridSemanticAnnotator(cloud, local)

    payloads = hybrid.annotate_batch(requests)

    assert set(payloads) == {request.episode_id for request in requests}
    assert len(provider.calls) == 1
    assert len(json.loads(provider.calls[0][1])["episodes"]) == 8
    assert local.calls == [(requests[-1],)]
    assert hybrid.routes == {"cloud": 8, "local_fallback": 1}
    assert cloud.estimated_spend_used_usd == pytest.approx(0.60)


def test_oversized_episode_stays_local_before_a_provider_call(conn):
    request = _request(
        conn,
        "oversized",
        "This bounded episode is larger than the deliberately tiny fixture budget.",
        sensitivity=Sensitivity.PUBLIC,
    )
    provider = _EmptyGroupProvider()
    local = _LocalAnnotator()
    hybrid = HybridSemanticAnnotator(_cloud(conn, provider, max_context_chars=10), local)

    hybrid.annotate(request)

    assert provider.calls == []
    assert local.calls == [(request,)]
    assert hybrid.routes == {"local_fallback": 1}


def test_hybrid_compiler_keeps_only_observations_external(conn):
    provider = _EmptyGroupProvider()
    composition = build_hybrid_semantic_compiler(
        conn,
        Config(),
        provider_builder=_local_provider_builder,
        cloud_provider=provider,
        estimated_spend_usd_per_call=0.01,
        spend_cap_usd=1.00,
    )

    assert composition.destination == "cloud:claude"
    assert composition.frontier_model_name == "frontier-fixture"
    assert composition.local_model_name == "managed-local-fixture"
    assert composition.compiler.policy.user_present is True
    assert composition.compiler.policy.allow_cloud_egress is True
    assert composition.compiler.annotator.execution_locality == ExecutionLocality.EXTERNAL
    assert (
        composition.compiler.candidate_adapter.execution_locality == ExecutionLocality.MANAGED_LOCAL
    )
    assert (
        composition.compiler.synthesis_adapter.execution_locality == ExecutionLocality.MANAGED_LOCAL
    )


def test_api_key_composition_requires_spend_estimate_and_cloud_authorization(conn):
    provider = _EmptyGroupProvider()
    with pytest.raises(ValueError, match="positive estimated spend"):
        build_hybrid_semantic_compiler(
            conn,
            Config(),
            provider_builder=_local_provider_builder,
            cloud_provider=provider,
        )

    with pytest.raises(ValueError, match="user-present cloud-egress authorization"):
        build_hybrid_semantic_compiler(
            conn,
            Config(),
            provider_builder=_local_provider_builder,
            cloud_provider=provider,
            estimated_spend_usd_per_call=0.01,
            policy=CompilerPolicy(user_present=True, allow_cloud_egress=False),
        )

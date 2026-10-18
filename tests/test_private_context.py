"""Fail-closed policy, transformations, consent, broker, and receipts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from varyz.destinations.actions import ActionDestinationAdapter
from varyz.destinations.export import MemoryExportAdapter
from varyz.retrieval.rag import ask

from varyz.connectors.manual import ManualConnector
from varyz.domain import Sensitivity, new_id
from varyz.llm.router import LLMRouter
from varyz.pipeline.embed import embed_pending
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.privacy.broker import EgressBroker, EgressDenied, PayloadMismatch
from varyz.privacy.doctor import provider_bypasses
from varyz.privacy.models import (
    Approval,
    ConsentGrant,
    ContextItem,
    ContextRequest,
    Interaction,
    LabelSource,
    PolicyEffect,
    PolicyRule,
    RulePrecedence,
)
from varyz.privacy.policy import PolicyEngine
from varyz.privacy.receipts import PreparedRequestConflict, ReceiptStore
from varyz.privacy.transforms import TransformationError, TransformationRegistry


def _request(
    *,
    request_id: str = "req-1",
    purpose: str = "answer_question",
    destination: str = "cloud:fake",
    max_items: int = 10,
    max_chars: int = 1000,
    consent_grant_id: str | None = None,
):
    intent = "When did I move?"
    return ContextRequest(
        request_id=request_id,
        actor="local_user",
        purpose=purpose,
        destination=destination,
        intent=intent,
        intent_hash=hashlib.sha256(intent.encode()).hexdigest(),
        requested_types=("evidence_excerpt",),
        requested_scope={},
        max_items=max_items,
        max_chars=max_chars,
        interaction=Interaction.INTERACTIVE,
        user_present=True,
        reversible=False,
        consent_grant_id=consent_grant_id,
    )


def _item(
    resource_id: str = "e1",
    *,
    sensitivity: Sensitivity = Sensitivity.PERSONAL,
    content: str = "I moved to Berlin.",
    metadata: dict | None = None,
):
    return ContextItem(
        item_id=f"entry:{resource_id}",
        resource_type="evidence_excerpt",
        resource_id=resource_id,
        content=content,
        sensitivity=sensitivity,
        label_source=LabelSource.INHERITED,
        source_ids=(resource_id,),
        metadata=metadata or {},
    )


def test_unknown_request_dimensions_fail_closed():
    engine = PolicyEngine()
    item = _item()
    for request in (
        _request(purpose="invented"),
        _request(destination="cloud:unknown"),
    ):
        decision = engine.evaluate(request, (item,))[0]
        assert decision.effect == PolicyEffect.DENY
        assert decision.reason_codes[0].startswith("unknown_")


def test_unknown_transformation_fails_closed_in_pure_policy_engine():
    rule = PolicyRule(
        rule_id="unknown-transform",
        name="unknown transform",
        precedence=RulePrecedence.DESTINATION,
        destination="cloud:fake",
        effect=PolicyEffect.TRANSFORM,
        transformation="remote_magic",
        reason_code="unsafe_transform",
    )
    decision = PolicyEngine().evaluate(
        _request(),
        (_item(),),
        rules=(rule,),
    )[0]
    assert decision.effect == PolicyEffect.DENY
    assert decision.reason_codes == ("unknown_transformation",)


def test_sensitive_final_gate_cannot_be_widened_by_allow_rule():
    rule = PolicyRule(
        rule_id="allow",
        name="unsafe allow",
        precedence=RulePrecedence.USER_RESOURCE_OVERRIDE,
        effect=PolicyEffect.ALLOW,
        reason_code="user_allow",
        resource_id="e1",
    )
    decision = PolicyEngine().evaluate(
        _request(), (_item(sensitivity=Sensitivity.SENSITIVE),), rules=(rule,)
    )[0]
    assert decision.effect == PolicyEffect.LOCAL_ONLY
    assert "entry_gate_final_deny" in decision.reason_codes


def test_cloud_shadow_mode_records_would_decision_but_preserves_legacy_gate(conn):
    store = ReceiptStore(conn)
    store.add_rule(
        PolicyRule(
            rule_id="shadow-deny",
            name="shadow deny",
            precedence=RulePrecedence.DESTINATION,
            destination="cloud:fake",
            effect=PolicyEffect.DENY,
            reason_code="new_policy_deny",
        )
    )
    adapter = RecordingAdapter()
    broker = EgressBroker(store, adapters=(adapter,), mode="shadow")

    personal = broker.prepare(_request(), (_item(),))
    sensitive = broker.prepare(
        _request(request_id="shadow-sensitive"),
        (_item(sensitivity=Sensitivity.SENSITIVE),),
    )

    assert personal.decisions[0].effect == PolicyEffect.ALLOW
    assert "shadow_policy_effect:deny" in personal.decisions[0].reason_codes
    assert "shadow_legacy_allow" in personal.decisions[0].reason_codes
    assert broker.execute(personal).state == "succeeded"
    assert len(adapter.calls) == 1
    assert sensitive.blocked is True
    assert sensitive.decisions[0].effect == PolicyEffect.LOCAL_ONLY
    assert "shadow_legacy_local_only" in sensitive.decisions[0].reason_codes


def test_explicit_deny_has_highest_precedence_and_is_monotonic():
    item = _item()
    allow = PolicyRule(
        rule_id="allow",
        name="purpose allow",
        precedence=RulePrecedence.PURPOSE,
        purpose="answer_question",
        effect=PolicyEffect.ALLOW,
        reason_code="purpose_allow",
    )
    deny = PolicyRule(
        rule_id="deny",
        name="resource deny",
        precedence=RulePrecedence.EXPLICIT_DENY,
        resource_id="e1",
        effect=PolicyEffect.DENY,
        reason_code="explicit_deny",
    )
    engine = PolicyEngine()
    assert engine.evaluate(_request(), (item,), rules=(allow,))[0].effect == "allow"
    assert engine.evaluate(_request(), (item,), rules=(allow, deny))[0].effect == "deny"


def test_transformations_are_hashed_and_fail_when_ambiguous():
    registry = TransformationRegistry()
    item = _item(
        content="Anna arrived on 2024-03-14. Email anna@example.com.",
        metadata={"people": ["Anna"], "date_precision": "month"},
    )
    pseudo = registry.apply("pseudonymize_people", item, _request())
    assert "Anna" not in pseudo.content
    assert pseudo.output_hash != pseudo.input_hash
    date = registry.apply("generalize_date", item, _request())
    assert "2024-03-14" not in date.content and "2024-03" in date.content
    redacted = registry.apply("redact_sensitive_attributes", item, _request())
    assert "anna@example.com" not in redacted.content
    ambiguous_request = _request().model_copy(
        update={
            "intent": "What?",
            "intent_hash": hashlib.sha256(b"What?").hexdigest(),
        }
    )
    with pytest.raises(TransformationError):
        registry.apply("select_sentences", item, ambiguous_request)
    with pytest.raises(TransformationError):
        registry.apply(
            "pseudonymize_people",
            item.model_copy(update={"metadata": {}}),
            _request(),
        )


class RecordingAdapter:
    destination = "cloud:fake"
    destination_type = "cloud_model"
    version = "1"
    supports_idempotency = True
    produces_external_effect = False
    reversible_default = False
    timeout_seconds = 30.0
    max_retries = 0

    def __init__(self):
        self.calls = []

    def execute(self, prepared, *, idempotency_key):
        self.calls.append((prepared.payload, idempotency_key))
        return "ok"

    @staticmethod
    def classify_error(error):
        return "recording_error"


def test_broker_enforces_budget_receipts_and_idempotency(conn):
    adapter = RecordingAdapter()
    broker = EgressBroker(ReceiptStore(conn), adapters=(adapter,))
    prepared = broker.prepare(
        _request(max_items=1),
        (_item("a"), _item("b", content="second")),
    )
    assert len(prepared.items) == 1
    assert any("item_budget_exceeded" in d.reason_codes for d in prepared.decisions)
    first = broker.execute(prepared)
    second = broker.execute(prepared)
    assert first.state == "succeeded"
    assert second.replayed is True
    assert len(adapter.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM egress_receipts").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM egress_executions WHERE state='succeeded'"
    ).fetchone()[0] == 1


def test_malformed_spend_budget_denies_and_receipts_instead_of_raising(conn):
    broker = EgressBroker(ReceiptStore(conn), adapters=(RecordingAdapter(),))
    request = _request().model_copy(
        update={"parameters": {"estimated_spend_usd": "not-a-number"}}
    )

    prepared = broker.prepare(request, (_item(),))

    assert prepared.blocked is True
    assert prepared.items == ()
    assert prepared.decisions[0].effect == PolicyEffect.DENY
    assert "invalid_spend_budget" in prepared.decisions[0].reason_codes
    assert conn.execute(
        """SELECT COUNT(*) FROM egress_executions
           WHERE state='blocked' AND result_category='policy_blocked'"""
    ).fetchone()[0] == 1


def test_broker_rejects_payload_hash_mismatch(conn):
    broker = EgressBroker(ReceiptStore(conn), adapters=(RecordingAdapter(),))
    prepared = broker.prepare(_request(), (_item(),))
    tampered = prepared.model_copy(update={"payload": prepared.payload + b"x"})
    with pytest.raises(PayloadMismatch):
        broker.execute(tampered)
    assert conn.execute(
        """SELECT COUNT(*) FROM egress_executions
           WHERE result_category='payload_hash_mismatch' AND state='blocked'"""
    ).fetchone()[0] == 1


def test_request_id_is_bound_to_one_payload_and_fresh_preview_can_be_forced(conn):
    broker = EgressBroker(ReceiptStore(conn), adapters=(RecordingAdapter(),))
    request = _request()
    broker.prepare(request, (_item(content="first payload"),))

    with pytest.raises(PreparedRequestConflict):
        broker.prepare(request, (_item(content="changed payload"),))

    fresh = request.model_copy(
        update={
            "request_id": "req-2",
            "parameters": {"force_approval": True},
        }
    )
    prepared = broker.prepare(
        fresh,
        (_item(sensitivity=Sensitivity.PUBLIC, content="changed payload"),),
    )
    assert prepared.approval_required is True
    assert conn.execute("SELECT COUNT(*) FROM egress_receipts").fetchone()[0] == 2


def test_blocked_attempt_gets_receipt_and_no_adapter_call(conn):
    adapter = RecordingAdapter()
    broker = EgressBroker(ReceiptStore(conn), adapters=(adapter,))
    prepared = broker.prepare(
        _request(), (_item(sensitivity=Sensitivity.SENSITIVE),)
    )
    assert prepared.blocked is True
    with pytest.raises(EgressDenied):
        broker.execute(prepared)
    assert adapter.calls == []
    execution = conn.execute(
        """SELECT state, completed_at FROM egress_executions
           ORDER BY started_at DESC LIMIT 1"""
    ).fetchone()
    assert execution["state"] == "blocked"
    assert execution["completed_at"] is not None


def test_failed_external_action_is_not_retried_without_idempotency(conn):
    class FailingAction:
        destination = "export:file"
        destination_type = "export"
        version = "1"
        supports_idempotency = False
        produces_external_effect = True
        reversible_default = True
        timeout_seconds = 5.0
        max_retries = 0

        def __init__(self):
            self.calls = 0

        def execute(self, prepared, *, idempotency_key):
            self.calls += 1
            raise TimeoutError("outcome unknown")

        @staticmethod
        def classify_error(error):
            return (
                "destination_timeout"
                if isinstance(error, TimeoutError)
                else "action_error"
            )

    adapter = FailingAction()
    broker = EgressBroker(ReceiptStore(conn), adapters=(adapter,))
    prepared = broker.prepare(_request(destination="export:file"), (_item(),))
    approval = Approval(
        request_id=prepared.request.request_id,
        payload_hash=prepared.payload_hash,
        actor="local_user",
        approved_at=datetime.now(UTC),
    )
    with pytest.raises(TimeoutError):
        broker.execute(prepared, approval)
    with pytest.raises(EgressDenied, match="duplicate"):
        broker.execute(prepared, approval)
    assert adapter.calls == 1
    states = [
        tuple(row)
        for row in conn.execute(
            """SELECT state, result_category FROM egress_executions
               ORDER BY attempt"""
        )
    ]
    assert ("unknown", "destination_timeout") in states
    assert ("blocked", "duplicate_action_risk") in states


def test_registered_action_persists_classified_result_and_external_id(conn):
    class CalendarTransport:
        def execute(self, payload, *, idempotency_key, timeout_seconds):
            assert timeout_seconds == 30.0
            return {
                "response": {"created": True},
                "result_category": "calendar_event_created",
                "response_metadata": {"provider": "fixture"},
                "external_action_id": "event-42",
                "reversible": True,
            }

    adapter = ActionDestinationAdapter(
        "action:calendar",
        CalendarTransport(),
        supports_idempotency=True,
    )
    broker = EgressBroker(ReceiptStore(conn), adapters=(adapter,))
    request = _request(
        purpose="external_action",
        destination="action:calendar",
    ).model_copy(update={"reversible": True})
    prepared = broker.prepare(
        request,
        (_item(sensitivity=Sensitivity.PUBLIC, content="2026-08-02 10:00"),),
    )
    result = broker.execute(prepared)

    assert result.state == "succeeded"
    assert result.result_category == "calendar_event_created"
    assert result.response_metadata == {"provider": "fixture"}
    assert result.external_action_id == "event-42"
    assert result.reversible is True
    execution = conn.execute(
        """SELECT adapter_version, response_metadata_json, external_action_id,
                  reversible
           FROM egress_executions WHERE id=?""",
        (result.execution_id,),
    ).fetchone()
    assert execution["adapter_version"] == "1"
    assert execution["response_metadata_json"] == '{"provider": "fixture"}'
    assert execution["external_action_id"] == "event-42"
    assert execution["reversible"] == 1
    registered = conn.execute(
        """SELECT destination_type, adapter_version
           FROM destination_registry WHERE destination='action:calendar'"""
    ).fetchone()
    assert tuple(registered) == ("action", "1")


def test_idempotent_action_retries_are_bounded_and_audited(conn):
    class FlakyTransport:
        def __init__(self):
            self.keys = []

        def execute(self, payload, *, idempotency_key, timeout_seconds):
            assert timeout_seconds == 30.0
            self.keys.append(idempotency_key)
            if len(self.keys) == 1:
                raise TimeoutError("retryable timeout")
            return {
                "result_category": "task_created",
                "external_action_id": "task-7",
            }

    transport = FlakyTransport()
    adapter = ActionDestinationAdapter(
        "action:task",
        transport,
        supports_idempotency=True,
        max_retries=1,
    )
    broker = EgressBroker(ReceiptStore(conn), adapters=(adapter,))
    prepared = broker.prepare(
        _request(purpose="external_action", destination="action:task"),
        (_item(sensitivity=Sensitivity.PUBLIC),),
    )

    result = broker.execute(prepared)

    assert result.state == "succeeded"
    assert result.external_action_id == "task-7"
    assert len(transport.keys) == 2
    assert transport.keys[0] == transport.keys[1]
    attempts = [
        tuple(row)
        for row in conn.execute(
            """SELECT state, result_category FROM egress_executions
               WHERE receipt_id=? ORDER BY attempt""",
            (prepared.receipt_id,),
        )
    ]
    assert attempts == [
        ("unknown", "destination_timeout"),
        ("succeeded", "task_created"),
    ]


def test_memory_passport_uses_the_same_versioned_destination_contract(conn):
    writes = []

    def write(payload, *, idempotency_key, timeout_seconds):
        writes.append((payload, idempotency_key, timeout_seconds))
        return {
            "result_category": "passport_written",
            "external_action_id": "passport-1",
            "reversible": True,
        }

    adapter = MemoryExportAdapter(
        write,
        destination="passport:encrypted",
    )
    broker = EgressBroker(ReceiptStore(conn), adapters=(adapter,))
    prepared = broker.prepare(
        _request(
            purpose="memory_passport",
            destination="passport:encrypted",
        ),
        (_item(sensitivity=Sensitivity.PUBLIC),),
    )

    result = broker.execute(prepared)

    assert result.result_category == "passport_written"
    assert result.external_action_id == "passport-1"
    assert writes[0][2] == adapter.timeout_seconds
    assert conn.execute(
        """SELECT destination_type FROM destination_registry
           WHERE destination='passport:encrypted'"""
    ).fetchone()[0] == "export"


def test_exact_approval_is_required_for_ask_effect(conn):
    now = datetime.now(UTC)
    rule = PolicyRule(
        rule_id="ask",
        name="ask before export",
        precedence=RulePrecedence.DESTINATION,
        destination="export:file",
        effect=PolicyEffect.ASK,
        reason_code="ask_export",
    )
    with conn:
        conn.execute(
            """INSERT INTO policy_rules
               (id,name,precedence,priority,destination,effect,reason_code,
                version,user_authored,active,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rule.rule_id,
                rule.name,
                rule.precedence,
                0,
                rule.destination,
                rule.effect,
                rule.reason_code,
                "1",
                1,
                1,
                now.isoformat(),
            ),
        )

    class ExportAdapter(RecordingAdapter):
        destination = "export:file"
        destination_type = "export"
        reversible_default = True

    broker = EgressBroker(ReceiptStore(conn), adapters=(ExportAdapter(),))
    prepared = broker.prepare(_request(destination="export:file"), (_item(),))
    assert prepared.approval_required
    blocked = broker.execute(prepared)
    assert blocked.state == "blocked"
    approval = Approval(
        request_id=prepared.request.request_id,
        payload_hash=prepared.payload_hash,
        actor="local_user",
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    assert broker.execute(prepared, approval).state == "succeeded"
    execution = conn.execute(
        """SELECT approval_scope_json, adapter_version
           FROM egress_executions WHERE state='succeeded'"""
    ).fetchone()
    assert '"actor": "local_user"' in execution["approval_scope_json"]
    assert prepared.payload_hash in execution["approval_scope_json"]
    assert execution["adapter_version"] == "1"


def test_ask_one_time_approval_reuses_exact_prepared_payload(
    conn, vault, cfg, fake_embedder, fake_llm
):
    ingest_item(
        conn,
        vault,
        ManualConnector(),
        SensitivityGate(cfg.sensitivity),
        ManualConnector.make_item("I moved to Berlin in 2021."),
    )
    embed_pending(conn, cfg, fake_embedder)
    with conn:
        conn.execute(
            """INSERT INTO policy_rules
               (id,name,precedence,priority,destination,effect,reason_code,
                version,user_authored,active,created_at)
               VALUES ('ask-cloud','Ask cloud','destination',0,'cloud:claude',
                       'ask','ask_cloud','1',1,1,?)""",
            (datetime.now(UTC).isoformat(),),
        )
    router = LLMRouter(cfg, provider=fake_llm)
    preview = ask(conn, cfg, "When did I move?", fake_embedder, router=router)
    assert preview.approval_required
    assert preview.sent == 0
    assert preview.payload_preview
    assert "I moved to Berlin in 2021." in preview.payload_preview
    assert "When did I move?" in preview.payload_preview
    assert fake_llm.calls == []

    approved = ask(
        conn,
        cfg,
        "When did I move?",
        fake_embedder,
        router=router,
        request_id=preview.request_id,
        approval=Approval(
            request_id=preview.request_id,
            payload_hash=preview.payload_hash,
            actor="local_user",
            approved_at=datetime.now(UTC),
        ),
    )
    assert "".join(approved.stream)
    assert approved.approval_required is False
    assert len(fake_llm.calls) == 1


def test_expired_and_revoked_consent_cannot_execute(conn):
    now = datetime.now(UTC)
    grant_id = new_id()
    with conn:
        conn.execute(
            """INSERT INTO consent_grants
               (id,actor,destination,purpose,valid_until,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                grant_id,
                "local_user",
                "cloud:fake",
                "answer_question",
                (now + timedelta(minutes=5)).isoformat(),
                now.isoformat(),
            ),
        )
    broker = EgressBroker(ReceiptStore(conn), adapters=(RecordingAdapter(),))
    prepared = broker.prepare(
        _request(consent_grant_id=grant_id), (_item(),)
    )
    assert ReceiptStore(conn).revoke_grant(grant_id)
    with pytest.raises(EgressDenied, match="revoked"):
        broker.execute(prepared)


def test_consent_scope_matching_is_deterministic():
    now = datetime.now(UTC)
    grant = ConsentGrant(
        id="g1",
        actor="local_user",
        destination="export:file",
        purpose="export",
        resource_type="evidence_excerpt",
        resource_id="e1",
        valid_until=now + timedelta(hours=1),
    )
    rule = PolicyRule(
        rule_id="ask",
        name="ask",
        precedence=RulePrecedence.DESTINATION,
        destination="export:file",
        effect=PolicyEffect.ASK,
        reason_code="ask",
    )
    request = _request(purpose="export", destination="export:file")
    engine = PolicyEngine(now=now)
    first = engine.evaluate(request, (_item(),), rules=(rule,), grants=(grant,))
    second = engine.evaluate(request, (_item(),), rules=(rule,), grants=(grant,))
    assert first == second
    assert first[0].effect == PolicyEffect.ALLOW
    assert "active_consent_grant" in first[0].reason_codes


def test_revoked_named_consent_precedence_is_fail_closed():
    now = datetime.now(UTC)
    revoked = ConsentGrant(
        id="revoked",
        actor="local_user",
        destination="export:file",
        purpose="export",
        revoked_at=now,
    )
    destination_allow = PolicyRule(
        rule_id="destination-allow",
        name="destination allow",
        precedence=RulePrecedence.DESTINATION,
        destination="export:file",
        effect=PolicyEffect.ALLOW,
        reason_code="destination_allow",
    )
    user_override = PolicyRule(
        rule_id="user-override",
        name="user override",
        precedence=RulePrecedence.USER_RESOURCE_OVERRIDE,
        resource_id="e1",
        effect=PolicyEffect.ALLOW,
        reason_code="user_override",
    )
    request = _request(
        purpose="export",
        destination="export:file",
        consent_grant_id=revoked.id,
    )
    engine = PolicyEngine(now=now)

    denied = engine.evaluate(
        request,
        (_item(),),
        rules=(destination_allow,),
        grants=(revoked,),
    )[0]
    overridden = engine.evaluate(
        request,
        (_item(),),
        rules=(destination_allow, user_override),
        grants=(revoked,),
    )[0]

    assert denied.effect == PolicyEffect.DENY
    assert denied.reason_codes == ("consent_revoked",)
    assert overridden.effect == PolicyEffect.ALLOW
    assert overridden.reason_codes[0] == "user_override"
    assert not any(code.startswith("grant:") for code in overridden.reason_codes)


def test_consent_entity_topic_and_content_time_scopes_fail_closed(conn):
    now = datetime.now(UTC)
    grant = ConsentGrant(
        id="scoped",
        actor="local_user",
        destination="export:file",
        purpose="export",
        entity_id="entity-anna",
        topic="travel",
        scope_valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        scope_valid_to=datetime(2025, 12, 31, tzinfo=UTC),
        valid_until=now + timedelta(hours=1),
    )
    ReceiptStore(conn).create_grant(grant)
    stored = ReceiptStore(conn).get_grant(grant.id)
    assert stored is not None
    assert stored.entity_id == grant.entity_id
    assert stored.topic == grant.topic
    assert stored.scope_valid_from == grant.scope_valid_from
    assert stored.scope_valid_to == grant.scope_valid_to
    rule = PolicyRule(
        rule_id="scoped-ask",
        name="scoped ask",
        precedence=RulePrecedence.DESTINATION,
        destination="export:file",
        effect=PolicyEffect.ASK,
        reason_code="ask",
    )
    request = _request(purpose="export", destination="export:file")
    engine = PolicyEngine(now=now)
    matching = _item(
        metadata={
            "entity_ids": ["entity-anna"],
            "topics": ["travel"],
            "valid_from": "2025-06-01T00:00:00+00:00",
        }
    )
    wrong_entity = matching.model_copy(
        update={"metadata": {**matching.metadata, "entity_ids": ["entity-bob"]}}
    )
    wrong_topic = matching.model_copy(
        update={"metadata": {**matching.metadata, "topics": ["work"]}}
    )
    out_of_range = matching.model_copy(
        update={
            "metadata": {
                **matching.metadata,
                "valid_from": "2026-01-01T00:00:00+00:00",
            }
        }
    )
    decisions = engine.evaluate(
        request,
        (matching, wrong_entity, wrong_topic, out_of_range),
        rules=(rule,),
        grants=(stored,),
    )
    assert [decision.effect for decision in decisions] == [
        PolicyEffect.ALLOW,
        PolicyEffect.ASK,
        PolicyEffect.ASK,
        PolicyEffect.ASK,
    ]
    named_mismatch = engine.evaluate(
        request.model_copy(update={"consent_grant_id": grant.id}),
        (wrong_entity,),
        rules=(rule,),
        grants=(stored,),
    )[0]
    assert named_mismatch.effect == PolicyEffect.DENY
    assert named_mismatch.reason_codes == ("consent_scope_mismatch",)


def test_no_provider_sdk_imports_outside_adapters():
    source_root = Path(__file__).parents[1] / "src" / "varyz"
    assert provider_bypasses(source_root) == []

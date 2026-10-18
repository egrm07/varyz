"""Append-only context receipts, executions, consent, and rule persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from varyz.domain import iso, new_id, utc_now
from varyz.privacy.models import (
    Approval,
    ConsentGrant,
    ContextItem,
    DestinationAdapter,
    PolicyRule,
    PreparedEgress,
    RulePrecedence,
    content_hash,
)


class PreparedRequestConflict(RuntimeError):
    """A request ID was already bound to a different immutable payload."""


class ConsentReservationError(RuntimeError):
    """A consent grant changed or ran out before execution was reserved."""


class ExecutionAlreadyActive(RuntimeError):
    """The same disclosure is already in flight or already completed."""


class ReceiptIntegrityError(RuntimeError):
    """A durable audit row could not be created, updated, or verified."""


class ReceiptStore:
    _LOGICAL_REQUEST_KEY = "__varyz_logical_request_id"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def register_destination(
        self,
        *,
        destination: str,
        version: str,
        destination_type: str,
        supports_idempotency: bool,
        reversible_default: bool,
    ) -> None:
        existing = self.conn.execute(
            """SELECT adapter_version, destination_type, supports_idempotency,
                      reversible_default, active
               FROM destination_registry WHERE destination=?""",
            (destination,),
        ).fetchone()
        desired = (
            version,
            destination_type,
            int(supports_idempotency),
            int(reversible_default),
            1,
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO destination_registry
                   (destination, adapter_version, destination_type,
                    supports_idempotency, reversible_default, active)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(destination) DO UPDATE SET
                     adapter_version=excluded.adapter_version,
                     destination_type=excluded.destination_type,
                     supports_idempotency=excluded.supports_idempotency,
                     reversible_default=excluded.reversible_default,
                     active=1""",
                (
                    destination,
                    version,
                    destination_type,
                    int(supports_idempotency),
                    int(reversible_default),
                ),
            )
            if existing is None or tuple(existing) != desired:
                from varyz.events import emit_event

                emit_event(
                    self.conn,
                    "policy_changed",
                    "destination",
                    destination,
                    {"adapter_version": version, "destination_type": destination_type},
                )

    def policy_rules(self) -> tuple[PolicyRule, ...]:
        rows = self.conn.execute(
            "SELECT * FROM policy_rules WHERE active=1 ORDER BY id"
        ).fetchall()
        return tuple(
            PolicyRule(
                rule_id=row["id"],
                name=row["name"],
                precedence=RulePrecedence(row["precedence"]),
                priority=row["priority"],
                actor=row["actor"],
                purpose=row["purpose"],
                destination=row["destination"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                source=row["source"],
                sensitivity=row["sensitivity"],
                effect=row["effect"],
                transformation=row["transformation"],
                reason_code=row["reason_code"],
                version=row["version"],
                active=bool(row["active"]),
            )
            for row in rows
        )

    def consent_grants(self) -> tuple[ConsentGrant, ...]:
        rows = self.conn.execute("SELECT * FROM consent_grants ORDER BY id").fetchall()
        grants = []
        for row in rows:
            try:
                grants.append(self._row_to_grant(row))
            except ValueError:
                # Malformed persisted grants are ignored by policy and surfaced
                # by privacy doctor; they never broaden access.
                continue
        return tuple(grants)

    def get_grant(self, grant_id: str) -> ConsentGrant | None:
        row = self.conn.execute(
            "SELECT * FROM consent_grants WHERE id=?", (grant_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return self._row_to_grant(row)
        except ValueError:
            return None

    def create_grant(self, grant: ConsentGrant) -> str:
        with self.conn:
            self.conn.execute(
                """INSERT INTO consent_grants
                   (id, actor, destination, purpose, request_id, conversation_id,
                    resource_type, resource_id, entity_id, topic, scope_valid_from,
                    scope_valid_to, valid_from, valid_until, max_items, max_chars,
                    used_items, used_chars, revoked_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    grant.id,
                    grant.actor,
                    grant.destination,
                    grant.purpose,
                    grant.request_id,
                    grant.conversation_id,
                    grant.resource_type,
                    grant.resource_id,
                    grant.entity_id,
                    grant.topic,
                    iso(grant.scope_valid_from),
                    iso(grant.scope_valid_to),
                    iso(grant.valid_from),
                    iso(grant.valid_until),
                    grant.max_items,
                    grant.max_chars,
                    grant.used_items,
                    grant.used_chars,
                    iso(grant.revoked_at),
                    iso(utc_now()),
                ),
            )
        return grant.id

    def add_rule(self, rule: PolicyRule, *, user_authored: bool = True) -> str:
        with self.conn:
            self.conn.execute(
                """INSERT INTO policy_rules
                   (id, name, precedence, priority, actor, purpose, destination,
                    resource_type, resource_id, source, sensitivity, effect,
                    transformation, reason_code, version, user_authored, active,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rule.rule_id,
                    rule.name,
                    rule.precedence,
                    rule.priority,
                    rule.actor,
                    rule.purpose,
                    rule.destination,
                    rule.resource_type,
                    rule.resource_id,
                    rule.source,
                    rule.sensitivity,
                    rule.effect,
                    rule.transformation,
                    rule.reason_code,
                    rule.version,
                    int(user_authored),
                    int(rule.active),
                    iso(utc_now()),
                ),
            )
            from varyz.events import emit_event

            emit_event(
                self.conn,
                "policy_changed",
                "policy_rule",
                rule.rule_id,
                {"version": rule.version, "effect": str(rule.effect)},
            )
        return rule.rule_id

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> ConsentGrant:
        def dt(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return ConsentGrant(
            id=row["id"],
            actor=row["actor"],
            destination=row["destination"],
            purpose=row["purpose"],
            request_id=row["request_id"],
            conversation_id=row["conversation_id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            entity_id=row["entity_id"],
            topic=row["topic"],
            scope_valid_from=dt(row["scope_valid_from"]),
            scope_valid_to=dt(row["scope_valid_to"]),
            valid_from=dt(row["valid_from"]),
            valid_until=dt(row["valid_until"]),
            max_items=row["max_items"],
            max_chars=row["max_chars"],
            used_items=row["used_items"],
            used_chars=row["used_chars"],
            revoked_at=dt(row["revoked_at"]),
        )

    def record_preparation(
        self,
        prepared: PreparedEgress,
        considered: tuple[ContextItem, ...],
    ) -> str:
        request = prepared.request
        now = iso(utc_now())
        summary: dict[str, int] = {}
        for decision in prepared.decisions:
            summary[str(decision.effect)] = summary.get(str(decision.effect), 0) + 1
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing_receipt = self.conn.execute(
                """SELECT id, payload_hash, approval_required FROM egress_receipts
                   WHERE request_id=? ORDER BY created_at LIMIT 1""",
                (request.request_id,),
            ).fetchone()
            if existing_receipt is not None:
                if existing_receipt["payload_hash"] != prepared.payload_hash:
                    storage_request_id = self._approved_revision_id(
                        prepared, existing_receipt
                    )
                    if storage_request_id is None:
                        raise PreparedRequestConflict(
                            "request ID is already bound to a different prepared payload"
                        )
                    existing_revision = self.conn.execute(
                        """SELECT id FROM egress_receipts
                           WHERE request_id=? AND payload_hash=?""",
                        (storage_request_id, prepared.payload_hash),
                    ).fetchone()
                    if existing_revision is not None:
                        replay = prepared.model_copy(
                            update={"receipt_id": str(existing_revision["id"])}
                        )
                        if not self.verify_prepared(replay):
                            raise PreparedRequestConflict(
                                "approved revision has a different persisted decision"
                            )
                        self.conn.commit()
                        return str(existing_revision["id"])
                else:
                    storage_request_id = request.request_id
                    replay = prepared.model_copy(
                        update={"receipt_id": str(existing_receipt["id"])}
                    )
                    if not self.verify_prepared(replay):
                        raise PreparedRequestConflict(
                            "request ID has a different persisted decision or scope"
                        )
                    self.conn.commit()
                    return str(existing_receipt["id"])
            else:
                storage_request_id = request.request_id

            stored_scope = dict(request.requested_scope)
            if storage_request_id != request.request_id:
                stored_scope[self._LOGICAL_REQUEST_KEY] = request.request_id
            self.conn.execute(
                """INSERT INTO context_requests
                   (id, actor, purpose, destination, intent_hash, requested_types_json,
                    requested_scope_json, max_items, max_chars, interaction, user_present,
                    reversible, consent_grant_id, conversation_id, policy_version,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    storage_request_id,
                    request.actor,
                    request.purpose,
                    request.destination,
                    request.intent_hash,
                    json.dumps(request.requested_types, sort_keys=True),
                    json.dumps(stored_scope, sort_keys=True),
                    request.max_items,
                    request.max_chars,
                    request.interaction,
                    int(request.user_present),
                    int(request.reversible),
                    request.consent_grant_id,
                    request.conversation_id,
                    request.policy_version,
                    now,
                ),
            )
            for decision in prepared.decisions:
                transformed = next(
                    (item for item in prepared.items if item.item_id == decision.item_id), None
                )
                self.conn.execute(
                    """INSERT INTO context_decisions
                       (id, request_id, item_id, resource_type, resource_id, effect,
                        reason_codes_json, transformation, input_hash, output_hash,
                        policy_version, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id(),
                        storage_request_id,
                        decision.item_id,
                        decision.resource_type,
                        decision.resource_id,
                        decision.effect,
                        json.dumps(decision.reason_codes, sort_keys=True),
                        decision.transformation,
                        decision.input_hash,
                        transformed.output_hash if transformed else None,
                        decision.policy_version,
                        now,
                    ),
                )
            self.conn.execute(
                """INSERT INTO egress_receipts
                   (id, request_id, payload_hash, payload_item_count, payload_char_count,
                    considered_ids_json, decision_summary_json, policy_version,
                    transformation_versions_json, approval_required, consent_grant_id,
                    receipt_version, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prepared.receipt_id,
                    storage_request_id,
                    prepared.payload_hash,
                    len(prepared.items),
                    prepared.char_count,
                    json.dumps(
                        [
                            {
                                "item_id": item.item_id,
                                "resource_type": item.resource_type,
                                "resource_id": item.resource_id,
                                "source_ids": item.source_ids,
                            }
                            for item in considered
                        ],
                        sort_keys=True,
                    ),
                    json.dumps(summary, sort_keys=True),
                    prepared.policy_version,
                    json.dumps(prepared.transformation_versions, sort_keys=True),
                    int(prepared.approval_required),
                    request.consent_grant_id,
                    "1",
                    now,
                ),
            )
            self.conn.commit()
            return prepared.receipt_id
        except Exception:
            self.conn.rollback()
            raise

    def _approved_revision_id(
        self, prepared: PreparedEgress, existing_receipt: sqlite3.Row
    ) -> str | None:
        """Return a storage ID for ASK -> request-bound-grant reevaluation."""
        request = prepared.request
        grant_id = request.consent_grant_id
        if not grant_id or not existing_receipt["approval_required"]:
            return None
        grant = self.get_grant(grant_id)
        if (
            grant is None
            or not grant.is_active(utc_now())
            or grant.request_id != request.request_id
            or grant.actor != request.actor
            or grant.destination != request.destination
            or grant.purpose != request.purpose
        ):
            return None
        active = self.conn.execute(
            """SELECT 1 FROM egress_executions
               WHERE receipt_id=? AND state IN ('started','succeeded')""",
            (existing_receipt["id"],),
        ).fetchone()
        if active is not None:
            return None
        original_request = self.conn.execute(
            "SELECT * FROM context_requests WHERE id=?", (request.request_id,)
        ).fetchone()
        if original_request is None:
            return None
        unchanged_request = (
            original_request["actor"],
            original_request["purpose"],
            original_request["destination"],
            original_request["intent_hash"],
            tuple(json.loads(original_request["requested_types_json"])),
            json.loads(original_request["requested_scope_json"]),
            original_request["max_items"],
            original_request["max_chars"],
            original_request["interaction"],
            original_request["user_present"],
            original_request["reversible"],
            original_request["conversation_id"],
            original_request["policy_version"],
        ) == (
            request.actor,
            request.purpose,
            request.destination,
            request.intent_hash,
            request.requested_types,
            request.requested_scope,
            request.max_items,
            request.max_chars,
            str(request.interaction),
            int(request.user_present),
            int(request.reversible),
            request.conversation_id,
            request.policy_version,
        )
        if not unchanged_request:
            return None
        previous = {
            row["item_id"]: row
            for row in self.conn.execute(
                "SELECT * FROM context_decisions WHERE request_id=?",
                (request.request_id,),
            )
        }
        outputs = {item.item_id: item.output_hash for item in prepared.items}
        if len(previous) != len(prepared.decisions):
            return None
        transitioned = False
        for decision in prepared.decisions:
            old = previous.get(decision.item_id)
            if old is None or (
                old["resource_type"],
                old["resource_id"],
                old["input_hash"],
                old["output_hash"],
                old["transformation"],
            ) != (
                decision.resource_type,
                decision.resource_id,
                decision.input_hash,
                outputs.get(decision.item_id),
                decision.transformation,
            ):
                return None
            old_effect = str(old["effect"])
            new_effect = str(decision.effect)
            if old_effect == new_effect:
                continue
            if (
                old_effect != "ask"
                or new_effect != "allow"
                or f"grant:{grant_id}" not in decision.reason_codes
            ):
                return None
            transitioned = True
        if not transitioned:
            return None
        digest = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:12]
        return f"approval-revision:{digest}:{prepared.payload_hash[:20]}"

    def start_execution(
        self,
        prepared: PreparedEgress,
        *,
        state: str = "started",
        category: str = "attempt_started",
        error: str | None = None,
        approval: Approval | None = None,
        adapter_version: str | None = None,
    ) -> tuple[str, int]:
        previous = self.conn.execute(
            """SELECT COALESCE(MAX(attempt), 0) FROM egress_executions
               WHERE request_id=? AND payload_hash=?""",
            (prepared.request.request_id, prepared.payload_hash),
        ).fetchone()[0]
        execution_id = new_id()
        attempt = int(previous) + 1
        now = iso(utc_now())
        approval_scope = (
            {
                "request_id": approval.request_id,
                "payload_hash": approval.payload_hash,
                "actor": approval.actor,
                "approved_at": iso(approval.approved_at),
                "expires_at": iso(approval.expires_at),
                "allow_duplicate_risk": approval.allow_duplicate_risk,
            }
            if approval
            else {}
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO egress_executions
                   (id, receipt_id, request_id, payload_hash, attempt, state,
                    result_category, adapter_version, approval_scope_json,
                    response_metadata_json, reversible, error, started_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    prepared.receipt_id,
                    prepared.request.request_id,
                    prepared.payload_hash,
                    attempt,
                    state,
                    category,
                    adapter_version,
                    json.dumps(approval_scope, sort_keys=True),
                    "{}",
                    int(prepared.request.reversible),
                    error,
                    now,
                    now if state != "started" else None,
                ),
            )
        return execution_id, attempt

    def reserve_execution(
        self,
        prepared: PreparedEgress,
        *,
        approval: Approval | None,
        adapter: DestinationAdapter,
        adapter_version: str,
        grant_usages: dict[str, tuple[int, int, str]],
    ) -> tuple[str, int]:
        """Atomically spend consent and durably claim the adapter call.

        `BEGIN IMMEDIATE` serializes contenders before either can consume a
        grant or write a `started` receipt. The transaction commits before the
        adapter is called, so a crash can leave `started`, never no record.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if not self.verify_prepared(prepared):
                raise ReceiptIntegrityError("prepared disclosure does not match receipt")
            if not self.verify_destination(adapter):
                raise ReceiptIntegrityError("destination registry changed")
            existing = self.conn.execute(
                """SELECT state FROM egress_executions
                   WHERE request_id=? AND payload_hash=?
                     AND state IN ('started','succeeded')
                   ORDER BY attempt DESC LIMIT 1""",
                (prepared.request.request_id, prepared.payload_hash),
            ).fetchone()
            if existing is not None:
                raise ExecutionAlreadyActive(str(existing["state"]))

            now = iso(utc_now())
            for grant_id, (item_count, char_count, expected_hash) in grant_usages.items():
                grant_row = self.conn.execute(
                    "SELECT * FROM consent_grants WHERE id=?", (grant_id,)
                ).fetchone()
                try:
                    current_hash = content_hash(
                        self._row_to_grant(grant_row).model_dump(mode="python")
                    )
                except (TypeError, ValueError):
                    current_hash = ""
                if current_hash != expected_hash:
                    raise ConsentReservationError(grant_id)
                cur = self.conn.execute(
                    """UPDATE consent_grants
                       SET used_items=used_items+?, used_chars=used_chars+?
                       WHERE id=? AND revoked_at IS NULL
                         AND (valid_from IS NULL OR valid_from <= ?)
                         AND (valid_until IS NULL OR valid_until >= ?)
                         AND (max_items IS NULL OR used_items+? <= max_items)
                         AND (max_chars IS NULL OR used_chars+? <= max_chars)""",
                    (
                        item_count,
                        char_count,
                        grant_id,
                        now,
                        now,
                        item_count,
                        char_count,
                    ),
                )
                if cur.rowcount != 1:
                    raise ConsentReservationError(grant_id)

            previous = self.conn.execute(
                """SELECT COALESCE(MAX(attempt), 0) FROM egress_executions
                   WHERE request_id=? AND payload_hash=?""",
                (prepared.request.request_id, prepared.payload_hash),
            ).fetchone()[0]
            execution_id = new_id()
            attempt = int(previous) + 1
            approval_scope = (
                {
                    "request_id": approval.request_id,
                    "payload_hash": approval.payload_hash,
                    "actor": approval.actor,
                    "approved_at": iso(approval.approved_at),
                    "expires_at": iso(approval.expires_at),
                    "allow_duplicate_risk": approval.allow_duplicate_risk,
                }
                if approval
                else {}
            )
            self.conn.execute(
                """INSERT INTO egress_executions
                   (id, receipt_id, request_id, payload_hash, attempt, state,
                    result_category, adapter_version, approval_scope_json,
                    response_metadata_json, reversible, error, started_at, completed_at)
                   VALUES (?,?,?,?,?,'started','attempt_started',?,?,?, ?,NULL,?,NULL)""",
                (
                    execution_id,
                    prepared.receipt_id,
                    prepared.request.request_id,
                    prepared.payload_hash,
                    attempt,
                    adapter_version,
                    json.dumps(approval_scope, sort_keys=True),
                    "{}",
                    int(prepared.request.reversible),
                    now,
                ),
            )
            self.conn.commit()
            return execution_id, attempt
        except Exception:
            self.conn.rollback()
            raise

    def finish_execution(
        self,
        execution_id: str,
        *,
        state: str,
        category: str,
        metadata: dict | None = None,
        external_action_id: str | None = None,
        reversible: bool | None = None,
        error: str | None = None,
    ) -> None:
        started = self.conn.execute(
            "SELECT started_at FROM egress_executions WHERE id=?", (execution_id,)
        ).fetchone()
        if started is None:
            raise ReceiptIntegrityError("execution receipt is missing")
        completed = max(str(started["started_at"]), str(iso(utc_now())))
        with self.conn:
            cur = self.conn.execute(
                """UPDATE egress_executions
                   SET state=?, result_category=?, response_metadata_json=?,
                       external_action_id=?, reversible=COALESCE(?, reversible),
                       error=?, completed_at=?
                   WHERE id=? AND state='started'""",
                (
                    state,
                    category,
                    json.dumps(metadata or {}, sort_keys=True),
                    external_action_id,
                    int(reversible) if reversible is not None else None,
                    error,
                    completed,
                    execution_id,
                ),
            )
        if cur.rowcount != 1:
            raise ReceiptIntegrityError("execution receipt was not in started state")

    def successful_execution(self, request_id: str, payload_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM egress_executions
               WHERE request_id=? AND payload_hash=? AND state='succeeded'
               ORDER BY attempt DESC LIMIT 1""",
            (request_id, payload_hash),
        ).fetchone()

    def failed_executions(self, request_id: str, payload_hash: str) -> int:
        return self.conn.execute(
            """SELECT COUNT(*) FROM egress_executions
               WHERE request_id=? AND payload_hash=? AND state IN ('failed','unknown')""",
            (request_id, payload_hash),
        ).fetchone()[0]

    def indeterminate_executions(self, request_id: str, payload_hash: str) -> int:
        return self.conn.execute(
            """SELECT COUNT(*) FROM egress_executions
               WHERE request_id=? AND payload_hash=? AND state IN ('started','unknown')""",
            (request_id, payload_hash),
        ).fetchone()[0]

    def consume_grant(self, grant_id: str, item_count: int, char_count: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """UPDATE consent_grants
                   SET used_items=used_items+?, used_chars=used_chars+?
                   WHERE id=? AND revoked_at IS NULL
                     AND (valid_from IS NULL OR valid_from <= ?)
                     AND (max_items IS NULL OR used_items+? <= max_items)
                     AND (max_chars IS NULL OR used_chars+? <= max_chars)
                     AND (valid_until IS NULL OR valid_until >= ?)""",
                (
                    item_count,
                    char_count,
                    grant_id,
                    iso(utc_now()),
                    item_count,
                    char_count,
                    iso(utc_now()),
                ),
            )
        return cur.rowcount == 1

    def verify(self, receipt_id: str, payload_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT payload_hash FROM egress_receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        return bool(row and row["payload_hash"] == payload_hash)

    def verify_destination(self, adapter: DestinationAdapter) -> bool:
        row = self.conn.execute(
            """SELECT adapter_version, destination_type, supports_idempotency,
                      reversible_default, active
               FROM destination_registry WHERE destination=?""",
            (adapter.destination,),
        ).fetchone()
        return bool(
            row
            and (
                row["adapter_version"],
                row["destination_type"],
                row["supports_idempotency"],
                row["reversible_default"],
                row["active"],
            )
            == (
                adapter.version,
                adapter.destination_type,
                int(adapter.supports_idempotency),
                int(adapter.reversible_default),
                1,
            )
        )

    def verify_prepared(self, prepared: PreparedEgress) -> bool:
        """Verify every persisted field that can authorize or describe egress."""
        try:
            row = self.conn.execute(
                """SELECT r.*, q.actor, q.purpose, q.destination, q.intent_hash,
                          q.requested_types_json, q.requested_scope_json, q.max_items,
                          q.max_chars, q.interaction, q.user_present, q.reversible,
                          q.conversation_id, q.policy_version AS request_policy_version
                   FROM egress_receipts r
                   JOIN context_requests q ON q.id=r.request_id
                   WHERE r.id=?""",
                (prepared.receipt_id,),
            ).fetchone()
            if row is None:
                return False
            request = prepared.request
            stored_scope = json.loads(row["requested_scope_json"])
            logical_request_id = stored_scope.pop(
                self._LOGICAL_REQUEST_KEY, row["request_id"]
            )
            expected = (
                request.request_id,
                prepared.payload_hash,
                len(prepared.items),
                prepared.char_count,
                prepared.policy_version,
                int(prepared.approval_required),
                request.consent_grant_id,
                request.actor,
                request.purpose,
                request.destination,
                request.intent_hash,
                request.max_items,
                request.max_chars,
                str(request.interaction),
                int(request.user_present),
                int(request.reversible),
                request.conversation_id,
                request.policy_version,
            )
            actual = (
                logical_request_id,
                row["payload_hash"],
                row["payload_item_count"],
                row["payload_char_count"],
                row["policy_version"],
                row["approval_required"],
                row["consent_grant_id"],
                row["actor"],
                row["purpose"],
                row["destination"],
                row["intent_hash"],
                row["max_items"],
                row["max_chars"],
                row["interaction"],
                row["user_present"],
                row["reversible"],
                row["conversation_id"],
                row["request_policy_version"],
            )
            if actual != expected:
                return False
            if row["receipt_version"] != "1":
                return False
            if tuple(json.loads(row["requested_types_json"])) != request.requested_types:
                return False
            if stored_scope != request.requested_scope:
                return False
            if json.loads(row["transformation_versions_json"]) != (
                prepared.transformation_versions
            ):
                return False
            summary: dict[str, int] = {}
            for decision in prepared.decisions:
                summary[str(decision.effect)] = summary.get(str(decision.effect), 0) + 1
            if json.loads(row["decision_summary_json"]) != summary:
                return False
            considered = json.loads(row["considered_ids_json"])
            if not isinstance(considered, list) or len(considered) != len(
                prepared.decisions
            ):
                return False
            decision_resources = {
                (decision.item_id, decision.resource_type, decision.resource_id)
                for decision in prepared.decisions
            }
            stored_resources = {
                (value["item_id"], value["resource_type"], value["resource_id"])
                for value in considered
            }
            if stored_resources != decision_resources:
                return False
            decisions = self.conn.execute(
                "SELECT * FROM context_decisions WHERE request_id=?",
                (row["request_id"],),
            ).fetchall()
            if len(decisions) != len(prepared.decisions):
                return False
            stored = {row["item_id"]: row for row in decisions}
            if len(stored) != len(decisions):
                return False
            transformed = {item.item_id: item for item in prepared.items}
            if len(transformed) != len(prepared.items):
                return False
            for item in prepared.items:
                if hashlib.sha256(item.content.encode("utf-8")).hexdigest() != (
                    item.output_hash
                ):
                    return False
            for decision in prepared.decisions:
                saved = stored.get(decision.item_id)
                output = transformed.get(decision.item_id)
                if saved is None or (
                    saved["resource_type"],
                    saved["resource_id"],
                    saved["effect"],
                    tuple(json.loads(saved["reason_codes_json"])),
                    saved["transformation"],
                    saved["input_hash"],
                    saved["output_hash"],
                    saved["policy_version"],
                ) != (
                    decision.resource_type,
                    decision.resource_id,
                    str(decision.effect),
                    decision.reason_codes,
                    decision.transformation,
                    decision.input_hash,
                    output.output_hash if output else None,
                    decision.policy_version,
                ):
                    return False
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def revoke_grant(self, grant_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """UPDATE consent_grants SET revoked_at=?
                   WHERE id=? AND revoked_at IS NULL""",
                (iso(utc_now()), grant_id),
            )
            if cur.rowcount:
                from varyz.events import emit_event

                emit_event(
                    self.conn,
                    "consent_revoked",
                    "consent_grant",
                    grant_id,
                    {"revoked_at": iso(utc_now())},
                )
        return cur.rowcount == 1

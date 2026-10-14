"""Pure, deterministic, fail-closed policy evaluation."""

from __future__ import annotations

from datetime import UTC, datetime

from varyz.domain import Sensitivity
from varyz.privacy import categories
from varyz.privacy.models import (
    ConsentGrant,
    ContextItem,
    ContextRequest,
    PolicyDecision,
    PolicyEffect,
    PolicyRule,
    RulePrecedence,
)

KNOWN_PURPOSES = frozenset(
    {
        "local_search",
        "graph_extraction",
        "timeline_render",
        "local_inference",
        "answer_question",
        "enrich_timeline",
        "graph_model_extraction",
        "personal_model_observation",
        "export",
        "memory_passport",
        "share",
        "remote_sync",
        "analytics",
        "plugin_access",
        "external_action",
    }
)
# Sources Varyz reads and indexes on this machine and never hands to a cloud
# model for its own processing -- extraction, annotation, enrichment -- whatever
# their label (owner decision, 2026-10-01). Learning how the owner works is done
# from their AI conversations alone. An agent that looks something up
# (`answer_question`) still receives the excerpt it asked for.
LOCAL_ONLY_SOURCES = frozenset({"documents"})
AGENT_PURPOSES = frozenset({"answer_question"})

KNOWN_RESOURCE_TYPES = frozenset(
    {
        "entry",
        "chunk",
        "evidence_excerpt",
        "entity",
        "alias",
        "claim",
        "evidence_quote",
        "summary",
        "action_parameter",
        "aggregate",
        "question",
        "personal_model_episode",
    }
)
KNOWN_DESTINATIONS = frozenset(
    {
        "local",
        "cloud:claude",
        "cloud:openai",
        "cloud:deepseek",
        "cloud:fake",
        "export:file",
        "share:encrypted",
    }
)
KNOWN_TRANSFORMATIONS = frozenset(
    {
        "select_claim",
        "select_sentences",
        "pseudonymize_people",
        "generalize_location",
        "generalize_date",
        "redact_sensitive_attributes",
        "aggregate_count",
        "local_answer",
    }
)

PRECEDENCE = {
    RulePrecedence.EXPLICIT_DENY: 700,
    RulePrecedence.USER_RESOURCE_OVERRIDE: 600,
    RulePrecedence.REVOKED_CONSENT: 500,
    RulePrecedence.DESTINATION: 400,
    RulePrecedence.PURPOSE: 300,
    RulePrecedence.SOURCE_DEFAULT: 200,
    RulePrecedence.SYSTEM_DEFAULT: 100,
}


def _metadata_values(item: ContextItem, singular: str, plural: str) -> set[str]:
    values: set[str] = set()
    single = item.metadata.get(singular)
    if single is not None:
        values.add(str(single))
    many = item.metadata.get(plural, ())
    if isinstance(many, str):
        values.add(many)
    else:
        values.update(str(value) for value in many)
    return values


def item_categories(item: ContextItem) -> tuple[str, ...]:
    """The confidential categories in one item.

    A caller that has already classified the item — the local interface, a
    connector that knows what it imported — can say so in
    `metadata["confidential_categories"]`, and only names this module recognises
    are honoured. Otherwise the text is read. Either way the answer is the same
    for the same input, because the receipt hashes it.
    """
    declared = item.metadata.get("confidential_categories")
    values = (
        set()
        if declared is None
        else {declared}
        if isinstance(declared, str)
        else {str(value) for value in declared}
    )
    # Declared labels may add caution, never suppress what the text itself says.
    values.update(categories.detect(item.content))
    return tuple(name for name in categories.LABELS if name in values)


def _metadata_time(item: ContextItem, *keys: str) -> datetime | None:
    for key in keys:
        raw = item.metadata.get(key)
        if raw is None:
            continue
        try:
            value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _deny(item: ContextItem, version: str, code: str) -> PolicyDecision:
    return PolicyDecision(
        item_id=item.item_id,
        resource_type=item.resource_type,
        resource_id=item.resource_id,
        effect=PolicyEffect.DENY,
        reason_codes=(code,),
        policy_version=version,
        input_hash=item.input_hash,
    )


class PolicyEngine:
    """No I/O and no network access; identical inputs produce identical output."""

    def __init__(
        self,
        *,
        version: str = "1",
        known_purposes: frozenset[str] = KNOWN_PURPOSES,
        known_destinations: frozenset[str] = KNOWN_DESTINATIONS,
        known_resource_types: frozenset[str] = KNOWN_RESOURCE_TYPES,
        known_transformations: frozenset[str] = KNOWN_TRANSFORMATIONS,
        known_actors: frozenset[str] = frozenset({"local_user"}),
        now: datetime | None = None,
    ):
        self.version = version
        self.known_purposes = known_purposes
        self.known_destinations = known_destinations
        self.known_resource_types = known_resource_types
        self.known_transformations = known_transformations
        self.known_actors = known_actors
        self._now = now

    def with_destinations(self, *destinations: str) -> PolicyEngine:
        """Return an equivalent engine that recognizes explicitly registered adapters."""
        return PolicyEngine(
            version=self.version,
            known_purposes=self.known_purposes,
            known_destinations=self.known_destinations.union(destinations),
            known_resource_types=self.known_resource_types,
            known_transformations=self.known_transformations,
            known_actors=self.known_actors,
            now=self._now,
        )

    def with_actors(self, *actors: str) -> PolicyEngine:
        """Authorize principals already authenticated at the transport door."""
        return PolicyEngine(
            version=self.version,
            known_purposes=self.known_purposes,
            known_destinations=self.known_destinations,
            known_resource_types=self.known_resource_types,
            known_transformations=self.known_transformations,
            known_actors=self.known_actors.union(actor for actor in actors if actor),
            now=self._now,
        )

    def evaluate(
        self,
        request: ContextRequest,
        items: tuple[ContextItem, ...],
        *,
        rules: tuple[PolicyRule, ...] = (),
        grants: tuple[ConsentGrant, ...] = (),
    ) -> tuple[PolicyDecision, ...]:
        try:
            request_error = self._validate_request(request)
            return tuple(
                _deny(item, self.version, request_error)
                if request_error
                else self._evaluate_item(request, item, rules, grants)
                for item in items
            )
        except Exception:
            # The boundary's failure mode is part of the public contract.
            return tuple(_deny(item, self.version, "policy_evaluation_error") for item in items)

    def _validate_request(self, request: ContextRequest) -> str | None:
        if request.actor not in self.known_actors:
            return "unknown_actor"
        if request.purpose not in self.known_purposes:
            return "unknown_purpose"
        if request.destination not in self.known_destinations:
            return "unknown_destination"
        if any(t not in self.known_resource_types for t in request.requested_types):
            return "unknown_requested_type"
        if request.interaction == "background" and request.user_present:
            return "invalid_interaction"
        return None

    def _evaluate_item(
        self,
        request: ContextRequest,
        item: ContextItem,
        rules: tuple[PolicyRule, ...],
        grants: tuple[ConsentGrant, ...],
    ) -> PolicyDecision:
        if item.resource_type not in self.known_resource_types:
            return _deny(item, self.version, "unknown_resource_type")
        if item.sensitivity not in tuple(Sensitivity):
            return _deny(item, self.version, "unknown_label")

        matching = [rule for rule in rules if rule.active and self._matches(rule, request, item)]
        matching.sort(
            key=lambda rule: (PRECEDENCE[rule.precedence], rule.priority, rule.rule_id),
            reverse=True,
        )
        selected = matching[0] if matching else None
        selected_overrides_consent = selected is not None and selected.precedence in {
            RulePrecedence.EXPLICIT_DENY,
            RulePrecedence.USER_RESOURCE_OVERRIDE,
        }
        validated_named_grant: ConsentGrant | None = None
        if request.consent_grant_id and not selected_overrides_consent:
            named_grant = next(
                (
                    grant
                    for grant in grants
                    if grant.id == request.consent_grant_id
                ),
                None,
            )
            failure = self._grant_failure(request, item, named_grant)
            if failure:
                return _deny(item, self.version, failure)
            validated_named_grant = named_grant

        if selected is not None:
            effect = selected.effect
            reasons = (selected.reason_code, f"rule:{selected.rule_id}")
            transform = selected.transformation
        else:
            effect, reasons = self._system_default(request, item)
            transform = None
        if effect == PolicyEffect.TRANSFORM and transform not in self.known_transformations:
            return _deny(item, self.version, "unknown_transformation")

        # The original entry-level gate remains a final deny layer during and
        # after rollout. Finer rules may reduce content, never silently widen it.
        if request.destination.startswith("cloud:") and item.sensitivity in {
            Sensitivity.SENSITIVE,
            Sensitivity.UNCLASSIFIED,
        }:
            effect = PolicyEffect.LOCAL_ONLY
            reasons = ("entry_gate_final_deny", f"sensitivity:{item.sensitivity}")
            transform = None

        if (
            request.destination.startswith("cloud:")
            and request.purpose not in AGENT_PURPOSES
            and item.source in LOCAL_ONLY_SOURCES
        ):
            effect = PolicyEffect.LOCAL_ONLY
            reasons = ("local_only_source", f"source:{item.source}")
            transform = None

        # An entry can be labelled `personal` — ordinary, cloud-eligible — and
        # still be about a diagnosis, a lawyer, or a child. Leaving anything the
        # user explicitly denied alone, this only ever turns an allow into an
        # ask; it never widens a decision.
        confidential = self._confidential_categories(request, item, effect)
        if confidential:
            # Nobody is there to answer a background request, and an ask that
            # cannot be answered is a deny wearing better clothes. Say the true
            # thing: this stays local. Timeline enrichment and graph extraction
            # therefore never send this material to a cloud model unattended.
            interactive = request.user_present and request.interaction == "interactive"
            effect = PolicyEffect.ASK if interactive else PolicyEffect.LOCAL_ONLY
            reasons = (
                *reasons,
                "confidential_category_requires_confirmation"
                if interactive
                else "confidential_category_not_interactive",
                *(f"category:{name}" for name in confidential),
            )

        if effect == PolicyEffect.ASK:
            grant = self._matching_grant(request, item, grants)
            if grant is not None:
                effect = PolicyEffect.ALLOW
                reasons = (*reasons, "active_consent_grant", f"grant:{grant.id}")
        elif validated_named_grant is not None and effect not in {
            PolicyEffect.DENY,
            PolicyEffect.LOCAL_ONLY,
        }:
            reasons = (
                *reasons,
                "validated_consent_grant",
                f"grant:{validated_named_grant.id}",
            )

        return PolicyDecision(
            item_id=item.item_id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            effect=effect,
            reason_codes=reasons,
            policy_version=self.version,
            transformation=transform,
            input_hash=item.input_hash,
        )

    @staticmethod
    def _confidential_categories(
        request: ContextRequest, item: ContextItem, effect: PolicyEffect
    ) -> tuple[str, ...]:
        """Categories that make this disclosure one the user must be asked about.

        Nothing leaves the machine on a `local` destination, so the question is
        not worth asking there. A decision already at deny or local_only is left
        exactly as it is: this layer's only power is to raise a question.
        """
        if not request.destination.startswith("cloud:"):
            return ()
        if effect in {PolicyEffect.DENY, PolicyEffect.LOCAL_ONLY, PolicyEffect.ASK}:
            return ()
        return item_categories(item)

    @staticmethod
    def _matches(rule: PolicyRule, request: ContextRequest, item: ContextItem) -> bool:
        dimensions = (
            (rule.actor, request.actor),
            (rule.purpose, request.purpose),
            (rule.destination, request.destination),
            (rule.resource_type, item.resource_type),
            (rule.resource_id, item.resource_id),
            (rule.source, item.source),
            (str(rule.sensitivity) if rule.sensitivity else None, str(item.sensitivity)),
        )
        return all(expected is None or expected == actual for expected, actual in dimensions)

    @staticmethod
    def _system_default(
        request: ContextRequest, item: ContextItem
    ) -> tuple[PolicyEffect, tuple[str, ...]]:
        if request.destination == "local":
            return PolicyEffect.ALLOW, ("authorized_local_processing",)
        if item.sensitivity in (Sensitivity.SENSITIVE, Sensitivity.UNCLASSIFIED):
            return PolicyEffect.LOCAL_ONLY, (
                "entry_gate_final_deny",
                f"sensitivity:{item.sensitivity}",
            )
        if request.destination.startswith("cloud:") and request.purpose in {
            "answer_question",
            "enrich_timeline",
            "graph_model_extraction",
            "personal_model_observation",
        }:
            return PolicyEffect.ALLOW, (
                "cloud_eligible_label",
                f"sensitivity:{item.sensitivity}",
            )
        if item.sensitivity == Sensitivity.PUBLIC:
            return PolicyEffect.ALLOW, ("public_resource",)
        if request.user_present and request.interaction == "interactive":
            return PolicyEffect.ASK, ("external_personal_requires_approval",)
        return PolicyEffect.DENY, ("system_default_deny",)

    def _matching_grant(
        self,
        request: ContextRequest,
        item: ContextItem,
        grants: tuple[ConsentGrant, ...],
    ) -> ConsentGrant | None:
        for grant in sorted(grants, key=lambda value: value.id):
            if request.consent_grant_id and grant.id != request.consent_grant_id:
                continue
            if self._grant_failure(request, item, grant) is None:
                return grant
        return None

    def _grant_failure(
        self,
        request: ContextRequest,
        item: ContextItem,
        grant: ConsentGrant | None,
    ) -> str | None:
        if grant is None:
            return "consent_grant_missing"
        now = self._now or datetime.now(UTC)
        if grant.revoked_at is not None:
            return "consent_revoked"
        if not grant.is_active(now):
            return "consent_expired_or_inactive"
        if (
            grant.actor != request.actor
            or grant.destination != request.destination
            or grant.purpose != request.purpose
        ):
            return "consent_scope_mismatch"
        if grant.request_id and grant.request_id != request.request_id:
            return "consent_scope_mismatch"
        # Standing consent covers routine evidence. A grant that is not bound to
        # this one request cannot carry medical, sexual, legal, detailed
        # financial, children's, or in-confidence material — approving that once
        # in March is not approval in June.
        if not grant.request_id and item_categories(item):
            return "consent_requires_one_time"
        if grant.conversation_id and grant.conversation_id != request.conversation_id:
            return "consent_scope_mismatch"
        if grant.resource_type and grant.resource_type != item.resource_type:
            return "consent_scope_mismatch"
        if grant.resource_id and grant.resource_id != item.resource_id:
            return "consent_scope_mismatch"
        entity_ids = _metadata_values(item, "entity_id", "entity_ids")
        if item.resource_type == "entity":
            entity_ids.add(item.resource_id)
        if grant.entity_id and grant.entity_id not in entity_ids:
            return "consent_scope_mismatch"
        topics = _metadata_values(item, "topic", "topics")
        if grant.topic and grant.topic not in topics:
            return "consent_scope_mismatch"
        if grant.scope_valid_from is not None or grant.scope_valid_to is not None:
            item_from = _metadata_time(
                item, "valid_from", "occurred_at", "observed_at"
            )
            item_to = _metadata_time(item, "valid_to") or item_from
            if item_from is None and item_to is None:
                return "consent_scope_mismatch"
            if (
                grant.scope_valid_from is not None
                and item_to is not None
                and item_to < grant.scope_valid_from
            ):
                return "consent_scope_mismatch"
            if (
                grant.scope_valid_to is not None
                and item_from is not None
                and item_from > grant.scope_valid_to
            ):
                return "consent_scope_mismatch"
        if grant.max_items is not None and grant.used_items >= grant.max_items:
            return "consent_budget_exhausted"
        if (
            grant.max_chars is not None
            and grant.used_chars + len(item.content) > grant.max_chars
        ):
            return "consent_budget_exhausted"
        return None

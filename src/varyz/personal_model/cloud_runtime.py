"""Explicitly authorized cloud observation runtime with fail-closed local routing.

Every cloud call is prepared and executed by :class:`EgressBroker`.  A group is
cloud-eligible only when every source entry is public or personal; a single
sensitive, unclassified, or missing label routes the complete group locally so
conversation context is never partially disclosed or silently weakened.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any

from varyz.config import Config
from varyz.destinations.llm import LLMDestinationAdapter
from varyz.domain import Sensitivity, new_id
from varyz.personal_model import annotation_wire as wire
from varyz.personal_model.compiler import (
    CompilerPolicy,
    ExecutionLocality,
    PersonalModelCompiler,
    build_personal_model_compiler,
)
from varyz.personal_model.observations import (
    OBSERVATION_SCHEMA_VERSION,
    AnnotationBatchRequest,
    SemanticAnnotatorDescriptor,
)
from varyz.personal_model.semantic_runtime import (
    ProviderBuilder,
    build_local_semantic_runtime,
    group_annotation_system,
)
from varyz.privacy.broker import EgressBroker, EgressDenied
from varyz.privacy.models import ContextItem, ContextRequest, Interaction, LabelSource
from varyz.privacy.policy import LOCAL_ONLY_SOURCES, PolicyEngine
from varyz.privacy.receipts import ReceiptStore

log = logging.getLogger(__name__)


class CloudAnnotationBlocked(PermissionError):
    """The broker did not authorize the exact annotation payload."""


def fallback_allowed(error: Exception) -> bool:
    """Fail over only for policy, quota, timeout, or provider availability."""

    if isinstance(error, (CloudAnnotationBlocked, EgressDenied, TimeoutError)):
        return True
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "429",
            "rate limit",
            "rate_limit",
            "quota",
            "overloaded",
            "temporarily unavailable",
            "service unavailable",
        )
    )


def _json_object(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model reply contained no JSON object")
    return raw[start : end + 1]


class BrokerSemanticAnnotator:
    """Grouped frontier annotator whose only transport is ``EgressBroker``."""

    execution_locality = ExecutionLocality.EXTERNAL

    def __init__(
        self,
        conn: sqlite3.Connection,
        broker: EgressBroker,
        *,
        destination: str,
        model_name: str,
        estimated_spend_usd: float = 0.0,
        spend_remaining_usd: float | None = None,
        episodes_per_call: int = 8,
        max_context_chars: int = 32_000,
    ) -> None:
        if not destination.startswith("cloud:"):
            raise ValueError("frontier annotation destination must start with 'cloud:'")
        if estimated_spend_usd < 0:
            raise ValueError("estimated spend cannot be negative")
        if spend_remaining_usd is not None and spend_remaining_usd < 0:
            raise ValueError("remaining spend cannot be negative")
        if episodes_per_call < 1:
            raise ValueError("episodes per frontier call must be positive")
        if max_context_chars < 1:
            raise ValueError("frontier context budget must be positive")
        self.conn = conn
        self.broker = broker
        self.destination = destination
        self.model_name = model_name
        self.estimated_spend_usd = estimated_spend_usd
        self.spend_remaining_usd = spend_remaining_usd
        self.episodes_per_call = episodes_per_call
        self.max_context_chars = max_context_chars
        self._estimated_spend_used_usd = 0.0
        self._spend_lock = Lock()
        self.descriptor = SemanticAnnotatorDescriptor(
            name="egress-broker-observation-annotator",
            version="1",
            model_name=model_name,
            prompt_version="personal-model-observations-v5-frontier-v1",
            schema_version=OBSERVATION_SCHEMA_VERSION,
        )

    def _labels(self, request: AnnotationBatchRequest) -> dict[str, Sensitivity]:
        ids = tuple(source.entry_id for source in request.sources)
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id,sensitivity FROM entries WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {str(row["id"]): Sensitivity(row["sensitivity"]) for row in rows}

    def cloud_eligible(self, request: AnnotationBatchRequest) -> bool:
        labels = self._labels(request)
        return (
            len(labels) == len(request.sources)
            and all(labels[source.entry_id].cloud_ok for source in request.sources)
            and not self._local_only_source(request)
        )

    def _local_only_source(self, request: AnnotationBatchRequest) -> bool:
        """Whether any entry comes from a source that is never sent to a cloud model."""
        ids = tuple(source.entry_id for source in request.sources)
        placeholders = ",".join("?" for _ in ids)
        sources = LOCAL_ONLY_SOURCES
        marks = ",".join("?" for _ in sources)
        return (
            self.conn.execute(
                f"SELECT 1 FROM entries WHERE id IN ({placeholders}) "  # noqa: S608 - placeholders only
                f"AND source IN ({marks}) LIMIT 1",
                (*ids, *sorted(sources)),
            ).fetchone()
            is not None
        )

    def annotate(self, request: AnnotationBatchRequest) -> str:
        return self.annotate_batch((request,))[request.episode_id]

    @property
    def estimated_spend_used_usd(self) -> float:
        with self._spend_lock:
            return self._estimated_spend_used_usd

    def groups(
        self, requests: Sequence[AnnotationBatchRequest]
    ) -> tuple[tuple[AnnotationBatchRequest, ...], ...]:
        """Bound call size by both episode count and disclosed characters."""

        groups: list[tuple[AnnotationBatchRequest, ...]] = []
        current: list[AnnotationBatchRequest] = []
        current_chars = 0
        for request in requests:
            single, _ = wire.compact_group_request((request,))
            episode_chars = len(single.episodes[0].model_dump_json())
            if episode_chars > self.max_context_chars:
                if current:
                    groups.append(tuple(current))
                    current = []
                    current_chars = 0
                groups.append((request,))
                continue
            if current and (
                len(current) >= self.episodes_per_call
                or current_chars + episode_chars > self.max_context_chars
            ):
                groups.append(tuple(current))
                current = []
                current_chars = 0
            current.append(request)
            current_chars += episode_chars
        if current:
            groups.append(tuple(current))
        return tuple(groups)

    def annotate_batch(self, requests: tuple[AnnotationBatchRequest, ...]) -> Mapping[str, str]:
        result: dict[str, str] = {}
        for group in self.groups(requests):
            result.update(self.annotate_group(group))
        return result

    def annotate_group(self, requests: tuple[AnnotationBatchRequest, ...]) -> Mapping[str, str]:
        if not requests:
            return {}
        if len(requests) > self.episodes_per_call:
            raise ValueError("frontier annotation group exceeds its episode budget")
        if not all(self.cloud_eligible(request) for request in requests):
            raise CloudAnnotationBlocked(
                "cloud annotation group contains sensitive, unclassified, or missing entries"
            )

        payload, legend = wire.compact_group_request(requests)
        items: list[ContextItem] = []
        for request, episode in zip(requests, payload.episodes, strict=True):
            labels = self._labels(request)
            sensitivity = (
                Sensitivity.PERSONAL
                if Sensitivity.PERSONAL in labels.values()
                else Sensitivity.PUBLIC
            )
            content = episode.model_dump_json()
            items.append(
                ContextItem(
                    item_id=f"annotation:{request.dependency_fingerprint}",
                    resource_type="personal_model_episode",
                    resource_id=request.episode_id,
                    content=content,
                    sensitivity=sensitivity,
                    label_source=LabelSource.INHERITED,
                    label_reasons=("all_episode_sources_cloud_eligible",),
                    source_ids=tuple(source.entry_id for source in request.sources),
                    metadata={"target_entry_ids": request.target_entry_ids},
                )
            )
        disclosed_chars = sum(len(item.content) for item in items)
        if disclosed_chars > self.max_context_chars:
            raise CloudAnnotationBlocked(
                "frontier annotation group exceeds the disclosure character budget"
            )

        with self._spend_lock:
            spend_remaining = (
                None
                if self.spend_remaining_usd is None
                else max(0.0, self.spend_remaining_usd - self._estimated_spend_used_usd)
            )

        intent = f"Annotate {len(requests)} bounded personal-model episodes"
        parameters: dict[str, Any] = {
            "payload_kind": "llm",
            "system_prompt": group_annotation_system(len(requests)),
            "user_prefix": '{"episodes":[',
            "user_suffix": "]}",
            "context_separator": ",",
            "estimated_spend_usd": self.estimated_spend_usd,
        }
        if spend_remaining is not None:
            parameters["spend_remaining_usd"] = spend_remaining
        context_request = ContextRequest(
            request_id=new_id(),
            actor="local_user",
            purpose="personal_model_observation",
            destination=self.destination,
            intent=intent,
            intent_hash=hashlib.sha256(intent.encode()).hexdigest(),
            requested_types=("evidence_excerpt",),
            requested_scope={
                "episode_ids": [request.episode_id for request in requests],
                "entry_ids": sorted(
                    {source.entry_id for request in requests for source in request.sources}
                ),
            },
            max_items=len(items),
            max_chars=disclosed_chars,
            interaction=Interaction.INTERACTIVE,
            user_present=True,
            reversible=True,
            parameters=parameters,
        )
        prepared = self.broker.prepare(context_request, tuple(items))
        if prepared.blocked or len(prepared.items) != len(items):
            raise CloudAnnotationBlocked(
                "privacy policy did not authorize every episode in the cloud group"
            )
        # Reserve the estimate immediately before transport.  A timeout or bad
        # response may still have consumed provider tokens, so only a
        # pre-transport policy block is free.
        with self._spend_lock:
            current_remaining = (
                None
                if self.spend_remaining_usd is None
                else self.spend_remaining_usd - self._estimated_spend_used_usd
            )
            if current_remaining is not None and self.estimated_spend_usd > current_remaining:
                raise CloudAnnotationBlocked("frontier annotation spend budget is exhausted")
            self._estimated_spend_used_usd += self.estimated_spend_usd
        result = self.broker.execute(prepared)
        if result.state != "succeeded" or result.response is None:
            raise CloudAnnotationBlocked(
                f"cloud annotation did not execute: {result.result_category}"
            )
        raw = str(result.response)
        parsed = wire.WireGroupResponse.model_validate_json(_json_object(raw))
        by_episode, unknown, refused = wire.expand_group_response(parsed, legend)
        answered = {
            episode_id: json.dumps(
                wire.episode_payload(episode_id, candidates),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for episode_id, candidates in by_episode.items()
        }
        if len(answered) < len(requests) or refused:
            # The tolerance the local path has had all along, and this one has
            # more reason for it: the group has already been paid for.  One
            # episode the model would not answer used to discard every other
            # answer in the group, which then had to be asked -- and bought --
            # again on the next run.  The caller reports what is missing and
            # quarantines it one episode at a time.
            #
            # Counts only.  A refusal names the text that failed it, and on
            # this path that text has just come back from another company's
            # model.
            log.warning(
                "frontier annotation returned %d of %d episodes (unknown=%d refused=%d)",
                len(answered),
                len(requests),
                len(unknown),
                len(refused),
            )
        if not answered:
            raise ValueError(
                f"frontier annotation answered none of {len(requests)} episodes"
            )
        return answered


class HybridSemanticAnnotator:
    """Route whole episodes to frontier or local without widening disclosure."""

    # A hybrid is external for compiler safety even when a particular batch is
    # routed locally.  This makes background compilation reject it before any
    # payload can be prepared; cloud runs must be explicit and user-present.
    execution_locality = ExecutionLocality.EXTERNAL

    def __init__(self, cloud: BrokerSemanticAnnotator, local: Any) -> None:
        self.cloud = cloud
        self.local = local
        self.routes: Counter[str] = Counter()
        local_descriptor = local.descriptor
        self.descriptor = SemanticAnnotatorDescriptor(
            name="privacy-gated-hybrid-observation-annotator",
            version="1",
            model_name=f"{cloud.model_name}+{local_descriptor.model_name}",
            prompt_version="personal-model-observations-v5-hybrid-v1",
            schema_version=OBSERVATION_SCHEMA_VERSION,
        )

    def annotate(self, request: AnnotationBatchRequest) -> str:
        return self.annotate_batch((request,))[request.episode_id]

    def annotate_batch(self, requests: tuple[AnnotationBatchRequest, ...]) -> Mapping[str, str]:
        cloud_requests = tuple(
            request for request in requests if self.cloud.cloud_eligible(request)
        )
        cloud_ids = {id(request) for request in cloud_requests}
        local_requests = tuple(request for request in requests if id(request) not in cloud_ids)
        result: dict[str, str] = {}
        if cloud_requests:
            for group in self.cloud.groups(cloud_requests):
                try:
                    result.update(self.cloud.annotate_group(group))
                    self.routes["cloud"] += len(group)
                except Exception as exc:
                    # Availability, quota, or consent expiry may degrade locally;
                    # a bad request or malformed output must stay loud.
                    if not fallback_allowed(exc):
                        raise
                    result.update(self.local.annotate_batch(group))
                    self.routes["local_fallback"] += len(group)
        if local_requests:
            result.update(self.local.annotate_batch(local_requests))
            self.routes["local_sensitive"] += len(local_requests)
        return result


@dataclass(frozen=True)
class HybridSemanticCompiler:
    compiler: PersonalModelCompiler
    local_model_name: str
    frontier_model_name: str
    destination: str


def _uses_subscription_auth(provider: Any) -> bool:
    """Recognize the two OAuth-backed providers returned by Varyz' factory."""

    return bool(getattr(provider, "oauth", False)) or (
        provider.__class__.__module__ == "varyz.llm.openai_oauth"
    )


def build_hybrid_semantic_compiler(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    provider_builder: ProviderBuilder | None = None,
    cloud_provider: Any | None = None,
    estimated_spend_usd_per_call: float | None = None,
    spend_cap_usd: float | None = None,
    episodes_per_call: int = 8,
    max_context_chars: int | None = None,
    **compiler_options: Any,
) -> HybridSemanticCompiler:
    """Compose explicit user-present frontier observations with local reasoning.

    Provider construction uses Varyz' OAuth-first factory.  API-key fallback is
    accepted only with a positive caller-supplied estimate, so the broker can
    enforce a cumulative cap rather than treating unknown spend as zero.
    Candidate projection and synthesis remain managed-local.
    """

    local_runtime = build_local_semantic_runtime(cfg, provider_builder=provider_builder)
    if cloud_provider is None:
        from varyz.llm.router import LLMRouter

        cloud_provider = LLMRouter(cfg).enrich_provider()
    subscription = _uses_subscription_auth(cloud_provider)
    if estimated_spend_usd_per_call is None:
        if not subscription:
            raise ValueError(
                "API-key frontier annotation requires a positive estimated spend per call"
            )
        estimated_spend_usd_per_call = 0.0
    if not subscription and estimated_spend_usd_per_call <= 0:
        raise ValueError("API-key frontier annotation requires a positive estimated spend per call")

    destination = f"cloud:{cfg.llm.provider}"
    frontier_model = str(getattr(cloud_provider, "model", cfg.llm.models_for().enrich_model))
    broker = EgressBroker(
        ReceiptStore(conn),
        policy=PolicyEngine(version=cfg.privacy.policy_version),
        adapters=(LLMDestinationAdapter(destination, cloud_provider, stream=False),),
        mode=cfg.privacy.mode,
    )
    cloud = BrokerSemanticAnnotator(
        conn,
        broker,
        destination=destination,
        model_name=frontier_model,
        estimated_spend_usd=estimated_spend_usd_per_call,
        spend_remaining_usd=(
            cfg.llm.monthly_spend_cap_usd if spend_cap_usd is None else spend_cap_usd
        ),
        episodes_per_call=episodes_per_call,
        max_context_chars=max_context_chars or cfg.privacy.max_context_chars,
    )
    hybrid = HybridSemanticAnnotator(cloud, local_runtime.annotator)
    selected_policy = compiler_options.pop("policy", None) or CompilerPolicy(
        user_present=True,
        allow_cloud_egress=True,
        require_observation_annotator=True,
        allow_literal_only_activation=True,
    )
    if not selected_policy.user_present or not selected_policy.allow_cloud_egress:
        raise ValueError(
            "hybrid semantic compilation requires user-present cloud-egress authorization"
        )
    selected_policy = selected_policy.model_copy(
        update={
            "observation_batch_size": min(
                256,
                8 * max(1, cfg.local_model.parallel_slots) * 4,
            )
        }
    )
    compiler = build_personal_model_compiler(
        conn,
        cfg,
        policy=selected_policy,
        annotator=hybrid,
        candidate_adapter=local_runtime.candidate_adapter,
        synthesis_adapter=local_runtime.synthesis_adapter,
        **compiler_options,
    )
    return HybridSemanticCompiler(
        compiler=compiler,
        local_model_name=local_runtime.model_name,
        frontier_model_name=frontier_model,
        destination=destination,
    )


__all__ = [
    "BrokerSemanticAnnotator",
    "CloudAnnotationBlocked",
    "HybridSemanticAnnotator",
    "HybridSemanticCompiler",
    "build_hybrid_semantic_compiler",
    "fallback_allowed",
]

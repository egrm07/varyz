"""Bounded, read-only lookup over the active compiled personal model.

This module deliberately cannot compile knowledge.  It resolves entities first,
reads typed records from the active generation, and optionally exposes raw semantic
search hits under an explicitly uncompiled result type.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from varyz.domain import Sensitivity
from varyz.personal_model.memory import EVENT_KINDS, FACT_VALUE_KINDS, PREFERENCE_PREDICATES
from varyz.personal_model.models import (
    Event,
    Fact,
    Goal,
    Interpretation,
    Person,
    Preference,
    PreferenceValence,
    Project,
    RelationshipSnapshot,
)
from varyz.personal_model.projects import is_a_relationship_label, names_something
from varyz.privacy.broker import EgressBroker
from varyz.privacy.models import ContextItem, ContextRequest, LabelSource, PolicyEffect
from varyz.privacy.policy import item_categories

MAX_QUERY_LIMIT = 50
MAX_CANDIDATE_ROWS = 200
MAX_ENTITY_CHOICES = 50

THIRD_PARTY_RELATIONSHIP_NECESSITY_RULE_VERSION = "1"
THIRD_PARTY_RELATIONSHIP_NECESSITY_REASON = (
    "third_party_relationship_necessity_not_established"
)
THIRD_PARTY_ONE_TIME_PERMISSION_REASON = (
    "third_party_sensitive_requires_one_time_permission"
)

# These are the canonical record shapes that already encode an owner/other-person
# relationship or an owner commitment. Facts and preferences never qualify merely
# because knowing more about somebody might someday be useful.
RELATIONSHIP_NECESSARY_RECORD_KINDS = frozenset({"event", "relationship_snapshot"})
RELATIONSHIP_NECESSARY_PREDICATES: dict[str, frozenset[str]] = {
    "observation": frozenset({"commitment"}),
    "interpretation": frozenset({"relationship"}),
}


class QueryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class LifeRelation(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"
    PERSON = "person"
    PROJECT_GOAL = "project_goal"
    ACTION_HANDOFF = "action_handoff"
    COMMITMENT = "commitment"
    RELATIONSHIP_HISTORY = "relationship_history"
    INTERPRETATION = "interpretation"


PREDICATE_ALIASES = {
    "birthday": "person.birth_date",
    "birth_date": "person.birth_date",
    "date_of_birth": "person.birth_date",
    "dob": "person.birth_date",
    "current_location": "person.current_location",
    "location": "person.current_location",
    "full_name": "person.full_name",
    "name": "person.full_name",
    "role": "person.role",
    "constraint": "person.constraint",
    "deadline": "project.deadline",
}

ALLOWED_PREDICATES_BY_RELATION: dict[LifeRelation, frozenset[str]] = {
    LifeRelation.FACT: frozenset((*FACT_VALUE_KINDS, "project.purpose", "goal.status")),
    LifeRelation.PREFERENCE: frozenset(PREFERENCE_PREDICATES),
    LifeRelation.EVENT: frozenset(EVENT_KINDS),
    LifeRelation.PERSON: frozenset(),
    LifeRelation.PROJECT_GOAL: frozenset(),
    LifeRelation.ACTION_HANDOFF: frozenset(),
    LifeRelation.COMMITMENT: frozenset(),
    LifeRelation.RELATIONSHIP_HISTORY: frozenset(),
    LifeRelation.INTERPRETATION: frozenset(),
}


# What a record's readiness has to be for an answer to be built from it. Named
# once because the row query both applies it and, on an empty result, lifts it
# back out to tell "not compiled yet" apart from "not known".
VISIBLE_READINESS_CLAUSE = "record.readiness IN ('ready','limited','superseded')"


class QueryStatus(StrEnum):
    ACTIVE = "active"
    CURRENT = "current"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    PROPOSED = "proposed"
    STALLED = "stalled"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"
    READY = "ready"
    LIMITED = "limited"


class QueryDisposition(StrEnum):
    ANSWERED = "answered"
    AMBIGUOUS = "ambiguous"
    NOT_KNOWN = "not_known"
    CLARIFICATION = "clarification"


class AnswerBasis(StrEnum):
    EXPLICIT = "explicit"
    REPORTED = "reported"
    OBSERVED = "observed"
    INFERRED = "inferred"
    USER_CONFIRMED = "user_confirmed"
    ESCALATED = "escalated"


class EntitySelector(QueryModel):
    value: str = Field(min_length=1, max_length=200)
    kind: str | None = Field(default=None, pattern=r"^(person|project|goal)$")

    @field_validator("value")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("entity selector cannot be blank")
        return value.strip()


class LifeQuery(QueryModel):
    entities: tuple[EntitySelector, ...] = Field(default=(), max_length=2)
    relation: LifeRelation
    predicate: str | None = Field(
        default=None, max_length=100, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
    )
    concept: str | None = Field(
        default=None, max_length=100, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
    )
    preference_valence: PreferenceValence | None = None
    scope_label: str | None = Field(default=None, min_length=1, max_length=160)
    time_from: datetime | None = None
    time_to: datetime | None = None
    statuses: tuple[QueryStatus, ...] = ()
    limit: int = Field(default=20, ge=1, le=MAX_QUERY_LIMIT)

    @field_validator("time_from", "time_to")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("query times must be timezone-aware")
        return value

    @field_validator("predicate", mode="before")
    @classmethod
    def canonical_predicate(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold().replace(" ", "_").replace("-", "_")
        if info.data.get("relation") == LifeRelation.FACT:
            return PREDICATE_ALIASES.get(normalized, normalized)
        return normalized

    @model_validator(mode="after")
    def coherent(self) -> LifeQuery:
        if self.time_from and self.time_to and self.time_to < self.time_from:
            raise ValueError("time_to precedes time_from")
        if len({(item.kind, normalize_alias(item.value)) for item in self.entities}) != len(
            self.entities
        ):
            raise ValueError("duplicate entity selectors are not useful")
        if self.preference_valence is not None and self.relation != LifeRelation.PREFERENCE:
            raise ValueError("preference_valence is valid only for preference queries")
        if self.scope_label is not None and self.relation != LifeRelation.PREFERENCE:
            raise ValueError("scope_label is valid only for preference queries")
        if self.concept is not None and self.relation != LifeRelation.INTERPRETATION:
            raise ValueError("concept is valid only for interpretation queries")
        allowed = ALLOWED_PREDICATES_BY_RELATION[self.relation]
        if self.predicate is not None and self.predicate not in allowed:
            raise ValueError(
                f"predicate {self.predicate!r} is not supported for relation {self.relation}"
            )
        return self


class ResolvedEntity(QueryModel):
    entity_kind: str
    entity_id: str
    display_name: str
    matched_alias: str
    resolution_tier: int = Field(ge=0, le=2)
    # The stored name stayed behind and this entity came back under the word the
    # caller used for it. Nothing was dropped, so this is not a withheld record:
    # it is the one thing the sensitivity gate buys on a first-person question.
    display_name_withheld: bool = False


class AmbiguityChoice(QueryModel):
    entity_kind: str
    entity_id: str
    display_name: str
    matched_alias: str


class QueryProvenance(QueryModel):
    generation_id: str
    record_id: str
    entry_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()


class TypedAnswer(QueryModel):
    relation: LifeRelation
    record_kind: str
    subject_entity_ids: tuple[str, ...]
    predicate_or_concept: str | None
    value_kind: str
    display_value: str
    normalized_value: str | None = None
    preference_valence: PreferenceValence | None = None
    scope_kind: str | None = None
    scope_label: str | None = None
    assertion_kind: str | None = None
    basis: AnswerBasis
    status: str
    confidence: float = Field(ge=0, le=1)
    readiness: str
    observed_at: datetime | None = None
    stated_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    original_time_literal: str | None = None
    possibly_outdated: bool = False
    conflict_record_ids: tuple[str, ...] = ()
    sensitivity: str
    provenance: QueryProvenance


class WithheldRecordMarker(QueryModel):
    record_kind: str
    predicate_or_concept: str | None = None
    reason: str
    rule_version: str
    categories: tuple[str, ...] = ()
    policy_reason_codes: tuple[str, ...] = ()
    request_id: str | None = None


class UncompiledEvidenceCandidate(QueryModel):
    entry_id: str
    excerpt: str = Field(max_length=1000)
    source: str | None = None
    occurred_at: datetime | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    sensitivity: Sensitivity
    label: str = "uncompiled_evidence_candidate"


class LifeQueryResult(QueryModel):
    disposition: QueryDisposition
    generation_id: str | None
    query: LifeQuery | None = None
    resolved_entities: tuple[ResolvedEntity, ...] = ()
    answers: tuple[TypedAnswer, ...] = ()
    ambiguity_choices: tuple[AmbiguityChoice, ...] = ()
    clarification: str | None = None
    not_known: bool = False
    staged_records_withheld: bool = False
    """Records matched the question but none of them has been promoted yet.

    Different from `not_known` in the way that matters to whoever reads the
    answer: the archive holds this and has not finished compiling it, which is
    a thing to fix rather than a fact about the owner's life.
    """
    sensitive_records_withheld: bool = False
    withheld_records: tuple[WithheldRecordMarker, ...] = ()
    egress_request_id: str | None = None
    uncompiled_evidence_candidates: tuple[UncompiledEvidenceCandidate, ...] = ()


class QueryTelemetry(QueryModel):
    relation: LifeRelation | None
    entity_count: int = Field(ge=0, le=2)
    has_predicate: bool
    has_concept: bool
    has_time_filter: bool
    has_status_filter: bool
    requested_limit: int = Field(ge=0, le=MAX_QUERY_LIMIT)
    disposition: QueryDisposition
    result_count: int = Field(ge=0, le=MAX_QUERY_LIMIT)
    elapsed_ms_bucket: str


class SemanticEvidenceSearchPort(Protocol):
    def search(
        self, query: LifeQuery, *, resolved_entities: tuple[ResolvedEntity, ...], limit: int
    ) -> Sequence[UncompiledEvidenceCandidate]: ...


class IntentParserPort(Protocol):
    def parse(self, text: str) -> LifeQuery | dict[str, Any] | None: ...


TelemetrySink = Callable[[QueryTelemetry], None]


class TrustedLocalSurface(StrEnum):
    CLI = "cli"
    LOOPBACK_UI = "loopback_ui"


_SENSITIVITY_CAPABILITY_TOKEN = object()


class TrustedLocalSensitivityCapability:
    """Opaque authority intentionally unavailable as a query-schema boolean."""

    __slots__ = ("surface",)

    def __init__(self, token: object, surface: TrustedLocalSurface) -> None:
        if token is not _SENSITIVITY_CAPABILITY_TOKEN:
            raise TypeError("sensitivity capabilities must be issued for a trusted local surface")
        self.surface = surface


def issue_trusted_local_sensitivity_capability(
    surface: TrustedLocalSurface,
) -> TrustedLocalSensitivityCapability:
    """Issue restricted-read authority only to an explicit local CLI/UI adapter."""
    if not isinstance(surface, TrustedLocalSurface):
        raise TypeError("restricted sensitivity reads are limited to trusted local surfaces")
    return TrustedLocalSensitivityCapability(_SENSITIVITY_CAPABILITY_TOKEN, surface)


def normalize_alias(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(folded.split())


# "me" is nobody's alias, so it is in no alias row and resolves to nothing. The
# owner is the one person the archive knows structurally, by the flag the compiler
# sets on their person record, and a question about oneself is the question this
# tool exists to answer.
FIRST_PERSON_SELF = frozenset({"i", "me", "my", "mine", "myself"})
_OWNER: tuple[EntitySelector, ...] = (EntitySelector(value="me", kind="person"),)

_PATTERNS: tuple[tuple[re.Pattern[str], dict[str, Any]], ...] = (
    (
        re.compile(r"^what are my (?:design|visual) preferences$", re.I),
        {
            "relation": LifeRelation.PREFERENCE,
            "entities": _OWNER,
            "predicate": "preference.visual",
        },
    ),
    (
        re.compile(r"^what are my workflow preferences$", re.I),
        {
            "relation": LifeRelation.PREFERENCE,
            "entities": _OWNER,
            "predicate": "preference.workflow",
        },
    ),
    (
        re.compile(r"^what are my (?:communication|answer|writing) preferences$", re.I),
        {
            "relation": LifeRelation.PREFERENCE,
            "entities": _OWNER,
            "predicate": "preference.communication",
        },
    ),
    (
        re.compile(r"^what (?:are )?my preferences(?: for (?P<scope>.+))?$", re.I),
        {"relation": LifeRelation.PREFERENCE, "entities": _OWNER},
    ),
    (
        re.compile(r"^what do i prefer(?: for (?P<scope>.+))?$", re.I),
        {"relation": LifeRelation.PREFERENCE, "entities": _OWNER},
    ),
    (
        re.compile(r"^what does (?P<a>.+?) prefer(?: for (?P<scope>.+))?$", re.I),
        {"relation": LifeRelation.PREFERENCE},
    ),
    (
        re.compile(
            r"^(?:what (?:are )?)?(?:the )?(?:actions?|next steps?|handoffs?)"
            r" (?:for|on) (?P<a>.+)$",
            re.I,
        ),
        {"relation": LifeRelation.ACTION_HANDOFF},
    ),
    (
        re.compile(r"^(?:what does|what do) (?P<a>.+?) (?:like|love|enjoy)$", re.I),
        {"relation": LifeRelation.PREFERENCE, "preference_valence": PreferenceValence.LIKE},
    ),
    (
        re.compile(r"^(?:what does|what do) (?P<a>.+?) (?:dislike|hate|avoid)$", re.I),
        {
            "relation": LifeRelation.PREFERENCE,
            "preference_valence": PreferenceValence.DISLIKE,
        },
    ),
    (
        re.compile(r"^(?:when is )?(?P<a>.+?)(?:'s|’s) birthday$", re.I),
        {"relation": LifeRelation.FACT, "predicate": "person.birth_date"},
    ),
    (
        re.compile(r"^(?:when|what date) is (?P<a>.+?)(?:'s|’s) birthday$", re.I),
        {"relation": LifeRelation.FACT, "predicate": "person.birth_date"},
    ),
    (
        re.compile(
            r"^(?:what (?:projects|goals) (?:does|is) )(?P<a>.+?) (?:have|working on)$",
            re.I,
        ),
        {"relation": LifeRelation.PROJECT_GOAL},
    ),
    (
        re.compile(r"^what is (?P<a>.+?) working on$", re.I),
        {"relation": LifeRelation.PROJECT_GOAL},
    ),
    (
        re.compile(r"^(?:what happened|what did we do|events?) with (?P<a>.+)$", re.I),
        {"relation": LifeRelation.EVENT},
    ),
    (
        re.compile(r"^what did (?P<a>.+?) and (?P<b>.+?) do$", re.I),
        {"relation": LifeRelation.EVENT},
    ),
    (
        re.compile(r"^(?:what is )?(?:my |the )?relationship (?:history )?with (?P<a>.+)$", re.I),
        {"relation": LifeRelation.RELATIONSHIP_HISTORY},
    ),
    (
        re.compile(
            r"^(?:what )?(?:commitments?|promises?) (?:do i have )?(?:to|with) (?P<a>.+)$",
            re.I,
        ),
        {"relation": LifeRelation.COMMITMENT},
    ),
    (
        re.compile(
            r"^(?:what do you understand|what (?:values|patterns)) about (?P<a>.+)$",
            re.I,
        ),
        {"relation": LifeRelation.INTERPRETATION},
    ),
    (
        re.compile(r"^who is (?P<a>.+)$", re.I),
        {"relation": LifeRelation.PERSON},
    ),
    (
        re.compile(r"^(?:facts? about|what do you know about) (?P<a>.+)$", re.I),
        {"relation": LifeRelation.FACT},
    ),
    # First-person forms. Most of them need no owner clause of their own: the
    # pronoun lands in the entity group and FIRST_PERSON_SELF resolves it. Only
    # the possessive ones, where there is no group to land in, name _OWNER.
    (
        re.compile(r"^(?:where do|where does) (?P<a>.+?) live$", re.I),
        {"relation": LifeRelation.FACT, "predicate": "person.current_location"},
    ),
    (
        re.compile(r"^(?:when |what date )?is my birthday$|^my birthday$", re.I),
        {
            "relation": LifeRelation.FACT,
            "predicate": "person.birth_date",
            "entities": _OWNER,
        },
    ),
    (
        re.compile(r"^what is my (?:full )?name$", re.I),
        {"relation": LifeRelation.FACT, "predicate": "person.full_name", "entities": _OWNER},
    ),
    (
        re.compile(
            r"^what (?:am i working on|are my (?:projects|goals)"
            r"|projects am i working on|goals do i have)$",
            re.I,
        ),
        {"relation": LifeRelation.PROJECT_GOAL, "entities": _OWNER},
    ),
    (
        re.compile(
            r"^what (?:are my commitments|did i commit to|have i committed to)$", re.I
        ),
        {"relation": LifeRelation.COMMITMENT, "entities": _OWNER},
    ),
    (
        # Handoffs hang off projects and goals, not people, so this one asks the
        # generation-wide projection rather than naming the owner.
        re.compile(r"^what are my (?:open )?(?:actions?|next steps?|handoffs?)$", re.I),
        {"relation": LifeRelation.ACTION_HANDOFF},
    ),
    (
        re.compile(r"^tell me about (?P<a>.+)$", re.I),
        {"relation": LifeRelation.FACT},
    ),
    # Spanish deterministic dictionary.
    (
        re.compile(r"^qu[eé] le gusta a (?P<a>.+)$", re.I),
        {"relation": LifeRelation.PREFERENCE, "preference_valence": PreferenceValence.LIKE},
    ),
    (
        re.compile(r"^cu[aá]ndo es el cumplea[nñ]os de (?P<a>.+)$", re.I),
        {"relation": LifeRelation.FACT, "predicate": "person.birth_date"},
    ),
    (
        re.compile(r"^(?:proyectos|metas) de (?P<a>.+)$", re.I),
        {"relation": LifeRelation.PROJECT_GOAL},
    ),
    (
        re.compile(r"^(?:mi |la )?relaci[oó]n con (?P<a>.+)$", re.I),
        {"relation": LifeRelation.RELATIONSHIP_HISTORY},
    ),
    # Russian deterministic dictionary.
    (
        re.compile(r"^что (?:любит|нравится) (?P<a>.+)$", re.I),
        {"relation": LifeRelation.PREFERENCE, "preference_valence": PreferenceValence.LIKE},
    ),
    (
        re.compile(r"^когда день рождения у (?P<a>.+)$", re.I),
        {"relation": LifeRelation.FACT, "predicate": "person.birth_date"},
    ),
    (
        re.compile(r"^(?:проекты|цели) (?P<a>.+)$", re.I),
        {"relation": LifeRelation.PROJECT_GOAL},
    ),
    (
        re.compile(r"^отношения с (?P<a>.+)$", re.I),
        {"relation": LifeRelation.RELATIONSHIP_HISTORY},
    ),
)


def parse_life_query(
    text: str, *, intent_parser: IntentParserPort | None = None
) -> LifeQueryResult | LifeQuery:
    """Parse only lookup intents; unsupported prose asks for clarification."""
    cleaned = " ".join(text.strip().strip("?!¿¡. ").split())
    if not cleaned or len(cleaned) > 500 or "\x00" in cleaned:
        return _clarification("Please ask a short question about a stored life fact or relation.")
    for pattern, fields in _PATTERNS:
        match = pattern.fullmatch(cleaned)
        if match is None:
            continue
        selectors = tuple(
            EntitySelector(value=match.group(key))
            for key in ("a", "b")
            if key in match.groupdict() and match.group(key)
        )
        fields = dict(fields)
        stated = fields.pop("entities", ())
        if "scope" in match.groupdict() and match.group("scope"):
            fields["scope_label"] = match.group("scope")
        return LifeQuery(entities=selectors or stated, **fields)
    if intent_parser is not None:
        candidate = intent_parser.parse(cleaned)
        if candidate is not None:
            try:
                if isinstance(candidate, LifeQuery):
                    return LifeQuery.model_validate(candidate.model_dump(), strict=True)
                return LifeQuery.model_validate(candidate, strict=True)
            except (TypeError, ValueError):
                pass
    return _clarification(
        "I can look up stored facts, preferences, events, people, projects/goals, "
        "actions, commitments, relationship history, or existing interpretations. "
        "Ask in one of those shapes: 'Who is someone?', 'What does someone "
        "like?', 'What is someone working on?', 'What am I working on?', "
        "'Where do I live?'."
    )


def _clarification(message: str) -> LifeQueryResult:
    return LifeQueryResult(
        disposition=QueryDisposition.CLARIFICATION,
        generation_id=None,
        clarification=message,
    )


class LifeModelQueryService:
    """One bounded SELECT-only service shared by future CLI, UI, and MCP adapters."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        semantic_search: SemanticEvidenceSearchPort | None = None,
        telemetry_sink: TelemetrySink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.semantic_search = semantic_search
        self.telemetry_sink = telemetry_sink
        self.clock = clock or (lambda: datetime.now(UTC))
        self._person_name_cache: dict[str, frozenset[str]] = {}

    def ask_text(
        self,
        text: str,
        *,
        intent_parser: IntentParserPort | None = None,
        sensitivity_capability: TrustedLocalSensitivityCapability | None = None,
        allow_semantic_fallback: bool = False,
    ) -> LifeQueryResult:
        parsed = parse_life_query(text, intent_parser=intent_parser)
        if isinstance(parsed, LifeQueryResult):
            return parsed
        return self.query(
            parsed,
            sensitivity_capability=sensitivity_capability,
            allow_semantic_fallback=allow_semantic_fallback,
        )

    def query(
        self,
        query: LifeQuery,
        *,
        sensitivity_capability: TrustedLocalSensitivityCapability | None = None,
        allow_semantic_fallback: bool = False,
    ) -> LifeQueryResult:
        if sensitivity_capability is not None and not isinstance(
            sensitivity_capability, TrustedLocalSensitivityCapability
        ):
            raise TypeError("restricted sensitivity reads require trusted local capability")
        validated = LifeQuery.model_validate(query.model_dump(), strict=True)
        started = time.perf_counter()
        result = self._query(
            validated,
            sensitivity_capability=sensitivity_capability,
            allow_semantic_fallback=allow_semantic_fallback,
        )
        self._emit_telemetry(validated, result, (time.perf_counter() - started) * 1000)
        return result

    def _query(
        self,
        query: LifeQuery,
        *,
        sensitivity_capability: TrustedLocalSensitivityCapability | None,
        allow_semantic_fallback: bool,
    ) -> LifeQueryResult:
        generation_id = self._active_generation_id()
        if generation_id is None:
            return LifeQueryResult(
                disposition=QueryDisposition.NOT_KNOWN,
                generation_id=None,
                query=query,
                not_known=True,
            )
        resolved: list[ResolvedEntity] = []
        resolution_withheld = False
        for selector in query.entities:
            choices, selector_withheld = self._resolve_entity(
                generation_id,
                selector,
                include_restricted=sensitivity_capability is not None,
            )
            resolution_withheld = resolution_withheld or selector_withheld
            if len(choices) != 1:
                if choices:
                    return LifeQueryResult(
                        disposition=QueryDisposition.AMBIGUOUS,
                        generation_id=generation_id,
                        query=query,
                        resolved_entities=tuple(resolved),
                        ambiguity_choices=tuple(
                            AmbiguityChoice(
                                entity_kind=item.entity_kind,
                                entity_id=item.entity_id,
                                display_name=item.display_name,
                                matched_alias=item.matched_alias,
                            )
                            for item in choices
                        ),
                        clarification="Choose which matching entity you meant.",
                        sensitive_records_withheld=resolution_withheld,
                    )
                return LifeQueryResult(
                    disposition=QueryDisposition.NOT_KNOWN,
                    generation_id=generation_id,
                    query=query,
                    resolved_entities=tuple(resolved),
                    not_known=True,
                    sensitive_records_withheld=resolution_withheld,
                )
            resolved.append(choices[0])

        rows, sensitive_withheld, staged_withheld = (
            self._record_rows(
                generation_id,
                query,
                tuple(resolved),
                include_restricted=sensitivity_capability is not None,
            )
            if query.relation != LifeRelation.ACTION_HANDOFF
            else ([], False, False)
        )
        sensitive_withheld = sensitive_withheld or resolution_withheld
        if query.relation == LifeRelation.ACTION_HANDOFF:
            answers, handoff_withheld = self._action_handoff_answers(
                generation_id,
                query,
                tuple(resolved),
                include_restricted=sensitivity_capability is not None,
            )
            sensitive_withheld = sensitive_withheld or handoff_withheld
        else:
            answers = [
                answer
                for row in rows
                if (
                    answer := self._answer_from_row(
                        generation_id, query, tuple(resolved), row
                    )
                )
                is not None
            ]
        answers.sort(key=self._rank_answer)
        answers = answers[: query.limit]
        if answers:
            return LifeQueryResult(
                disposition=QueryDisposition.ANSWERED,
                generation_id=generation_id,
                query=query,
                resolved_entities=tuple(resolved),
                answers=tuple(answers),
                sensitive_records_withheld=sensitive_withheld,
            )

        candidates: tuple[UncompiledEvidenceCandidate, ...] = ()
        if allow_semantic_fallback and self.semantic_search is not None:
            raw = self.semantic_search.search(
                query, resolved_entities=tuple(resolved), limit=min(query.limit, 10)
            )
            validated_candidates = tuple(
                UncompiledEvidenceCandidate.model_validate(item.model_dump(), strict=True)
                for item in raw[: min(query.limit, 10)]
            )
            candidates = tuple(
                item
                for item in validated_candidates
                if sensitivity_capability is not None
                or item.sensitivity not in {Sensitivity.SENSITIVE, Sensitivity.UNCLASSIFIED}
            )
            sensitive_withheld = sensitive_withheld or len(candidates) < len(
                validated_candidates
            )
        return LifeQueryResult(
            disposition=QueryDisposition.NOT_KNOWN,
            generation_id=generation_id,
            query=query,
            resolved_entities=tuple(resolved),
            not_known=True,
            sensitive_records_withheld=sensitive_withheld,
            staged_records_withheld=staged_withheld,
            uncompiled_evidence_candidates=candidates,
        )

    def _active_generation_id(self) -> str | None:
        row = self.conn.execute(
            "SELECT generation_id FROM personal_model_active_generation WHERE singleton=1"
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _person_names(self, generation_id: str) -> frozenset[str]:
        """Every name this generation holds as a person, folded once."""
        cached = self._person_name_cache.get(generation_id)
        if cached is None:
            names = {
                normalize_alias(json.loads(row["record_json"]).get("display_name") or "")
                for row in self.conn.execute(
                    """SELECT record_json FROM personal_model_records
                        WHERE generation_id=? AND record_kind='person'""",
                    (generation_id,),
                ).fetchall()
            }
            names.discard("")
            cached = frozenset(names)
            self._person_name_cache[generation_id] = cached
        return cached

    def _owner_rows(self, generation_id: str) -> list[sqlite3.Row]:
        """The person records the compiler flagged as the archive's owner."""
        return [
            row
            for row in self.conn.execute(
                """SELECT record_id, record_json, sensitivity
                     FROM personal_model_records
                    WHERE generation_id=? AND record_kind='person'
                    ORDER BY record_id""",
                (generation_id,),
            ).fetchall()
            if json.loads(row["record_json"]).get("is_archive_owner") is True
        ]

    def _owner_ids(self, generation_id: str) -> frozenset[str]:
        return frozenset(str(row["record_id"]) for row in self._owner_rows(generation_id))

    def _resolve_owner(
        self,
        generation_id: str,
        selector: EntitySelector,
        *,
        include_restricted: bool,
    ) -> tuple[tuple[ResolvedEntity, ...], bool]:
        """Resolve a first-person selector to the person the archive belongs to."""
        rows = self._owner_rows(generation_id)
        # The owner is not dropped for being sensitive, the way a third party is.
        # A person record takes the highest sensitivity of the observations under
        # it, and the owner appears in every private message they ever sent, so
        # theirs is `sensitive` on any real archive -- it is `sensitive` on this
        # one. Dropping it made every first-person question unanswerable for
        # exactly the people the tool is for, and protected nothing: the caller is
        # the owner, asking about themselves, and each answer is still gated on
        # its own sensitivity further down. What the gate does buy is the name, so
        # the name is what is held back: unresolved material stays behind, and the
        # entity comes back under the word the caller used for themselves.
        resolved = tuple(
            ResolvedEntity(
                entity_kind="person",
                entity_id=str(row["record_id"]),
                display_name=(
                    json.loads(row["record_json"]).get("display_name")
                    or str(row["record_id"])
                )
                if include_restricted
                or row["sensitivity"] not in {"sensitive", "unclassified"}
                else selector.value,
                matched_alias=selector.value,
                display_name_withheld=(
                    not include_restricted
                    and row["sensitivity"] in {"sensitive", "unclassified"}
                ),
                # The compiler wrote the flag; nothing about this match is a guess.
                resolution_tier=0,
            )
            for row in rows[:MAX_ENTITY_CHOICES]
        )
        # No record is withheld here, so none is reported as withheld. Saying
        # otherwise ended every first-person question with "add
        # --include-sensitive to read them here" and the flag added nothing to
        # read: the only thing held back is the name, which this answer does not
        # print. It is reported on the entity that lost it instead.
        return resolved, False

    def _resolve_entity(
        self,
        generation_id: str,
        selector: EntitySelector,
        *,
        include_restricted: bool,
    ) -> tuple[tuple[ResolvedEntity, ...], bool]:
        if selector.kind in (None, "person") and normalize_alias(selector.value) in (
            FIRST_PERSON_SELF
        ):
            return self._resolve_owner(
                generation_id, selector, include_restricted=include_restricted
            )
        kind_clause = " AND alias.entity_kind=?" if selector.kind else ""
        kind_params: tuple[Any, ...] = (selector.kind,) if selector.kind else ()
        # Tier 0/1 exact aliases are durable. Candidate/display names deliberately
        # proceed through normalized matching so same-name people stay ambiguous.
        exact_rows = self.conn.execute(
            f"""SELECT alias.entity_kind, alias.entity_id, alias.exact_value,
                       alias.resolution_tier, record.record_json, record.sensitivity
                FROM personal_model_entity_aliases AS alias
                JOIN personal_model_records AS record
                  ON record.generation_id=alias.generation_id
                 AND record.record_id=alias.entity_id
                WHERE alias.generation_id=? AND alias.exact_value=?
                  AND alias.resolution_tier <= 1 {kind_clause}
                ORDER BY alias.resolution_tier, alias.entity_kind, alias.entity_id
                LIMIT {MAX_ENTITY_CHOICES + 1}""",
            (generation_id, selector.value, *kind_params),
        ).fetchall()
        # A record only counts as withheld where this call would drop it.
        restricted_found = not include_restricted and any(
            row["sensitivity"] in {"sensitive", "unclassified"} for row in exact_rows
        )
        if not include_restricted:
            exact_rows = [
                row
                for row in exact_rows
                if row["sensitivity"] not in {"sensitive", "unclassified"}
            ]
        rows = exact_rows
        if rows:
            best_tier = min(row["resolution_tier"] for row in rows)
            rows = [row for row in rows if row["resolution_tier"] == best_tier]
        if not rows:
            rows = self.conn.execute(
                f"""SELECT alias.entity_kind, alias.entity_id, alias.exact_value,
                           MIN(alias.resolution_tier) AS resolution_tier,
                           record.record_json, record.sensitivity
                    FROM personal_model_entity_aliases AS alias
                    JOIN personal_model_records AS record
                      ON record.generation_id=alias.generation_id
                     AND record.record_id=alias.entity_id
                    WHERE alias.generation_id=? AND alias.normalized_value=? {kind_clause}
                    GROUP BY alias.entity_kind, alias.entity_id
                    ORDER BY resolution_tier, alias.entity_kind, alias.entity_id
                    LIMIT {MAX_ENTITY_CHOICES + 1}""",
                (generation_id, normalize_alias(selector.value), *kind_params),
            ).fetchall()
            restricted_found = restricted_found or (
                not include_restricted
                and any(row["sensitivity"] in {"sensitive", "unclassified"} for row in rows)
            )
            if not include_restricted:
                rows = [
                    row
                    for row in rows
                    if row["sensitivity"] not in {"sensitive", "unclassified"}
                ]
        unique: dict[tuple[str, str], ResolvedEntity] = {}
        for row in rows:
            payload = json.loads(row["record_json"])
            display = payload.get("display_name") or payload.get("title") or row["entity_id"]
            key = (row["entity_kind"], row["entity_id"])
            unique[key] = ResolvedEntity(
                entity_kind=row["entity_kind"],
                entity_id=row["entity_id"],
                display_name=display,
                matched_alias=row["exact_value"],
                resolution_tier=row["resolution_tier"],
            )
        return (
            tuple(unique[key] for key in sorted(unique)[:MAX_ENTITY_CHOICES]),
            restricted_found,
        )

    def _record_rows(
        self,
        generation_id: str,
        query: LifeQuery,
        entities: tuple[ResolvedEntity, ...],
        *,
        include_restricted: bool,
    ) -> tuple[list[sqlite3.Row], bool]:
        kinds = {
            LifeRelation.FACT: ("fact",),
            LifeRelation.PREFERENCE: ("preference",),
            LifeRelation.EVENT: ("event",),
            LifeRelation.PERSON: ("person",),
            LifeRelation.PROJECT_GOAL: ("project", "goal"),
            LifeRelation.ACTION_HANDOFF: (),
            LifeRelation.COMMITMENT: ("observation",),
            LifeRelation.RELATIONSHIP_HISTORY: ("relationship_snapshot",),
            LifeRelation.INTERPRETATION: ("interpretation",),
        }[query.relation]
        if not kinds:
            raise AssertionError("action handoffs use their typed generation-scoped projection")
        where = [
            "record.generation_id=?",
            f"record.record_kind IN ({','.join('?' for _ in kinds)})",
        ]
        params: list[Any] = [generation_id, *kinds]
        if query.relation == LifeRelation.COMMITMENT:
            where.append("json_extract(record.record_json, '$.kind')='commitment'")
        if query.predicate:
            where.append("record.normalized_predicate=?")
            params.append(query.predicate)
        if query.concept:
            where.append("record.normalized_concept=?")
            params.append(query.concept)
        if query.preference_valence:
            where.append("json_extract(record.record_json, '$.valence')=?")
            params.append(str(query.preference_valence))
        if query.scope_label:
            where.append("lower(json_extract(record.record_json, '$.scope_label'))=lower(?)")
            params.append(query.scope_label)
        if query.time_from:
            where.append("(record.valid_to IS NULL OR record.valid_to>=?)")
            params.append(query.time_from.isoformat())
        if query.time_to:
            where.append("(record.valid_from IS NULL OR record.valid_from<=?)")
            params.append(query.time_to.isoformat())
        owners = self._owner_ids(generation_id) if entities else frozenset()
        for entity in entities:
            if query.relation == LifeRelation.PROJECT_GOAL and entity.entity_kind == "person":
                clause = (
                    "(EXISTS (SELECT 1 FROM personal_model_entity_memberships member "
                    "WHERE member.generation_id=record.generation_id "
                    "AND member.record_id=record.record_id AND member.entity_kind=? "
                    "AND member.entity_id=?) OR (record.record_kind='goal' AND EXISTS "
                    "(SELECT 1 FROM personal_model_entity_memberships goal_project "
                    "JOIN personal_model_entity_memberships project_person "
                    "ON project_person.generation_id=goal_project.generation_id "
                    "AND project_person.record_id=goal_project.entity_id "
                    "WHERE goal_project.generation_id=record.generation_id "
                    "AND goal_project.record_id=record.record_id "
                    "AND goal_project.entity_kind='project' "
                    "AND project_person.entity_kind='person' "
                    "AND project_person.entity_id=?))"
                )
                params.extend((entity.entity_kind, entity.entity_id, entity.entity_id))
                if entity.entity_id in owners:
                    # A project nobody is named on belongs to whoever the archive
                    # belongs to. Requiring a membership row is right for "what is
                    # Anna working on" -- there has to be evidence Anna is on it --
                    # and backwards for the owner, whose model this is. It made
                    # `What am I working on?`, the example in the tool's own
                    # description, answer nothing: on generation
                    # 01M1Z23M5463KQY4T0BZ04QBRH not one of the 312 projects and
                    # goals carries a person membership, because participants come
                    # from `ProjectProposal.participant_person_ids` and the
                    # extractor filled it in for none of them. The Mirror reads the
                    # same records without a person filter and shows all 312 under
                    # "Your goals", so the two surfaces disagreed about one
                    # question. A record that *does* name somebody else still
                    # answers only for them.
                    clause += (
                        " OR NOT EXISTS (SELECT 1 FROM personal_model_entity_memberships "
                        "unnamed WHERE unnamed.generation_id=record.generation_id "
                        "AND unnamed.record_id=record.record_id "
                        "AND unnamed.entity_kind='person')"
                    )
                where.append(clause + ")")
            else:
                where.append(
                    "EXISTS (SELECT 1 FROM personal_model_entity_memberships member "
                    "WHERE member.generation_id=record.generation_id "
                    "AND member.record_id=record.record_id AND member.entity_kind=? "
                    "AND member.entity_id=?)"
                )
                params.extend((entity.entity_kind, entity.entity_id))
        # Literals rather than bound values so this clause can be dropped from a
        # copy of the query without disturbing the parameter list -- the same
        # trick the sensitivity count below uses.
        where.append(VISIBLE_READINESS_CLAUSE)
        if not include_restricted:
            where.append("record.sensitivity NOT IN ('sensitive','unclassified')")
        # The window is a truncation, so its order decides which records can be
        # answered at all. Ordering by record_id sorted by a sha256, which on
        # generation 01M1Z23M5463KQY4T0BZ04QBRH meant 112 of the 312 projects and
        # goals could never come back however much of the archive stood behind
        # them -- including the one carrying 2,112 entries across 187 days. Count
        # the record's own evidence instead: it is the same list the answer
        # reports as its provenance, so the window and the ranking agree, and the
        # kind and id remain as tie-breakers so the order stays total.
        sql = f"""SELECT record.* FROM personal_model_records AS record
                  WHERE {' AND '.join(where)}
                  ORDER BY COALESCE(
                             json_array_length(record.record_json, '$.evidence'), 0
                           ) DESC,
                           record.record_kind, record.record_id LIMIT ?"""
        params.append(MAX_CANDIDATE_ROWS)
        rows = self.conn.execute(sql, params).fetchall()
        sensitive_withheld = False
        if not include_restricted:
            count_where = [
                item
                for item in where
                if item != "record.sensitivity NOT IN ('sensitive','unclassified')"
            ]
            sensitive_withheld = (
                self.conn.execute(
                    f"""SELECT 1 FROM personal_model_records AS record
                         WHERE {' AND '.join(count_where)}
                           AND record.sensitivity IN ('sensitive','unclassified') LIMIT 1""",
                    params[:-1],
                ).fetchone()
                is not None
            )
        staged_withheld = False
        if not rows:
            # Nothing came back. Before reporting that the archive does not know,
            # find out whether it does know and has simply never promoted what it
            # knows: every observation ever compiled is `staged` -- 9,455 of them
            # on the owner's archive, 503 of them commitments -- and no path in
            # the compiler moves one to `ready`. A tool that answers "what did I
            # commit to" out of observations therefore cannot answer it on any
            # archive, and said so as `not_known`, which reads as "you never
            # committed to anything" rather than "this was never compiled".
            staged_where = [item for item in where if item != VISIBLE_READINESS_CLAUSE]
            staged_withheld = (
                self.conn.execute(
                    f"""SELECT 1 FROM personal_model_records AS record
                         WHERE {' AND '.join(staged_where)} LIMIT 1""",
                    params[:-1],
                ).fetchone()
                is not None
            )
        return rows, sensitive_withheld, staged_withheld

    def _action_handoff_answers(
        self,
        generation_id: str,
        query: LifeQuery,
        entities: tuple[ResolvedEntity, ...],
        *,
        include_restricted: bool,
    ) -> tuple[list[TypedAnswer], bool]:
        """Read only compiler-approved leaf actions from the active generation.

        Handoffs deliberately remain outside canonical knowledge records.  This
        projection gives lookup consumers a bounded typed answer without turning
        an errand or implementation step into a goal or durable life fact.
        """
        where = ["handoff.generation_id=?"]
        params: list[Any] = [generation_id]
        for entity in entities:
            if entity.entity_kind == "project":
                where.append(
                    "EXISTS (SELECT 1 FROM json_each(handoff.project_ids_json) "
                    "WHERE value=?)"
                )
                params.append(entity.entity_id)
            elif entity.entity_kind == "goal":
                where.append(
                    "EXISTS (SELECT 1 FROM json_each(handoff.parent_goal_ids_json) "
                    "WHERE value=?)"
                )
                params.append(entity.entity_id)
            else:
                return [], False
        rows = self.conn.execute(
            f"""SELECT handoff.* FROM personal_model_action_handoffs AS handoff
                  WHERE {' AND '.join(where)}
                  ORDER BY handoff.candidate_id LIMIT ?""",
            (*params, min(query.limit, MAX_QUERY_LIMIT)),
        ).fetchall()
        answers: list[TypedAnswer] = []
        withheld = False
        for row in rows:
            entry_ids = tuple(
                str(item) for item in json.loads(row["source_entry_ids_json"])
            )[:MAX_CANDIDATE_ROWS]
            claim_ids = tuple(str(item) for item in json.loads(row["claim_ids_json"]))[
                :MAX_CANDIDATE_ROWS
            ]
            project_ids = tuple(str(item) for item in json.loads(row["project_ids_json"]))[
                :MAX_ENTITY_CHOICES
            ]
            parent_goal_ids = tuple(
                str(item) for item in json.loads(row["parent_goal_ids_json"])
            )[:MAX_ENTITY_CHOICES]
            sensitivity = self._handoff_sensitivity(
                generation_id, entry_ids=entry_ids, project_ids=project_ids
            )
            if not include_restricted and sensitivity in {
                Sensitivity.SENSITIVE,
                Sensitivity.UNCLASSIFIED,
            }:
                withheld = True
                continue
            title = str(row["title"])
            answers.append(
                TypedAnswer(
                    relation=LifeRelation.ACTION_HANDOFF,
                    record_kind="action_handoff",
                    subject_entity_ids=tuple(dict.fromkeys((*project_ids, *parent_goal_ids))),
                    predicate_or_concept="action_handoff",
                    value_kind=str(row["node_kind"]),
                    display_value=title[:1000],
                    normalized_value=normalize_alias(title)[:1000],
                    basis=AnswerBasis.OBSERVED,
                    status="active",
                    confidence=1.0,
                    readiness="ready",
                    sensitivity=str(sensitivity),
                    provenance=QueryProvenance(
                        generation_id=generation_id,
                        record_id=str(row["candidate_id"]),
                        entry_ids=entry_ids,
                        claim_ids=claim_ids,
                    ),
                )
            )
        return answers, withheld

    def _handoff_sensitivity(
        self,
        generation_id: str,
        *,
        entry_ids: tuple[str, ...],
        project_ids: tuple[str, ...],
    ) -> Sensitivity:
        values: set[str] = set()
        if entry_ids:
            placeholders = ",".join("?" for _ in entry_ids)
            values.update(
                str(row[0])
                for row in self.conn.execute(
                    f"SELECT sensitivity FROM entries WHERE id IN ({placeholders})",
                    entry_ids,
                ).fetchall()
            )
        if project_ids:
            placeholders = ",".join("?" for _ in project_ids)
            values.update(
                str(row[0])
                for row in self.conn.execute(
                    f"""SELECT sensitivity FROM personal_model_records
                         WHERE generation_id=? AND record_id IN ({placeholders})""",
                    (generation_id, *project_ids),
                ).fetchall()
            )
        for sensitivity in (
            Sensitivity.SENSITIVE,
            Sensitivity.UNCLASSIFIED,
            Sensitivity.PERSONAL,
            Sensitivity.PUBLIC,
        ):
            if str(sensitivity) in values:
                return sensitivity
        return Sensitivity.UNCLASSIFIED

    def _answer_from_row(
        self,
        generation_id: str,
        query: LifeQuery,
        entities: tuple[ResolvedEntity, ...],
        row: sqlite3.Row,
    ) -> TypedAnswer | None:
        actual_hash = hashlib.sha256(row["record_json"].encode("utf-8")).hexdigest()
        if actual_hash != row["canonical_json_hash"]:
            raise RuntimeError(
                f"stored personal-model record failed hash check: {row['record_id']}"
            )
        payload = json.loads(row["record_json"])
        status = _record_status(row["record_kind"], payload, row["readiness"])
        if query.statuses and status not in {str(item) for item in query.statuses}:
            return None
        model: (
            Fact
            | Preference
            | Event
            | Person
            | Project
            | Goal
            | RelationshipSnapshot
            | Interpretation
        )
        preference_model: Preference | None = None
        model_type = {
            "fact": Fact,
            "preference": Preference,
            "event": Event,
            "person": Person,
            "project": Project,
            "goal": Goal,
            "relationship_snapshot": RelationshipSnapshot,
            "interpretation": Interpretation,
        }.get(row["record_kind"])
        if model_type is None:
            # Commitments are observations; their exact literal remains evidence,
            # while this surface exposes only the typed observation kind.
            display = payload.get("proposition", payload.get("kind", "commitment"))
            value_kind = "observation"
            normalized = normalize_alias(display)
            confidence = float(payload["confidence"])
            derivation = payload["derivation"]
            temporal = payload.get("temporal", {})
            evidence = payload.get("evidence", [])
            claim_ids = payload.get("claim_ids", [])
            conflicts: tuple[str, ...] = ()
            predicate = "commitment"
        else:
            model = model_type.model_validate_json(row["record_json"], strict=True)
            if isinstance(model, Preference):
                preference_model = model
            display, normalized, value_kind, predicate, conflicts = _record_value(model)
            if value_kind in {"project", "goal"} and (
                not names_something(display)
                or is_a_relationship_label(display)
                or normalize_alias(display) in self._person_names(generation_id)
            ):
                # A generation compiled before the naming guard carries projects
                # and goals the archive can only name by the account they were
                # read out of -- `<eleven digits>@s.whatsapp.net`, `Relationship
                # with <eleven digits>`, `No specific name 5`. Forty-four of the
                # 192 projects and seventeen of the 120 goals on generation
                # 01M1Z23M5463KQY4T0BZ04QBRH are those, and with
                # --include-sensitive they filled the answer to "what am I
                # working on?" and pushed the real projects past the limit.
                # The compiler no longer emits them; this is what an archive
                # already holding them answers, since recompiling one is hours
                # of work.
                #
                # And the two the naming guard cannot see, both of which the
                # Mirror already refuses. A relationship is not a goal, and a
                # project the same generation also holds as a person is not one
                # either: 56 of the 312 projects and goals on that generation
                # are also people, so "what am I working on?" answered with
                # `Aarni Koskela` -- a contributor to a repository on disk --
                # and `Relationship with Nuullll`.
                return None
            confidence = model.confidence
            derivation = str(model.derivation)
            temporal = model.temporal.model_dump(mode="json")
            evidence = [item.model_dump(mode="json") for item in model.evidence]
            claim_ids = list(model.claim_ids)
        valid_from = _parse_datetime(temporal.get("valid_from"))
        valid_to = _parse_datetime(temporal.get("valid_to"))
        if not _time_matches(query, temporal):
            return None
        basis = _answer_basis(payload, derivation)
        memberships = self.conn.execute(
            """SELECT entity_id FROM personal_model_entity_memberships
               WHERE generation_id=? AND record_id=?
                 AND NOT (role='self' AND entity_kind='event')
               ORDER BY entity_kind, entity_id""",
            (generation_id, row["record_id"]),
        ).fetchall()
        return TypedAnswer(
            relation=query.relation,
            record_kind=row["record_kind"],
            subject_entity_ids=tuple(dict.fromkeys(item[0] for item in memberships)),
            predicate_or_concept=predicate,
            value_kind=value_kind,
            display_value=display,
            normalized_value=normalized,
            preference_valence=preference_model.valence if preference_model else None,
            scope_kind=preference_model.scope_kind if preference_model else None,
            scope_label=preference_model.scope_label if preference_model else None,
            assertion_kind=preference_model.assertion_kind if preference_model else None,
            basis=basis,
            status=status,
            confidence=confidence,
            readiness=row["readiness"],
            observed_at=_parse_datetime(temporal.get("observed_at")),
            stated_at=_parse_datetime(temporal.get("stated_at")),
            valid_from=valid_from,
            valid_to=valid_to,
            original_time_literal=temporal.get("original_time_literal"),
            possibly_outdated=(
                status in {"superseded", "contradicted"}
                or (valid_to is not None and valid_to < self.clock())
            ),
            conflict_record_ids=conflicts,
            sensitivity=row["sensitivity"],
            provenance=QueryProvenance(
                generation_id=generation_id,
                record_id=row["record_id"],
                entry_ids=tuple(dict.fromkeys(item["entry_id"] for item in evidence)),
                claim_ids=tuple(claim_ids),
            ),
        )

    @staticmethod
    def _rank_answer(answer: TypedAnswer) -> tuple[Any, ...]:
        basis_rank = {
            AnswerBasis.USER_CONFIRMED: 0,
            AnswerBasis.EXPLICIT: 1,
            AnswerBasis.REPORTED: 2,
            AnswerBasis.OBSERVED: 3,
            AnswerBasis.INFERRED: 4,
        }
        status_rank = {
            "active": 0,
            "current": 0,
            "ready": 0,
            "completed": 1,
            "limited": 2,
            "contradicted": 3,
            "superseded": 4,
            "rejected": 5,
        }
        return (
            basis_rank[answer.basis],
            status_rank.get(answer.status, 2),
            # Confidence grades how sure the compiler is that a record is real,
            # not how much of a life it accounts for, and it is near-constant
            # within a kind -- every goal on the live archive scored 0.75 and
            # every project 0.7. Ranking on it alone left the record id, a
            # sha256, deciding which twenty of the projects an agent was shown.
            -len(answer.provenance.entry_ids),
            -answer.confidence,
            answer.provenance.record_id,
        )

    def _emit_telemetry(
        self, query: LifeQuery, result: LifeQueryResult, elapsed_ms: float
    ) -> None:
        if self.telemetry_sink is None:
            return
        if elapsed_ms < 10:
            bucket = "lt_10ms"
        elif elapsed_ms < 50:
            bucket = "10_49ms"
        elif elapsed_ms < 250:
            bucket = "50_249ms"
        else:
            bucket = "gte_250ms"
        self.telemetry_sink(
            QueryTelemetry(
                relation=query.relation,
                entity_count=len(query.entities),
                has_predicate=query.predicate is not None,
                has_concept=query.concept is not None,
                has_time_filter=query.time_from is not None or query.time_to is not None,
                has_status_filter=bool(query.statuses),
                requested_limit=query.limit,
                disposition=result.disposition,
                result_count=len(result.answers),
                elapsed_ms_bucket=bucket,
            )
        )


class _AgentEgressRecord:
    def __init__(
        self,
        *,
        record_id: str,
        record_kind: str,
        predicate: str | None,
        payload: dict[str, Any],
        sensitivity: Sensitivity,
        member_person_ids: frozenset[str],
        content: str,
    ) -> None:
        self.record_id = record_id
        self.record_kind = record_kind
        self.predicate = predicate
        self.payload = payload
        self.sensitivity = sensitivity
        self.member_person_ids = member_person_ids
        self.content = content


class PersonalModelAgentEgressGate:
    """Apply third-party minimization only at the agent boundary.

    Local callers continue to use ``LifeModelQueryService`` directly. The MCP
    adapter passes its results through this gate, which makes the distinction
    structural instead of adding another classifier or model judgment.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        broker: EgressBroker,
        request: ContextRequest,
    ) -> None:
        self.conn = conn
        self.broker = broker
        self.request = request

    def filter_query_result(self, result: LifeQueryResult) -> LifeQueryResult:
        if result.generation_id is None or result.query is None:
            return result.model_copy(update={"egress_request_id": self.request.request_id})
        owner_ids = self._owner_ids(result.generation_id)
        anchored = frozenset(
            entity.entity_id
            for entity in result.resolved_entities
            if entity.entity_kind == "person" and entity.entity_id not in owner_ids
        )
        records: list[_AgentEgressRecord] = []
        answers_by_record: dict[str, TypedAnswer] = {}
        passthrough: list[TypedAnswer] = []
        for answer in result.answers:
            if answer.record_kind == "action_handoff":
                passthrough.append(answer)
                continue
            record = self._record(
                result.generation_id,
                answer.provenance.record_id,
                content=answer.model_dump_json(),
            )
            if record is None:
                continue
            records.append(record)
            answers_by_record[record.record_id] = answer

        withheld = self._withheld(records, owner_ids=owner_ids, anchored=anchored)
        answers = [*passthrough]
        answers.extend(
            answer
            for record_id, answer in answers_by_record.items()
            if record_id not in withheld
        )
        answers.sort(key=LifeModelQueryService._rank_answer)
        markers = list(withheld.values())
        if result.sensitive_records_withheld:
            markers.append(
                WithheldRecordMarker(
                    record_kind=_relation_record_name(result.query.relation),
                    predicate_or_concept=result.query.predicate or result.query.concept,
                    reason="restricted_sensitivity_local_only",
                    rule_version="existing_sensitivity_gate",
                    request_id=self.request.request_id,
                )
            )
        markers = _unique_markers(markers)
        disposition = result.disposition
        not_known = result.not_known
        if result.answers and not answers:
            disposition = QueryDisposition.NOT_KNOWN
            not_known = True
        return result.model_copy(
            update={
                "answers": tuple(answers),
                "disposition": disposition,
                "not_known": not_known,
                "sensitive_records_withheld": bool(markers),
                "withheld_records": tuple(markers),
                "egress_request_id": self.request.request_id,
                # Raw semantic hits are not canonical records and cannot satisfy
                # this record-shaped minimization rule at an agent boundary.
                "uncompiled_evidence_candidates": (),
            }
        )

    def withheld_record(
        self,
        *,
        generation_id: str,
        record_id: str,
        content: str,
        anchored_person_ids: frozenset[str],
    ) -> WithheldRecordMarker | None:
        record = self._record(generation_id, record_id, content=content)
        if record is None:
            return WithheldRecordMarker(
                record_kind="unknown",
                reason="personal_model_record_not_available",
                rule_version=THIRD_PARTY_RELATIONSHIP_NECESSITY_RULE_VERSION,
                request_id=self.request.request_id,
            )
        withheld = self._withheld(
            [record],
            owner_ids=self._owner_ids(generation_id),
            anchored=anchored_person_ids,
        )
        return withheld.get(record_id)

    def _record(
        self, generation_id: str, record_id: str, *, content: str
    ) -> _AgentEgressRecord | None:
        row = self.conn.execute(
            """SELECT record_kind,record_json,normalized_predicate,
                      normalized_concept,sensitivity
                 FROM personal_model_records
                WHERE generation_id=? AND record_id=?""",
            (generation_id, record_id),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["record_json"])
        predicate = _egress_predicate(row["record_kind"], payload, row)
        people = frozenset(
            str(item[0])
            for item in self.conn.execute(
                """SELECT entity_id FROM personal_model_entity_memberships
                     WHERE generation_id=? AND record_id=? AND entity_kind='person'""",
                (generation_id, record_id),
            ).fetchall()
        )
        return _AgentEgressRecord(
            record_id=record_id,
            record_kind=str(row["record_kind"]),
            predicate=predicate,
            payload=payload,
            sensitivity=Sensitivity(row["sensitivity"]),
            member_person_ids=people,
            content=content,
        )

    def _owner_ids(self, generation_id: str) -> frozenset[str]:
        rows = self.conn.execute(
            """SELECT record_id,record_json FROM personal_model_records
                 WHERE generation_id=? AND record_kind='person'""",
            (generation_id,),
        ).fetchall()
        return frozenset(
            str(row["record_id"])
            for row in rows
            if json.loads(row["record_json"]).get("is_archive_owner") is True
        )

    def _withheld(
        self,
        records: Sequence[_AgentEgressRecord],
        *,
        owner_ids: frozenset[str],
        anchored: frozenset[str],
    ) -> dict[str, WithheldRecordMarker]:
        assessments: dict[str, tuple[bool, tuple[str, ...], ContextItem | None]] = {}
        confidential_items: list[ContextItem] = []
        for record in records:
            third_party_ids = third_party_person_ids(
                record.record_kind,
                record.payload,
                owner_ids=owner_ids,
                member_person_ids=record.member_person_ids,
            )
            if not third_party_ids:
                assessments[record.record_id] = (True, (), None)
                continue
            necessary = relationship_necessity_allows(
                record.record_kind,
                record.predicate,
                record.payload,
                owner_ids=owner_ids,
                third_party_ids=third_party_ids,
                anchored_person_ids=anchored,
            )
            detector_item = ContextItem(
                item_id=f"personal-model:{record.record_id}",
                resource_type="summary",
                resource_id=record.record_id,
                content=json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
                sensitivity=record.sensitivity,
            )
            categories = item_categories(detector_item)
            context_item = None
            if categories:
                context_item = detector_item.model_copy(
                    update={
                        "content": record.content,
                        "label_source": LabelSource.DETERMINISTIC_RULE,
                        "label_reasons": ("personal_model_inherited",),
                        "metadata": {
                            "confidential_categories": categories,
                            "entity_ids": sorted(third_party_ids),
                            "relationship_necessity_rule_version": (
                                THIRD_PARTY_RELATIONSHIP_NECESSITY_RULE_VERSION
                            ),
                        },
                    }
                )
                confidential_items.append(context_item)
            assessments[record.record_id] = (necessary, categories, context_item)

        policy_by_item: dict[str, Any] = {}
        if confidential_items:
            prepared = self.broker.prepare(self.request, confidential_items)
            policy_by_item = {decision.item_id: decision for decision in prepared.decisions}

        withheld: dict[str, WithheldRecordMarker] = {}
        for record in records:
            necessary, categories, context_item = assessments[record.record_id]
            decision = (
                policy_by_item.get(context_item.item_id) if context_item is not None else None
            )
            policy_reasons = tuple(decision.reason_codes) if decision is not None else ()
            if not necessary:
                withheld[record.record_id] = WithheldRecordMarker(
                    record_kind=record.record_kind,
                    predicate_or_concept=record.predicate,
                    reason=THIRD_PARTY_RELATIONSHIP_NECESSITY_REASON,
                    rule_version=THIRD_PARTY_RELATIONSHIP_NECESSITY_RULE_VERSION,
                    categories=categories,
                    policy_reason_codes=policy_reasons,
                    request_id=self.request.request_id,
                )
            elif decision is not None and decision.effect != PolicyEffect.ALLOW:
                withheld[record.record_id] = WithheldRecordMarker(
                    record_kind=record.record_kind,
                    predicate_or_concept=record.predicate,
                    reason=THIRD_PARTY_ONE_TIME_PERMISSION_REASON,
                    rule_version=THIRD_PARTY_RELATIONSHIP_NECESSITY_RULE_VERSION,
                    categories=categories,
                    policy_reason_codes=policy_reasons,
                    request_id=self.request.request_id,
                )
        return withheld


class TrustedLocalSurfaceGate:
    """The gate for a surface that is not a destination.

    Third-party minimization exists because the MCP surface hands records to
    an agent. `varyz model interpret` prints them to the owner's own terminal,
    on the machine the archive lives on, so there is nobody to minimize for --
    the same reason `lookup` from the CLI carries a TrustedLocalSurface
    capability instead of an egress request. Sensitivity redaction happens
    inside the interpretation itself and is unaffected by this.

    It exists as a class rather than an optional argument because a gate that
    defaults to None gives the next caller the agent boundary by omission, and
    the omission is invisible at the call site.
    """

    class _NoRequest:
        request_id: str | None = None

    def __init__(self, surface: TrustedLocalSurface = TrustedLocalSurface.CLI) -> None:
        self.surface = surface
        self.request = self._NoRequest()

    def filter_query_result(self, result: LifeQueryResult) -> LifeQueryResult:
        return result

    def withheld_record(
        self,
        *,
        generation_id: str,
        record_id: str,
        content: str,
        anchored_person_ids: frozenset[str],
    ) -> WithheldRecordMarker | None:
        return None


def third_party_person_ids(
    record_kind: str,
    payload: dict[str, Any],
    *,
    owner_ids: frozenset[str],
    member_person_ids: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Return structurally named non-owner people; this performs no inference."""
    people = set(member_person_ids)
    if record_kind in {"fact", "preference"}:
        subject = payload.get("subject", {})
        if subject.get("kind") == "person" and subject.get("id"):
            people.add(str(subject["id"]))
    elif record_kind == "person" and payload.get("id"):
        people.add(str(payload["id"]))
    elif record_kind == "event":
        people.update(str(item) for item in payload.get("participant_ids", ()))
    elif record_kind == "project":
        people.update(str(item) for item in payload.get("participant_person_ids", ()))
    elif record_kind == "observation":
        if payload.get("subject_kind") == "person" and payload.get("subject_id"):
            people.add(str(payload["subject_id"]))
        if payload.get("speaker_person_id"):
            people.add(str(payload["speaker_person_id"]))
        people.update(str(item) for item in payload.get("addressee_person_ids", ()))
    elif record_kind == "relationship_snapshot":
        people.update(
            str(payload[key])
            for key in ("archive_owner_person_id", "other_person_id")
            if payload.get(key)
        )
    elif record_kind == "interpretation":
        scope = payload.get("scope", {})
        people.update(str(item) for item in scope.get("relationship_person_ids") or ())
    return frozenset(people - set(owner_ids))


def relationship_necessity_allows(
    record_kind: str,
    predicate: str | None,
    payload: dict[str, Any],
    *,
    owner_ids: frozenset[str],
    third_party_ids: frozenset[str],
    anchored_person_ids: frozenset[str],
) -> bool:
    """Version 1 of the auditable third-party relationship-necessity rule."""
    anchored = third_party_ids & anchored_person_ids
    if not anchored or not owner_ids:
        return False
    if record_kind == "event" and record_kind in RELATIONSHIP_NECESSARY_RECORD_KINDS:
        participants = frozenset(str(item) for item in payload.get("participant_ids", ()))
        return bool(participants & owner_ids) and bool(participants & anchored)
    if (
        record_kind == "relationship_snapshot"
        and record_kind in RELATIONSHIP_NECESSARY_RECORD_KINDS
    ):
        return (
            payload.get("archive_owner_person_id") in owner_ids
            and payload.get("other_person_id") in anchored
        )
    allowed_predicates = RELATIONSHIP_NECESSARY_PREDICATES.get(record_kind, frozenset())
    if predicate not in allowed_predicates:
        return False
    if record_kind == "observation":
        recipients = set(str(item) for item in payload.get("addressee_person_ids", ()))
        if payload.get("subject_kind") == "person" and payload.get("subject_id"):
            recipients.add(str(payload["subject_id"]))
        return payload.get("speaker_person_id") in owner_ids and bool(recipients & anchored)
    if record_kind == "interpretation":
        scope = payload.get("scope", {})
        people = frozenset(str(item) for item in scope.get("relationship_person_ids") or ())
        return scope.get("kind") == "relationship" and bool(people & owner_ids) and bool(
            people & anchored
        )
    return False


def _egress_predicate(
    record_kind: str, payload: dict[str, Any], row: sqlite3.Row
) -> str | None:
    if record_kind in {"fact", "preference"}:
        return row["normalized_predicate"] or payload.get("predicate")
    if record_kind == "event":
        return payload.get("event_kind")
    if record_kind == "observation":
        return payload.get("kind")
    if record_kind == "relationship_snapshot":
        return "relationship"
    if record_kind == "interpretation":
        return payload.get("kind")
    return row["normalized_concept"]


def _relation_record_name(relation: LifeRelation) -> str:
    return {
        LifeRelation.PROJECT_GOAL: "project_or_goal",
        LifeRelation.ACTION_HANDOFF: "action_handoff",
        LifeRelation.COMMITMENT: "observation",
        LifeRelation.RELATIONSHIP_HISTORY: "relationship_snapshot",
    }.get(relation, str(relation))


def _unique_markers(markers: Sequence[WithheldRecordMarker]) -> list[WithheldRecordMarker]:
    unique: dict[tuple[Any, ...], WithheldRecordMarker] = {}
    for marker in markers:
        key = (
            marker.record_kind,
            marker.predicate_or_concept,
            marker.reason,
            marker.categories,
            marker.policy_reason_codes,
        )
        unique[key] = marker
    return list(unique.values())


def _record_status(record_kind: str, payload: dict[str, Any], readiness: str) -> str:
    if record_kind in {"fact", "preference", "goal", "person", "project"}:
        return str(payload.get("status") or payload.get("state") or readiness)
    return readiness


def _answer_basis(payload: dict[str, Any], derivation: str) -> AnswerBasis:
    if derivation == "explicit":
        subject = payload.get("subject", {}).get("id")
        asserted_by = payload.get("asserted_by_person_id")
        if asserted_by and subject and asserted_by != subject:
            return AnswerBasis.REPORTED
    return AnswerBasis(derivation)


def _fact_value(value: Any) -> tuple[str, str | None, str]:
    payload = value.model_dump(mode="json")
    kind = str(payload["kind"])
    if kind == "month_day":
        display = f"{payload['month']:02d}-{payload['day']:02d}"
        return display, display, kind
    if kind == "date":
        display = payload.get("value") or payload["exact_literal"]
        return str(display), str(display), kind
    if kind == "text":
        display = payload.get("normalized") or payload["exact_literal"]
        return str(display), payload.get("normalized"), kind
    if kind == "number":
        display = str(payload["value"])
        if payload.get("unit"):
            display += f" {payload['unit']}"
        return display, str(payload["value"]), kind
    if kind == "boolean":
        display = "true" if payload["value"] else "false"
        return display, display, kind
    if kind == "location":
        return payload["label"], normalize_alias(payload["label"]), kind
    if kind == "entity_ref":
        return payload["entity_id"], payload["entity_id"], kind
    return payload["exact_literal"], None, kind


def _record_value(
    model: (
        Fact
        | Preference
        | Event
        | Person
        | Project
        | Goal
        | RelationshipSnapshot
        | Interpretation
    ),
) -> tuple[str, str | None, str, str | None, tuple[str, ...]]:
    if isinstance(model, Fact):
        display, normalized, kind = _fact_value(model.value)
        return (
            display,
            normalized,
            kind,
            model.predicate,
            tuple(dict.fromkeys((*model.contradicted_by_fact_ids, *model.supersedes_fact_ids))),
        )
    if isinstance(model, Preference):
        display, normalized, kind = _fact_value(model.object)
        return (
            display,
            normalized,
            kind,
            model.predicate,
            tuple(
                dict.fromkeys(
                    (
                        *model.contradicted_by_preference_ids,
                        *model.supersedes_preference_ids,
                    )
                )
            ),
        )
    if isinstance(model, Event):
        return model.title, normalize_alias(model.title), "event", model.event_kind, ()
    if isinstance(model, Person):
        return model.display_name, normalize_alias(model.display_name), "person", None, ()
    if isinstance(model, Project):
        return model.display_name, normalize_alias(model.display_name), "project", None, ()
    if isinstance(model, Goal):
        return model.title, normalize_alias(model.title), "goal", str(model.kind), ()
    if isinstance(model, RelationshipSnapshot):
        display = "; ".join(
            f"{item.key}: {item.count}" for item in model.measurements
        ) or "relationship history"
        return display, None, "relationship_snapshot", "relationship", ()
    return model.thesis, None, "interpretation", model.concept_key, ()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _time_matches(query: LifeQuery, temporal: dict[str, Any]) -> bool:
    points: list[datetime] = []
    for key in ("observed_at", "stated_at", "valid_from", "valid_to"):
        parsed = _parse_datetime(temporal.get(key))
        if parsed is not None:
            points.append(parsed)
    literal = temporal.get("original_time_literal")
    if not points and literal:
        try:
            parsed_date = date.fromisoformat(literal)
            points.append(
                datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
            )
        except ValueError:
            pass
    if query.time_from and points and max(points) < query.time_from:
        return False
    if query.time_to and points and min(points) > query.time_to:
        return False
    return not ((query.time_from or query.time_to) and not points)


__all__ = [
    "AmbiguityChoice",
    "ALLOWED_PREDICATES_BY_RELATION",
    "AnswerBasis",
    "EntitySelector",
    "IntentParserPort",
    "LifeModelQueryService",
    "LifeQuery",
    "LifeQueryResult",
    "LifeRelation",
    "PREDICATE_ALIASES",
    "PersonalModelAgentEgressGate",
    "QueryDisposition",
    "QueryProvenance",
    "QueryStatus",
    "QueryTelemetry",
    "ResolvedEntity",
    "SemanticEvidenceSearchPort",
    "TrustedLocalSensitivityCapability",
    "TrustedLocalSurface",
    "THIRD_PARTY_ONE_TIME_PERMISSION_REASON",
    "THIRD_PARTY_RELATIONSHIP_NECESSITY_REASON",
    "THIRD_PARTY_RELATIONSHIP_NECESSITY_RULE_VERSION",
    "TypedAnswer",
    "UncompiledEvidenceCandidate",
    "WithheldRecordMarker",
    "normalize_alias",
    "parse_life_query",
    "issue_trusted_local_sensitivity_capability",
    "relationship_necessity_allows",
    "third_party_person_ids",
]

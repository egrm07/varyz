"""Deterministic compilation of literal observations into durable memory records.

This module deliberately does not extract prose. A provider may propose one of the
closed candidate schemas below, but every material literal and attribution is checked
against pinned observations or atomic claims before a staged record is produced.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from varyz.domain import Sensitivity, TimePrecision
from varyz.personal_model.models import (
    AttributionState,
    BooleanFactValue,
    DateAmbiguity,
    DateAmbiguityKind,
    DateFactValue,
    DerivationKind,
    DialogueContext,
    EntityKind,
    EntityRefFactValue,
    Event,
    EvidenceReference,
    EvidenceRole,
    Fact,
    FactStatus,
    FactValue,
    FactValueKind,
    LocationFactValue,
    MonthDayFactValue,
    NumberFactValue,
    Observation,
    ObservationKind,
    ObservationModality,
    Polarity,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    SubjectReference,
    TemporalScope,
    TextFactValue,
    TextSpan,
    derived_record_id,
)
from varyz.personal_model.preference_sources import (
    communication_guidance_phrases,
    project_guidance_phrases,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class PreferenceBasis(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class CandidateKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"


class QuarantineReason(StrEnum):
    UNKNOWN_EVIDENCE = "unknown_evidence"
    CONFLICTING_PIN = "conflicting_pin"
    UNSUPPORTED_SUBJECT = "unsupported_subject"
    UNSUPPORTED_ASSERTED_BY = "unsupported_asserted_by"
    UNRESOLVED_ATTRIBUTION = "unresolved_attribution"
    UNSUPPORTED_LITERAL = "unsupported_literal"
    PREDICATE_NOT_ALLOWED = "predicate_not_allowed"
    VALUE_KIND_NOT_ALLOWED = "value_kind_not_allowed"
    NORMALIZATION_NOT_ALLOWED = "normalization_not_allowed"
    CONTEXT_DISALLOWS_LITERAL_MEMORY = "context_disallows_literal_memory"
    CONTEXT_DISALLOWS_PREFERENCE = "context_disallows_preference"
    MENTION_IS_NOT_PREFERENCE = "mention_is_not_preference"
    INSUFFICIENT_INFERRED_BASIS = "insufficient_inferred_basis"
    DATE_UNPARSEABLE = "date_unparseable"
    EVENT_KIND_NOT_ALLOWED = "event_kind_not_allowed"
    UNSUPPORTED_PARTICIPANT = "unsupported_participant"
    UNSUPPORTED_PROJECT = "unsupported_project"
    MISSING_EVENT_TIME = "missing_event_time"
    MALFORMED_CANDIDATE = "malformed_candidate"


class AtomicClaimInput(_FrozenModel):
    """Minimal pinned claim projection accepted by the compiler.

    ``content_hash`` binds the exact literal and source revision; callers cannot silently
    swap claim text while retaining the same claim id.
    """

    id: str = Field(min_length=1)
    subject: SubjectReference
    asserted_by_person_id: str = Field(min_length=1)
    span: TextSpan
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED
    dialogue_context: DialogueContext = DialogueContext.UNKNOWN
    modality: ObservationModality = ObservationModality.UNKNOWN
    polarity: Polarity = Polarity.UNKNOWN

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("atomic claim occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def hash_is_pinned(self) -> AtomicClaimInput:
        expected = atomic_claim_content_hash(self.span.exact_literal, self.span.source_revision)
        if self.content_hash != expected:
            raise ValueError("atomic claim content_hash does not match its pinned content")
        return self


def atomic_claim_content_hash(exact_literal: str, source_revision: str | None) -> str:
    payload = json.dumps(
        {"exact_literal": exact_literal, "source_revision": source_revision},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FactCandidate(_FrozenModel):
    kind: Literal[CandidateKind.FACT]
    subject: SubjectReference
    predicate: str = Field(min_length=1)
    value_kind: FactValueKind
    exact_value_literal: str = Field(min_length=1)
    proposed_normalized_value: str | None = None
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    asserted_by_person_id: str = Field(min_length=1)
    locale: str | None = None
    timezone: str | None = None
    entity_ref_kind: EntityKind | None = None
    entity_ref_id: str | None = None

    @model_validator(mode="after")
    def evidence_required(self) -> FactCandidate:
        if not self.observation_ids and not self.claim_ids:
            raise ValueError("fact candidates require pinned evidence")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("observation ids must be unique")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("claim ids must be unique")
        if self.value_kind == FactValueKind.ENTITY_REF:
            if self.entity_ref_kind is None or self.entity_ref_id is None:
                raise ValueError("entity-ref candidates require a resolved kind and id")
        elif self.entity_ref_kind is not None or self.entity_ref_id is not None:
            raise ValueError("only entity-ref candidates may carry a resolved entity")
        return self


class PinnedEntityReference(_FrozenModel):
    """An identity-resolver output bound to an exact source span."""

    entity: SubjectReference
    span: TextSpan
    resolver_version: str = Field(min_length=1)


class PreferenceCandidate(_FrozenModel):
    kind: Literal[CandidateKind.PREFERENCE]
    subject: SubjectReference
    predicate: str = Field(min_length=1)
    exact_object_literal: str = Field(min_length=1)
    proposed_normalized_object: str | None = None
    valence: PreferenceValence
    basis: PreferenceBasis
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    asserted_by_person_id: str = Field(min_length=1)
    scope_kind: str | None = None
    scope_label: str | None = None
    assertion_kind: str = "stated_taste"

    @model_validator(mode="after")
    def evidence_required(self) -> PreferenceCandidate:
        if not self.observation_ids and not self.claim_ids:
            raise ValueError("preference candidates require pinned evidence")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("observation ids must be unique")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("claim ids must be unique")
        if (self.scope_kind is None) != (self.scope_label is None):
            raise ValueError("preference scope kind and label must be supplied together")
        if self.scope_kind not in {None, "project", "domain", "global"}:
            raise ValueError("unsupported preference scope kind")
        if self.scope_label is not None and not 1 <= len(self.scope_label.strip()) <= 160:
            raise ValueError("preference scope label must be 1-160 characters")
        if self.assertion_kind not in {"stated_taste", "project_directive"}:
            raise ValueError("unsupported preference assertion kind")
        if self.assertion_kind == "project_directive" and self.scope_kind != "project":
            raise ValueError("project directives require project scope")
        return self


class EventCandidate(_FrozenModel):
    kind: Literal[CandidateKind.EVENT]
    event_kind: str = Field(min_length=1)
    title_literal: str = Field(min_length=1)
    time_literal: str = Field(min_length=1)
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    asserted_by_person_id: str = Field(min_length=1)
    participant_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    location_literal: str | None = None
    locale: str | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def evidence_required(self) -> EventCandidate:
        if not self.observation_ids and not self.claim_ids:
            raise ValueError("event candidates require pinned evidence")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("observation ids must be unique")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("claim ids must be unique")
        return self


MemoryCandidate = Annotated[
    FactCandidate | PreferenceCandidate | EventCandidate,
    Field(discriminator="kind"),
]
_MEMORY_CANDIDATE_ADAPTER = TypeAdapter(MemoryCandidate)


class MemoryQuarantine(_FrozenModel):
    candidate_fingerprint: str
    kind: CandidateKind | None
    reason: QuarantineReason
    detail: str


class ReadinessReport(_FrozenModel):
    record_id: str
    eligible: bool
    reasons: tuple[str, ...]


class MemoryCompilation(_FrozenModel):
    facts: tuple[Fact, ...]
    preferences: tuple[Preference, ...]
    events: tuple[Event, ...]
    quarantines: tuple[MemoryQuarantine, ...]
    readiness: tuple[ReadinessReport, ...]


FACT_VALUE_KINDS: Mapping[str, frozenset[FactValueKind]] = {
    "person.age": frozenset({FactValueKind.NUMBER}),
    "person.birth_date": frozenset({FactValueKind.DATE, FactValueKind.MONTH_DAY}),
    "person.anniversary_date": frozenset({FactValueKind.DATE, FactValueKind.MONTH_DAY}),
    "person.full_name": frozenset({FactValueKind.TEXT}),
    "person.role": frozenset({FactValueKind.TEXT}),
    "person.current_location": frozenset({FactValueKind.LOCATION}),
    "person.constraint": frozenset({FactValueKind.TEXT}),
    "person.has_driver_license": frozenset({FactValueKind.BOOLEAN}),
    "person.height_cm": frozenset({FactValueKind.NUMBER}),
    "person.is_vegetarian": frozenset({FactValueKind.BOOLEAN}),
    "person.manager": frozenset({FactValueKind.ENTITY_REF}),
    "person.partner": frozenset({FactValueKind.ENTITY_REF}),
    "project.deadline": frozenset({FactValueKind.DATE}),
    "project.name": frozenset({FactValueKind.TEXT}),
    "project.owner": frozenset({FactValueKind.ENTITY_REF}),
    "project.status": frozenset({FactValueKind.TEXT}),
    "relationship.anniversary_date": frozenset({FactValueKind.DATE, FactValueKind.MONTH_DAY}),
}

PREFERENCE_PREDICATES = frozenset(
    {
        "preference.activity",
        "preference.flower",
        "preference.food",
        "preference.music",
        "preference.place",
        "preference.thing",
        "preference.communication",
        "preference.visual",
        "preference.workflow",
    }
)

EVENT_KINDS = frozenset(
    {
        "anniversary",
        "birthday",
        "celebration",
        "commitment",
        "deadline",
        "meeting",
        "milestone",
        "move",
        "trip",
    }
)

_CHANGEABLE_FACT_PREDICATES = frozenset(
    {"person.current_location", "person.role", "project.name", "project.status"}
)
_NUMBER_UNITS_BY_PREDICATE: Mapping[str, frozenset[str | None]] = {
    "person.age": frozenset({None, "years"}),
    "person.height_cm": frozenset({"cm"}),
}
_ENTITY_KIND_BY_PREDICATE: Mapping[str, EntityKind] = {
    "person.manager": EntityKind.PERSON,
    "person.partner": EntityKind.PERSON,
    "project.owner": EntityKind.PERSON,
}
_DISALLOWED_PREFERENCE_CONTEXTS = frozenset(
    {
        DialogueContext.BRAINSTORMING,
        DialogueContext.HUMOUR_OR_SARCASM,
        DialogueContext.HYPOTHETICAL,
        DialogueContext.INSTRUCTION,
        DialogueContext.PROMPTED_AGREEMENT,
        DialogueContext.QUOTATION,
    }
)
_DISALLOWED_PREFERENCE_MODALITIES = frozenset(
    {
        ObservationModality.HUMOROUS_OR_SARCASTIC,
        ObservationModality.HYPOTHETICAL,
        ObservationModality.QUOTED,
    }
)
_DISALLOWED_LITERAL_CONTEXTS = _DISALLOWED_PREFERENCE_CONTEXTS
_DISALLOWED_LITERAL_MODALITIES = frozenset(
    {
        ObservationModality.HUMOROUS_OR_SARCASTIC,
        ObservationModality.HYPOTHETICAL,
        ObservationModality.QUOTED,
        ObservationModality.UNCERTAIN,
        ObservationModality.UNKNOWN,
    }
)

_MONTHS = {
    "january": 1,
    "jan": 1,
    "enero": 1,
    "января": 1,
    "январь": 1,
    "february": 2,
    "feb": 2,
    "febrero": 2,
    "февраля": 2,
    "февраль": 2,
    "march": 3,
    "mar": 3,
    "marzo": 3,
    "марта": 3,
    "март": 3,
    "april": 4,
    "apr": 4,
    "abril": 4,
    "апреля": 4,
    "апрель": 4,
    "may": 5,
    "mayo": 5,
    "мая": 5,
    "май": 5,
    "june": 6,
    "jun": 6,
    "junio": 6,
    "июня": 6,
    "июнь": 6,
    "july": 7,
    "jul": 7,
    "julio": 7,
    "июля": 7,
    "июль": 7,
    "august": 8,
    "aug": 8,
    "agosto": 8,
    "августа": 8,
    "август": 8,
    "september": 9,
    "sep": 9,
    "septiembre": 9,
    "сентября": 9,
    "сентябрь": 9,
    "october": 10,
    "oct": 10,
    "octubre": 10,
    "октября": 10,
    "октябрь": 10,
    "november": 11,
    "nov": 11,
    "noviembre": 11,
    "ноября": 11,
    "ноябрь": 11,
    "december": 12,
    "dec": 12,
    "diciembre": 12,
    "декабря": 12,
    "декабрь": 12,
}
_WEEKDAYS = {
    "monday": 0,
    "lunes": 0,
    "понедельник": 0,
    "tuesday": 1,
    "martes": 1,
    "вторник": 1,
    "wednesday": 2,
    "miercoles": 2,
    "среду": 2,
    "среда": 2,
    "thursday": 3,
    "jueves": 3,
    "четверг": 3,
    "friday": 4,
    "viernes": 4,
    "пятницу": 4,
    "пятница": 4,
    "saturday": 5,
    "sabado": 5,
    "субботу": 5,
    "суббота": 5,
    "sunday": 6,
    "domingo": 6,
    "воскресенье": 6,
}


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalized_text(value: str) -> str:
    return " ".join(_fold(value).split())


def _numeric_locale_order(locale: str | None) -> str | None:
    if locale is None:
        return None
    normalized = locale.replace("_", "-").casefold()
    if normalized in {"en-us", "en-ca"}:
        return "mdy"
    if normalized.startswith(("en-gb", "es", "ru")):
        return "dmy"
    return None


def normalize_date_literal(
    exact_literal: str,
    *,
    source_timestamp: datetime | None = None,
    locale: str | None = None,
    timezone: str | None = None,
) -> DateFactValue | MonthDayFactValue:
    """Normalize only date forms whose precision can be justified from supplied context."""
    raw = exact_literal
    folded = _fold(raw.strip()).replace(",", " ")
    folded = re.sub(r"\s+", " ", folded)

    relative_match = re.fullmatch(
        r"(?:next|proxim[oa]|siguiente|следующ(?:ий|ую|ее))\s+([\wа-яё]+)", folded
    )
    if relative_match:
        weekday = _WEEKDAYS.get(relative_match.group(1))
        if (
            weekday is None
            or source_timestamp is None
            or source_timestamp.tzinfo is None
            or source_timestamp.utcoffset() is None
            or locale is None
            or timezone is None
        ):
            return DateFactValue(
                kind=FactValueKind.DATE,
                exact_literal=raw,
                ambiguity=DateAmbiguity(
                    kind=DateAmbiguityKind.RELATIVE,
                    source_timestamp=source_timestamp,
                    locale=locale,
                    timezone=timezone,
                    explanation=(
                        "relative date lacks a resolvable aware timestamp, locale, "
                        "timezone, or weekday"
                    ),
                ),
            )
        try:
            local_date = source_timestamp.astimezone(ZoneInfo(timezone)).date()
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        days = (weekday - local_date.weekday()) % 7
        if days == 0:
            days = 7
        return DateFactValue(
            kind=FactValueKind.DATE,
            exact_literal=raw,
            value=local_date + timedelta(days=days),
        )

    iso_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", folded)
    if iso_match:
        return DateFactValue(
            kind=FactValueKind.DATE,
            exact_literal=raw,
            value=date(*(int(part) for part in iso_match.groups())),
        )

    numeric = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?", folded)
    if numeric:
        first, second = int(numeric.group(1)), int(numeric.group(2))
        year = int(numeric.group(3)) if numeric.group(3) else None
        order = _numeric_locale_order(locale)
        possible: list[tuple[int, int]] = []
        for month, day in ((first, second), (second, first)):
            try:
                date(year or 2000, month, day)
            except ValueError:
                continue
            if (month, day) not in possible:
                possible.append((month, day))
        if not possible:
            raise ValueError("invalid numeric date")
        if order is None and len(possible) > 1:
            candidates = tuple(date(year, month, day) for month, day in possible) if year else ()
            return DateFactValue(
                kind=FactValueKind.DATE,
                exact_literal=raw,
                ambiguity=DateAmbiguity(
                    kind=DateAmbiguityKind.NUMERIC_ORDER,
                    candidates=candidates,
                    source_timestamp=source_timestamp,
                    locale=locale,
                    timezone=timezone,
                    explanation=(
                        "numeric date order is ambiguous without an explicit supported locale"
                    ),
                ),
            )
        if order == "mdy":
            chosen = (first, second)
        elif order == "dmy":
            chosen = (second, first)
        else:
            chosen = possible[0]
        if chosen not in possible:
            raise ValueError("numeric date is invalid for the supplied locale")
        month, day = chosen
        if year is None:
            return MonthDayFactValue(
                kind=FactValueKind.MONTH_DAY,
                exact_literal=raw,
                month=month,
                day=day,
            )
        return DateFactValue(
            kind=FactValueKind.DATE,
            exact_literal=raw,
            value=date(year, month, day),
        )

    words = folded.replace(" de ", " ").split()
    month_indexes = [index for index, word in enumerate(words) if word in _MONTHS]
    if len(month_indexes) == 1:
        month_index = month_indexes[0]
        month = _MONTHS[words[month_index]]
        numbers = [(index, int(word)) for index, word in enumerate(words) if word.isdigit()]
        year_values = [value for _, value in numbers if value >= 1000]
        day_values = [value for index, value in numbers if index != month_index and value < 100]
        if len(day_values) != 1 or len(year_values) > 1:
            raise ValueError("date literal does not identify one day")
        day = day_values[0]
        if not year_values:
            return MonthDayFactValue(
                kind=FactValueKind.MONTH_DAY,
                exact_literal=raw,
                month=month,
                day=day,
            )
        return DateFactValue(
            kind=FactValueKind.DATE,
            exact_literal=raw,
            value=date(year_values[0], month, day),
        )

    return DateFactValue(
        kind=FactValueKind.DATE,
        exact_literal=raw,
        ambiguity=DateAmbiguity(
            kind=DateAmbiguityKind.UNKNOWN,
            source_timestamp=source_timestamp,
            locale=locale,
            timezone=timezone,
            explanation="date literal was preserved because no supported exact parse was justified",
        ),
    )


def _candidate_fingerprint(candidate: FactCandidate | PreferenceCandidate | EventCandidate) -> str:
    payload = candidate.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _raw_candidate_fingerprint(candidate: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _value_token(value: FactValue) -> str:
    if isinstance(value, TextFactValue):
        payload: object = {"kind": value.kind.value, "normalized": value.normalized}
    elif isinstance(value, LocationFactValue):
        payload = {
            "kind": value.kind.value,
            "label": _normalized_text(value.label),
            "latitude": value.latitude,
            "longitude": value.longitude,
        }
    elif isinstance(value, DateFactValue):
        payload = {
            "kind": value.kind.value,
            "value": value.value.isoformat() if value.value else None,
            "ambiguity_kind": value.ambiguity.kind.value if value.ambiguity else None,
            "ambiguity_candidates": (
                tuple(item.isoformat() for item in value.ambiguity.candidates)
                if value.ambiguity
                else ()
            ),
        }
    elif isinstance(value, MonthDayFactValue):
        payload = {"kind": value.kind.value, "month": value.month, "day": value.day}
    elif isinstance(value, NumberFactValue):
        payload = {
            "kind": value.kind.value,
            "value": format(value.value.normalize(), "f"),
            "unit": value.unit,
        }
    elif isinstance(value, BooleanFactValue):
        payload = {"kind": value.kind.value, "value": value.value}
    elif isinstance(value, EntityRefFactValue):
        payload = {
            "kind": value.kind.value,
            "entity_kind": value.entity_kind.value,
            "entity_id": value.entity_id,
        }
    else:
        payload = value.model_dump(mode="json", exclude={"exact_literal"})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_time(record: Fact | Preference) -> datetime:
    return (
        record.temporal.valid_from
        or record.temporal.stated_at
        or record.temporal.observed_at
        or record.created_at
    )


class MemoryCompiler:
    """Validate closed candidates and compile an order-independent staged model slice."""

    def __init__(
        self,
        *,
        compiler_version: str = "memory-v1",
        independence_policy_version: str = "decision-correlation-v1",
    ) -> None:
        self.compiler_version = compiler_version
        self.independence_policy_version = independence_policy_version
        self.identity_version = f"{compiler_version}+{independence_policy_version}"
        self._producer = ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="varyz-personal-memory-compiler",
            version=compiler_version,
            rule_version=self.identity_version,
        )

    def compile_raw(
        self,
        *,
        generation_id: str,
        observations: Sequence[Observation],
        candidates: Sequence[Mapping[str, object]],
        created_at: datetime,
        atomic_claims: Sequence[AtomicClaimInput] = (),
        entity_references: Sequence[PinnedEntityReference] = (),
        episode_independence_groups: Mapping[str, str] | None = None,
    ) -> MemoryCompilation:
        """Validate untrusted provider payloads, quarantining schema failures."""
        validated: list[FactCandidate | PreferenceCandidate | EventCandidate] = []
        malformed: list[MemoryQuarantine] = []
        for raw in sorted(candidates, key=_raw_candidate_fingerprint):
            try:
                payload = json.dumps(
                    raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                validated.append(_MEMORY_CANDIDATE_ADAPTER.validate_json(payload))
            except (TypeError, ValueError, ValidationError) as exc:
                raw_kind = raw.get("kind")
                try:
                    kind = CandidateKind(raw_kind) if isinstance(raw_kind, str) else None
                except ValueError:
                    kind = None
                if isinstance(exc, ValidationError):
                    error = exc.errors(include_url=False, include_input=False)[0]
                    detail = (
                        "candidate failed closed-schema validation at "
                        f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                    )
                else:
                    detail = "candidate is not a deterministic JSON-compatible payload"
                malformed.append(
                    MemoryQuarantine(
                        candidate_fingerprint=_raw_candidate_fingerprint(raw),
                        kind=kind,
                        reason=QuarantineReason.MALFORMED_CANDIDATE,
                        detail=detail,
                    )
                )
        compiled = self.compile(
            generation_id=generation_id,
            observations=observations,
            candidates=validated,
            created_at=created_at,
            atomic_claims=atomic_claims,
            entity_references=entity_references,
            episode_independence_groups=episode_independence_groups,
        )
        quarantines = tuple(
            sorted(
                (*compiled.quarantines, *malformed),
                key=lambda item: item.candidate_fingerprint,
            )
        )
        return compiled.model_copy(update={"quarantines": quarantines})

    def compile(
        self,
        *,
        generation_id: str,
        observations: Sequence[Observation],
        candidates: Sequence[FactCandidate | PreferenceCandidate | EventCandidate],
        created_at: datetime,
        atomic_claims: Sequence[AtomicClaimInput] = (),
        entity_references: Sequence[PinnedEntityReference] = (),
        episode_independence_groups: Mapping[str, str] | None = None,
    ) -> MemoryCompilation:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        observation_index = self._unique_index(observations, "observation")
        claim_index = self._unique_index(atomic_claims, "atomic claim")
        independence_groups = episode_independence_groups or {}
        if any(
            not episode_id or not group_id for episode_id, group_id in independence_groups.items()
        ):
            raise ValueError("episode independence mappings require non-blank ids")
        facts: list[Fact] = []
        preferences: list[Preference] = []
        events: list[Event] = []
        quarantines: list[MemoryQuarantine] = []
        ordered_candidates = sorted(candidates, key=_candidate_fingerprint)
        for candidate in ordered_candidates:
            fingerprint = _candidate_fingerprint(candidate)
            try:
                if isinstance(candidate, FactCandidate):
                    facts.append(
                        self._compile_fact(
                            candidate,
                            generation_id,
                            created_at,
                            observation_index,
                            claim_index,
                            entity_references,
                        )
                    )
                elif isinstance(candidate, PreferenceCandidate):
                    preferences.append(
                        self._compile_preference(
                            candidate,
                            generation_id,
                            created_at,
                            observation_index,
                            claim_index,
                            independence_groups,
                        )
                    )
                else:
                    events.append(
                        self._compile_event(
                            candidate,
                            generation_id,
                            created_at,
                            observation_index,
                            claim_index,
                        )
                    )
            except _CandidateError as exc:
                quarantines.append(
                    MemoryQuarantine(
                        candidate_fingerprint=fingerprint,
                        kind=candidate.kind,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                )
        facts = self._resolve_fact_statuses(facts)
        preferences = self._resolve_preference_statuses(preferences)
        facts.sort(key=lambda item: item.id)
        preferences.sort(key=lambda item: item.id)
        events.sort(key=lambda item: item.id)
        quarantines.sort(key=lambda item: item.candidate_fingerprint)
        readiness = tuple(readiness_for_fact(fact) for fact in facts)
        return MemoryCompilation(
            facts=tuple(facts),
            preferences=tuple(preferences),
            events=tuple(events),
            quarantines=tuple(quarantines),
            readiness=readiness,
        )

    @staticmethod
    def _unique_index(items: Sequence[BaseModel], label: str) -> dict[str, object]:
        result: dict[str, object] = {}
        serialized: dict[str, str] = {}
        for item in items:
            item_id = str(item.id)
            material = item.model_dump_json()
            if item_id in serialized and serialized[item_id] != material:
                raise ValueError(f"conflicting duplicate {label} id: {item_id}")
            serialized[item_id] = material
            result[item_id] = item
        return result

    def _evidence_context(
        self,
        candidate: FactCandidate | PreferenceCandidate | EventCandidate,
        observation_index: Mapping[str, object],
        claim_index: Mapping[str, object],
    ) -> _EvidenceContext:
        observations: list[Observation] = []
        claims: list[AtomicClaimInput] = []
        for observation_id in candidate.observation_ids:
            item = observation_index.get(observation_id)
            if not isinstance(item, Observation):
                raise _CandidateError(QuarantineReason.UNKNOWN_EVIDENCE, observation_id)
            observations.append(item)
        for claim_id in candidate.claim_ids:
            item = claim_index.get(claim_id)
            if not isinstance(item, AtomicClaimInput):
                raise _CandidateError(QuarantineReason.UNKNOWN_EVIDENCE, claim_id)
            claims.append(item)
        subjects = {
            (item.subject_kind, item.subject_id)
            for item in observations
            if item.subject_attribution == AttributionState.RESOLVED
        }
        subjects.update((item.subject.kind, item.subject.id) for item in claims)
        expected = (
            (candidate.subject.kind, candidate.subject.id)
            if hasattr(candidate, "subject")
            else None
        )
        if expected is not None and (not subjects or subjects != {expected}):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_SUBJECT,
                "candidate subject does not exactly match all pinned evidence",
            )
        if any(
            item.subject_attribution != AttributionState.RESOLVED
            or item.speaker_attribution != AttributionState.RESOLVED
            for item in observations
        ):
            raise _CandidateError(
                QuarantineReason.UNRESOLVED_ATTRIBUTION,
                "memory cannot be compiled from unresolved speaker or subject attribution",
            )
        speakers = {item.speaker_person_id for item in observations}
        speakers.update(item.asserted_by_person_id for item in claims)
        if speakers != {candidate.asserted_by_person_id}:
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_ASSERTED_BY,
                "asserted-by person does not exactly match all pinned evidence",
            )
        return _EvidenceContext(tuple(observations), tuple(claims))

    @staticmethod
    def _require_literal(context: _EvidenceContext, literal: str) -> None:
        if not any(literal in source for source in context.source_literals):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_LITERAL,
                f"exact literal is absent from pinned source: {literal!r}",
            )

    def _compile_fact(
        self,
        candidate: FactCandidate,
        generation_id: str,
        created_at: datetime,
        observation_index: Mapping[str, object],
        claim_index: Mapping[str, object],
        entity_references: Sequence[PinnedEntityReference],
    ) -> Fact:
        allowed = FACT_VALUE_KINDS.get(candidate.predicate)
        if allowed is None:
            raise _CandidateError(QuarantineReason.PREDICATE_NOT_ALLOWED, candidate.predicate)
        if (
            candidate.predicate.startswith("person.")
            and candidate.subject.kind != EntityKind.PERSON
        ):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_SUBJECT,
                "person predicates require a canonical person subject",
            )
        if (
            candidate.predicate.startswith("project.")
            and candidate.subject.kind != EntityKind.PROJECT
        ):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_SUBJECT,
                "project predicates require a canonical project subject",
            )
        if candidate.value_kind not in allowed:
            raise _CandidateError(
                QuarantineReason.VALUE_KIND_NOT_ALLOWED,
                f"{candidate.value_kind} is invalid for {candidate.predicate}",
            )
        context = self._evidence_context(candidate, observation_index, claim_index)
        self._require_literal_assertion_context(
            context, allow_negative=candidate.value_kind == FactValueKind.BOOLEAN
        )
        self._require_literal(context, candidate.exact_value_literal)
        if not self._predicate_is_lexically_supported(candidate.predicate, context.source_literals):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_LITERAL,
                "source does not contain the predicate-specific assertion marker",
            )
        source_timestamp = context.single_stated_at
        try:
            value = self._fact_value(candidate, source_timestamp, context, entity_references)
        except ValueError as exc:
            reason = (
                QuarantineReason.DATE_UNPARSEABLE
                if candidate.value_kind in {FactValueKind.DATE, FactValueKind.MONTH_DAY}
                else QuarantineReason.VALUE_KIND_NOT_ALLOWED
            )
            raise _CandidateError(reason, str(exc)) from exc
        evidence = context.evidence
        identity_membership = context.identity_membership
        record_id = derived_record_id(
            "fact",
            canonical_subject=f"{candidate.subject.kind.value}:{candidate.subject.id}",
            canonical_predicate=f"{candidate.predicate}:{_value_token(value)}",
            evidence_membership=identity_membership,
            identity_version=self.compiler_version,
        )
        temporal = context.temporal
        if candidate.predicate in _CHANGEABLE_FACT_PREDICATES and temporal.stated_at:
            temporal = temporal.model_copy(update={"valid_from": temporal.stated_at})
        return Fact(
            id=record_id,
            generation_id=generation_id,
            evidence=evidence,
            claim_ids=tuple(sorted(candidate.claim_ids)),
            temporal=temporal,
            confidence=context.confidence,
            sensitivity=context.sensitivity,
            derivation=DerivationKind.EXPLICIT,
            producer=self._producer,
            readiness=Readiness.STAGED,
            created_at=created_at,
            subject=candidate.subject,
            predicate=candidate.predicate,
            value=value,
            asserted_by_person_id=candidate.asserted_by_person_id,
        )

    def _fact_value(
        self,
        candidate: FactCandidate,
        source_timestamp: datetime | None,
        context: _EvidenceContext,
        entity_references: Sequence[PinnedEntityReference],
    ) -> FactValue:
        if candidate.value_kind in {FactValueKind.DATE, FactValueKind.MONTH_DAY}:
            value = normalize_date_literal(
                candidate.exact_value_literal,
                source_timestamp=source_timestamp,
                locale=candidate.locale,
                timezone=candidate.timezone,
            )
            if (
                isinstance(value, DateFactValue)
                and value.ambiguity is not None
                and value.ambiguity.kind == DateAmbiguityKind.UNKNOWN
            ):
                raise ValueError("date literal is not a supported date form")
            if candidate.value_kind == FactValueKind.MONTH_DAY and not isinstance(
                value, MonthDayFactValue
            ):
                raise ValueError("candidate requested month/day but literal did not yield one")
            if candidate.value_kind == FactValueKind.DATE and not isinstance(value, DateFactValue):
                raise ValueError("candidate requested full date but literal did not yield one")
            return value
        if candidate.value_kind == FactValueKind.TEXT:
            normalized = _normalized_text(candidate.exact_value_literal)
            self._validate_proposed_normalization(candidate.proposed_normalized_value, normalized)
            return TextFactValue(
                kind=FactValueKind.TEXT,
                exact_literal=candidate.exact_value_literal,
                normalized=normalized,
            )
        if candidate.value_kind == FactValueKind.LOCATION:
            normalized = _normalized_text(candidate.exact_value_literal)
            self._validate_proposed_normalization(candidate.proposed_normalized_value, normalized)
            return LocationFactValue(
                kind=FactValueKind.LOCATION,
                exact_literal=candidate.exact_value_literal,
                label=candidate.exact_value_literal,
            )
        if candidate.proposed_normalized_value is not None:
            raise _CandidateError(
                QuarantineReason.NORMALIZATION_NOT_ALLOWED,
                "typed values are normalized only by deterministic parsers",
            )
        if candidate.value_kind == FactValueKind.NUMBER:
            value, unit = self._parse_number(candidate.exact_value_literal)
            if unit not in _NUMBER_UNITS_BY_PREDICATE[candidate.predicate]:
                raise ValueError("number unit is invalid for the canonical predicate")
            return NumberFactValue(
                kind=FactValueKind.NUMBER,
                exact_literal=candidate.exact_value_literal,
                value=value,
                unit=unit,
            )
        if candidate.value_kind == FactValueKind.BOOLEAN:
            parsed = self._parse_boolean(candidate.predicate, candidate.exact_value_literal)
            if parsed and any(
                item.polarity == Polarity.NEGATIVE
                for item in (*context.observations, *context.claims)
            ):
                raise _CandidateError(
                    QuarantineReason.CONTEXT_DISALLOWS_LITERAL_MEMORY,
                    "a positive boolean cannot be cut from negated evidence",
                )
            return BooleanFactValue(
                kind=FactValueKind.BOOLEAN,
                exact_literal=candidate.exact_value_literal,
                value=parsed,
            )
        if candidate.value_kind == FactValueKind.ENTITY_REF:
            expected = SubjectReference(
                kind=candidate.entity_ref_kind,
                id=candidate.entity_ref_id,
            )
            if expected.kind != _ENTITY_KIND_BY_PREDICATE[candidate.predicate]:
                raise _CandidateError(
                    QuarantineReason.VALUE_KIND_NOT_ALLOWED,
                    "resolved entity kind is invalid for the canonical predicate",
                )
            if not any(
                item.entity == expected
                and item.span.exact_literal == candidate.exact_value_literal
                and context.contains_span(item.span)
                for item in entity_references
            ):
                raise _CandidateError(
                    QuarantineReason.UNSUPPORTED_LITERAL,
                    "entity reference lacks a pinned resolver span in candidate evidence",
                )
            return EntityRefFactValue(
                kind=FactValueKind.ENTITY_REF,
                exact_literal=candidate.exact_value_literal,
                entity_kind=expected.kind,
                entity_id=expected.id,
            )
        raise _CandidateError(
            QuarantineReason.VALUE_KIND_NOT_ALLOWED,
            f"compiler does not accept provider-proposed {candidate.value_kind} values",
        )

    @staticmethod
    def _parse_number(exact_literal: str) -> tuple[Decimal, str | None]:
        match = re.fullmatch(
            r"\s*([+-]?\d+(?:\.\d+)?)\s*(years?|kg|cm|people|%)?\s*",
            exact_literal.casefold(),
        )
        if match is None:
            raise ValueError("number literal is malformed or locale-ambiguous")
        try:
            value = Decimal(match.group(1))
        except InvalidOperation as exc:
            raise ValueError("number literal is invalid") from exc
        unit = match.group(2)
        if unit == "year":
            unit = "years"
        return value, unit

    @staticmethod
    def _parse_boolean(predicate: str, exact_literal: str) -> bool:
        token = _normalized_text(exact_literal)
        tokens = {
            "person.has_driver_license": {
                True: {
                    "has a driver's license",
                    "has a driving licence",
                    "tiene carnet de conducir",
                },
                False: {
                    "does not have a driver's license",
                    "doesn't have a driving licence",
                    "no tiene carnet de conducir",
                },
            },
            "person.is_vegetarian": {
                True: {"vegetarian", "is vegetarian", "es vegetariano", "es vegetariana"},
                False: {
                    "not vegetarian",
                    "is not vegetarian",
                    "no es vegetariano",
                    "no es vegetariana",
                },
            },
        }
        for value, allowed in tokens.get(predicate, {}).items():
            if token in allowed:
                return value
        raise ValueError("boolean literal is not an allowlisted exact form")

    @staticmethod
    def _validate_proposed_normalization(proposed: str | None, deterministic: str) -> None:
        if proposed is not None and proposed != deterministic:
            raise _CandidateError(
                QuarantineReason.NORMALIZATION_NOT_ALLOWED,
                "proposed normalized value differs from deterministic source normalization",
            )

    def _compile_preference(
        self,
        candidate: PreferenceCandidate,
        generation_id: str,
        created_at: datetime,
        observation_index: Mapping[str, object],
        claim_index: Mapping[str, object],
        episode_independence_groups: Mapping[str, str],
    ) -> Preference:
        if candidate.predicate not in PREFERENCE_PREDICATES:
            raise _CandidateError(QuarantineReason.PREDICATE_NOT_ALLOWED, candidate.predicate)
        context = self._evidence_context(candidate, observation_index, claim_index)
        self._require_literal(context, candidate.exact_object_literal)
        disallowed_contexts = (
            _DISALLOWED_PREFERENCE_CONTEXTS - {DialogueContext.INSTRUCTION}
            if candidate.assertion_kind == "project_directive"
            else _DISALLOWED_PREFERENCE_CONTEXTS
        )
        if any(
            item.dialogue_context in disallowed_contexts
            or item.modality in _DISALLOWED_PREFERENCE_MODALITIES
            for item in (*context.observations, *context.claims)
        ):
            raise _CandidateError(
                QuarantineReason.CONTEXT_DISALLOWS_PREFERENCE,
                (
                    "quotation, hypothetical, brainstorming, instruction, humour, "
                    "or prompted agreement"
                ),
            )
        if any(
            item.modality != ObservationModality.ASSERTED
            for item in (*context.observations, *context.claims)
        ):
            raise _CandidateError(
                QuarantineReason.CONTEXT_DISALLOWS_PREFERENCE,
                "a preference requires asserted rather than uncertain or unknown evidence",
            )
        explicit_marker = (
            any(
                phrase.valence == candidate.valence.value
                and f"preference.{phrase.dimension}" == candidate.predicate
                and phrase.object_literal == candidate.exact_object_literal
                for literal in context.source_literals
                for phrase in (
                    *project_guidance_phrases(literal),
                    *communication_guidance_phrases(literal),
                )
            )
            if candidate.assertion_kind == "project_directive"
            else self._preference_marker(context.source_literals, candidate.valence)
        )
        if candidate.basis == PreferenceBasis.EXPLICIT and not explicit_marker:
            raise _CandidateError(
                QuarantineReason.MENTION_IS_NOT_PREFERENCE,
                "the item is mentioned but no explicit preference assertion is present",
            )
        if candidate.basis == PreferenceBasis.INFERRED:
            episode_ids = {item.episode_id for item in context.observations}
            missing_groups = sorted(episode_ids - episode_independence_groups.keys())
            independence_groups = {
                episode_independence_groups[episode_id]
                for episode_id in episode_ids
                if episode_id in episode_independence_groups
            }
            kinds = {item.kind for item in context.observations}
            if (
                missing_groups
                or len(independence_groups) < 2
                or not context.each_item_supports_literal(candidate.exact_object_literal)
                or not kinds.intersection({ObservationKind.CHOICE, ObservationKind.REACTION})
                or len(context.observations) < 2
            ):
                raise _CandidateError(
                    QuarantineReason.INSUFFICIENT_INFERRED_BASIS,
                    (
                        "inferred preference requires choice/reaction evidence in two "
                        "versioned independent decision groups"
                    ),
                )
        normalized = _normalized_text(candidate.exact_object_literal)
        self._validate_proposed_normalization(candidate.proposed_normalized_object, normalized)
        value = TextFactValue(
            kind=FactValueKind.TEXT,
            exact_literal=candidate.exact_object_literal,
            normalized=normalized,
        )
        identity_membership = context.identity_membership
        record_id = derived_record_id(
            "preference",
            canonical_subject=f"{candidate.subject.kind.value}:{candidate.subject.id}",
            canonical_predicate=(
                f"{candidate.predicate}:{candidate.valence.value}:{normalized}"
                + (
                    f":{candidate.scope_kind}:{candidate.scope_label}"
                    if candidate.scope_kind is not None
                    else ""
                )
            ),
            evidence_membership=identity_membership,
            identity_version=(
                self.identity_version
                if candidate.basis == PreferenceBasis.INFERRED
                else self.compiler_version
            ),
        )
        return Preference(
            id=record_id,
            generation_id=generation_id,
            evidence=context.evidence,
            claim_ids=tuple(sorted(candidate.claim_ids)),
            temporal=(
                context.temporal.model_copy(update={"valid_from": context.temporal.stated_at})
                if context.temporal.stated_at
                else context.temporal
            ),
            confidence=(
                context.confidence
                if candidate.basis == PreferenceBasis.EXPLICIT
                else context.confidence * 0.7
            ),
            sensitivity=context.sensitivity,
            derivation=(
                DerivationKind.EXPLICIT
                if candidate.basis == PreferenceBasis.EXPLICIT
                else DerivationKind.INFERRED
            ),
            producer=self._producer,
            readiness=Readiness.STAGED,
            created_at=created_at,
            subject=candidate.subject,
            predicate=candidate.predicate,
            object=value,
            valence=candidate.valence,
            strength=self._preference_strength(
                context.source_literals, candidate.valence, candidate.basis
            ),
            asserted_by_person_id=candidate.asserted_by_person_id,
            scope_kind=candidate.scope_kind,
            scope_label=candidate.scope_label,
            assertion_kind=candidate.assertion_kind,
        )

    def _compile_event(
        self,
        candidate: EventCandidate,
        generation_id: str,
        created_at: datetime,
        observation_index: Mapping[str, object],
        claim_index: Mapping[str, object],
    ) -> Event:
        if candidate.event_kind not in EVENT_KINDS:
            raise _CandidateError(QuarantineReason.EVENT_KIND_NOT_ALLOWED, candidate.event_kind)
        context = self._event_evidence_context(candidate, observation_index, claim_index)
        self._require_literal_assertion_context(context)
        self._require_literal(context, candidate.title_literal)
        self._require_literal(context, candidate.time_literal)
        if not self._event_kind_is_supported(candidate.event_kind, context.source_literals):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_LITERAL,
                "source does not contain the event-kind assertion marker",
            )
        if candidate.location_literal:
            self._require_literal(context, candidate.location_literal)
        supported_people = context.supported_people
        if not set(candidate.participant_ids).issubset(supported_people):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_PARTICIPANT,
                "event participant is absent from pinned attribution",
            )
        supported_projects = context.supported_projects
        if not set(candidate.project_ids).issubset(supported_projects):
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_PROJECT,
                "event project is absent from pinned attribution",
            )
        try:
            normalized_date = normalize_date_literal(
                candidate.time_literal,
                source_timestamp=context.single_stated_at,
                locale=candidate.locale,
                timezone=candidate.timezone,
            )
        except ValueError as exc:
            raise _CandidateError(QuarantineReason.DATE_UNPARSEABLE, str(exc)) from exc
        temporal = context.temporal.model_copy(
            update={
                "original_time_literal": candidate.time_literal,
                "time_precision": TimePrecision.DAY,
            }
        )
        if (
            isinstance(normalized_date, DateFactValue)
            and normalized_date.value is not None
            and candidate.timezone is not None
        ):
            try:
                event_start = datetime.combine(
                    normalized_date.value, time.min, tzinfo=ZoneInfo(candidate.timezone)
                )
            except ZoneInfoNotFoundError as exc:
                raise _CandidateError(
                    QuarantineReason.DATE_UNPARSEABLE, "unknown timezone"
                ) from exc
            temporal = temporal.model_copy(update={"valid_from": event_start})
        location = (
            LocationFactValue(
                kind=FactValueKind.LOCATION,
                exact_literal=candidate.location_literal,
                label=candidate.location_literal,
            )
            if candidate.location_literal
            else None
        )
        canonical_title = _normalized_text(candidate.title_literal)
        record_id = derived_record_id(
            "event",
            canonical_subject=f"event:{candidate.event_kind}",
            canonical_predicate=canonical_title,
            evidence_membership=context.identity_membership,
            identity_version=self.compiler_version,
        )
        return Event(
            id=record_id,
            generation_id=generation_id,
            evidence=context.evidence,
            claim_ids=tuple(sorted(candidate.claim_ids)),
            temporal=temporal,
            confidence=context.confidence,
            sensitivity=context.sensitivity,
            derivation=DerivationKind.EXPLICIT,
            producer=self._producer,
            readiness=Readiness.STAGED,
            created_at=created_at,
            canonical_key=f"{candidate.event_kind}:{canonical_title}",
            title=candidate.title_literal,
            event_kind=candidate.event_kind,
            participant_ids=tuple(sorted(candidate.participant_ids)),
            project_ids=tuple(sorted(candidate.project_ids)),
            location=location,
        )

    def _event_evidence_context(
        self,
        candidate: EventCandidate,
        observation_index: Mapping[str, object],
        claim_index: Mapping[str, object],
    ) -> _EvidenceContext:
        # Events may involve several resolved subjects, so use the common attribution
        # checks without requiring one candidate subject.
        observations: list[Observation] = []
        claims: list[AtomicClaimInput] = []
        for observation_id in candidate.observation_ids:
            item = observation_index.get(observation_id)
            if not isinstance(item, Observation):
                raise _CandidateError(QuarantineReason.UNKNOWN_EVIDENCE, observation_id)
            observations.append(item)
        for claim_id in candidate.claim_ids:
            item = claim_index.get(claim_id)
            if not isinstance(item, AtomicClaimInput):
                raise _CandidateError(QuarantineReason.UNKNOWN_EVIDENCE, claim_id)
            claims.append(item)
        if any(
            item.subject_attribution != AttributionState.RESOLVED
            or item.speaker_attribution != AttributionState.RESOLVED
            for item in observations
        ):
            raise _CandidateError(
                QuarantineReason.UNRESOLVED_ATTRIBUTION,
                "event evidence has unresolved attribution",
            )
        speakers = {item.speaker_person_id for item in observations}
        speakers.update(item.asserted_by_person_id for item in claims)
        if speakers != {candidate.asserted_by_person_id}:
            raise _CandidateError(
                QuarantineReason.UNSUPPORTED_ASSERTED_BY,
                "asserted-by person does not match event evidence",
            )
        return _EvidenceContext(tuple(observations), tuple(claims))

    @staticmethod
    def _require_literal_assertion_context(
        context: _EvidenceContext, *, allow_negative: bool = False
    ) -> None:
        items = (*context.observations, *context.claims)
        if any(
            item.dialogue_context in _DISALLOWED_LITERAL_CONTEXTS
            or item.modality in _DISALLOWED_LITERAL_MODALITIES
            or (item.polarity == Polarity.NEGATIVE and not allow_negative)
            for item in items
        ):
            raise _CandidateError(
                QuarantineReason.CONTEXT_DISALLOWS_LITERAL_MEMORY,
                "literal fact or event evidence is negated, uncertain, quoted, or non-assertive",
            )

    @staticmethod
    def _predicate_is_lexically_supported(predicate: str, literals: tuple[str, ...]) -> bool:
        text = _fold(" ".join(literals))
        markers = {
            "person.age": ("years old", "year old", "aged", "anos", "лет"),
            "person.birth_date": (
                "birthday",
                "born",
                "cumpleanos",
                "nacio",
                "день рождения",
                "родил",
            ),
            "person.anniversary_date": ("anniversary", "aniversario", "годовщин"),
            "relationship.anniversary_date": ("anniversary", "aniversario", "годовщин"),
            "person.full_name": ("name is", "called", "nombre", "зовут", "имя"),
            "person.role": ("works as", "role", "job", "trabaja", "роль", "работает"),
            "person.current_location": (
                "lives in",
                "live in",
                "moved to",
                "based in",
                "vive en",
                "vivo en",
                "живет",
                "живу",
                "переех",
            ),
            "person.constraint": (
                "must",
                "cannot",
                "can't",
                "need to",
                "debe",
                "no puede",
                "нельзя",
                "долж",
            ),
            "person.has_driver_license": (
                "driver's license",
                "driving licence",
                "carnet de conducir",
            ),
            "person.height_cm": ("height", "tall", "cm", "altura", "рост"),
            "person.is_vegetarian": ("vegetarian", "vegetariano", "vegetariana"),
            "person.manager": ("manager", "reports to", "gerente", "руководител"),
            "person.partner": ("partner", "boyfriend", "girlfriend", "pareja", "партнер"),
            "project.deadline": ("deadline", "due", "fecha limite", "срок", "дедлайн"),
            "project.name": ("called", "named", "project", "proyecto", "проект"),
            "project.owner": ("owner", "owned by", "dueño", "propietari", "владел"),
            "project.status": ("status", "paused", "active", "done", "estado", "статус", "готов"),
        }
        return any(marker in text for marker in markers[predicate])

    @staticmethod
    def _preference_marker(literals: tuple[str, ...], valence: PreferenceValence) -> bool:
        text = _fold(" ".join(literals))
        negative = (
            "doesn't like",
            "does not like",
            "don't like",
            "dont like",
            "do not like",
            "no le gusta",
            "не любит",
        )
        if valence == PreferenceValence.LIKE and any(marker in text for marker in negative):
            return False
        markers = {
            PreferenceValence.LIKE: (
                " like ",
                " likes ",
                "love",
                "favorite",
                "favourite",
                "gusta",
                "encanta",
                "любит",
                "нрав",
            ),
            PreferenceValence.DISLIKE: (
                "dislike",
            "doesn't like",
            "don't like",
            "dont like",
                "do not like",
                "hate",
                "no le gusta",
                "odio",
                "не любит",
                "ненавид",
            ),
            PreferenceValence.AVOID: ("avoid", "avoids", "evita", "избега"),
            PreferenceValence.PREFER: ("prefer", "prefers", "prefiere", "предпочит"),
        }
        padded = f" {text} "
        return any(marker in padded for marker in markers[valence])

    @staticmethod
    def _preference_strength(
        literals: tuple[str, ...],
        valence: PreferenceValence,
        basis: PreferenceBasis,
    ) -> float:
        if basis == PreferenceBasis.INFERRED:
            return 0.5
        text = _fold(" ".join(literals))
        strong_markers = {
            PreferenceValence.LIKE: (
                "love",
                "favorite",
                "favourite",
                "encanta",
                "любит",
            ),
            PreferenceValence.DISLIKE: ("hate", "odio", "ненавид"),
            PreferenceValence.AVOID: ("always avoid", "siempre evita", "всегда избег"),
            PreferenceValence.PREFER: (
                "strongly prefer",
                "prefiere mucho",
                "очень предпочит",
            ),
        }
        return 1.0 if any(marker in text for marker in strong_markers[valence]) else 0.75

    @staticmethod
    def _event_kind_is_supported(event_kind: str, literals: tuple[str, ...]) -> bool:
        text = _fold(" ".join(literals))
        markers = {
            "anniversary": ("anniversary", "aniversario", "годовщин"),
            "birthday": ("birthday", "cumpleanos", "день рождения"),
            "celebration": ("celebrat", "celebr", "праздн"),
            "commitment": ("promise", "committed", "promet", "обещ"),
            "deadline": ("deadline", "due", "fecha limite", "дедлайн", "срок"),
            "meeting": ("meeting", "meet", "reunion", "встреч"),
            "milestone": ("milestone", "hito", "этап"),
            "move": ("moved", "move to", "mud", "переех"),
            "trip": ("trip", "travel", "viaje", "поезд", "путешеств"),
        }
        return any(marker in text for marker in markers[event_kind])

    @staticmethod
    def _resolve_fact_statuses(facts: list[Fact]) -> list[Fact]:
        grouped: dict[tuple[EntityKind, str, str], list[Fact]] = defaultdict(list)
        for fact in facts:
            grouped[(fact.subject.kind, fact.subject.id, fact.predicate)].append(fact)
        resolved: list[Fact] = []
        for (_, _, predicate), group in grouped.items():
            tokens = {_value_token(item.value) for item in group}
            if len(tokens) <= 1:
                resolved.extend(group)
                continue
            if predicate in _CHANGEABLE_FACT_PREDICATES:
                by_time: dict[datetime, list[Fact]] = defaultdict(list)
                for item in group:
                    by_time[_record_time(item)].append(item)
                ordered_times = sorted(by_time)
                if len(ordered_times) > 1:
                    for index, item_time in enumerate(ordered_times):
                        bucket = by_time[item_time]
                        next_time = (
                            ordered_times[index + 1] if index + 1 < len(ordered_times) else None
                        )
                        previous_ids = (
                            tuple(sorted(item.id for item in by_time[ordered_times[index - 1]]))
                            if index > 0
                            else ()
                        )
                        bucket_tokens = {_value_token(item.value) for item in bucket}
                        for item in bucket:
                            temporal = item.temporal
                            if next_time is not None and temporal.valid_to is None:
                                temporal = temporal.model_copy(update={"valid_to": next_time})
                            updates: dict[str, object] = {
                                "temporal": temporal,
                                "supersedes_fact_ids": previous_ids,
                            }
                            if len(bucket_tokens) > 1:
                                updates["status"] = FactStatus.CONTRADICTED
                                updates["contradicted_by_fact_ids"] = tuple(
                                    sorted(
                                        other.id
                                        for other in bucket
                                        if _value_token(other.value) != _value_token(item.value)
                                    )
                                )
                            elif next_time is not None:
                                updates["status"] = FactStatus.SUPERSEDED
                            resolved.append(item.model_copy(update=updates))
                    continue
            for item in group:
                contradictions = tuple(
                    sorted(
                        other.id
                        for other in group
                        if _value_token(other.value) != _value_token(item.value)
                    )
                )
                resolved.append(
                    item.model_copy(
                        update={
                            "status": FactStatus.CONTRADICTED,
                            "contradicted_by_fact_ids": contradictions,
                        }
                    )
                )
        return resolved

    @staticmethod
    def _resolve_preference_statuses(preferences: list[Preference]) -> list[Preference]:
        grouped: dict[tuple[EntityKind, str, str, str], list[Preference]] = defaultdict(list)
        for preference in preferences:
            grouped[
                (
                    preference.subject.kind,
                    preference.subject.id,
                    preference.predicate,
                    _value_token(preference.object),
                )
            ].append(preference)
        resolved: list[Preference] = []
        for group in grouped.values():
            valences = {item.valence for item in group}
            if len(valences) <= 1:
                resolved.extend(group)
                continue
            by_time: dict[datetime, list[Preference]] = defaultdict(list)
            for item in group:
                by_time[_record_time(item)].append(item)
            ordered_times = sorted(by_time)
            for index, item_time in enumerate(ordered_times):
                bucket = by_time[item_time]
                next_time = ordered_times[index + 1] if index + 1 < len(ordered_times) else None
                bucket_valences = {item.valence for item in bucket}
                for item in bucket:
                    temporal = item.temporal
                    if next_time is not None and temporal.valid_to is None:
                        temporal = temporal.model_copy(update={"valid_to": next_time})
                    if len(bucket_valences) > 1:
                        contradictions = tuple(
                            sorted(other.id for other in bucket if other.valence != item.valence)
                        )
                        resolved.append(
                            item.model_copy(
                                update={
                                    "status": FactStatus.CONTRADICTED,
                                    "contradicted_by_preference_ids": contradictions,
                                    "temporal": temporal,
                                }
                            )
                        )
                    elif next_time is not None:
                        resolved.append(
                            item.model_copy(
                                update={
                                    "status": FactStatus.SUPERSEDED,
                                    "temporal": temporal,
                                }
                            )
                        )
                    else:
                        resolved.append(item.model_copy(update={"temporal": temporal}))
        return resolved


class _CandidateError(Exception):
    def __init__(self, reason: QuarantineReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _EvidenceContext:
    def __init__(
        self,
        observations: tuple[Observation, ...],
        claims: tuple[AtomicClaimInput, ...],
    ) -> None:
        self.observations = observations
        self.claims = claims

    @property
    def source_literals(self) -> tuple[str, ...]:
        return tuple(
            span.exact_literal for observation in self.observations for span in observation.spans
        ) + tuple(claim.span.exact_literal for claim in self.claims)

    def each_item_supports_literal(self, literal: str) -> bool:
        return all(
            any(literal in span.exact_literal for span in item.spans) for item in self.observations
        ) and all(literal in item.span.exact_literal for item in self.claims)

    @property
    def single_stated_at(self) -> datetime | None:
        values = {
            value
            for value in (
                *(item.temporal.stated_at for item in self.observations),
                *(item.occurred_at for item in self.claims),
            )
            if value is not None
        }
        return next(iter(values)) if len(values) == 1 else None

    @property
    def temporal(self) -> TemporalScope:
        stated = self.single_stated_at
        literals = {
            item.temporal.original_time_literal
            for item in self.observations
            if item.temporal.original_time_literal
        }
        return TemporalScope(
            stated_at=stated,
            time_precision=TimePrecision.EXACT if stated else TimePrecision.UNKNOWN,
            original_time_literal=next(iter(literals)) if len(literals) == 1 else None,
        )

    @property
    def sensitivity(self) -> Sensitivity:
        rank = {
            Sensitivity.PUBLIC: 0,
            Sensitivity.PERSONAL: 1,
            Sensitivity.UNCLASSIFIED: 2,
            Sensitivity.SENSITIVE: 3,
        }
        values = [item.sensitivity for item in self.observations] + [
            item.sensitivity for item in self.claims
        ]
        return max(values, key=rank.__getitem__) if values else Sensitivity.UNCLASSIFIED

    @property
    def confidence(self) -> float:
        values = [item.confidence for item in self.observations]
        values.extend(1.0 for _ in self.claims)
        return min(values) if values else 0.0

    @property
    def evidence(self) -> tuple[EvidenceReference, ...]:
        references: dict[str, EvidenceReference] = {}
        for observation in self.observations:
            for span in observation.spans:
                key = (
                    f"obs:{observation.id}:{span.entry_id}:{span.start}:{span.end}:"
                    f"{span.source_revision}"
                )
                references[key] = EvidenceReference(
                    entry_id=span.entry_id,
                    role=EvidenceRole.SUPPORTS,
                    episode_id=observation.episode_id,
                    observation_id=observation.id,
                    span=span,
                    sensitivity=observation.sensitivity,
                )
        for claim in self.claims:
            references[f"claim:{claim.id}:{claim.content_hash}"] = EvidenceReference(
                entry_id=claim.span.entry_id,
                role=EvidenceRole.SUPPORTS,
                claim_id=claim.id,
                span=claim.span,
                sensitivity=claim.sensitivity,
            )
        return tuple(references[key] for key in sorted(references))

    @property
    def identity_membership(self) -> tuple[str, ...]:
        values = {f"entry:{span.entry_id}" for item in self.observations for span in item.spans}
        values.update(f"entry:{item.span.entry_id}" for item in self.claims)
        return tuple(sorted(values))

    def contains_span(self, candidate: TextSpan) -> bool:
        source_spans = [span for item in self.observations for span in item.spans] + [
            item.span for item in self.claims
        ]
        for source in source_spans:
            if (
                candidate.entry_id != source.entry_id
                or candidate.start < source.start
                or candidate.end > source.end
            ):
                continue
            offset = candidate.start - source.start
            if (
                source.exact_literal[offset : offset + len(candidate.exact_literal)]
                == candidate.exact_literal
            ):
                return True
        return False

    @property
    def supported_people(self) -> set[str]:
        values: set[str] = set()
        for item in self.observations:
            if item.speaker_person_id:
                values.add(item.speaker_person_id)
            if item.subject_kind == EntityKind.PERSON and item.subject_id:
                values.add(item.subject_id)
            values.update(item.addressee_person_ids)
        for item in self.claims:
            values.add(item.asserted_by_person_id)
            if item.subject.kind == EntityKind.PERSON:
                values.add(item.subject.id)
        return values

    @property
    def supported_projects(self) -> set[str]:
        values = {
            item.subject_id
            for item in self.observations
            if item.subject_kind == EntityKind.PROJECT and item.subject_id
        }
        values.update(
            item.subject.id for item in self.claims if item.subject.kind == EntityKind.PROJECT
        )
        return values


def readiness_for_fact(fact: Fact) -> ReadinessReport:
    """Report deterministic eligibility; never activate a record or generation."""
    reasons: list[str] = []
    if fact.readiness != Readiness.STAGED:
        reasons.append("record_is_not_staged")
    if fact.derivation not in {DerivationKind.EXPLICIT, DerivationKind.USER_CONFIRMED}:
        reasons.append("fact_is_not_explicit")
    if fact.status != FactStatus.ACTIVE:
        reasons.append("fact_is_not_active")
    if fact.contradicted_by_fact_ids:
        reasons.append("fact_has_contradiction")
    if fact.confidence < 0.9:
        reasons.append("fact_confidence_below_exact_readiness_floor")
    if isinstance(fact.value, DateFactValue) and fact.value.ambiguity is not None:
        reasons.append("date_is_ambiguous")
    if not fact.evidence and fact.derivation != DerivationKind.USER_CONFIRMED:
        reasons.append("fact_has_no_evidence")
    return ReadinessReport(
        record_id=fact.id,
        eligible=not reasons,
        reasons=tuple(reasons),
    )


__all__ = [
    "AtomicClaimInput",
    "CandidateKind",
    "EventCandidate",
    "FACT_VALUE_KINDS",
    "FactCandidate",
    "MemoryCandidate",
    "MemoryCompilation",
    "MemoryCompiler",
    "MemoryQuarantine",
    "PREFERENCE_PREDICATES",
    "PinnedEntityReference",
    "PreferenceBasis",
    "PreferenceCandidate",
    "QuarantineReason",
    "ReadinessReport",
    "atomic_claim_content_hash",
    "normalize_date_literal",
    "readiness_for_fact",
]

"""Source-grounded literal observation extraction.

The semantic annotator is an untrusted proposal mechanism.  This module owns the
deterministic boundary that turns a proposal into a staged :class:`Observation`:
source spans must round-trip, episode and target membership must agree, and speaker
and subject identity must resolve from connector metadata.  Provider prose is never
accepted as evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from varyz.domain import Entry, Sensitivity, TimePrecision
from varyz.llm.refusal import refusal_reason
from varyz.personal_model.episodes import EpisodeBuildResult, TargetEpisodeCoverage
from varyz.personal_model.models import (
    AttributionState,
    ContextCompleteness,
    ConversationEpisode,
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
    derived_record_id,
)

log = logging.getLogger(__name__)

OBSERVATION_SCHEMA_VERSION = "literal-observation-candidate/v1"
#: The same string as a type, so it reaches the server as a JSON Schema
#: ``const`` and the grammar can emit nothing else.
#:
#: It used to be a free ``str`` the model had to copy back, which is the same
#: defect as asking it to echo an id: on 2026-09-06 a candidate window came
#: back declaring its schema version as ``":"`` -- one colon -- and the whole
#: window was thrown away for it. A window is a stretch of the archive that is
#: then simply never read. Nothing about the version is the model's to decide;
#: the caller chose the schema when it chose the grammar, so the grammar says
#: it. Spelt out rather than referring to the constant beside it because
#: ``Literal`` takes a literal; the test suite holds the two together.
_ObservationSchemaVersion = Literal["literal-observation-candidate/v1"]
ANNOTATION_GROUP_SCHEMA_VERSION = "literal-observation-group/v1"
DEFAULT_ANNOTATION_BATCH_SIZE = 8
# What reaches the model is the serialised request, not the text inside it. A
# chat episode carries around forty sources whose bodies run to a median of
# forty-five characters each, and every one of them travels wrapped in two
# sixty-four character hashes and its field names: measured at 411 characters
# of envelope per source, nine times the message it addresses. A budget spent
# on bodies alone therefore underestimates the request roughly ninefold, which
# is how batches sized at 16,000 characters arrived at the server as 64,000
# tokens and were refused there, one call at a time, in silence.
#
# Those hashes no longer travel — the request is renumbered before it is sent —
# and an episode now measures around 2,300 characters rather than 17,700. The
# budget below is for the whole group the compiler hands over at once, which is
# split into one call per server slot, so what has to fit a context window is a
# fraction of it.
#
# It is a group budget and not a call budget, which is worth saying plainly
# because it is the reason it cannot be read as a safety margin: raising it
# does not let any single call grow, and lowering it would not stop one. A call
# is bounded by its episode count alone, which is a separate defect.
#
# Raised from 160,000 when a group stopped being one call per slot and became
# four, so that the count is what ends a group and not the budget: at around
# 2,300 characters an episode, 192 episodes measure some 440,000.
DEFAULT_ANNOTATION_BATCH_CHARACTERS = 640_000
# A batch is later split across model calls, so its generous budget cannot
# protect one pathological episode from overrunning the model context by
# itself.  This limit is measured on the compact wire representation that is
# actually sent.  At roughly four characters per token, 20k leaves about
# 3,000 tokens of an 8,192-token context for the system prompt, schema, and
# answer.  Requests above it are a bounded coverage gap, not a provider fault.
DEFAULT_ANNOTATION_REQUEST_CHARACTERS = 20_000


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SourceTextRole(StrEnum):
    """The only role source bodies may have in an annotator request."""

    UNTRUSTED_SOURCE_DATA = "untrusted_source_data"


class AnnotationSource(_FrozenModel):
    entry_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    body: str
    source: str = Field(min_length=1)
    source_account: str
    speaker_source_key: str | None = None
    addressee_source_keys: tuple[str, ...] = ()
    source_order: int | str | None = None
    role: SourceTextRole = SourceTextRole.UNTRUSTED_SOURCE_DATA


class AnnotationSubjectResolution(_FrozenModel):
    """One resolver-established subject alias the annotator may reference."""

    source_key: str = Field(min_length=1)
    kind: EntityKind
    entity_id: str = Field(min_length=1)
    surface: str | None = Field(default=None, min_length=1)


def _request_dependency_fingerprint(
    *,
    episode_id: str,
    target_entry_ids: tuple[str, ...],
    context_complete: bool,
    sources: tuple[AnnotationSource, ...],
    allowed_subjects: tuple[AnnotationSubjectResolution, ...],
) -> str:
    material = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "episode_id": episode_id,
        "targets": target_entry_ids,
        "context_complete": context_complete,
        "sources": [
            {
                "entry_id": source.entry_id,
                "body_hash": hashlib.sha256(source.body.encode("utf-8")).hexdigest(),
                "source": source.source,
                "source_account": source.source_account,
                "speaker_source_key": source.speaker_source_key,
                "addressee_source_keys": source.addressee_source_keys,
                "source_order": source.source_order,
            }
            for source in sources
        ],
        "allowed_subjects": [item.model_dump(mode="json") for item in allowed_subjects],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AnnotationBatchRequest(_FrozenModel):
    """Bounded, inert source data supplied to a provider-neutral annotator."""

    schema_version: str = OBSERVATION_SCHEMA_VERSION
    episode_id: str = Field(min_length=1)
    target_entry_ids: tuple[str, ...]
    context_complete: bool
    sources: tuple[AnnotationSource, ...]
    allowed_subjects: tuple[AnnotationSubjectResolution, ...]
    dependency_fingerprint: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def valid_membership(self) -> AnnotationBatchRequest:
        source_ids = tuple(source.entry_id for source in self.sources)
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError("annotation sources must have unique entry ids")
        if not self.target_entry_ids or not set(self.target_entry_ids) <= set(source_ids):
            raise ValueError("annotation targets must belong to the source batch")
        if tuple(
            sorted(
                self.allowed_subjects,
                key=lambda item: (item.kind.value, item.source_key, item.entity_id),
            )
        ) != self.allowed_subjects:
            raise ValueError("allowed subject resolutions must be in canonical order")
        expected_fingerprint = _request_dependency_fingerprint(
            episode_id=self.episode_id,
            target_entry_ids=self.target_entry_ids,
            context_complete=self.context_complete,
            sources=self.sources,
            allowed_subjects=self.allowed_subjects,
        )
        if self.dependency_fingerprint != expected_fingerprint:
            raise ValueError("annotation dependency fingerprint does not match request inputs")
        return self


class CandidateSpan(_FrozenModel):
    entry_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    exact_literal: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> CandidateSpan:
        if self.end <= self.start:
            raise ValueError("candidate span end must follow start")
        if len(self.exact_literal) != self.end - self.start:
            raise ValueError("candidate literal length must equal its offsets")
        return self


class SubjectCandidate(_FrozenModel):
    attribution: AttributionState
    kind: EntityKind | None = None
    source_key: str | None = None
    refers_to_speaker: bool = False
    surface: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def reference_matches_attribution(self) -> SubjectCandidate:
        has_one_reference = self.refers_to_speaker != (self.source_key is not None)
        if self.attribution == AttributionState.RESOLVED and (
            not has_one_reference or self.kind is None
        ):
            raise ValueError("resolved subject needs one source reference and entity kind")
        if self.attribution != AttributionState.RESOLVED and (
            self.refers_to_speaker or self.source_key is not None
        ):
            raise ValueError("unresolved subject cannot assert a source reference")
        if self.refers_to_speaker and self.kind != EntityKind.PERSON:
            raise ValueError("the speaker can only be a person subject")
        return self


class ObservationCandidate(_FrozenModel):
    episode_id: str = Field(min_length=1)
    target_entry_id: str = Field(min_length=1)
    kind: ObservationKind
    proposition: str = Field(min_length=1)
    spans: tuple[CandidateSpan, ...]
    subject: SubjectCandidate
    dialogue_context: DialogueContext = DialogueContext.UNKNOWN
    polarity: Polarity = Polarity.UNKNOWN
    modality: ObservationModality = ObservationModality.UNKNOWN
    cost_or_stakes_literal: str | None = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def literal_fields_have_candidate_evidence(self) -> ObservationCandidate:
        if not self.spans:
            raise ValueError("an observation candidate requires source spans")
        if len({(span.entry_id, span.start, span.end) for span in self.spans}) != len(
            self.spans
        ):
            raise ValueError("candidate spans must be unique")
        if self.proposition not in {span.exact_literal for span in self.spans}:
            raise ValueError("proposition must be one exact source span, not a paraphrase")
        if self.cost_or_stakes_literal is not None and not any(
            self.cost_or_stakes_literal in span.exact_literal for span in self.spans
        ):
            raise ValueError("cost or stakes must be literal source text")
        return self


class AnnotationBatchResponse(_FrozenModel):
    schema_version: _ObservationSchemaVersion
    episode_id: str = Field(min_length=1)
    candidates: tuple[ObservationCandidate, ...] = ()


# A group of episodes shares one invocation, but the shape it is sent and
# answered in is not this module's: addressing a message by its sixty-four
# character id costs more of the window than the message itself, and the model
# transcribes such an id wrongly about as often as it does not. The projection
# that renumbers the exchange lives in
# :mod:`varyz.personal_model.annotation_wire`; what comes back from it is a
# plain :class:`AnnotationBatchResponse` per episode, as before.


class SemanticAnnotatorDescriptor(_FrozenModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    schema_version: str = OBSERVATION_SCHEMA_VERSION


type AnnotatorPayload = str | bytes | Mapping[str, Any] | AnnotationBatchResponse


@runtime_checkable
class SemanticAnnotator(Protocol):
    """Versioned structured-output port; provider SDKs belong in adapters."""

    @property
    def descriptor(self) -> SemanticAnnotatorDescriptor: ...

    def annotate(self, request: AnnotationBatchRequest) -> AnnotatorPayload: ...


@runtime_checkable
class GroupSemanticAnnotator(Protocol):
    def annotate_batch(
        self, requests: tuple[AnnotationBatchRequest, ...]
    ) -> Mapping[str, AnnotatorPayload]: ...


class ObservationBudgetExhausted(RuntimeError):
    """Control-flow signal: the caller declined to start another annotation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.partial_result: ObservationExtractionResult | None = None


class ObservationCacheMiss(RuntimeError):
    """A cache-only compile intentionally did not send this episode to a model."""


class QuarantineCode(StrEnum):
    CACHE_MISS = "cache_miss"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_OUTPUT = "malformed_output"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    RESPONSE_EPISODE_MISMATCH = "response_episode_mismatch"
    UNKNOWN_EPISODE = "unknown_episode"
    TARGET_NOT_COVERED = "target_not_covered"
    TARGET_UNKNOWN = "target_unknown"
    CROSS_EPISODE_REFERENCE = "cross_episode_reference"
    FABRICATED_SPAN = "fabricated_span"
    AMBIGUOUS_SPEAKER = "ambiguous_speaker"
    UNRESOLVED_SPEAKER = "unresolved_speaker"
    UNRESOLVED_SUBJECT = "unresolved_subject"
    INVENTED_SUBJECT_REFERENCE = "invented_subject_reference"
    AMBIGUOUS_SUBJECT_SURFACE = "ambiguous_subject_surface"
    INCOMPLETE_CONTEXT_HIGH_CONFIDENCE = "incomplete_context_high_confidence"
    EVIDENCE_AT_CONTEXT_CUT = "evidence_at_context_cut"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    REQUEST_TOO_LARGE = "request_too_large"


class EpisodeOrder(StrEnum):
    """How the episodes to annotate are put in order."""

    IDENTIFIER = "identifier"
    COVERAGE = "coverage"
    BREADTH = "breadth"


class ObservationSkipRule(StrEnum):
    EMPTY_TEXT = "empty_text"
    BARE_LINK = "bare_link"
    PLACEHOLDER_BOILERPLATE = "placeholder_boilerplate"
    UNATTRIBUTABLE_SPEAKER = "unattributable_speaker"
    MACHINE_GENERATED = "machine_generated"
    SOMEBODY_ELSES_COMMITS = "somebody_elses_commits"


class ObservationSkip(_FrozenModel):
    episode_id: str = Field(min_length=1)
    target_entry_ids: tuple[str, ...] = Field(min_length=1)
    rule: ObservationSkipRule
    detail: str = Field(min_length=1)


class ObservationAnnotationPlan(_FrozenModel):
    episodes_total: int = Field(ge=0)
    requests: tuple[AnnotationBatchRequest, ...] = ()
    skips: tuple[ObservationSkip, ...] = ()


class ObservationQuarantine(_FrozenModel):
    code: QuarantineCode
    episode_id: str | None = None
    target_entry_id: str | None = None
    candidate_index: int | None = Field(default=None, ge=0)
    candidate_fingerprint: str | None = None
    detail: str = Field(min_length=1)


class ObservationExtractionResult(_FrozenModel):
    observations: tuple[Observation, ...] = ()
    quarantines: tuple[ObservationQuarantine, ...] = ()
    skips: tuple[ObservationSkip, ...] = ()
    annotator_version: str = Field(min_length=1)
    schema_version: str = OBSERVATION_SCHEMA_VERSION

    @model_validator(mode="after")
    def unique_staged_observations(self) -> ObservationExtractionResult:
        ids = tuple(item.id for item in self.observations)
        if len(set(ids)) != len(ids):
            raise ValueError("observation ids must be unique")
        if any(item.readiness != Readiness.STAGED for item in self.observations):
            raise ValueError("literal extraction may emit staged observations only")
        return self


class SourceEntityResolution(_FrozenModel):
    """A previously established canonical entity alias, scoped to its source account."""

    source: str = Field(min_length=1)
    source_account: str
    source_key: str = Field(min_length=1)
    kind: EntityKind
    entity_id: str = Field(min_length=1)
    surface: str | None = None

    @model_validator(mode="after")
    def non_blank_surface(self) -> SourceEntityResolution:
        if self.surface is not None and not self.surface:
            raise ValueError("entity resolution surface cannot be blank")
        return self


class ObservationPolicy(_FrozenModel):
    # Bumped to 2 when the episode left an observation's identity, so records
    # written under the old rule cannot be mistaken for records written under
    # this one; the ids are different material and would collide on nothing.
    extraction_version: str = "2"
    high_confidence_threshold: float = Field(default=0.8, gt=0, le=1)


_SENSITIVITY_ORDER = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.UNCLASSIFIED: 3,
}

_BARE_LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PLACEHOLDER_TEXT = frozenset(
    {
        "<media omitted>",
        "image omitted",
        "media omitted",
        "message deleted",
        "this message was deleted",
        "unsupported message",
    }
)


# Words that agree, greet, laugh or acknowledge, and assert nothing about
# anybody.  The annotator returns them as propositions with ``confidence: 1.0``
# -- correctly, since it is certain the word is there -- and 5.5% of every
# candidate in the cache is one of them.  Synthesis then reads a wall of
# certain evidence that says "yes", "lol", "alr" and concludes from it.
#
# The set is closed and written out rather than derived, because every rule
# that would generate it is wrong.  Length does not work: 47% of propositions
# are twenty-four characters or shorter and among them are ``3000€``,
# ``15th april``, ``orange`` and ``8``, which are content.  A stop-word list
# does not work either, since ``fair``, ``solid``, ``bet`` and ``based`` carry
# meaning in a dictionary and none in a chat.  What is in here is what was read
# in the cache and judged to assert nothing; adding to it is a deliberate act.
_ACKNOWLEDGEMENT = frozenset(
    {
        # assent
        "yes", "yeah", "yea", "ye", "yep", "yup", "yess", "yesss", "ya", "yah",
        "aye", "ok", "okay", "okey", "oki", "okk", "k", "kk", "alr", "alright",
        "aight", "sure", "fine", "will do", "sounds good", "of course",
        "ofcourse", "for sure", "definitely", "absolutely", "indeed",
        # dissent
        "no", "nope", "nah", "naw",
        # appraisal that names nothing appraised
        "cool", "nice", "great", "good", "perfect", "awesome", "amazing",
        "excellent", "exactly", "true", "right", "correct", "agreed", "agree",
        "fair", "fair enough", "valid", "facts", "bet", "solid", "based",
        "well said", "same", "me too",
        # laughter and exclamation
        "lol", "lmao", "lmfao", "haha", "hahaha", "hehe", "omg", "wow", "damn",
        "oof", "yikes", "bruh", "bro",
        # hesitation
        "hm", "hmm", "hmmm", "huh", "oh", "ah", "ahh", "ohh", "eh", "uh", "um",
        "meh", "idk", "dunno",
        # courtesy
        "thanks", "thank you", "ty", "thx", "cheers", "np", "no worries",
        "no problem", "welcome", "you're welcome", "please", "sorry", "my bad",
        "oops", "congrats", "congratulations", "nvm", "never mind",
        # receipt
        "got it", "gotcha", "i see", "makes sense", "understood", "noted",
        # opening and closing
        "hi", "hey", "hello", "yo", "sup", "bye", "cya", "see you", "gn", "gm",
        "goodnight", "good night", "good morning",
        # the same, in Russian
        "да", "нет", "ок", "окей", "ага", "угу", "ясно", "понятно", "спасибо",
        "пока", "привет", "хорошо", "ладно", "конечно", "точно",
    }
)

_ACKNOWLEDGEMENT_EDGES = ".!?,…:;\"'()-–— "


_HELD_LAST_LETTER = re.compile(r"(.)\1+$")


def asserts_nothing(proposition: str) -> bool:
    """Whether a proposition carries no claim at all, about anyone.

    Three ways it can.  It is a word from the closed acknowledgement lexicon.
    It is that word with its last letter held down -- ``okayyy``, ``omgggg``,
    ``nooooooooooooooooooooo`` -- which is folded rather than listed, since the
    tail of spellings has no end and the fold only fires when what it produces
    is already in the lexicon, so ``all``, ``off`` and ``egg`` are untouched.
    Or it has no letter or digit in it at all and is punctuation or emoji.
    """

    folded = proposition.strip().lower().strip(_ACKNOWLEDGEMENT_EDGES).strip()
    if folded in _ACKNOWLEDGEMENT:
        return True
    held = _HELD_LAST_LETTER.sub(lambda match: match.group(1), folded)
    if held != folded and held in _ACKNOWLEDGEMENT:
        return True
    return not any(
        unicodedata.category(character)[0] in {"L", "N"} for character in proposition
    )


def observation_skip(
    episode: ConversationEpisode,
    target_ids: tuple[str, ...],
    entries: Mapping[str, Entry],
) -> ObservationSkip | None:
    """Return only high-precision reasons an episode cannot produce a ready observation.

    High-precision is the whole discipline here, and it was tested rather than
    assumed.  Five of them already withhold 6,126 of the 23,127 episodes an
    eight-hour compile can afford, so the obvious silence is gone before the
    model is asked anything, and what remains is not obvious at all.

    A rule for empty episodes was looked for against the model's verdict on
    1,414 of them, of which 394 came back with nothing.  Nothing predicts
    which.  A one-word target is empty 31.7% of the time against a 27.9% base
    rate, and skipping every one of them would save 21.9% of the calls at the
    price of 19.4% of the candidates -- an even trade, which is no trade.
    Length, character count, turns in the window, context size, who spoke,
    whether it asks a question, digits, links, emoji: every bucket of every
    one of them sits within a few points of the base rate.

    The reason is in what an episode is.  A candidate is read from the window,
    not the message: 40% of them quote a turn other than the target, so "yes"
    answering "are you coming tomorrow?" is a commitment and there is nothing
    in the word "yes" that says so.  And emptiness is not even a fixed property
    of an episode -- the same 696 episodes put twice to the same model agreed
    on empty-or-not only 78% of the time, so roughly a third of it is the
    model's coin rather than the archive's silence.

    Which bounds the whole idea: an oracle that skipped every empty episode and
    no other would save 28% of the calls.  A rule that guesses saves nothing.
    """

    targets = tuple(entries[item] for item in target_ids)
    bodies = tuple(item.body.strip() for item in targets)
    rule: ObservationSkipRule | None = None
    detail = ""
    if not any(bodies):
        rule = ObservationSkipRule.EMPTY_TEXT
        detail = "all target bodies are empty after trimming whitespace"
    elif all(_BARE_LINK.fullmatch(body) for body in bodies if body):
        rule = ObservationSkipRule.BARE_LINK
        detail = "every non-empty target body is a bare web link"
    elif all(body.casefold() in _PLACEHOLDER_TEXT for body in bodies if body):
        rule = ObservationSkipRule.PLACEHOLDER_BOILERPLATE
        detail = "every non-empty target body is a known content placeholder"
    elif not any(
        _source_key(item.conversation.sender_id, item.conversation.sender_label)
        for item in targets
    ):
        rule = ObservationSkipRule.UNATTRIBUTABLE_SPEAKER
        detail = "no target has connector-established speaker identity"
    elif all(
        item.source == "ai_conversations"
        and not item.authored_by_user
        for item in targets
    ):
        rule = ObservationSkipRule.MACHINE_GENERATED
        detail = "all targets are non-owner turns from a machine-generated source"
    elif all(item.source == "git" and not item.authored_by_user for item in targets):
        # A folder of repositories holds clones next to the owner's own work,
        # and on this machine 2,156 of 2,267 indexed commits were written by
        # somebody else. Nothing separated them here, so the compiler read a
        # cloned project's history as the owner's life: "What am I working
        # on?" answered AUTOMATIC1111, and two of that project's upstream
        # contributors were filed as projects of his. The git connector
        # already decides this per commit, by comparing the author address
        # against the identity git is configured to commit with in that
        # checkout; the archive keeps every one of those commits for recall
        # and understand, which is what they are good for. A model of one
        # person is not.
        rule = ObservationSkipRule.SOMEBODY_ELSES_COMMITS
        detail = "every target is a commit varyz cannot show the owner wrote"
    if rule is None:
        return None
    return ObservationSkip(
        episode_id=episode.id,
        target_entry_ids=target_ids,
        rule=rule,
        detail=detail,
    )


def annotation_request_characters(request: AnnotationBatchRequest) -> int:
    """Size of the request as the model will receive it.

    Measured on what is actually sent, which is the renumbered projection and
    not this module's own form: the same episode is some seven times smaller
    once its ids are replaced by positions, and a budget spent on the larger
    figure refuses batches that would have fitted comfortably.  Counting only
    the source bodies would be the opposite error, leaving the envelope around
    them — most of the payload either way — unbudgeted.

    The import is deferred because the projection is defined in terms of these
    contracts, and only one of the two directions can be a module import.
    """

    from varyz.personal_model.annotation_wire import compact_group_request

    payload, _legend = compact_group_request((request,))
    return len(payload.episodes[0].model_dump_json())


def form_annotation_batches(
    requests: Iterable[AnnotationBatchRequest],
    *,
    batch_size: int = DEFAULT_ANNOTATION_BATCH_SIZE,
    max_characters: int = DEFAULT_ANNOTATION_BATCH_CHARACTERS,
) -> tuple[tuple[AnnotationBatchRequest, ...], ...]:
    """Group requests by source scope so unrelated archives do not share a prompt."""

    if batch_size < 1:
        raise ValueError("annotation batch_size must be at least one")
    if max_characters < 1:
        raise ValueError("annotation max_characters must be at least one")
    by_scope: dict[tuple[str, str], list[AnnotationBatchRequest]] = {}
    for request in requests:
        first = request.sources[0]
        by_scope.setdefault((first.source, first.source_account), []).append(request)
    ordered = (
        request
        for scope in sorted(by_scope)
        for request in sorted(by_scope[scope], key=lambda item: item.episode_id)
    )
    return tuple(
        iter_annotation_batches(
            ordered,
            batch_size=batch_size,
            max_characters=max_characters,
        )
    )


def iter_annotation_batches(
    requests: Iterable[AnnotationBatchRequest],
    *,
    batch_size: int = DEFAULT_ANNOTATION_BATCH_SIZE,
    max_characters: int = DEFAULT_ANNOTATION_BATCH_CHARACTERS,
) -> Iterator[tuple[AnnotationBatchRequest, ...]]:
    """Yield bounded batches from requests already in their canonical order.

    Pending state is bounded by one batch per source scope.  Callers that need the
    legacy canonical ordering should order by source scope and episode id first;
    :func:`form_annotation_batches` remains the eager compatibility wrapper.
    """

    if batch_size < 1:
        raise ValueError("annotation batch_size must be at least one")
    if max_characters < 1:
        raise ValueError("annotation max_characters must be at least one")
    pending_by_scope: dict[
        tuple[str, str], tuple[list[AnnotationBatchRequest], int]
    ] = {}
    for request in requests:
        first = request.sources[0]
        scope = (first.source, first.source_account)
        pending, pending_characters = pending_by_scope.setdefault(scope, ([], 0))
        request_characters = annotation_request_characters(request)
        if pending and (
            len(pending) >= batch_size
            or pending_characters + request_characters > max_characters
        ):
            yield tuple(pending)
            pending = []
            pending_characters = 0
        pending.append(request)
        pending_characters += request_characters
        pending_by_scope[scope] = (pending, pending_characters)
    for scope in sorted(pending_by_scope):
        pending, _characters = pending_by_scope[scope]
        if pending:
            yield tuple(pending)


def _source_key(identifier: str | None, label: str | None) -> str | None:
    if identifier:
        return f"id:{identifier}"
    if label:
        return f"label:{label}"
    return None


def _addressee_keys(entry: Entry) -> tuple[str, ...]:
    metadata = entry.conversation
    identifiers = metadata.addressee_ids or metadata.recipient_ids
    labels = metadata.addressee_labels or metadata.recipient_labels
    keys = [f"id:{value}" for value in identifiers if value]
    keys.extend(f"label:{value}" for value in labels if value)
    return tuple(dict.fromkeys(keys))


def _fingerprint(value: Any) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    try:
        material = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        material = repr(type(value))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _UnreadCandidates(_FrozenModel):
    """The batch envelope with its candidates left exactly as they arrived.

    :class:`AnnotationBatchResponse` validates every candidate at once, so a
    single contradictory one takes the episode with it -- and the response is
    then never cached, so the episode is sent again, and refused again, on
    every run after this.  Reading the envelope on its own lets each candidate
    be judged by itself, which is what :meth:`_consume_response` already does
    for every other way a candidate can be wrong.
    """

    schema_version: str
    episode_id: str = Field(min_length=1)
    candidates: tuple[dict[str, Any], ...] = ()


def _parse_response(payload: AnnotatorPayload) -> AnnotationBatchResponse:
    if isinstance(payload, AnnotationBatchResponse):
        return payload
    if isinstance(payload, bytes):
        return AnnotationBatchResponse.model_validate_json(payload)
    if isinstance(payload, str):
        return AnnotationBatchResponse.model_validate_json(payload)
    return AnnotationBatchResponse.model_validate_json(
        json.dumps(payload, ensure_ascii=False, default=repr)
    )


def _parse_candidates_singly(
    payload: AnnotatorPayload,
) -> tuple[AnnotationBatchResponse, tuple[int, ...], tuple[tuple[int, str, str], ...]]:
    """Parse the envelope, then each candidate alone, keeping the ones that read.

    Each candidate is handed back to pydantic as JSON rather than as the dict
    it was parsed into.  Strict mode judges the two differently -- a whole
    number where a float belongs is a valid JSON float and an invalid Python
    one -- and reading a candidate here must refuse exactly what reading the
    whole batch refuses, no more.

    Returns the readable candidates, their positions in the original response,
    and one ``(position, fingerprint, reason)`` triple per candidate refused.
    """

    if isinstance(payload, AnnotationBatchResponse):
        return payload, tuple(range(len(payload.candidates))), ()
    if isinstance(payload, bytes | str):
        envelope = _UnreadCandidates.model_validate_json(payload)
    else:
        envelope = _UnreadCandidates.model_validate_json(
            json.dumps(payload, ensure_ascii=False, default=repr)
        )
    kept: list[ObservationCandidate] = []
    indices: list[int] = []
    refused: list[tuple[int, str, str]] = []
    for index, candidate in enumerate(envelope.candidates):
        try:
            parsed = ObservationCandidate.model_validate_json(
                json.dumps(candidate, ensure_ascii=False, default=repr)
            )
        except (ValidationError, ValueError, TypeError) as exc:
            refused.append((index, _fingerprint(candidate), refusal_reason(exc)))
        else:
            kept.append(parsed)
            indices.append(index)
    return (
        AnnotationBatchResponse(
            schema_version=envelope.schema_version,
            episode_id=envelope.episode_id,
            candidates=tuple(kept),
        ),
        tuple(indices),
        tuple(refused),
    )


def readable_candidate_count(payload: AnnotatorPayload) -> int:
    """How many candidates the extractor will get out of this reply.

    Raises whatever pydantic raises when the envelope itself cannot be read --
    truncated output, a shape this schema does not describe -- which is the
    only case where a reply is worth nothing at all.  A reply whose candidates
    are individually malformed still returns here, because that is exactly what
    :meth:`ObservationExtractor.extract` does with it: it reads the ones that
    parse and quarantines the rest.

    This exists so the annotation cache can hold what the reader accepts and
    not something stricter.  Written for the cache, kept here beside the two
    functions whose behaviour it has to match.
    """

    try:
        return len(_parse_response(payload).candidates)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        response, _positions, _refused = _parse_candidates_singly(payload)
        return len(response.candidates)


def _cut_edges(episode: ConversationEpisode) -> set[str]:
    """The turns an omission was made beside.

    A window over a conversation almost always has messages before it and
    after it, and each gap is recorded as an :class:`OmittedRange` naming the
    turn it follows and the turn it precedes.  Those two turns are where a
    proposition can mean something other than what it says -- an "I agree"
    that answers a caveat we cannot see, a "no" that refuses an offer outside
    the frame.  A turn in the middle of the window has its neighbours.

    This replaced a test on ``completeness``, which on the owner's archive was
    not COMPLETE for 97.4% of 116,498 episodes and earned it the same way in
    97.2%: the window had an outside, which is what a window is.  It was
    conjoined with ``confidence >= 0.8``, and the annotator returns 1.0 on
    98.6% of candidates -- told the context is incomplete, and answering 1.0
    regardless.  Two near-constants are not a filter; the pair kept 1,428
    observations out of 23,432 annotated episodes.  Adjacency separates the
    same archive 70.8% admitted to 24.9% refused, and refuses exactly where
    the cut touches the evidence.
    """

    edges: set[str] = set()
    for omitted in episode.omitted_ranges:
        if omitted.after_entry_id is not None:
            edges.add(omitted.after_entry_id)
        if omitted.before_entry_id is not None:
            edges.add(omitted.before_entry_id)
    return edges


class ObservationExtractor:
    """Validate annotator proposals and emit source-grounded staged observations."""

    def __init__(
        self,
        annotator: SemanticAnnotator,
        *,
        policy: ObservationPolicy | None = None,
    ) -> None:
        self.annotator = annotator
        self.policy = policy or ObservationPolicy()
        descriptor = annotator.descriptor
        if descriptor.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ValueError("annotator descriptor uses an unsupported schema version")

    def extract(
        self,
        episode_result: EpisodeBuildResult,
        entries: Iterable[Entry],
        *,
        entity_resolutions: Iterable[SourceEntityResolution],
        generation_id: str,
        created_at: datetime,
        batch_size: int = DEFAULT_ANNOTATION_BATCH_SIZE,
        max_characters: int = DEFAULT_ANNOTATION_BATCH_CHARACTERS,
        max_request_characters: int = DEFAULT_ANNOTATION_REQUEST_CHARACTERS,
        prefilter: bool = False,
        order: EpisodeOrder = EpisodeOrder.COVERAGE,
        defer_repeat_windows: bool = False,
        budget: AnnotationBudget | None = None,
    ) -> ObservationExtractionResult:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if not generation_id.strip():
            raise ValueError("generation_id must not be blank")
        if batch_size < 1:
            raise ValueError("annotation batch_size must be at least one")
        if max_characters < 1:
            raise ValueError("annotation max_characters must be at least one")
        if max_request_characters < 1:
            raise ValueError("annotation max_request_characters must be at least one")

        entry_by_id = self._entry_index(entries)
        resolution_index = self._resolution_index(entity_resolutions)
        coverage_by_target = {
            item.target_entry_id: item for item in episode_result.target_coverage
        }
        episode_by_id = {item.id: item for item in episode_result.episodes}
        observations: dict[str, Observation] = {}
        quarantines: list[ObservationQuarantine] = []
        skips: list[ObservationSkip] = []

        def planned_requests() -> Iterator[AnnotationBatchRequest]:
            for _episode, planned in iter_observation_annotation_items(
                episode_result,
                entry_by_id.values(),
                entity_resolutions=resolution_index.values(),
                prefilter=prefilter,
                order=order,
                defer_repeat_windows=defer_repeat_windows,
                budget=budget,
            ):
                if isinstance(planned, ObservationSkip):
                    skips.append(planned)
                elif isinstance(planned, ObservationQuarantine):
                    quarantines.append(planned)
                else:
                    size = annotation_request_characters(planned)
                    if size > max_request_characters:
                        # No batch containing this episode can fit, so sending it
                        # would buy a refusal at full price. Recording it keeps a
                        # too-large episode visible as a bounded, countable gap
                        # instead of a call that quietly returns nothing.
                        log.warning(
                            "episode %s needs %d characters, over the %d budget; "
                            "not sent for annotation",
                            planned.episode_id,
                            size,
                            max_request_characters,
                        )
                        quarantines.append(
                            ObservationQuarantine(
                                code=QuarantineCode.REQUEST_TOO_LARGE,
                                episode_id=planned.episode_id,
                                detail=(
                                    f"request of {size} characters exceeds the "
                                    f"{max_request_characters} character request budget"
                                ),
                            )
                        )
                        continue
                    yield planned

        for requests in iter_annotation_batches(
            planned_requests(), batch_size=batch_size, max_characters=max_characters
        ):
            group = tuple(
                (episode_by_id[request.episode_id], request) for request in requests
            )
            if len(group) > 1 and isinstance(self.annotator, GroupSemanticAnnotator):
                requests = tuple(item[1] for item in group)
                try:
                    raw_by_episode = self.annotator.annotate_batch(requests)
                except ObservationBudgetExhausted as exc:
                    exc.partial_result = self._partial_result(
                        observations, quarantines, skips
                    )
                    raise
                except Exception as exc:  # provider adapters are an untrusted failure boundary
                    # Quarantining without a log makes a wholly broken annotator look
                    # like an archive with nothing to say: the compile finishes, the
                    # calls are paid for, and no record is produced.
                    log.warning(
                        "grouped annotation of %d episodes failed with %s: %s",
                        len(group),
                        type(exc).__name__,
                        exc,
                    )
                    for episode, _request in group:
                        quarantines.append(
                            ObservationQuarantine(
                                code=QuarantineCode.PROVIDER_ERROR,
                                episode_id=episode.id,
                                detail=f"annotator failed with {type(exc).__name__}",
                            )
                        )
                    continue
                for episode, _request in group:
                    raw_response = raw_by_episode.get(episode.id)
                    if raw_response is None:
                        quarantines.append(
                            ObservationQuarantine(
                                code=(
                                    QuarantineCode.CACHE_MISS
                                    if getattr(self.annotator, "cached_only", False)
                                    else QuarantineCode.MALFORMED_OUTPUT
                                ),
                                episode_id=episode.id,
                                detail=(
                                    "annotation absent from cache; no model call made"
                                    if getattr(self.annotator, "cached_only", False)
                                    else "group response omitted the requested episode"
                                ),
                            )
                        )
                        continue
                    self._consume_response(
                        raw_response,
                        episode=episode,
                        observations=observations,
                        quarantines=quarantines,
                        entry_by_id=entry_by_id,
                        resolution_index=resolution_index,
                        coverage_by_target=coverage_by_target,
                        generation_id=generation_id,
                        created_at=created_at,
                    )
                continue
            for episode, request in group:
                try:
                    raw_response = self.annotator.annotate(request)
                except ObservationCacheMiss:
                    quarantines.append(
                        ObservationQuarantine(
                            code=QuarantineCode.CACHE_MISS,
                            episode_id=episode.id,
                            detail="annotation absent from cache; no model call made",
                        )
                    )
                    continue
                except ObservationBudgetExhausted as exc:
                    exc.partial_result = self._partial_result(
                        observations, quarantines, skips
                    )
                    raise
                except Exception as exc:  # provider adapters are an untrusted failure boundary
                    quarantines.append(
                        ObservationQuarantine(
                            code=QuarantineCode.PROVIDER_ERROR,
                            episode_id=episode.id,
                            detail=f"annotator failed with {type(exc).__name__}",
                        )
                    )
                    continue
                self._consume_response(
                    raw_response,
                    episode=episode,
                    observations=observations,
                    quarantines=quarantines,
                    entry_by_id=entry_by_id,
                    resolution_index=resolution_index,
                    coverage_by_target=coverage_by_target,
                    generation_id=generation_id,
                    created_at=created_at,
                )

        return ObservationExtractionResult(
            observations=tuple(observations[key] for key in sorted(observations)),
            quarantines=tuple(quarantines),
            skips=tuple(skips),
            annotator_version=self.annotator.descriptor.version,
        )

    def _partial_result(
        self,
        observations: Mapping[str, Observation],
        quarantines: Iterable[ObservationQuarantine],
        skips: Iterable[ObservationSkip],
    ) -> ObservationExtractionResult:
        return ObservationExtractionResult(
            observations=tuple(observations[key] for key in sorted(observations)),
            quarantines=tuple(quarantines),
            skips=tuple(skips),
            annotator_version=self.annotator.descriptor.version,
        )

    def _consume_response(
        self,
        raw_response: AnnotatorPayload,
        *,
        episode: ConversationEpisode,
        observations: dict[str, Observation],
        quarantines: list[ObservationQuarantine],
        entry_by_id: Mapping[str, Entry],
        resolution_index: Mapping[
            tuple[str, str, str, EntityKind], SourceEntityResolution
        ],
        coverage_by_target: Mapping[str, TargetEpisodeCoverage],
        generation_id: str,
        created_at: datetime,
    ) -> None:
        refused: tuple[tuple[int, str, str], ...] = ()
        try:
            response = _parse_response(raw_response)
            positions = tuple(range(len(response.candidates)))
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            try:
                response, positions, refused = _parse_candidates_singly(raw_response)
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
                # The envelope itself is unreadable -- truncated output, or a
                # shape this schema does not describe -- and there are no
                # candidates to salvage from it.
                quarantines.append(
                    ObservationQuarantine(
                        code=QuarantineCode.MALFORMED_OUTPUT,
                        episode_id=episode.id,
                        candidate_fingerprint=_fingerprint(raw_response),
                        detail=(
                            "annotator output failed schema validation: "
                            f"{type(exc).__name__}"
                        ),
                    )
                )
                return
        if response.schema_version != OBSERVATION_SCHEMA_VERSION:
            quarantines.append(
                ObservationQuarantine(
                    code=QuarantineCode.SCHEMA_VERSION_MISMATCH,
                    episode_id=episode.id,
                    candidate_fingerprint=_fingerprint(raw_response),
                    detail="annotator response uses an unsupported schema version",
                )
            )
            return
        if response.episode_id != episode.id:
            quarantines.append(
                ObservationQuarantine(
                    code=QuarantineCode.RESPONSE_EPISODE_MISMATCH,
                    episode_id=episode.id,
                    candidate_fingerprint=_fingerprint(raw_response),
                    detail="annotator response references a different episode",
                )
            )
            return

        for index, fingerprint, reason in refused:
            quarantines.append(
                ObservationQuarantine(
                    code=QuarantineCode.MALFORMED_OUTPUT,
                    episode_id=episode.id,
                    candidate_index=index,
                    candidate_fingerprint=fingerprint,
                    detail=f"candidate failed schema validation: {reason}",
                )
            )

        for index, candidate in zip(positions, response.candidates, strict=True):
            observation, quarantine = self._validate_candidate(
                candidate,
                candidate_index=index,
                episode=episode,
                entry_by_id=entry_by_id,
                resolution_index=resolution_index,
                coverage_by_target=coverage_by_target,
                generation_id=generation_id,
                created_at=created_at,
            )
            if quarantine is not None:
                quarantines.append(quarantine)
            elif observation is not None:
                if observation.id in observations:
                    quarantines.append(
                        self._quarantine(
                            QuarantineCode.DUPLICATE_CANDIDATE,
                            candidate,
                            index,
                            "candidate duplicates an observation in this extraction",
                        )
                    )
                else:
                    observations[observation.id] = observation

    @staticmethod
    def _entry_index(entries: Iterable[Entry]) -> dict[str, Entry]:
        result: dict[str, Entry] = {}
        for entry in entries:
            prior = result.get(entry.id)
            if prior is not None and prior != entry:
                raise ValueError(f"conflicting canonical entries for id {entry.id}")
            result[entry.id] = entry
        return result

    @staticmethod
    def _resolution_index(
        resolutions: Iterable[SourceEntityResolution],
    ) -> dict[tuple[str, str, str, EntityKind], SourceEntityResolution]:
        result: dict[tuple[str, str, str, EntityKind], SourceEntityResolution] = {}
        for resolution in resolutions:
            key = (
                resolution.source,
                resolution.source_account,
                resolution.source_key,
                resolution.kind,
            )
            prior = result.get(key)
            if prior is not None and prior != resolution:
                raise ValueError("one source alias resolves to conflicting canonical entities")
            result[key] = resolution
        return result

    @staticmethod
    def _targets_by_episode(
        coverage: Iterable[TargetEpisodeCoverage],
    ) -> dict[str, tuple[str, ...]]:
        target_sets: dict[str, set[str]] = {}
        for item in coverage:
            if item.quarantined:
                continue
            for episode_id in item.episode_ids:
                target_sets.setdefault(episode_id, set()).add(item.target_entry_id)
        return {key: tuple(sorted(value)) for key, value in target_sets.items()}

    @staticmethod
    def _request(
        episode: ConversationEpisode,
        target_ids: tuple[str, ...],
        entries: Mapping[str, Entry],
        resolutions: Mapping[
            tuple[str, str, str, EntityKind], SourceEntityResolution
        ],
    ) -> AnnotationBatchRequest:
        sources = []
        for turn in episode.turns:
            entry = entries[turn.entry_id]
            sources.append(
                AnnotationSource(
                    entry_id=entry.id,
                    content_hash=entry.content_hash,
                    body=entry.body,
                    source=entry.source,
                    source_account=entry.source_account,
                    speaker_source_key=_source_key(
                        entry.conversation.sender_id, entry.conversation.sender_label
                    ),
                    addressee_source_keys=_addressee_keys(entry),
                    source_order=entry.conversation.source_order,
                )
            )
        source_tuple = tuple(sources)
        episode_entry_ids = {turn.entry_id for turn in episode.turns}
        witnessed_keys = ObservationExtractor._witnessed_source_keys(
            episode_entry_ids, entries
        )
        source_scopes = {(source.source, source.source_account) for source in source_tuple}
        allowed_subjects = tuple(
            sorted(
                (
                    AnnotationSubjectResolution(
                        source_key=resolution.source_key,
                        kind=resolution.kind,
                        entity_id=resolution.entity_id,
                        surface=resolution.surface,
                    )
                    for resolution in resolutions.values()
                    if (resolution.source, resolution.source_account) in source_scopes
                    and (
                        resolution.source_key in witnessed_keys
                        or (
                            resolution.surface is not None
                            and any(
                                resolution.surface in source.body for source in source_tuple
                            )
                        )
                    )
                ),
                key=lambda item: (item.kind.value, item.source_key, item.entity_id),
            )
        )
        dependency_fingerprint = _request_dependency_fingerprint(
            episode_id=episode.id,
            target_entry_ids=target_ids,
            context_complete=episode.completeness == ContextCompleteness.COMPLETE,
            sources=source_tuple,
            allowed_subjects=allowed_subjects,
        )
        return AnnotationBatchRequest(
            episode_id=episode.id,
            target_entry_ids=target_ids,
            context_complete=episode.completeness == ContextCompleteness.COMPLETE,
            sources=source_tuple,
            allowed_subjects=allowed_subjects,
            dependency_fingerprint=dependency_fingerprint,
        )

    def _validate_candidate(
        self,
        candidate: ObservationCandidate,
        *,
        candidate_index: int,
        episode: ConversationEpisode,
        entry_by_id: Mapping[str, Entry],
        resolution_index: Mapping[
            tuple[str, str, str, EntityKind], SourceEntityResolution
        ],
        coverage_by_target: Mapping[str, TargetEpisodeCoverage],
        generation_id: str,
        created_at: datetime,
    ) -> tuple[Observation | None, ObservationQuarantine | None]:
        if candidate.episode_id != episode.id:
            return None, self._quarantine(
                QuarantineCode.UNKNOWN_EPISODE,
                candidate,
                candidate_index,
                "candidate references a different episode",
            )
        # Two different failures, and they were one code until 2026-08-31.
        #
        # On the live archive `target_not_covered` is the largest quarantine
        # class there is -- 14,504 of 19,650 on generation 01M1BW66, against
        # 3,702 observations that survived -- and nothing said which of these
        # it was, so nothing could be done about it. They want opposite
        # repairs. An id that names no entry at all is a model that mis-copied
        # one of the 64-character hexadecimal ids it is made to echo, and the
        # repair is to stop making it echo them -- done, by sending ordinals,
        # and on 2026-09-02 `target_unknown` was zero across the whole archive.
        # An id that names a real entry the request did not ask about is a
        # model reading the window it was given and answering about the message
        # the observation is actually in; the repair for that one is below, and
        # it is to keep it.
        #
        # The test is `entry_by_id`, not `coverage_by_target`: the coverage map
        # holds the targets this request asked about, so a miss there is either
        # of these and says nothing.
        if candidate.target_entry_id not in entry_by_id:
            return None, self._quarantine(
                QuarantineCode.TARGET_UNKNOWN,
                candidate,
                candidate_index,
                "candidate target does not name an entry",
            )
        episode_entry_ids = {turn.entry_id for turn in episode.turns}
        coverage = coverage_by_target.get(candidate.target_entry_id)
        if coverage is None or episode.id not in coverage.episode_ids:
            if candidate.target_entry_id not in episode_entry_ids:
                return None, self._quarantine(
                    QuarantineCode.TARGET_NOT_COVERED,
                    candidate,
                    candidate_index,
                    "candidate target is not a turn of this episode",
                )
            # A real message of this episode that the request did not name. It
            # used to be thrown away here, and that was the single largest loss
            # in the stage: on the live archive, 14,504 of 19,650 quarantines
            # against 3,702 observations kept.
            #
            # Which target a window was built for is bookkeeping. It appears
            # nowhere in the record below: the id, the evidence, the speaker,
            # the subject, the addressees, the temporal scope and the
            # sensitivity are all derived from the spans and the episode. It is
            # not what makes evidence sound, and every check that is -- the
            # span round-trip, episode membership, the cut edges, speaker and
            # subject resolution -- runs on this candidate either way.
            # `test_which_message_the_request_named_never_reaches_the_record`
            # holds that to be true rather than leaving it asserted. Replaying the
            # same 22,715 cached annotations with only this changed keeps 9,063
            # observations instead of 3,702, and of the 14,504: 5,971 turn out
            # to be duplicates of observations already kept, because an
            # observation's id is derived from its subject and its spans and
            # pools across every window over them; 3,124 are refused by
            # `evidence_at_context_cut`; 48 by `ambiguous_speaker`; and 5,361
            # are evidence the archive had and the model was not being given.
            #
            # The coverage this stands in for is the episode itself, which is
            # what is actually known: the message is whole inside this window,
            # and nothing says whether its own context was cut somewhere this
            # window does not reach. `omitted_entry_ids` is therefore empty
            # rather than guessed, which sends this candidate to the cut-edge
            # check below rather than the incomplete-context one -- the
            # stricter of the two for a message read through someone else's
            # window, and the reason 3,124 of them are refused there.
            coverage = TargetEpisodeCoverage(
                target_entry_id=candidate.target_entry_id,
                episode_ids=(episode.id,),
                covered_entry_ids=tuple(sorted(episode_entry_ids)),
            )

        if any(span.entry_id not in episode_entry_ids for span in candidate.spans):
            return None, self._quarantine(
                QuarantineCode.CROSS_EPISODE_REFERENCE,
                candidate,
                candidate_index,
                "candidate span leaves canonical episode membership",
            )

        spans: list[TextSpan] = []
        span_entries: list[Entry] = []
        for span in candidate.spans:
            entry = entry_by_id[span.entry_id]
            if (
                span.end > len(entry.body)
                or entry.body[span.start : span.end] != span.exact_literal
            ):
                return None, self._quarantine(
                    QuarantineCode.FABRICATED_SPAN,
                    candidate,
                    candidate_index,
                    "candidate offsets and literal do not round-trip to canonical source text",
                )
            spans.append(
                TextSpan(
                    entry_id=entry.id,
                    start=span.start,
                    end=span.end,
                    exact_literal=span.exact_literal,
                    source_revision=entry.content_hash,
                )
            )
            span_entries.append(entry)

        speaker_keys = {
            _source_key(entry.conversation.sender_id, entry.conversation.sender_label)
            for entry in span_entries
        }
        if None in speaker_keys or len(speaker_keys) != 1:
            return None, self._quarantine(
                QuarantineCode.AMBIGUOUS_SPEAKER,
                candidate,
                candidate_index,
                "candidate evidence lacks one attributable connector speaker",
            )
        speaker_source_key = next(iter(speaker_keys))
        assert speaker_source_key is not None
        speaker_entry = span_entries[0]
        speaker_resolution = resolution_index.get(
            (
                speaker_entry.source,
                speaker_entry.source_account,
                speaker_source_key,
                EntityKind.PERSON,
            )
        )
        if speaker_resolution is None:
            return None, self._quarantine(
                QuarantineCode.UNRESOLVED_SPEAKER,
                candidate,
                candidate_index,
                "connector speaker alias has no canonical person resolution",
            )

        if candidate.subject.attribution != AttributionState.RESOLVED:
            return None, self._quarantine(
                QuarantineCode.UNRESOLVED_SUBJECT,
                candidate,
                candidate_index,
                "annotator preserved required subject attribution as unresolved",
            )
        subject_key = (
            speaker_source_key
            if candidate.subject.refers_to_speaker
            else candidate.subject.source_key
        )
        assert subject_key is not None
        witnessed_keys = self._witnessed_source_keys(episode_entry_ids, entry_by_id)
        surface_occurrences = (
            sum(span.exact_literal.count(candidate.subject.surface) for span in candidate.spans)
            if candidate.subject.surface is not None
            else 0
        )
        if candidate.subject.surface is not None and surface_occurrences == 0:
            return None, self._quarantine(
                QuarantineCode.INVENTED_SUBJECT_REFERENCE,
                candidate,
                candidate_index,
                "candidate subject surface does not occur in its exact source spans",
            )
        if surface_occurrences > 1:
            return None, self._quarantine(
                QuarantineCode.AMBIGUOUS_SUBJECT_SURFACE,
                candidate,
                candidate_index,
                "candidate subject surface is not unique within its exact source spans",
            )
        assert candidate.subject.kind is not None
        subject_resolution = resolution_index.get(
            (
                speaker_entry.source,
                speaker_entry.source_account,
                subject_key,
                candidate.subject.kind,
            )
        )
        if subject_resolution is None:
            return None, self._quarantine(
                QuarantineCode.UNRESOLVED_SUBJECT,
                candidate,
                candidate_index,
                "candidate subject alias has no canonical entity resolution",
            )
        if subject_key not in witnessed_keys and (
            candidate.subject.surface is None
            or subject_resolution.surface != candidate.subject.surface
        ):
            return None, self._quarantine(
                QuarantineCode.UNRESOLVED_SUBJECT,
                candidate,
                candidate_index,
                "text-only subject requires an exact resolver-established alias surface",
            )
        if subject_key not in witnessed_keys:
            matching_entity_ids = {
                resolution.entity_id
                for (source, account, _key, kind), resolution in resolution_index.items()
                if source == speaker_entry.source
                and account == speaker_entry.source_account
                and kind == candidate.subject.kind
                and resolution.surface == candidate.subject.surface
            }
            if len(matching_entity_ids) != 1:
                return None, self._quarantine(
                    QuarantineCode.AMBIGUOUS_SUBJECT_SURFACE,
                    candidate,
                    candidate_index,
                    "text-only subject surface maps to multiple canonical entities",
                )
        speaker_person_id = speaker_resolution.entity_id
        subject_id = subject_resolution.entity_id

        if episode.completeness is ContextCompleteness.UNKNOWN or coverage.omitted_entry_ids:
            if candidate.confidence >= self.policy.high_confidence_threshold:
                return None, self._quarantine(
                    QuarantineCode.INCOMPLETE_CONTEXT_HIGH_CONFIDENCE,
                    candidate,
                    candidate_index,
                    "high-confidence candidate depends on an incomplete context window",
                )
        elif _cut_edges(episode) & {entry.id for entry in span_entries}:
            return None, self._quarantine(
                QuarantineCode.EVIDENCE_AT_CONTEXT_CUT,
                candidate,
                candidate_index,
                "candidate rests on a turn the context window was cut beside",
            )

        sensitivity = max(
            (episode.sensitivity, *(entry.sensitivity for entry in span_entries)),
            key=_SENSITIVITY_ORDER.__getitem__,
        )
        # The episode is where the observation was read, not what it rests on.
        # A message has up to seventeen windows over it, and the same sentence
        # quoted through two of them is one observation seen twice: with the
        # episode in the identity it was two records instead, differing in a
        # field no reader compares.  Without it the second arrival collides with
        # the first and is quarantined as the duplicate it is, which is also what
        # makes reading a message through more than one window worth doing.
        #
        # ``episode_id`` on the record still names one episode: the first one to
        # produce it, in the order episodes are annotated.
        evidence_membership = tuple(
            f"{span.entry_id}:{span.start}:{span.end}:{span.source_revision}"
            for span in spans
        )
        identity_subject = f"{candidate.subject.kind.value}:{subject_id}"
        observation_id = derived_record_id(
            "observation",
            canonical_subject=identity_subject,
            canonical_predicate=f"observation:{candidate.kind.value}",
            evidence_membership=evidence_membership,
            identity_version=self.policy.extraction_version,
        )
        evidence = tuple(
            EvidenceReference(
                entry_id=span.entry_id,
                role=EvidenceRole.SUPPORTS,
                episode_id=episode.id,
                span=span,
                sensitivity=entry_by_id[span.entry_id].sensitivity,
            )
            for span in spans
        )
        addressee_ids = self._resolved_addressees(
            span_entries,
            resolution_index,
        )
        temporal = self._temporal_scope(speaker_entry)
        observation = Observation(
            id=observation_id,
            generation_id=generation_id,
            evidence=evidence,
            temporal=temporal,
            confidence=candidate.confidence,
            sensitivity=sensitivity,
            derivation=DerivationKind.OBSERVED,
            producer=ProducerMetadata(
                kind=ProducerKind.LOCAL_MODEL,
                name=self.annotator.descriptor.name,
                version=self.annotator.descriptor.version,
                model_name=self.annotator.descriptor.model_name,
                prompt_version=self.annotator.descriptor.prompt_version,
            ),
            readiness=Readiness.STAGED,
            created_at=created_at,
            episode_id=episode.id,
            kind=candidate.kind,
            proposition=candidate.proposition,
            spans=tuple(spans),
            speaker_person_id=speaker_person_id,
            speaker_source_key=speaker_source_key,
            speaker_attribution=AttributionState.RESOLVED,
            subject_kind=candidate.subject.kind,
            subject_id=subject_id,
            subject_surface=candidate.subject.surface,
            subject_attribution=AttributionState.RESOLVED,
            addressee_person_ids=addressee_ids,
            dialogue_context=candidate.dialogue_context,
            polarity=candidate.polarity,
            modality=candidate.modality,
            cost_or_stakes_literal=candidate.cost_or_stakes_literal,
        )
        return observation, None

    @staticmethod
    def _witnessed_source_keys(
        episode_entry_ids: set[str], entry_by_id: Mapping[str, Entry]
    ) -> set[str]:
        witnessed: set[str] = set()
        for entry_id in episode_entry_ids:
            entry = entry_by_id[entry_id]
            metadata = entry.conversation
            for value in metadata.participant_ids:
                witnessed.add(f"id:{value}")
            for value in metadata.participant_labels:
                witnessed.add(f"label:{value}")
            speaker = _source_key(metadata.sender_id, metadata.sender_label)
            if speaker:
                witnessed.add(speaker)
            witnessed.update(_addressee_keys(entry))
        return witnessed

    @staticmethod
    def _resolved_addressees(
        entries: Iterable[Entry],
        resolutions: Mapping[
            tuple[str, str, str, EntityKind], SourceEntityResolution
        ],
    ) -> tuple[str, ...]:
        resolved: set[str] = set()
        for entry in entries:
            for source_key in _addressee_keys(entry):
                resolution = resolutions.get(
                    (entry.source, entry.source_account, source_key, EntityKind.PERSON)
                )
                if resolution:
                    resolved.add(resolution.entity_id)
        return tuple(sorted(resolved))

    @staticmethod
    def _temporal_scope(entry: Entry) -> TemporalScope:
        stated_at = None
        if entry.occurred_start:
            try:
                parsed = datetime.fromisoformat(entry.occurred_start.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
                stated_at = parsed
        return TemporalScope(
            stated_at=stated_at,
            time_precision=entry.time_precision if stated_at is not None else TimePrecision.UNKNOWN,
            original_time_literal=entry.occurred_start,
        )

    @staticmethod
    def _quarantine(
        code: QuarantineCode,
        candidate: ObservationCandidate,
        index: int,
        detail: str,
    ) -> ObservationQuarantine:
        return ObservationQuarantine(
            code=code,
            episode_id=candidate.episode_id,
            target_entry_id=candidate.target_entry_id,
            candidate_index=index,
            candidate_fingerprint=_fingerprint(candidate),
            detail=detail,
        )


type PlannedObservationAnnotation = (
    AnnotationBatchRequest | ObservationSkip | ObservationQuarantine
)


def coverage_ordered_episodes(
    episodes: Iterable[ConversationEpisode],
    entries: Mapping[str, Entry],
) -> tuple[ConversationEpisode, ...]:
    """Order episodes so that every prefix is a sample of the whole archive.

    Annotating the archive takes tens of hours, and any particular run stops
    when the budget, the deadline or the owner says so.  In identifier order
    that leaves a personal model built out of whichever conversations happened
    to sort first — a real model of a small, arbitrary corner of a life.

    So each episode is placed by where it falls within its own conversation:
    the k-th of n episodes in a thread sits at (k + 0.5) / n, and the whole
    archive is walked in that order.  A run that reaches a tenth of the way
    through has covered a tenth of every conversation, spread evenly over each
    one's history, rather than all of the first few.  Within a conversation the
    order is chronological, so the sample is of the whole life rather than of
    one season of it.

    The ordering is a pure function of content — no clock, no randomness — so a
    resumed run walks the same sequence and the annotations it already holds
    are still the ones it wants.
    """

    by_thread: dict[tuple[str, str], list[ConversationEpisode]] = {}
    for episode in episodes:
        scope = (episode.source, episode.source_thread_id or "")
        by_thread.setdefault(scope, []).append(episode)

    def occurred(episode: ConversationEpisode) -> str:
        entry = entries.get(episode.target_entry_id)
        return (entry.occurred_start if entry is not None else None) or ""

    placed: list[tuple[float, str, str, str, ConversationEpisode]] = []
    for scope, group in by_thread.items():
        group.sort(key=lambda item: (occurred(item), item.id))
        total = len(group)
        for index, episode in enumerate(group):
            placed.append(
                ((index + 0.5) / total, scope[0], scope[1], episode.id, episode)
            )
    placed.sort(key=lambda item: item[:4])
    return tuple(item[4] for item in placed)


def _spread_positions(count: int) -> tuple[int, ...]:
    """Chronological positions reordered so that any prefix spans the whole run.

    Taking a conversation's episodes in order would make a short run a reading
    of everyone's oldest messages.  Sorting the positions by their bit-reversal
    takes the first, then the middle, then the quarters, so a prefix of any
    length is spread across the thread's history rather than pooled at its
    start.  Pure arithmetic on the position, so it is the same sequence on every
    run.
    """

    if count <= 2:
        return tuple(range(count))
    width = 1
    while width < count:
        width <<= 1

    def reversed_bits(position: int) -> int:
        value, result, remaining = position, 0, width
        while remaining > 1:
            result = (result << 1) | (value & 1)
            value >>= 1
            remaining >>= 1
        return result

    return tuple(sorted(range(count), key=lambda item: (reversed_bits(item), item)))


def breadth_ordered_episodes(
    episodes: Iterable[ConversationEpisode],
    entries: Mapping[str, Entry],
) -> tuple[ConversationEpisode, ...]:
    """Order episodes so that every prefix covers as many conversations as it can.

    Coverage order samples each conversation in proportion to its size, which is
    the right answer for a sample of the archive and the wrong one for the only
    thing a durable reading can be built from.  An interpretation needs the same
    disposition witnessed in two conversations — the validator counts contexts,
    and a context is a thread — so what a run can conclude is bounded by the
    conversation *pairs* it reached, not by the episodes it read.

    Proportional placement reaches those pairs late.  A thread of n episodes has
    its first at (0.5 / n) of the walk, so the median conversation here — two
    episodes — is not touched until a quarter of the way in, and a thread with
    one episode not until halfway.  Measured on this archive: a fifth of the
    walk in coverage order has touched 191 of 404 conversations, where the same
    fifth here touches all 404.  That is 4.5x the pairs for the same reading.

    So conversations are taken in turn: every thread's first episode, then every
    thread's second, and so on, each thread's own episodes spread across its
    history by :func:`_spread_positions` rather than run from its start.  Ties
    inside a round go to the lower source and thread, then the lower episode id,
    so the order is a pure function of content and a resumed run walks the same
    sequence.

    What this gives up is proportionality: the largest conversation is 31% of
    this archive and is read at the same rate as a thread of two, so a prefix
    under-reads it on purpose.  That is the trade — a model of more of the
    life's relationships instead of more of its busiest one.

    **Measured against coverage order on a matched pair of 45-minute runs, this
    loses, and it is not the default.**  Breadth reached 15,795 messages to
    coverage's 5,314 for the same 1,749 vs 1,765 episodes — and found 23
    qualifying dispositions to coverage's 78, in 19 distinct sentences to 45,
    with one cluster spanning two conversations in the coverage arm and none
    here.  Its candidates are also dirtier: 35% of them trip the known label
    errors against coverage's 17%, because the thin windows it favours skew the
    model towards `commitment` (73 vs 52) and away from `reason` (5 vs 48).

    The reason the pair count did not convert is in the shape of the archive.
    The median conversation is one episode, so taking threads in turn spends the
    budget on windows with almost no context — mean prompt 4,861 tokens against
    coverage's 6,088 — and a disposition is read from the window, not the
    target.  More conversations reached, less said in each.  The "4.5x the
    pairs" above is still true as a count of pairs; pairs are simply not the
    binding constraint, and this docstring previously implied they were.
    """

    by_thread: dict[tuple[str, str], list[ConversationEpisode]] = {}
    for episode in episodes:
        scope = (episode.source, episode.source_thread_id or "")
        by_thread.setdefault(scope, []).append(episode)

    def occurred(episode: ConversationEpisode) -> str:
        entry = entries.get(episode.target_entry_id)
        return (entry.occurred_start if entry is not None else None) or ""

    placed: list[tuple[int, str, str, str, ConversationEpisode]] = []
    for scope, group in by_thread.items():
        group.sort(key=lambda item: (occurred(item), item.id))
        for round_number, position in enumerate(_spread_positions(len(group))):
            episode = group[position]
            placed.append((round_number, scope[0], scope[1], episode.id, episode))
    placed.sort(key=lambda item: item[:4])
    return tuple(item[4] for item in placed)


# What one hour of annotation buys, measured on this machine at six slots over
# a 47-minute run of the v4 exchange: 282 calls of eight episodes each, 2,256
# episodes in 2,823 seconds, 0.799 a second. It is a property of the card and
# the model rather than of the archive, and it is the one number here that has
# to be re-measured when either changes — an optimistic rate does not make a
# compile faster, it makes the budget silently untrue.
#
# Which is why it is written down low. The run above was 92% WhatsApp, and the
# telegram, git and AI-conversation episodes that now reach the model have not
# been timed; 2,800 leaves room for them to be dearer than they look. The
# figure is otherwise measured over sends only. Skips cost nothing and are now
# 0.6% of the archive rather than 19%, and cache hits cost nothing either, but
# a cold pass has none to speak of and a budget must be true of a cold pass.
#
# It has been re-measured twice. 2,100 came from the v1 exchange, which was
# quicker per episode only because "unknown" was on offer and writing it is
# cheap — every one of those 827 answers was refused downstream — so it was cut
# to 1,340 on the exchange that produces a usable reply. That 1,340 was 422
# episodes over nineteen minutes and it is now 2.1× too low: the four-calls-a-
# slot change and the renumbered wire both landed after it, and neither of them
# made the reply cheaper to write, only quicker to arrive. The direction is the
# point in both cases — the rate is not a fact about the model alone, it is a
# fact about the model as this code currently asks it.
MEASURED_EPISODES_PER_HOUR = 2800.0
DEFAULT_ANNOTATION_BUDGET_HOURS = 8.0


@dataclass(frozen=True)
class AnnotationBudget:
    """How long the owner is prepared to leave the archive being read.

    Every message is a target, and a target is read in each of the episodes that
    contain it — 116,498 episodes for 45,601 messages, a little over three
    windows each.  That is not waste.  Measured on the windows that had already
    been judged, two windows over the same message share about one turn in
    seventy: they are different conversational moments, not the same moment read
    twice, and dropping one drops evidence rather than duplication.

    So the redundancy is worth paying for when it can be afforded and not when
    it cannot, which is a question about the owner's day rather than about the
    archive.  Under the budget every window is kept.  Over it, each message
    keeps its best few, and how many is whatever fits.
    """

    hours: float = DEFAULT_ANNOTATION_BUDGET_HOURS
    episodes_per_hour: float = MEASURED_EPISODES_PER_HOUR

    @property
    def episodes(self) -> int:
        """How many episodes the budget pays for. Zero means no limit."""

        if self.hours <= 0 or self.episodes_per_hour <= 0:
            return 0
        return int(self.hours * self.episodes_per_hour)


def _windows_kept(
    ordered: Sequence[ConversationEpisode],
    targets_by_episode: Mapping[str, tuple[str, ...]],
    windows: int,
) -> list[ConversationEpisode]:
    """The episodes needed to give every message up to ``windows`` of them."""

    used: Counter[str] = Counter()
    kept: list[ConversationEpisode] = []
    for episode in ordered:
        targets = targets_by_episode.get(episode.id, ())
        if not targets:
            # Nothing to annotate, and a quarantine record to produce; it is
            # kept so that a budget never makes a gap disappear silently.
            kept.append(episode)
            continue
        if any(used[target] < windows for target in targets):
            kept.append(episode)
            used.update(targets)
    return kept


def episodes_within_budget(
    episode_result: EpisodeBuildResult,
    budget: AnnotationBudget | None,
) -> tuple[tuple[ConversationEpisode, ...], int]:
    """Trim the windows per message until the whole archive fits the budget.

    Returns the episodes to read and how many windows each message was allowed,
    zero meaning it kept all of them.  The windows kept are the longest ones,
    because context is the entire reason an episode is larger than its target:
    a short reply can only be read from the question it answers and the reply it
    draws.  Ties go to the lower episode id, so the same archive and the same
    budget always choose the same reading.

    One episode is usually the window for more than one message, so the choice
    cannot be made a message at a time; an episode is kept here when any message
    in it still wants a window, which is what stops the last window of a quiet
    message being thrown away to trim a talkative one.
    """

    episodes = tuple(episode_result.episodes)
    affordable = budget.episodes if budget is not None else 0
    if affordable <= 0 or len(episodes) <= affordable:
        return episodes, 0
    targets_by_episode = ObservationExtractor._targets_by_episode(
        episode_result.target_coverage
    )
    most = max(
        (len(row.episode_ids) for row in episode_result.target_coverage), default=1
    )
    ordered = sorted(episodes, key=lambda item: (-len(item.turns), item.id))
    chosen: list[ConversationEpisode] | None = None
    windows = 1
    for candidate in range(1, most + 1):
        kept = _windows_kept(ordered, targets_by_episode, candidate)
        if chosen is not None and len(kept) > affordable:
            break
        chosen, windows = kept, candidate
        if len(kept) > affordable:
            # Even one window each is more than the budget buys. Reading is
            # stopped by the budget rather than by this, and coverage order
            # makes what does get read a sample of the whole archive.
            break
    assert chosen is not None
    log.warning(
        "annotating %d of %d episodes: %d hours buys about %d, so each message "
        "keeps its longest %d window(s) of the %d it has at most",
        len(chosen),
        len(episodes),
        budget.hours if budget is not None else 0,
        affordable,
        windows,
        most,
    )
    return tuple(chosen), windows


def first_read_first(
    ordered: Sequence[ConversationEpisode],
    targets_by_episode: Mapping[str, Sequence[str]],
) -> tuple[ConversationEpisode, ...]:
    """Defer an episode whose every target has already been read this walk.

    A message near a boundary is the target of more than one window — one where
    it closes a conversation and one where it opens the next — and those windows
    are annotated separately at full price.

    So the repeats are moved behind the first reads rather than dropped: a full
    pass still annotates every window, in a different order, and a stopped run
    has spent its hours seeing more of the archive once instead of some of it
    twice.  Both partitions keep the order they arrived in, so this composes with
    whatever ordering produced them and stays a pure function of content.

    **At the default budget this is very nearly nothing, and it is off.**  The
    "45% of entries are read more than once" figure this was built on came from
    the accumulated cache across runs at different settings, not from one
    budgeted pass, and it does not survive being measured properly.  Eight hours
    keeps one window a message: 23,680 episodes covering all 45,601 targets at
    1.05 readings each, of which only 2,113 messages are read twice.  This
    defers 1,090 of 23,680 episodes — 4.6% of the pass, not 45%.

    It is kept, off, because the deferral is free and grows with the budget: a
    twenty-four-hour run keeps two or three windows a message and the repeat
    fraction rises with it.  Do not re-measure it at eight hours.
    """

    seen: set[str] = set()
    first: list[ConversationEpisode] = []
    repeats: list[ConversationEpisode] = []
    for episode in ordered:
        targets = targets_by_episode.get(episode.id, ())
        # An episode with no coverage at all is not a repeat of anything; it has
        # to stay in the first pass to be quarantined where it would have been.
        if not targets or any(target not in seen for target in targets):
            seen.update(targets)
            first.append(episode)
        else:
            repeats.append(episode)
    return tuple(first) + tuple(repeats)


def iter_observation_annotation_items(
    episode_result: EpisodeBuildResult,
    entries: Iterable[Entry],
    *,
    entity_resolutions: Iterable[SourceEntityResolution],
    prefilter: bool = True,
    order: EpisodeOrder = EpisodeOrder.COVERAGE,
    defer_repeat_windows: bool = False,
    budget: AnnotationBudget | None = None,
) -> Iterator[tuple[ConversationEpisode, PlannedObservationAnnotation]]:
    """Reconstruct the exact pre-model decision one episode at a time."""

    entry_by_id = ObservationExtractor._entry_index(entries)
    resolution_index = ObservationExtractor._resolution_index(entity_resolutions)
    targets_by_episode = ObservationExtractor._targets_by_episode(
        episode_result.target_coverage
    )
    affordable, _windows = episodes_within_budget(episode_result, budget)
    if order is EpisodeOrder.COVERAGE:
        ordered = coverage_ordered_episodes(affordable, entry_by_id)
    elif order is EpisodeOrder.BREADTH:
        ordered = breadth_ordered_episodes(affordable, entry_by_id)
    else:
        ordered = tuple(sorted(affordable, key=lambda item: item.id))
    if defer_repeat_windows:
        ordered = first_read_first(ordered, targets_by_episode)
    for episode in ordered:
        target_ids = targets_by_episode.get(episode.id, ())
        if not target_ids:
            yield episode, ObservationQuarantine(
                code=QuarantineCode.UNKNOWN_EPISODE,
                episode_id=episode.id,
                detail="episode has no target coverage and was not annotated",
            )
            continue
        if any(turn.entry_id not in entry_by_id for turn in episode.turns):
            yield episode, ObservationQuarantine(
                code=QuarantineCode.CROSS_EPISODE_REFERENCE,
                episode_id=episode.id,
                detail="canonical entry bodies are missing for episode membership",
            )
            continue
        skip = observation_skip(episode, target_ids, entry_by_id) if prefilter else None
        if skip is not None:
            yield episode, skip
            continue
        yield episode, ObservationExtractor._request(
            episode,
            target_ids,
            entry_by_id,
            resolution_index,
        )


def plan_observation_annotations(
    episode_result: EpisodeBuildResult,
    entries: Iterable[Entry],
    *,
    entity_resolutions: Iterable[SourceEntityResolution],
    budget: AnnotationBudget | None = None,
) -> ObservationAnnotationPlan:
    """Build the exact deterministic pre-model plan used by the estimator."""

    requests: list[AnnotationBatchRequest] = []
    skips: list[ObservationSkip] = []
    for _episode, planned in iter_observation_annotation_items(
        episode_result,
        entries,
        entity_resolutions=entity_resolutions,
        budget=budget,
    ):
        if isinstance(planned, AnnotationBatchRequest):
            requests.append(planned)
        elif isinstance(planned, ObservationSkip):
            skips.append(planned)
    return ObservationAnnotationPlan(
        episodes_total=len(episode_result.episodes),
        requests=tuple(requests),
        skips=tuple(skips),
    )

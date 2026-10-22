"""Ordered, checkpointable compiler for the canonical personal model.

The compiler coordinates pure domain modules. Provider SDKs, broad filesystem
access and product projections stay outside this module. A run pins one graph
snapshot and an exact evidence projection, stages every output, replays the
override prefix captured at generation creation, and only then swaps the active
generation pointer.
"""

from __future__ import annotations

import gc
import hashlib
import heapq
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from varyz.db.connection import LockHeldError, WriteLock
from varyz.domain import Entry, Sensitivity, iso, new_id
from varyz.personal_model.episodes import EpisodeBuildResult, EpisodeReconstructor
from varyz.personal_model.identity import (
    IdentityOverride,
    IdentityOverrideAction,
    IdentityResolutionResult,
    PersonResolver,
    SourceAlias,
    SourcePerson,
)
from varyz.personal_model.memory import (
    AtomicClaimInput,
    CandidateKind,
    EventCandidate,
    FactCandidate,
    MemoryCompiler,
    PinnedEntityReference,
    PreferenceBasis,
    PreferenceCandidate,
    atomic_claim_content_hash,
)
from varyz.personal_model.models import (
    AliasKind,
    CandidateAssemblyError,
    ContextCompleteness,
    ConversationEpisode,
    DerivationKind,
    DialogueContext,
    EntityKind,
    Fact,
    FactStatus,
    FactValueKind,
    Goal,
    Interpretation,
    InterpretationVerdict,
    Observation,
    ObservationModality,
    Person,
    Polarity,
    Preference,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Project,
    Readiness,
    RelationshipSnapshot,
    SubjectReference,
    TextSpan,
)
from varyz.personal_model.observations import (
    DEFAULT_ANNOTATION_BATCH_SIZE,
    DEFAULT_ANNOTATION_BUDGET_HOURS,
    MEASURED_EPISODES_PER_HOUR,
    AnnotationBatchRequest,
    AnnotationBatchResponse,
    AnnotationBudget,
    EpisodeOrder,
    ObservationBudgetExhausted,
    ObservationCacheMiss,
    ObservationExtractionResult,
    ObservationExtractor,
    SemanticAnnotator,
    SemanticAnnotatorDescriptor,
    readable_candidate_count,
    refusal_reason,
)
from varyz.personal_model.preference_sources import (
    ai_project_scope,
    communication_guidance_phrases,
    communication_theme,
    explicit_preference_phrases,
    explicit_thread_project_anchor,
    project_guidance_phrases,
    resets_design_direction,
)
from varyz.personal_model.projects import (
    ActionHandoff,
    GoalEvidenceCandidate,
    ProjectCompilation,
    ProjectEvidenceCandidate,
    ProjectHierarchyCompiler,
    ProjectHierarchyOverride,
    ProjectOverrideAction,
    ProposedNodeKind,
    identifies_an_account,
    names_something,
)
from varyz.personal_model.readiness import (
    GenerationReadiness,
    LifeLookupFixture,
    PersonalModelReadinessEvaluator,
    ReadinessInputs,
)
from varyz.personal_model.relationships import (
    ApprovedRelationshipRecord,
    CorrelationAssignment,
    ObservationRelationshipContext,
    RelationshipHistory,
    RelationshipHistoryCompiler,
    RelationshipPeriod,
)
from varyz.personal_model.repository import (
    FactValuePayload,
    InterpretationEditPayload,
    InterpretationVerdictPayload,
    MergePayload,
    ModelDependency,
    ModelGeneration,
    ModelOverride,
    OverrideAction,
    OverrideTargetKind,
    PersonalModelRecord,
    PersonalModelRepository,
    PreferenceValuePayload,
    ReclassifyPayload,
    ReparentPayload,
    ReplayOutcome,
    SplitPayload,
    SupersedePayload,
    canonical_json,
    compiler_input_dependency,
    compute_dependency_hash,
    content_hash,
    model_record_dependency,
    record_kind,
    required_model_references,
    required_record_dependencies,
)
from varyz.personal_model.synthesis import EpisodeCorrelation, SynthesisResult
from varyz.personal_model.validation import (
    InterpretationValidator,
    OwnerInterpretationReview,
    ValidationPolicy,
)

log = logging.getLogger(__name__)


class PersonalModelCompileAlreadyRunning(RuntimeError):
    """Another process owns the long-lived personal-model compile claim."""


def personal_model_compile_claim_path(data_dir: Path) -> Path:
    return data_dir / "personal-model-compile.lock"


def _personal_model_compile_metadata_path(data_dir: Path) -> Path:
    return data_dir / "personal-model-compile.lock.json"


def read_personal_model_compile_claim(data_dir: Path) -> dict[str, Any]:
    """Read holder metadata without opening and closing the locked file."""
    metadata = _personal_model_compile_metadata_path(data_dir)
    if metadata.is_file():
        return json.loads(metadata.read_text(encoding="utf-8"))
    # Pre-portability releases stored JSON after the locked byte.
    raw = personal_model_compile_claim_path(data_dir).read_bytes()
    return json.loads(raw.lstrip(b"\0").decode("utf-8"))


class PersonalModelCompileClaim(WriteLock):
    """Crash-safe compile singleton that does not exclude archive writers."""

    def __init__(self, data_dir: Path, *, generation_id: str | None):
        super().__init__(data_dir)
        self.path = personal_model_compile_claim_path(data_dir)
        self.generation_id = generation_id
        self.claim_id = new_id()

    def __enter__(self) -> PersonalModelCompileClaim:
        try:
            super().__enter__()
        except LockHeldError as exc:
            holder = self._holder_description()
            raise PersonalModelCompileAlreadyRunning(
                f"Personal-model compile {holder} is already running"
            ) from exc
        payload = {
            "claim_id": self.claim_id,
            "generation_id": self.generation_id,
            "pid": os.getpid(),
            "started_at": iso(datetime.now(UTC)),
        }
        try:
            metadata = _personal_model_compile_metadata_path(self.path.parent)
            stage = metadata.with_name(f".{metadata.name}.{self.claim_id}.tmp")
            stage.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(stage, metadata)
        except Exception:
            super().__exit__()
            raise
        return self

    def _holder_description(self) -> str:
        try:
            value = read_personal_model_compile_claim(self.path.parent)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return f"holding {self.path.name}"
        claim_id = str(value.get("claim_id") or "holding the claim")
        details = [
            f"PID {value['pid']}" if value.get("pid") is not None else "",
            f"started {value['started_at']}" if value.get("started_at") else "",
            (f"generation {value['generation_id']}" if value.get("generation_id") else ""),
        ]
        suffix = ", ".join(detail for detail in details if detail)
        return f"{claim_id} ({suffix})" if suffix else claim_id


class _WriteLockedPort:
    """Take the archive writer lock only while a compiler port mutates SQLite."""

    def __init__(self, target: Any, data_dir: Path, write_methods: frozenset[str]):
        self._target = target
        self._data_dir = data_dir
        self._write_methods = write_methods
        self._held = False

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name not in self._write_methods:
            return attribute

        @wraps(attribute)
        def locked(*args, **kwargs):
            if self._held:
                return attribute(*args, **kwargs)
            with WriteLock(self._data_dir):
                return attribute(*args, **kwargs)

        return locked

    @contextmanager
    def held(self) -> Iterator[None]:
        """Hold the writer lock once for a run of writes rather than once per write."""
        if self._held:
            yield
            return
        with WriteLock(self._data_dir):
            self._held = True
            try:
                yield
            finally:
                self._held = False


# Repository writes per transaction and writer lock: one commit per record
# synced the disk tens of thousands of times in a compile.
_WRITE_BATCH = 500
# And how long one transaction may keep the archive: the rules daemon and the
# preference reader wait at most 5 s for it (busy_timeout), and a slow laptop
# takes several times longer than a fast one for the same 500 records.
_WRITE_BATCH_SECONDS = 1.0
# And the pause between two transactions. A waiting writer only looks again
# every 100 ms; started back to back, the compile took the archive again
# before it looked, and on one slow core writers waited 8 s and failed.
_WRITE_PAUSE_SECONDS = 0.1
# Coverage rows per transaction: a full archive has about 41,000, and in one
# transaction they kept the archive 5.6 s on a slow laptop.
_COVERAGE_BATCH = 2000

_REPOSITORY_WRITES = frozenset(
    {
        "append_override",
        "clone_record_if_unchanged",
        "create_generation",
        "fail_generation",
        "quarantine",
        "record_metric",
        "record_override_replay",
        "stage_record",
    }
)
_STATE_STORE_WRITES = frozenset(
    {
        "cache_annotation",
        "checkpoint",
        "store_action_handoffs",
        "store_coverage",
        "store_identity_memberships",
        "store_observation_progress",
        "store_offered_questions",
        "store_project_memberships",
        "store_relationship_histories",
    }
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CompilerStage(StrEnum):
    PIN_INPUTS = "pin_inputs"
    IDENTITY = "identity"
    EPISODES = "episodes"
    OBSERVATIONS = "observations"
    MEMORY = "memory"
    PROJECTS = "projects"
    RELATIONSHIPS = "relationships"
    SYNTHESIS = "synthesis"
    OVERRIDES = "overrides"
    PERSIST = "persist"
    READINESS = "readiness"
    VALIDATE = "validate"
    ACTIVATE = "activate"


class CheckpointState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    STALE = "stale"


class CompileStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ExecutionLocality(StrEnum):
    OFFLINE = "offline"
    MANAGED_LOCAL = "managed_local"
    EXTERNAL = "external"


class ExplicitClaimProjection(_FrozenModel):
    """Lossless allowlisted graph claim input awaiting canonical identity resolution."""

    id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    object_surface: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    source_revision: str = Field(min_length=1)
    subject_entity_id: str = Field(min_length=1)
    asserted_by_entity_id: str | None = None
    speaker_entity_id: str | None = None
    occurred_at: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED

    @model_validator(mode="after")
    def exact_span(self) -> ExplicitClaimProjection:
        if self.char_end <= self.char_start:
            raise ValueError("explicit graph claim span is invalid")
        if len(self.quote) != self.char_end - self.char_start:
            raise ValueError("explicit graph quote length must match offsets")
        if self.occurred_at is not None and (
            self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("explicit graph claim occurred_at must be timezone-aware")
        return self


class GraphSnapshot(_FrozenModel):
    generation_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_name: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    model_name: str | None = None
    prompt_version: str | None = None
    policy_version: str = Field(min_length=1)
    evidence_content_hash: str | None = None
    evidence_entry_count: int | None = Field(default=None, ge=0)
    atomic_claims: tuple[AtomicClaimInput, ...] = ()
    explicit_claims: tuple[ExplicitClaimProjection, ...] = ()

    @model_validator(mode="after")
    def unique_claims(self) -> GraphSnapshot:
        ids = tuple(item.id for item in self.atomic_claims)
        if len(set(ids)) != len(ids):
            raise ValueError("graph snapshot atomic claim ids must be unique")
        explicit_ids = tuple(item.id for item in self.explicit_claims)
        if len(set(explicit_ids)) != len(explicit_ids):
            raise ValueError("graph snapshot explicit claim ids must be unique")
        return self


class OfferedQuestion(_FrozenModel):
    question_id: str = Field(min_length=1)
    interpretation_id: str = Field(min_length=1)
    display_text: str = Field(min_length=1, max_length=500)
    normalized_text: str = Field(min_length=1, max_length=500)
    offered_at: datetime
    state: str = Field(default="active", pattern=r"^(active|withdrawn)$")

    @model_validator(mode="after")
    def aware_and_normalized(self) -> OfferedQuestion:
        if self.offered_at.tzinfo is None or self.offered_at.utcoffset() is None:
            raise ValueError("offered_at must be timezone-aware")
        expected = " ".join(self.display_text.casefold().split())
        if self.normalized_text != expected:
            raise ValueError("offered question normalized_text must be canonical")
        return self


class OfferedQuestionProposal(_FrozenModel):
    """Question text awaiting a validated canonical interpretation target."""

    question_id: str = Field(min_length=1)
    interpretation_candidate_id: str = Field(min_length=1)
    display_text: str = Field(min_length=1, max_length=500)
    normalized_text: str = Field(min_length=1, max_length=500)
    offered_at: datetime
    state: str = Field(default="active", pattern=r"^(active|withdrawn)$")

    @model_validator(mode="after")
    def aware_and_normalized(self) -> OfferedQuestionProposal:
        if self.offered_at.tzinfo is None or self.offered_at.utcoffset() is None:
            raise ValueError("offered_at must be timezone-aware")
        expected = " ".join(self.display_text.casefold().split())
        if self.normalized_text != expected:
            raise ValueError("offered question normalized_text must be canonical")
        return self


@runtime_checkable
class ActiveGraphReader(Protocol):
    """Read exactly one immutable active graph projection."""

    def read_active(self) -> GraphSnapshot: ...


@runtime_checkable
class EvidenceReader(Protocol):
    """Read attributable entries belonging to the pinned graph input."""

    def read_entries(self, snapshot: GraphSnapshot) -> Sequence[Entry]: ...


class SQLiteActiveGraphReader:
    """Minimal local adapter which pins the exact active graph run metadata."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def read_active(self) -> GraphSnapshot:
        row = self.conn.execute("SELECT * FROM graph_runs WHERE state='active'").fetchone()
        if row is None:
            raise LookupError("no active atomic graph generation")
        columns = set(row.keys())
        material = {key: row[key] for key in columns if key not in {"started_at"}}
        extractor_name = (
            row["extractor_name"]
            if "extractor_name" in columns and row["extractor_name"]
            else "legacy-sqlite-graph"
        )
        extractor_version = (
            row["extractor_version"]
            if "extractor_version" in columns and row["extractor_version"]
            else "legacy-unknown"
        )
        predicate_version = (
            row["predicate_version"]
            if "predicate_version" in columns and row["predicate_version"]
            else "legacy-unknown"
        )
        identity_version = (
            row["identity_version"]
            if "identity_version" in columns and row["identity_version"]
            else "legacy-unknown"
        )
        return GraphSnapshot(
            generation_id=row["id"],
            content_hash=content_hash(material),
            extractor_name=extractor_name,
            extractor_version=extractor_version,
            model_name=row["model"] if "model" in columns else None,
            prompt_version=(row["prompt_version"] if "prompt_version" in columns else None),
            policy_version=(f"predicate:{predicate_version};identity:{identity_version}"),
            evidence_content_hash=(
                row["input_content_hash"] if "input_content_hash" in columns else None
            ),
            evidence_entry_count=(row["entries_total"] if "entries_total" in columns else None),
            # Typed atomic claim projections require subject resolution and exact
            # spans. The runtime graph-to-candidate adapter supplies them; never
            # reconstruct them from lossy claim display columns here.
            atomic_claims=(),
            explicit_claims=self._explicit_claims(row["id"]),
        )

    def _explicit_claims(self, generation_id: str) -> tuple[ExplicitClaimProjection, ...]:
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"claims", "claim_evidence", "entries"} <= tables:
            return ()
        required = {
            "claims": {
                "id",
                "predicate",
                "object_type",
                "object_surface",
                "subject_entity_id",
                "asserted_by_entity_id",
                "observed_at",
                "asserted_at",
                "sensitivity",
                "graph_generation_id",
                "lifecycle_state",
                "origin",
                "modality",
            },
            "claim_evidence": {
                "id",
                "claim_id",
                "entry_id",
                "quote",
                "char_start",
                "char_end",
                "speaker_entity_id",
                "evidence_role",
            },
            "entries": {"id", "body", "content_hash", "occurred_start"},
        }
        for table, fields in required.items():
            columns = {
                row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not fields <= columns:
                return ()
        rows = self.conn.execute(
            """SELECT c.id,c.predicate,c.object_type,c.object_surface,
                      c.subject_entity_id,c.asserted_by_entity_id,c.observed_at,c.asserted_at,
                      c.sensitivity,ev.entry_id,ev.quote,ev.char_start,ev.char_end,
                      ev.speaker_entity_id,e.body,e.content_hash,e.occurred_start
               FROM claims c
               JOIN claim_evidence ev ON ev.claim_id=c.id
               JOIN entries e ON e.id=ev.entry_id
               WHERE c.graph_generation_id=? AND c.lifecycle_state='active'
                 AND c.origin IN ('deterministic','user')
                 AND c.modality<>'inference' AND ev.evidence_role='supports'
               ORDER BY c.id,ev.id""",
            (generation_id,),
        ).fetchall()
        selected: dict[str, ExplicitClaimProjection] = {}
        for row in rows:
            if row["id"] in selected:
                continue
            start, end = row["char_start"], row["char_end"]
            if start is None or end is None or row["body"][start:end] != row["quote"]:
                first = row["body"].find(row["quote"])
                if first < 0 or row["body"].find(row["quote"], first + 1) >= 0:
                    continue
                start, end = first, first + len(row["quote"])
            occurred = None
            for literal in (row["asserted_at"], row["observed_at"], row["occurred_start"]):
                if not literal:
                    continue
                try:
                    parsed = datetime.fromisoformat(literal.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    occurred = parsed
                    break
            selected[row["id"]] = ExplicitClaimProjection(
                id=row["id"],
                predicate=row["predicate"],
                object_type=row["object_type"],
                object_surface=row["object_surface"],
                entry_id=row["entry_id"],
                quote=row["quote"],
                char_start=start,
                char_end=end,
                source_revision=row["content_hash"],
                subject_entity_id=row["subject_entity_id"],
                asserted_by_entity_id=row["asserted_by_entity_id"],
                speaker_entity_id=row["speaker_entity_id"],
                occurred_at=occurred,
                sensitivity=Sensitivity(row["sensitivity"]),
            )
        return tuple(selected[key] for key in sorted(selected))


class SQLiteEvidenceReader:
    def __init__(
        self, conn: sqlite3.Connection, *, excluded_sources: tuple[str, ...] = ()
    ):
        self.conn = conn
        self.excluded_sources = tuple(
            sorted({source.strip().casefold() for source in excluded_sources if source.strip()})
        )

    def read_entries(self, snapshot: GraphSnapshot) -> Sequence[Entry]:
        from varyz.db import dao

        placeholders = ",".join("?" for _ in self.excluded_sources)
        where = (
            f" WHERE lower(source) NOT IN ({placeholders})"
            if self.excluded_sources
            else ""
        )
        rows = self.conn.execute(
            f"SELECT id,content_hash FROM entries{where} ORDER BY id",  # noqa: S608
            self.excluded_sources,
        ).fetchall()
        entries = [dao.get_entry(self.conn, row["id"]) for row in rows]
        return tuple(item for item in entries if item is not None)


@dataclass(frozen=True)
class CompilerCandidateBatch:
    """Closed typed candidates proposed from pinned graph and observations."""

    memory: tuple[FactCandidate | PreferenceCandidate | EventCandidate, ...] = ()
    atomic_claims: tuple[AtomicClaimInput, ...] = ()
    entity_references: tuple[PinnedEntityReference, ...] = ()
    projects: tuple[ProjectEvidenceCandidate, ...] = ()
    goals: tuple[GoalEvidenceCandidate, ...] = ()
    relationship_periods: tuple[RelationshipPeriod, ...] = ()
    relationship_approvals: tuple[ApprovedRelationshipRecord, ...] = ()
    relationship_observation_contexts: tuple[ObservationRelationshipContext, ...] = ()
    relationship_correlations: tuple[CorrelationAssignment, ...] = ()
    episode_correlations: tuple[EpisodeCorrelation, ...] = ()
    significant_person_ids: tuple[str, ...] = ()
    owner_reviews: Mapping[str, OwnerInterpretationReview] = field(default_factory=dict)


# Which kind reads first where nothing forces an order.  It is a tiebreak, not
# the order itself: _staging_order below is what guarantees a record is staged
# after everything it names.
_STAGING_KIND_ORDER = {
    "conversation_episode": 0,
    "person": 1,
    "observation": 2,
    "project": 3,
    "goal": 4,
    "fact": 5,
    "preference": 6,
    "event": 7,
    "relationship_snapshot": 8,
    "interpretation_candidate": 9,
    "interpretation": 10,
}


def _staging_order(
    records: Sequence[PersonalModelRecord],
    order: Mapping[str, int] = _STAGING_KIND_ORDER,
) -> list[PersonalModelRecord]:
    """Order records so each one is staged after every record it names.

    The kind table alone was never an order.  Within a kind it fell back to
    the record id, which is a content hash and so says nothing about which
    record depends on which: a fact superseding a fact whose id happened to
    sort later was staged first, and staging refused the whole generation --
    after the archive had been read and every window bought -- with "depends
    on unstaged fact".  Across kinds it was wrong in the same way for an
    observation whose subject is a project, because observations come before
    projects in the table.

    The table survives as the tiebreak, so the order stays deterministic and
    still reads kind by kind wherever nothing forces otherwise.
    """

    by_id = {item.id: item for item in records}
    waiting_on: dict[str, set[str]] = {item.id: set() for item in records}
    blocks: dict[str, set[str]] = {item.id: set() for item in records}
    for record in records:
        for _, target in required_model_references(record):
            # A reference to something outside this generation is not an
            # ordering problem; _stage_records reports it as the dangling
            # reference it is, with the kind and the id.
            if target in by_id and target != record.id:
                waiting_on[record.id].add(target)
                blocks[target].add(record.id)

    ready = [
        (order[record_kind(item)], item.id)
        for item in records
        if not waiting_on[item.id]
    ]
    heapq.heapify(ready)
    ordered: list[PersonalModelRecord] = []
    while ready:
        _, record_id = heapq.heappop(ready)
        ordered.append(by_id[record_id])
        for dependent in sorted(blocks[record_id]):
            remaining = waiting_on[dependent]
            remaining.discard(record_id)
            if not remaining:
                heapq.heappush(ready, (order[record_kind(by_id[dependent])], dependent))

    if len(ordered) != len(records):
        held = sorted(item.id for item in records if item.id not in {x.id for x in ordered})
        raise ValueError(
            "personal-model records reference each other in a cycle and cannot "
            f"be staged: {held[:5]}"
        )
    return ordered


def _correlations_agreeing_with_episodes(
    correlations: Sequence[EpisodeCorrelation],
    episode_by_id: Mapping[str, ConversationEpisode],
) -> tuple[EpisodeCorrelation, ...]:
    """Whose correlation id it is.

    An episode's ``correlation_id`` is a hash of the turns a conversation was
    split across, minted by the reconstructor when one conversation did not fit
    in one episode. It says "these episodes are pieces of the same thing, so
    they are not independent evidence" -- and nothing the candidate model reads
    could tell it that hash, because the hash is over a membership the model is
    never shown.

    The model was asked for it anyway, and synthesis then refused the whole
    compile when the two disagreed. They will always disagree: one is a
    sha256 and the other is whatever the model wrote. A compile of this
    archive died on exactly that after reading all 324 windows, and it would
    have died there again however many times the run was retried.

    So the episode keeps its own id and the model keeps its opinion about
    grouping, which is what ``independence_group_id`` is for. Pieces of one
    conversation must then land in one group, or synthesis refuses again for a
    second reason; the lowest of their group ids is picked, arbitrary but the
    same on every run over the same archive.
    """

    for_correlation: dict[str, str] = {}
    for correlation in correlations:
        episode = episode_by_id.get(correlation.episode_id)
        if episode is None or episode.correlation_id is None:
            continue
        current = for_correlation.get(episode.correlation_id)
        if current is None or correlation.independence_group_id < current:
            for_correlation[episode.correlation_id] = correlation.independence_group_id
    agreed: list[EpisodeCorrelation] = []
    for correlation in correlations:
        episode = episode_by_id.get(correlation.episode_id)
        minted = None if episode is None else episode.correlation_id
        update: dict[str, Any] = {"correlation_id": minted}
        if minted is not None:
            update["independence_group_id"] = for_correlation[minted]
        agreed.append(correlation.model_copy(update=update))
    return tuple(agreed)


class CandidateAdapter(Protocol):
    name: str
    version: str
    model_name: str | None
    prompt_version: str | None
    execution_locality: ExecutionLocality

    def propose(
        self,
        *,
        snapshot: GraphSnapshot,
        entries: Sequence[Entry],
        identity: IdentityResolutionResult,
        observations: Sequence[Observation],
    ) -> CompilerCandidateBatch: ...


class EmptyDeterministicCandidateAdapter:
    """No-provider adapter: preserve explicit identities/claims, invent nothing."""

    name = "deterministic-empty-candidates"
    version = "1"
    model_name = None
    prompt_version = None
    execution_locality = ExecutionLocality.OFFLINE

    def propose(
        self,
        *,
        snapshot: GraphSnapshot,
        entries: Sequence[Entry],
        identity: IdentityResolutionResult,
        observations: Sequence[Observation],
    ) -> CompilerCandidateBatch:
        return CompilerCandidateBatch()


class PinnedGraphCandidateAdapter:
    """Compile only explicit allowlisted graph claims; perform no inference."""

    name = "pinned-explicit-graph-candidates"
    version = "11"
    model_name = None
    prompt_version = None
    execution_locality = ExecutionLocality.OFFLINE

    _FACTS: Mapping[str, tuple[str, FactValueKind]] = {
        "person.birth_date": ("person.birth_date", FactValueKind.DATE),
        "birth_date": ("person.birth_date", FactValueKind.DATE),
        "birthday": ("person.birth_date", FactValueKind.MONTH_DAY),
        "born_on": ("person.birth_date", FactValueKind.DATE),
        "person.full_name": ("person.full_name", FactValueKind.TEXT),
        "full_name": ("person.full_name", FactValueKind.TEXT),
        "person.current_location": ("person.current_location", FactValueKind.LOCATION),
        "current_location": ("person.current_location", FactValueKind.LOCATION),
        "lived_in": ("person.current_location", FactValueKind.LOCATION),
    }
    _PREFERENCES = frozenset({"likes", "liked", "loves", "prefers", "preference.thing"})

    def propose(
        self,
        *,
        snapshot: GraphSnapshot,
        entries: Sequence[Entry],
        identity: IdentityResolutionResult,
        observations: Sequence[Observation],
    ) -> CompilerCandidateBatch:
        entry_by_id = {item.id: item for item in entries}
        resolutions = {
            (item.source, item.source_account, item.source_key): item.entity_id
            for item in identity.source_entity_resolutions
            if item.kind == EntityKind.PERSON
        }
        atomic: list[AtomicClaimInput] = []
        candidates: list[FactCandidate | PreferenceCandidate] = []
        for claim in snapshot.explicit_claims:
            entry = entry_by_id.get(claim.entry_id)
            if entry is None or entry.body[claim.char_start : claim.char_end] != claim.quote:
                continue
            if claim.speaker_entity_id != claim.subject_entity_id:
                continue
            if claim.asserted_by_entity_id not in {None, claim.speaker_entity_id}:
                continue
            source_key = (
                f"id:{entry.conversation.sender_id}"
                if entry.conversation.sender_id
                else (
                    f"label:{entry.conversation.sender_label}"
                    if entry.conversation.sender_label
                    else None
                )
            )
            if source_key is None:
                continue
            person_id = resolutions.get((entry.source, entry.source_account, source_key))
            if person_id is None:
                continue
            span = TextSpan(
                entry_id=entry.id,
                start=claim.char_start,
                end=claim.char_end,
                exact_literal=claim.quote,
                source_revision=claim.source_revision,
            )
            subject = SubjectReference(kind=EntityKind.PERSON, id=person_id)
            atomic_claim = AtomicClaimInput(
                id=claim.id,
                subject=subject,
                asserted_by_person_id=person_id,
                span=span,
                content_hash=atomic_claim_content_hash(claim.quote, claim.source_revision),
                occurred_at=claim.occurred_at,
                sensitivity=claim.sensitivity,
                dialogue_context=DialogueContext.DIRECT_ANSWER,
                modality=ObservationModality.ASSERTED,
                polarity=Polarity.POSITIVE,
            )
            fact_mapping = self._FACTS.get(claim.predicate)
            if fact_mapping is not None:
                predicate, value_kind = fact_mapping
                if predicate == "person.birth_date" and not any(
                    char.isdigit() for char in claim.object_surface
                ):
                    continue
                if predicate == "person.birth_date" and not any(
                    token in claim.object_surface for token in ("19", "20")
                ):
                    value_kind = FactValueKind.MONTH_DAY
                candidates.append(
                    FactCandidate(
                        kind=CandidateKind.FACT,
                        subject=subject,
                        predicate=predicate,
                        value_kind=value_kind,
                        exact_value_literal=claim.object_surface,
                        claim_ids=(claim.id,),
                        asserted_by_person_id=person_id,
                        timezone=entry.tz or "UTC",
                    )
                )
            elif claim.predicate in self._PREFERENCES and any(
                marker in claim.quote.casefold()
                for marker in (" like ", " likes ", " love ", " loves ", " prefer ", " prefers ")
            ):
                candidates.append(
                    PreferenceCandidate(
                        kind=CandidateKind.PREFERENCE,
                        subject=subject,
                        predicate="preference.thing",
                        exact_object_literal=claim.object_surface,
                        valence=PreferenceValence.LIKE,
                        basis=PreferenceBasis.EXPLICIT,
                        claim_ids=(claim.id,),
                        asserted_by_person_id=person_id,
                    )
                )
            else:
                continue
            atomic.append(atomic_claim)
        # The graph's explicit-claim lane can be empty even when the archive
        # contains owner-voiced preferences and scoped design directions. Only
        # the AI connector's confirmed user turns qualify; quoted examples and
        # code are refused. A task direction is labelled as such, never promoted
        # to an enduring global taste.
        owner_ids = [person.id for person in identity.persons if person.is_archive_owner]
        if len(owner_ids) == 1:
            owner_id = owner_ids[0]
            seen_preferences: set[tuple[str, str, str]] = set()
            generic_threads: dict[str, list[Entry]] = {}
            for entry in entries:
                if (
                    entry.source == "ai_conversations"
                    and entry.authored_by_user
                    and entry.conversation.sender_id == "user"
                    and entry.thread_id
                    and ai_project_scope(entry.body) is None
                ):
                    generic_threads.setdefault(entry.thread_id, []).append(entry)
            inferred_scope: dict[str, str] = {}
            for turns in generic_threads.values():
                turns.sort(key=lambda item: (item.occurred_start or "", item.id))
                first_anchor = explicit_thread_project_anchor(turns[0].body)
                anchors = {
                    anchor
                    for item in turns
                    if (anchor := explicit_thread_project_anchor(item.body)) is not None
                }
                if first_anchor is None or anchors != {first_anchor}:
                    continue
                reset_positions = [
                    index for index, item in enumerate(turns)
                    if resets_design_direction(item.body)
                ]
                start = max(reset_positions) if reset_positions else 0
                inferred_scope.update({item.id: first_anchor for item in turns[start:]})
            communication_best: dict[
                tuple[str, str], tuple[tuple[int, int, str], str, int, int]
            ] = {}
            for entry in entries:
                if (
                    entry.source != "ai_conversations"
                    or not entry.authored_by_user
                    or entry.conversation.sender_id != "user"
                ):
                    continue
                scope = ai_project_scope(entry.body) or inferred_scope.get(entry.id)
                if scope is None or len(entry.body.split("\n\n", 1)[-1]) > 1_000:
                    continue
                for phrase in communication_guidance_phrases(entry.body):
                    key = (scope.casefold(), communication_theme(phrase))
                    folded = phrase.quote.casefold()
                    # A general writing instruction is more useful than a
                    # one-off shorter-summary request. The detailed contrast
                    # between conclusions and numbers has its own theme.
                    rank = (
                        int("type" in folded and "concise" in folded),
                        len(phrase.quote),
                        entry.occurred_start or "",
                    )
                    prior = communication_best.get(key)
                    if prior is None or rank > prior[0]:
                        communication_best[key] = (
                            rank, entry.id, phrase.char_start, phrase.char_end
                        )
            selected_communication = {
                (entry_id, start, end)
                for _, entry_id, start, end in communication_best.values()
            }
            for entry in sorted(entries, key=lambda item: item.occurred_start or "", reverse=True):
                if (
                    entry.source != "ai_conversations"
                    or not entry.authored_by_user
                    or entry.conversation.sender_id != "user"
                ):
                    continue
                scope = ai_project_scope(entry.body) or inferred_scope.get(entry.id)
                if scope is None:
                    continue
                owner_text = entry.body.split("\n\n", 1)[-1]
                # Long turns often paste transcripts, plans or skill files.
                # Their imperative sentences are not necessarily the owner's.
                direction_phrases = (
                    project_guidance_phrases(entry.body)
                    if len(owner_text) <= 1_000
                    else ()
                )
                communication_phrases = (
                    tuple(
                        phrase for phrase in communication_guidance_phrases(entry.body)
                        if (entry.id, phrase.char_start, phrase.char_end)
                        in selected_communication
                    )
                    if len(owner_text) <= 1_000
                    else ()
                )
                proposals = [
                    (phrase, "stated_taste", "preference.thing")
                    for phrase in explicit_preference_phrases(entry.body)
                ] + [
                    (phrase, "project_directive", f"preference.{phrase.dimension}")
                    for phrase in (*direction_phrases, *communication_phrases)
                ]
                for phrase, assertion_kind, predicate in proposals:
                    if entry.body[phrase.char_start : phrase.char_end] != phrase.quote:
                        continue
                    key = (scope.casefold(), predicate, phrase.quote.casefold())
                    if key in seen_preferences:
                        continue
                    seen_preferences.add(key)
                    claim_id = hashlib.sha256(
                        f"owner-preference:{entry.id}:{phrase.char_start}:{phrase.char_end}".encode()
                    ).hexdigest()
                    span = TextSpan(
                        entry_id=entry.id,
                        start=phrase.char_start,
                        end=phrase.char_end,
                        exact_literal=phrase.quote,
                        source_revision=entry.content_hash,
                    )
                    atomic.append(
                        AtomicClaimInput(
                            id=claim_id,
                            subject=SubjectReference(kind=EntityKind.PERSON, id=owner_id),
                            asserted_by_person_id=owner_id,
                            span=span,
                            content_hash=atomic_claim_content_hash(
                                phrase.quote, entry.content_hash
                            ),
                            occurred_at=(
                                datetime.fromisoformat(entry.occurred_start)
                                if entry.occurred_start
                                else None
                            ),
                            sensitivity=entry.sensitivity,
                            dialogue_context=(
                                DialogueContext.INSTRUCTION
                                if assertion_kind == "project_directive"
                                else DialogueContext.VOLUNTEERED_REASONING
                            ),
                            modality=ObservationModality.ASSERTED,
                            polarity=Polarity.POSITIVE,
                        )
                    )
                    candidates.append(
                        PreferenceCandidate(
                            kind=CandidateKind.PREFERENCE,
                            subject=SubjectReference(kind=EntityKind.PERSON, id=owner_id),
                            predicate=predicate,
                            exact_object_literal=phrase.object_literal,
                            valence=PreferenceValence(phrase.valence),
                            basis=PreferenceBasis.EXPLICIT,
                            claim_ids=(claim_id,),
                            asserted_by_person_id=owner_id,
                            scope_kind="project",
                            scope_label=scope,
                            assertion_kind=assertion_kind,
                        )
                    )
        return CompilerCandidateBatch(memory=tuple(candidates), atomic_claims=tuple(atomic))


class SynthesisAdapter(Protocol):
    name: str
    version: str
    model_name: str
    prompt_version: str
    execution_locality: ExecutionLocality

    def synthesize(
        self,
        *,
        generation_id: str,
        observations: Sequence[Observation],
        episodes: EpisodeBuildResult,
        correlations: Sequence[EpisodeCorrelation],
        people: Sequence[Person],
        projects: Sequence[Project],
        source_scopes: tuple[str, ...],
        created_at: datetime,
    ) -> SynthesisResult: ...

    def offered_question_proposals(
        self,
        *,
        synthesis: SynthesisResult,
        interpretations: Sequence[Interpretation],
    ) -> Sequence[OfferedQuestionProposal]: ...


class CompilerPolicy(_FrozenModel):
    version: str = "personal-model-policy-v1"
    compiler_version: str = "personal-model-compiler-v1"
    user_present: bool = False
    allow_cloud_egress: bool = False
    require_observation_annotator: bool = True
    allow_literal_only_activation: bool = False
    fail_on_quarantine: bool = False
    require_archive_owner: bool = True
    minimum_records: int = Field(default=1, ge=0)
    # One batch is handed to the annotator at a time, and the annotator splits
    # it across however many requests the server can serve at once. It has to
    # reach eight episodes for each of eight slots -- and then several times
    # over, because a batch holding exactly one call per slot leaves every slot
    # waiting on the slowest of them before any of them is given more.
    observation_batch_size: int = Field(default=DEFAULT_ANNOTATION_BATCH_SIZE, ge=1, le=256)
    # A full pass over the archive is a matter of days, so most runs are a
    # prefix of one. Coverage order makes that prefix a sample of the whole
    # archive rather than of whichever conversations sort first.
    observation_order: EpisodeOrder = EpisodeOrder.COVERAGE
    # Nearly half the messages a run reaches are the target of more than one
    # window. Reading every message once before reading any of them twice does
    # not change what a full pass sees, only what a stopped one has spent on.
    observation_defer_repeat_windows: bool = False
    # How long a full reading of the archive may take. Under it every window
    # over every message is read; over it each message keeps its longest few,
    # and how many is whatever the hours buy. Zero hours reads everything,
    # however long it takes.
    observation_budget_hours: float = Field(default=DEFAULT_ANNOTATION_BUDGET_HOURS, ge=0)
    observation_episodes_per_hour: float = Field(default=MEASURED_EPISODES_PER_HOUR, gt=0)

    @property
    def observation_budget(self) -> AnnotationBudget:
        return AnnotationBudget(
            hours=self.observation_budget_hours,
            episodes_per_hour=self.observation_episodes_per_hour,
        )

    @model_validator(mode="after")
    def background_is_local_only(self) -> CompilerPolicy:
        if not self.user_present and self.allow_cloud_egress:
            raise ValueError("background personal-model compilation cannot allow cloud egress")
        return self


class CompileRequest(_FrozenModel):
    generation_id: str | None = Field(default=None, min_length=1)
    target_entry_ids: tuple[str, ...] | None = None
    # Incremental scope is reduced to the target-entry seam the episode builder
    # already owns.  The full pinned archive remains the generation input, so
    # provenance validation, identity resolution and activation safety do not
    # acquire a second, weaker path.
    last_n_days: int | None = Field(default=None, ge=1)
    hot_topics: tuple[str, ...] = ()
    activate: bool = True
    required_applied_override_id: str | None = Field(default=None, min_length=1)
    required_record_ids: tuple[str, ...] = ()
    lookup_fixtures: tuple[LifeLookupFixture, ...] = ()
    max_annotation_calls: int | None = Field(default=None, ge=0)
    cached_observations_only: bool = False
    observation_deadline: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def aware_and_unique(self) -> CompileRequest:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("compile created_at must be timezone-aware")
        if self.observation_deadline is not None and (
            self.observation_deadline.tzinfo is None
            or self.observation_deadline.utcoffset() is None
        ):
            raise ValueError("observation deadline must be timezone-aware")
        if self.target_entry_ids is not None and len(set(self.target_entry_ids)) != len(
            self.target_entry_ids
        ):
            raise ValueError("compile target_entry_ids must be unique")
        normalized_topics = tuple(" ".join(item.casefold().split()) for item in self.hot_topics)
        if any(not item for item in normalized_topics):
            raise ValueError("compile hot_topics must be non-blank")
        if len(set(normalized_topics)) != len(normalized_topics):
            raise ValueError("compile hot_topics must be unique after normalization")
        if len(set(self.required_record_ids)) != len(self.required_record_ids):
            raise ValueError("required_record_ids must be unique")
        fixture_ids = tuple(item.fixture_id for item in self.lookup_fixtures)
        if len(set(fixture_ids)) != len(fixture_ids):
            raise ValueError("lookup fixture ids must be unique")
        return self


def _entry_time(entry: Entry) -> datetime | None:
    for literal in (entry.occurred_start, entry.captured_at, entry.ingested_at):
        if not literal:
            continue
        try:
            parsed = datetime.fromisoformat(literal.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def incremental_target_entry_ids(
    entries: Sequence[Entry], request: CompileRequest
) -> tuple[str, ...] | None:
    """Resolve recent/demand scope into stable episode targets.

    ``None`` preserves the ordinary full compile.  Explicit targets, recent
    entries, and hot-topic matches are unioned so a demand signal can add depth
    without silently dropping a caller-selected target.  The episode
    reconstructor expands each selected entry to its bounded conversation
    context exactly as it did before incremental mode existed.
    """
    incremental = request.last_n_days is not None or bool(request.hot_topics)
    if not incremental:
        return request.target_entry_ids
    selected = set(request.target_entry_ids or ())
    if request.last_n_days is not None:
        cutoff = request.created_at.astimezone(UTC) - timedelta(days=request.last_n_days)
        selected.update(
            entry.id
            for entry in entries
            if (stamp := _entry_time(entry)) is not None and stamp >= cutoff
        )
    topics = tuple(" ".join(item.casefold().split()) for item in request.hot_topics)
    if topics:
        selected.update(
            entry.id
            for entry in entries
            if any(
                topic
                in " ".join(f"{entry.title or ''} {entry.body}".casefold().split())
                for topic in topics
            )
        )
    return tuple(sorted(selected))


class ObservationProgress(_FrozenModel):
    episodes_annotated: int = Field(ge=0)
    episodes_total: int = Field(ge=0)
    episodes_remaining: int = Field(ge=0)
    entries_reached: int = Field(ge=0)
    annotation_calls: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    budget_exhausted_by: str | None = Field(
        default=None, pattern=r"^(call_budget|deadline|provider_unavailable)$"
    )

    @model_validator(mode="after")
    def honest_totals(self) -> ObservationProgress:
        if self.episodes_annotated > self.episodes_total:
            raise ValueError("annotated episodes cannot exceed total episodes")
        if self.episodes_remaining != self.episodes_total - self.episodes_annotated:
            raise ValueError("remaining episodes must equal total minus annotated")
        return self


class CompileResult(_FrozenModel):
    generation_id: str
    status: CompileStatus
    active_generation_id: str | None
    activated: bool
    failed: bool
    stale_stages: tuple[str, ...] = ()
    record_counts: dict[str, int] = {}
    reused_record_count: int = 0
    quarantine_count: int = 0
    readiness: GenerationReadiness | None = None
    observation_progress: ObservationProgress | None = None
    error: str | None = None


class CorrectionDisposition(StrEnum):
    APPLIED = "applied"
    REBUILD_NEEDED = "rebuild_needed"
    FAILED = "failed"


class CorrectionApplicationResult(_FrozenModel):
    disposition: CorrectionDisposition
    override_id: str
    override_sequence: int = Field(ge=1)
    generation_id: str
    active_generation_id: str | None
    error: str | None = None


class ModelRepository(Protocol):
    """Compiler-facing repository port, implemented by PersonalModelRepository."""

    def create_generation(self, graph_generation_id: str, **kwargs: Any) -> ModelGeneration: ...
    def active_generation_id(self) -> str | None: ...
    def stage_record(self, record: PersonalModelRecord, **kwargs: Any) -> str: ...
    def clone_record_if_unchanged(self, **kwargs: Any) -> bool: ...
    def overrides_for_generation(self, generation_id: str) -> list[ModelOverride]: ...
    def record_override_replay(
        self, generation_id: str, override_id: str, **kwargs: Any
    ) -> None: ...
    def quarantine(self, generation_id: str, **kwargs: Any) -> str: ...
    def record_metric(self, generation_id: str, **kwargs: Any) -> None: ...
    def activate_generation(self, generation_id: str) -> ModelGeneration: ...
    def fail_generation(self, generation_id: str, reason: str) -> ModelGeneration: ...


# target_entry_id, ordinal, episode_id, context_complete, correlation_id,
# omitted_ranges_json: one row of personal_model_target_episode_coverage.
CoverageRow = tuple[str, int, str, int, str | None, str]


def coverage_rows(result: EpisodeBuildResult) -> list[CoverageRow]:
    """The coverage rows to store for a build, in their stored order."""
    episode_by_id = {item.id: item for item in result.episodes}
    rows: list[CoverageRow] = []
    for coverage in sorted(result.target_coverage, key=lambda item: item.target_entry_id):
        for ordinal, episode_id in enumerate(coverage.episode_ids):
            episode = episode_by_id[episode_id]
            rows.append((
                coverage.target_entry_id,
                ordinal,
                episode_id,
                int(episode.completeness == ContextCompleteness.COMPLETE),
                episode.correlation_id,
                canonical_json([item.model_dump(mode="json") for item in episode.omitted_ranges]),
            ))
    return rows


class CompilerStateStore(Protocol):
    def checkpoint(
        self,
        generation_id: str,
        stage: CompilerStage,
        state: CheckpointState,
        input_hash: str,
        detail: Mapping[str, Any] = {},
    ) -> None: ...

    def store_coverage(self, generation_id: str, rows: Sequence[CoverageRow]) -> None: ...
    def store_action_handoffs(self, generation_id: str, items: Sequence[ActionHandoff]) -> None: ...

    def store_relationship_histories(
        self, generation_id: str, items: Sequence[RelationshipHistory]
    ) -> None: ...

    def store_offered_questions(
        self, generation_id: str, items: Sequence[OfferedQuestion]
    ) -> None: ...
    def store_identity_memberships(
        self,
        generation_id: str,
        identity: IdentityResolutionResult,
        source_people: Sequence[SourcePerson],
    ) -> None: ...
    def store_project_memberships(self, generation_id: str, result: ProjectCompilation) -> None: ...
    def previous_members(self, kind: str, record_id: str) -> tuple[str, ...]: ...
    def cached_annotation(self, cache_key: str) -> str | None: ...
    def cache_annotation(
        self,
        cache_key: str,
        request: AnnotationBatchRequest,
        descriptor: SemanticAnnotatorDescriptor,
        resolution_hash: str,
        response: str,
    ) -> None: ...

    def store_observation_progress(
        self,
        generation_id: str,
        progress: ObservationProgress,
        request: CompileRequest,
        *,
        annotated_episode_ids: Sequence[str],
        reached_entry_ids: Sequence[str],
        updated_at: datetime,
    ) -> None: ...


class SQLiteCompilerStateStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def checkpoint(
        self,
        generation_id: str,
        stage: CompilerStage,
        state: CheckpointState,
        input_hash: str,
        detail: Mapping[str, Any] = {},
    ) -> None:
        now = iso(datetime.now(UTC))
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_compiler_checkpoints
                   (generation_id,stage,state,input_hash,detail_json,started_at,completed_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(generation_id,stage) DO UPDATE SET
                     state=excluded.state,input_hash=excluded.input_hash,
                     detail_json=excluded.detail_json,
                     started_at=COALESCE(personal_model_compiler_checkpoints.started_at,excluded.started_at),
                     completed_at=excluded.completed_at""",
                (
                    generation_id,
                    str(stage),
                    str(state),
                    input_hash,
                    canonical_json(dict(detail)),
                    now,
                    now
                    if state
                    in {CheckpointState.COMPLETE, CheckpointState.FAILED, CheckpointState.STALE}
                    else None,
                ),
            )

    def store_observation_progress(
        self,
        generation_id: str,
        progress: ObservationProgress,
        request: CompileRequest,
        *,
        annotated_episode_ids: Sequence[str],
        reached_entry_ids: Sequence[str],
        updated_at: datetime,
    ) -> None:
        state = "partial" if progress.budget_exhausted_by is not None else "complete"
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_observation_progress
                   (generation_id,state,episodes_annotated,episodes_total,
                    episodes_remaining,entries_reached,annotation_calls,cache_hits,
                    max_annotation_calls,deadline,budget_exhausted_by,
                    annotated_episode_ids_json,reached_entry_ids_json,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(generation_id) DO UPDATE SET
                     state=excluded.state,
                     episodes_annotated=excluded.episodes_annotated,
                     episodes_total=excluded.episodes_total,
                     episodes_remaining=excluded.episodes_remaining,
                     entries_reached=excluded.entries_reached,
                     annotation_calls=excluded.annotation_calls,
                     cache_hits=excluded.cache_hits,
                     max_annotation_calls=excluded.max_annotation_calls,
                     deadline=excluded.deadline,
                     budget_exhausted_by=excluded.budget_exhausted_by,
                     annotated_episode_ids_json=excluded.annotated_episode_ids_json,
                     reached_entry_ids_json=excluded.reached_entry_ids_json,
                     updated_at=excluded.updated_at""",
                (
                    generation_id,
                    state,
                    progress.episodes_annotated,
                    progress.episodes_total,
                    progress.episodes_remaining,
                    progress.entries_reached,
                    progress.annotation_calls,
                    progress.cache_hits,
                    request.max_annotation_calls,
                    iso(request.observation_deadline),
                    progress.budget_exhausted_by,
                    canonical_json(sorted(set(annotated_episode_ids))),
                    canonical_json(sorted(set(reached_entry_ids))),
                    iso(updated_at),
                ),
            )

    def store_coverage(self, generation_id: str, rows: Sequence[CoverageRow]) -> None:
        with self.conn:
            self.conn.executemany(
                """INSERT INTO personal_model_target_episode_coverage
                   (generation_id,target_entry_id,ordinal,episode_id,context_complete,
                    correlation_id,omitted_ranges_json) VALUES (?,?,?,?,?,?,?)""",
                ((generation_id, *row) for row in rows),
            )

    def store_action_handoffs(self, generation_id: str, items: Sequence[ActionHandoff]) -> None:
        with self.conn:
            for item in sorted(items, key=lambda value: value.candidate_id):
                self.conn.execute(
                    """INSERT INTO personal_model_action_handoffs
                       (generation_id,candidate_id,title,node_kind,target_layer,
                        observation_ids_json,claim_ids_json,source_entry_ids_json,
                        project_ids_json,parent_goal_ids_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        item.candidate_id,
                        item.title,
                        str(item.node_kind),
                        item.target_layer,
                        canonical_json(item.observation_ids),
                        canonical_json(item.claim_ids),
                        canonical_json(item.source_entry_ids),
                        canonical_json(item.project_ids),
                        canonical_json(item.parent_goal_ids),
                    ),
                )

    def store_relationship_histories(
        self, generation_id: str, items: Sequence[RelationshipHistory]
    ) -> None:
        with self.conn:
            for item in sorted(items, key=lambda value: value.snapshot_id):
                self.conn.execute(
                    """INSERT INTO personal_model_relationship_histories
                       (generation_id,snapshot_id,archive_owner_person_id,other_person_id,
                        period_start,period_end,fact_ids_json,preference_ids_json,
                        shared_event_ids_json,commitment_observation_ids_json,
                        relationship_role_observation_ids_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        item.snapshot_id,
                        item.archive_owner_person_id,
                        item.other_person_id,
                        iso(item.period_start),
                        iso(item.period_end),
                        canonical_json(item.fact_ids),
                        canonical_json(item.preference_ids),
                        canonical_json(item.shared_event_ids),
                        canonical_json(item.commitment_observation_ids),
                        canonical_json(item.relationship_role_observation_ids),
                    ),
                )
                dependencies = (
                    *(("fact", value) for value in item.fact_ids),
                    *(("preference", value) for value in item.preference_ids),
                    *(("event", value) for value in item.shared_event_ids),
                    *(("observation", value) for value in item.commitment_observation_ids),
                    *(("observation", value) for value in item.relationship_role_observation_ids),
                )
                for ordinal, (kind, record_id) in enumerate(sorted(set(dependencies))):
                    row = self.conn.execute(
                        """SELECT record_kind,dependency_hash FROM personal_model_records
                           WHERE generation_id=? AND record_id=?""",
                        (generation_id, record_id),
                    ).fetchone()
                    if row is None or row["record_kind"] != kind:
                        raise ValueError(
                            f"relationship history dependency is absent: {kind} {record_id}"
                        )
                    self.conn.execute(
                        """INSERT INTO personal_model_relationship_history_dependencies
                           (generation_id,snapshot_id,ordinal,record_kind,record_id,
                            dependency_hash) VALUES (?,?,?,?,?,?)""",
                        (
                            generation_id,
                            item.snapshot_id,
                            ordinal,
                            kind,
                            record_id,
                            row["dependency_hash"],
                        ),
                    )

    def store_offered_questions(self, generation_id: str, items: Sequence[OfferedQuestion]) -> None:
        if len({item.question_id for item in items}) != len(items):
            raise ValueError("offered question ids must be unique")
        with self.conn:
            for item in sorted(items, key=lambda value: value.question_id):
                interpretation = self.conn.execute(
                    """SELECT record_kind,readiness FROM personal_model_records
                       WHERE generation_id=? AND record_id=?""",
                    (generation_id, item.interpretation_id),
                ).fetchone()
                if (
                    interpretation is None
                    or interpretation["record_kind"] != "interpretation"
                    or interpretation["readiness"] not in {"ready", "limited"}
                ):
                    raise ValueError(
                        "offered question must map to a validated visible interpretation"
                    )
                self.conn.execute(
                    """INSERT INTO personal_model_offered_questions
                       (generation_id,question_id,interpretation_id,display_text,
                        normalized_text,offered_at,state) VALUES (?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        item.question_id,
                        item.interpretation_id,
                        item.display_text,
                        item.normalized_text,
                        iso(item.offered_at),
                        item.state,
                    ),
                )

    def store_identity_memberships(
        self,
        generation_id: str,
        identity: IdentityResolutionResult,
        source_people: Sequence[SourcePerson],
    ) -> None:
        by_key = {item.identity_key: item for item in source_people}
        with self.conn:
            for key, person_id in sorted(identity.source_identity_to_person_id.items()):
                source = by_key[key]
                self.conn.execute(
                    """INSERT INTO personal_model_source_identity_memberships
                       (generation_id,person_id,source_identity_key,source,source_account,
                        source_key,membership_hash) VALUES (?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        person_id,
                        key,
                        source.source,
                        source.source_account,
                        source.source_key,
                        content_hash((key, person_id)),
                    ),
                )

    def store_project_memberships(self, generation_id: str, result: ProjectCompilation) -> None:
        with self.conn:
            for candidate_id, project_id in sorted(result.project_candidate_to_project_id.items()):
                self.conn.execute(
                    """INSERT INTO personal_model_project_candidate_memberships
                       (generation_id,project_id,candidate_id,membership_hash) VALUES (?,?,?,?)""",
                    (
                        generation_id,
                        project_id,
                        candidate_id,
                        content_hash((candidate_id, project_id)),
                    ),
                )
            for candidate_id, goal_id in sorted(result.goal_candidate_to_goal_id.items()):
                self.conn.execute(
                    """INSERT INTO personal_model_goal_candidate_memberships
                       (generation_id,goal_id,candidate_id,membership_hash) VALUES (?,?,?,?)""",
                    (generation_id, goal_id, candidate_id, content_hash((candidate_id, goal_id))),
                )

    def previous_members(self, kind: str, record_id: str) -> tuple[str, ...]:
        active = self.conn.execute(
            "SELECT generation_id FROM personal_model_active_generation WHERE singleton=1"
        ).fetchone()
        if active is None:
            return ()
        table, id_column, member_column = {
            "person": (
                "personal_model_source_identity_memberships",
                "person_id",
                "source_identity_key",
            ),
            "project": (
                "personal_model_project_candidate_memberships",
                "project_id",
                "candidate_id",
            ),
            "goal": ("personal_model_goal_candidate_memberships", "goal_id", "candidate_id"),
        }[kind]
        sql = (
            f"SELECT {member_column} AS member FROM {table} "
            f"WHERE generation_id=? AND {id_column}=? ORDER BY {member_column}"
        )
        rows = self.conn.execute(
            sql,
            (active["generation_id"], record_id),
        ).fetchall()
        return tuple(row["member"] for row in rows)

    def cached_candidate_windows(self, cache_keys: Sequence[str]) -> dict[str, str]:
        """The stored replies for these window keys; a key with none is absent."""

        found: dict[str, str] = {}
        keys = [key for key in cache_keys if key]
        for start in range(0, len(keys), 400):
            chunk = keys[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                "SELECT cache_key,response_json FROM personal_model_candidate_window_cache "
                f"WHERE cache_key IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                found[str(row["cache_key"])] = str(row["response_json"])
        return found

    def cache_candidate_windows(
        self,
        entries: Sequence[tuple[str, str]],
        *,
        adapter_name: str,
        adapter_version: str,
        model_name: str,
        prompt_version: str,
        schema_version: str,
    ) -> None:
        """Store replies that have already been read, never provider text as it came."""

        if not entries:
            return
        now = iso(datetime.now(UTC))
        with self.conn:
            self.conn.executemany(
                """INSERT OR IGNORE INTO personal_model_candidate_window_cache
                   (cache_key,adapter_name,adapter_version,model_name,prompt_version,
                    schema_version,response_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        key,
                        adapter_name,
                        adapter_version,
                        model_name,
                        prompt_version,
                        schema_version,
                        response,
                        now,
                    )
                    for key, response in entries
                ],
            )

    def cached_annotation(self, cache_key: str) -> str | None:
        row = self.conn.execute(
            """SELECT response_json FROM personal_model_observation_annotation_cache
               WHERE cache_key=?""",
            (cache_key,),
        ).fetchone()
        return row["response_json"] if row else None

    def cache_annotation(
        self,
        cache_key: str,
        request: AnnotationBatchRequest,
        descriptor: SemanticAnnotatorDescriptor,
        resolution_hash: str,
        response: str,
    ) -> None:
        # Validate before cache insertion: malformed provider text is never a
        # cache hit.  What counts as malformed is whatever the reader refuses
        # and no more.  This used to validate the whole batch, while
        # ``ObservationExtractor`` falls back to reading candidates one at a
        # time, so a single candidate the model got wrong -- an unresolved
        # subject carrying a source reference, ten of them in the first five
        # minutes of the 2026-08-31 run -- kept the whole episode out of the
        # cache.  The annotation was used by that run and paid for all the
        # same; it was only never marked done, so the episode came back and was
        # bought again on the run after it, and on every run after that.
        readable_candidate_count(response)
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO personal_model_observation_annotation_cache
                   (cache_key,dependency_fingerprint,resolution_hash,schema_version,
                    provider_name,provider_version,model_name,prompt_version,response_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    cache_key,
                    request.dependency_fingerprint,
                    resolution_hash,
                    descriptor.schema_version,
                    descriptor.name,
                    descriptor.version,
                    descriptor.model_name,
                    descriptor.prompt_version,
                    response,
                    iso(datetime.now(UTC)),
                ),
            )


class _CachingAnnotator:
    def __init__(
        self,
        inner: SemanticAnnotator,
        store: CompilerStateStore,
        *,
        max_annotation_calls: int | None,
        deadline: datetime | None,
        clock: Callable[[], datetime],
        cached_only: bool = False,
    ):
        self.inner = inner
        self.store = store
        self.max_annotation_calls = max_annotation_calls
        self.cached_only = cached_only
        self.deadline = deadline
        self.clock = clock
        self.hits = 0
        self.misses = 0
        self.annotated_episode_ids: set[str] = set()
        self.reached_entry_ids: set[str] = set()

    @property
    def descriptor(self) -> SemanticAnnotatorDescriptor:
        return self.inner.descriptor

    def annotate(self, request: AnnotationBatchRequest) -> str:
        descriptor = self.descriptor
        key, resolution_hash = annotation_cache_identity(request, descriptor)
        cached = self.store.cached_annotation(key)
        if cached is not None:
            self.hits += 1
            self._mark_completed(request)
            return cached
        if self.cached_only:
            raise ObservationCacheMiss()
        if self.max_annotation_calls is not None and self.misses >= self.max_annotation_calls:
            raise ObservationBudgetExhausted("call_budget")
        if self.deadline is not None and self.clock() >= self.deadline:
            raise ObservationBudgetExhausted("deadline")
        self.misses += 1
        raw = self.inner.annotate(request)
        if isinstance(raw, AnnotationBatchResponse):
            encoded = raw.model_dump_json()
        elif isinstance(raw, bytes):
            encoded = raw.decode("utf-8")
        elif isinstance(raw, str):
            encoded = raw
        else:
            encoded = canonical_json(dict(raw))
        self.store.cache_annotation(key, request, descriptor, resolution_hash, encoded)
        self._mark_completed(request)
        return encoded

    def annotate_batch(
        self, requests: tuple[AnnotationBatchRequest, ...]
    ) -> Mapping[str, str | bytes | Mapping[str, Any] | AnnotationBatchResponse]:
        if not requests:
            return {}
        if not hasattr(self.inner, "annotate_batch"):
            return {request.episode_id: self.annotate(request) for request in requests}

        descriptor = self.descriptor
        results: dict[str, str | bytes | Mapping[str, Any] | AnnotationBatchResponse] = {}
        misses: list[tuple[AnnotationBatchRequest, str, str]] = []
        for request in requests:
            key, resolution_hash = annotation_cache_identity(request, descriptor)
            cached = self.store.cached_annotation(key)
            if cached is not None:
                self.hits += 1
                self._mark_completed(request)
                results[request.episode_id] = cached
            else:
                misses.append((request, key, resolution_hash))
        if not misses:
            return results
        if self.cached_only:
            return results
        if self.max_annotation_calls is not None and self.misses >= self.max_annotation_calls:
            raise ObservationBudgetExhausted("call_budget")
        if self.deadline is not None and self.clock() >= self.deadline:
            raise ObservationBudgetExhausted("deadline")

        self.misses += 1
        try:
            raw_by_episode = self.inner.annotate_batch(tuple(item[0] for item in misses))
        except ObservationBudgetExhausted:
            # The budget, the deadline and the breaker all end the run on
            # purpose, and the compiler knows how to store what it has and
            # report itself partial.
            raise
        except Exception as exc:  # the annotator is an untrusted failure boundary
            # ``results`` already holds the cached annotations of this group,
            # read back from earlier runs and costing nothing.  Letting the
            # call for the uncached few take them with it discards work that
            # was already done and already paid for; the episodes that were
            # actually sent are reported missing by the caller, which
            # quarantines them one at a time.
            log.warning(
                "grouped annotation of %d uncached episodes failed with %s; "
                "keeping the %d already cached",
                len(misses),
                type(exc).__name__,
                len(results),
            )
            return results
        for request, key, resolution_hash in misses:
            raw = raw_by_episode.get(request.episode_id)
            if raw is None:
                continue
            results[request.episode_id] = raw
            encoded = self._encode(raw)
            try:
                self.store.cache_annotation(
                    key,
                    request,
                    descriptor,
                    resolution_hash,
                    encoded,
                )
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                # An annotation that cannot be cached is also not marked
                # completed, so the episode returns on the next run and is paid
                # for again. Silently, this is a compile that never finishes and
                # never says why.
                # The reason, never the reply.  Rendering a ValidationError
                # printed the candidate that failed -- on 2026-08-31 that put a
                # contact's own name into the run log.
                log.warning(
                    "annotation for episode %s could not be cached: %s: %s",
                    request.episode_id,
                    type(exc).__name__,
                    refusal_reason(exc),
                )
                continue
            self._mark_completed(request)
        return results

    @staticmethod
    def _encode(
        raw: str | bytes | Mapping[str, Any] | AnnotationBatchResponse,
    ) -> str:
        if isinstance(raw, AnnotationBatchResponse):
            return raw.model_dump_json()
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        if isinstance(raw, str):
            return raw
        return canonical_json(dict(raw))

    def _mark_completed(self, request: AnnotationBatchRequest) -> None:
        self.annotated_episode_ids.add(request.episode_id)
        self.reached_entry_ids.update(item.entry_id for item in request.sources)


def annotation_cache_identity(
    request: AnnotationBatchRequest,
    descriptor: SemanticAnnotatorDescriptor,
) -> tuple[str, str]:
    """Return the exact cache key for inputs that can change annotation conclusions."""

    resolution_hash = content_hash(
        {"subjects": [item.model_dump(mode="json") for item in request.allowed_subjects]}
    )
    return (
        content_hash(
            {
                "request": request.dependency_fingerprint,
                "resolution": resolution_hash,
                "schema": descriptor.schema_version,
                "provider": descriptor.name,
                "provider_version": descriptor.version,
                "model": descriptor.model_name,
                "prompt": descriptor.prompt_version,
            }
        ),
        resolution_hash,
    )


@dataclass(frozen=True)
class _ObservationStageResult:
    extraction: ObservationExtractionResult
    progress: ObservationProgress
    partial: bool


def _witnessing_entries(accumulated: dict[str, Any]) -> tuple[str, ...]:
    """The entries that date a source person, from both ends of their span.

    Deterministic: one entry a day, the earliest of that day, and the days
    taken from the two ends rather than sampled -- a record whose evidence
    changes because the archive was read in a different order is a record whose
    identity hash changes for no reason anyone can name.
    """

    days = sorted(accumulated["witness_by_day"].items())
    half = _PERSON_EVIDENCE_DAYS // 2
    chosen = days if len(days) <= _PERSON_EVIDENCE_DAYS else [*days[:half], *days[-half:]]
    return tuple(
        dict.fromkeys(
            [accumulated["first_entry_id"], *(witness[1] for _, witness in chosen)]
        )
    )


# How many days of witnessing entries a source person carries out of this
# adapter.  One was the number until 2026-09-08, and it was not a budget, it
# was an oversight: the display name, the owner flag and the sensitivity were
# all accumulated across every entry that witnessed the person, and only the
# evidence was truncated to the first.  The compiled model showed it plainly --
# 376 people carrying 379 evidence rows between them, against 192 projects
# carrying 3,125 -- and it cost Alpha acceptance criterion 2 outright, since
# exactly one person in the archive could be shown on two different days.
#
# Eight days, taken from both ends of the person's span rather than the start
# of it, because what the record has to be able to say is that this person is
# in the user's life over time.  Four and four costs about 3,000 rows on this
# archive, which is what one kind of record already spends.
_PERSON_EVIDENCE_DAYS = 8


def _repository_of(entry: Entry) -> str | None:
    """The repository a commit came from, or None for any other entry.

    The git connector addresses a commit as ``<repo path>@<sha>``, and the
    path is the only part that says which checkout it was read from.
    """

    if entry.source != "git" or "@" not in entry.external_id:
        return None
    return entry.external_id.rsplit("@", 1)[0] or None


class DeterministicSourceIdentityAdapter:
    """Build source-scoped people only from explicit connector metadata."""

    version = "connector-source-identities-v1"

    def source_people(self, entries: Iterable[Entry]) -> tuple[SourcePerson, ...]:
        accumulated: dict[tuple[str, str, str], dict[str, Any]] = {}
        # Which cloned repository witnessed each git person, and which
        # repositories the owner has ever committed to.  See the filter below.
        owned_repos: set[str] = set()
        sensitivity_rank = {
            Sensitivity.PUBLIC: 0,
            Sensitivity.PERSONAL: 1,
            Sensitivity.SENSITIVE: 2,
            Sensitivity.UNCLASSIFIED: 3,
        }
        for entry in entries:
            entry_order = (
                entry.occurred_start or entry.ingested_at,
                str(entry.conversation.source_order or ""),
                entry.id,
            )
            metadata = entry.conversation
            repo = _repository_of(entry)
            if repo is not None and entry.authored_by_user:
                owned_repos.add(repo)
            witnessed: list[tuple[str, str, bool]] = []
            if metadata.sender_id:
                # A Telegram export writes the word `unknown` in the name column
                # for an account that has since been deleted, and the People page
                # showed four different accounts as four cards all called
                # `unknown`.  The account id is not a name either, but it is the
                # one thing that tells them apart, and it is already how everyone
                # known only by a phone number appears there.
                #
                # An account id already in the name column is the case in
                # between, and it is the common one: the WhatsApp connector
                # writes the number without the `@s.whatsapp.net` after it, and
                # asking only whether the label named somebody swapped that for
                # the raw jid on every one of the fifty-two people this archive
                # knows by a number alone.  It distinguishes, so it stays.
                label = metadata.sender_label or ""
                keeps_them_apart = names_something(label) or identifies_an_account(label)
                witnessed.append(
                    (
                        f"id:{metadata.sender_id}",
                        label if keeps_them_apart else metadata.sender_id,
                        entry.authored_by_user,
                    )
                )
            elif metadata.sender_label:
                witnessed.append(
                    (
                        f"label:{metadata.sender_label}",
                        metadata.sender_label,
                        entry.authored_by_user,
                    )
                )
            for identifier in metadata.participant_ids:
                witnessed.append((f"id:{identifier}", identifier, False))
            if not metadata.participant_ids:
                for label in metadata.participant_labels:
                    witnessed.append((f"label:{label}", label, False))
            for source_key, display_name, owner in witnessed:
                key = (entry.source, entry.source_account, source_key)
                item = accumulated.setdefault(
                    key,
                    {
                        "display_name": display_name,
                        "first_entry_id": entry.id,
                        "first_entry_order": entry_order,
                        "owner": False,
                        "sensitivity": entry.sensitivity,
                        "witness_by_day": {},
                    },
                )
                if entry_order < item["first_entry_order"]:
                    item["display_name"] = display_name
                    item["first_entry_id"] = entry.id
                    item["first_entry_order"] = entry_order
                # The earliest entry of each day the person was witnessed on.
                # Kept per day rather than per entry because a day is the unit
                # the record is read in: a person seen forty times on one
                # afternoon has been seen once, as far as "known over time"
                # goes.
                # An ISO timestamp's date prefix, which is how the archive
                # itself dates an entry: `Entry.occurred_start` is a string.
                day = str(entry_order[0])[:10]
                witness = item["witness_by_day"].get(day)
                if witness is None or entry_order < witness[0]:
                    item["witness_by_day"][day] = (entry_order, entry.id)
                item["owner"] = item["owner"] or owner
                if repo is not None:
                    item.setdefault("repos", set()).add(repo)
                if sensitivity_rank[entry.sensitivity] > sensitivity_rank[item["sensitivity"]]:
                    item["sensitivity"] = entry.sensitivity
        people: list[SourcePerson] = []
        for (source, account, key), value in sorted(accumulated.items()):
            # Everyone who ever committed to a repository on this machine was
            # a person in the owner's life: 175 of the 376 people in the
            # compiled model were contributors to one project he had cloned
            # and never written a line of.  A repository he has committed to
            # is different -- the people in it are people he works with -- so
            # the test is the repository, not the commit.
            repos = value.get("repos")
            if repos and not (repos & owned_repos):
                continue
            durable = key.startswith("id:")
            alias_kind = AliasKind.SOURCE_ACCOUNT_ID if durable else AliasKind.NAME
            people.append(
                SourcePerson(
                    source=source,
                    source_account=account,
                    source_key=key,
                    display_name=value["display_name"],
                    aliases=(
                        SourceAlias(
                            kind=alias_kind,
                            exact_value=key.removeprefix("id:").removeprefix("label:"),
                            evidence_entry_ids=_witnessing_entries(value),
                            verified_durable=durable,
                            identifier_scope=f"{source}:{account}" if durable else None,
                        ),
                    ),
                    is_archive_owner=value["owner"],
                    source_key_is_durable=durable,
                    authorized_text_source_keys=(key,),
                    sensitivity=value["sensitivity"],
                )
            )
        return tuple(people)


class PersonalModelCompiler:
    def __init__(
        self,
        *,
        graph_reader: ActiveGraphReader,
        evidence_reader: EvidenceReader,
        repository: PersonalModelRepository,
        state_store: CompilerStateStore,
        annotator: SemanticAnnotator | None = None,
        candidate_adapter: CandidateAdapter | None = None,
        synthesis_adapter: SynthesisAdapter | None = None,
        source_identity_adapter: DeterministicSourceIdentityAdapter | None = None,
        validation_policy: ValidationPolicy | None = None,
        policy: CompilerPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.graph_reader = graph_reader
        self.evidence_reader = evidence_reader
        self._data_dir = data_dir
        self.repository = (
            _WriteLockedPort(repository, data_dir, _REPOSITORY_WRITES)
            if data_dir is not None
            else repository
        )
        self.state_store = (
            _WriteLockedPort(state_store, data_dir, _STATE_STORE_WRITES)
            if data_dir is not None
            else state_store
        )
        self.annotator = annotator
        self.candidate_adapter = candidate_adapter or PinnedGraphCandidateAdapter()
        self.synthesis_adapter = synthesis_adapter
        self.source_identity_adapter = (
            source_identity_adapter or DeterministicSourceIdentityAdapter()
        )
        self.validation_policy = validation_policy or ValidationPolicy()
        self.policy = policy or CompilerPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._on_progress: Callable[[str, float | None], None] | None = None

    def compile(
        self,
        request: CompileRequest | None = None,
        *,
        on_progress: Callable[[str, float | None], None] | None = None,
    ) -> CompileResult:
        """``on_progress`` hears (stage, fraction or None) as the compile moves on."""
        selected = request or CompileRequest()
        self._on_progress = on_progress
        try:
            if self._data_dir is None:
                return self._compile(selected)
            with PersonalModelCompileClaim(self._data_dir, generation_id=selected.generation_id):
                return self._compile(selected)
        finally:
            self._on_progress = None

    def _say(self, stage: str, fraction: float | None = None) -> None:
        # A compile on a full archive runs for minutes; a person watching a
        # bar that names no stage cannot tell it from a hung one.
        if self._on_progress is not None:
            self._on_progress(stage, fraction)

    @contextmanager
    def _write_batch(self) -> Iterator[None]:
        """One writer lock and one transaction for a chunk of repository writes."""
        batched = getattr(self.repository, "batched", None)
        lock = (self.repository.held() if isinstance(self.repository, _WriteLockedPort)
                else nullcontext())
        with lock, (batched() if batched is not None else nullcontext()):
            yield

    def preflight(self) -> None:
        """Validate the pinned graph projection without creating a generation."""
        snapshot = self.graph_reader.read_active()
        entries = tuple(
            sorted(self.evidence_reader.read_entries(snapshot), key=lambda item: item.id)
        )
        self._validate_inputs(snapshot, entries)

    def _compile(self, selected: CompileRequest) -> CompileResult:
        # Reading the graph and then every entry behind it is the first thing a
        # compile does and, on a real archive, one of the longest. Before this
        # line it was also silent, and a run watched by a person or a
        # supervisor is indistinguishable from a hung one.
        self._say("reading your archive")
        log.info("reading the active graph")
        snapshot = self.graph_reader.read_active()
        entries = tuple(
            sorted(self.evidence_reader.read_entries(snapshot), key=lambda item: item.id)
        )
        log.info("read %d entries behind %d claims", len(entries), len(snapshot.atomic_claims))
        targets = incremental_target_entry_ids(entries, selected)
        if targets != selected.target_entry_ids:
            selected = selected.model_copy(update={"target_entry_ids": targets})
        if targets is not None:
            log.info("incremental compile selected %d target entries", len(targets))
        input_hash = self._input_hash(snapshot, entries)
        prior_active = self.repository.active_generation_id()
        generation = self.repository.create_generation(
            snapshot.generation_id,
            compiler_version=self.policy.compiler_version,
            policy_version=self.policy.version,
            input_content_hash=input_hash,
            generation_id=selected.generation_id,
        )
        generation_id = generation.id
        stale: list[str] = []
        readiness: GenerationReadiness | None = None
        try:
            self._validate_inputs(snapshot, entries)
            self._checkpoint(
                generation_id,
                CompilerStage.PIN_INPUTS,
                input_hash,
                {
                    "entries": len(entries),
                    "claims": len(snapshot.atomic_claims),
                    "target_entries": len(targets) if targets is not None else len(entries),
                },
            )
            overrides = self.repository.overrides_for_generation(generation_id)
            source_people = self.source_identity_adapter.source_people(entries)
            identity_overrides, identity_conflicts = self._identity_overrides(overrides)
            identity = PersonResolver().resolve(
                source_people,
                generation_id=generation_id,
                created_at=selected.created_at,
                overrides=identity_overrides,
            )
            self._checkpoint(
                generation_id,
                CompilerStage.IDENTITY,
                input_hash,
                {"people": len(identity.persons), "reviews": len(identity.review_candidates)},
            )

            self._say("finding conversations")
            log.info("reconstructing conversation episodes from %d entries", len(entries))
            episodes = EpisodeReconstructor().build(
                entries,
                generation_id=generation_id,
                created_at=selected.created_at,
                target_entry_ids=selected.target_entry_ids,
            )
            self._checkpoint(
                generation_id,
                CompilerStage.EPISODES,
                input_hash,
                {
                    "episodes": len(episodes.episodes),
                    "quarantined_targets": len(episodes.quarantined_target_ids),
                },
            )

            self._say("reading what was said")
            observation_stage = self._observations(
                generation_id,
                entries,
                episodes,
                identity,
                selected,
            )
            observation_result = observation_stage.extraction
            observation_progress = observation_stage.progress
            if observation_stage.partial:
                detail = {
                    "status": "partial",
                    "quarantines_by_code": _quarantines_by_code(observation_result),
                    **observation_progress.model_dump(mode="json"),
                }
                self.state_store.checkpoint(
                    generation_id,
                    CompilerStage.OBSERVATIONS,
                    CheckpointState.RUNNING,
                    input_hash,
                    detail,
                )
                return CompileResult(
                    generation_id=generation_id,
                    status=CompileStatus.PARTIAL,
                    active_generation_id=self.repository.active_generation_id(),
                    activated=False,
                    failed=False,
                    observation_progress=observation_progress,
                )
            if self.annotator is None and entries:
                stale.append(CompilerStage.OBSERVATIONS)
            self._checkpoint(
                generation_id,
                CompilerStage.OBSERVATIONS,
                input_hash,
                {
                    "observations": len(observation_result.observations),
                    "quarantines": len(observation_result.quarantines),
                    "quarantines_by_code": _quarantines_by_code(observation_result),
                    **observation_progress.model_dump(mode="json"),
                },
            )

            self._say("sorting facts and preferences")
            try:
                candidates = self.candidate_adapter.propose(
                    snapshot=snapshot,
                    entries=entries,
                    identity=identity,
                    observations=observation_result.observations,
                )
                if not isinstance(candidates, CompilerCandidateBatch):
                    raise ValueError("candidate adapter returned an untyped batch")
            except ValueError as invalid:
                # Say which check refused.  The adapter tolerates a window it
                # could not read -- one stretch of archive is worth less than
                # the compile -- but the assembly that follows is strict, and a
                # single unresolvable proposal out of sixty windows ends an
                # eight-hour run.  Reporting only "invalid candidates" made
                # those two failures look like one, and left the operator with
                # a dead generation and nothing to act on.
                #
                # The message is safe to keep: every raise site in the assembly
                # is a fixed string naming the check, never the text it read.
                # `from None` still stands, because the traceback below it is
                # not.
                # Only an adapter's own named check may be repeated.  Any
                # other ValueError from in there is the provider's text and
                # stays where it is.
                reason = (
                    invalid.reason
                    if isinstance(invalid, CandidateAssemblyError)
                    else "the adapter refused without naming a check"
                )
                log.error("candidate assembly refused the proposals: %s", reason)
                self.repository.quarantine(
                    generation_id,
                    stage="candidates",
                    reason_code="malformed_candidate_response",
                    detail={"adapter": self.candidate_adapter.name, "refused": reason},
                )
                self.state_store.checkpoint(
                    generation_id,
                    CompilerStage.MEMORY,
                    CheckpointState.STALE,
                    input_hash,
                    {"reason": "malformed_candidate_response", "refused": reason},
                )
                raise RuntimeError(
                    f"candidate adapter returned invalid candidates ({reason})"
                ) from None
            candidates = replace(
                candidates,
                episode_correlations=_correlations_agreeing_with_episodes(
                    candidates.episode_correlations,
                    {item.id: item for item in episodes.episodes},
                ),
            )
            atomic_claims = (*snapshot.atomic_claims, *candidates.atomic_claims)
            if len({item.id for item in atomic_claims}) != len(atomic_claims):
                raise ValueError("candidate adapter returned duplicate pinned atomic claims")
            memory = MemoryCompiler().compile(
                generation_id=generation_id,
                observations=observation_result.observations,
                candidates=candidates.memory,
                created_at=selected.created_at,
                atomic_claims=atomic_claims,
                entity_references=candidates.entity_references,
                episode_independence_groups={
                    item.episode_id: item.independence_group_id
                    for item in candidates.episode_correlations
                },
            )
            # Local source-only claims are a strict input-validation device:
            # MemoryCompiler uses their exact pinned spans to reject unsupported
            # preference proposals. They are not graph claims. Persist the exact
            # entry/span evidence, but do not claim graph membership for IDs the
            # graph never minted. The entry dependency and source revision still
            # make the preference stale when its archived turn changes.
            local_claim_ids = {item.id for item in candidates.atomic_claims}
            if local_claim_ids:
                preferences = []
                for preference in memory.preferences:
                    detached = preference.model_copy(
                        update={
                            "claim_ids": tuple(
                                claim_id
                                for claim_id in preference.claim_ids
                                if claim_id not in local_claim_ids
                            ),
                            "evidence": tuple(
                                reference.model_copy(update={"claim_id": None})
                                if reference.claim_id in local_claim_ids
                                else reference
                                for reference in preference.evidence
                            ),
                        }
                    )
                    preferences.append(
                        Preference.model_validate(detached.model_dump(mode="python"), strict=True)
                    )
                memory = memory.model_copy(update={"preferences": tuple(preferences)})
            self._checkpoint(
                generation_id,
                CompilerStage.MEMORY,
                input_hash,
                {
                    "facts": len(memory.facts),
                    "preferences": len(memory.preferences),
                    "events": len(memory.events),
                    "quarantines": len(memory.quarantines),
                },
            )

            project_overrides, project_conflicts = self._project_overrides(overrides)
            projects = ProjectHierarchyCompiler().compile(
                generation_id=generation_id,
                observations=observation_result.observations,
                atomic_claims=atomic_claims,
                project_candidates=candidates.projects,
                goal_candidates=candidates.goals,
                created_at=selected.created_at,
                overrides=project_overrides,
            )
            self._checkpoint(
                generation_id,
                CompilerStage.PROJECTS,
                input_hash,
                {
                    "projects": len(projects.projects),
                    "goals": len(projects.goals),
                    "handoffs": len(projects.action_handoffs),
                },
            )

            owner_ids = tuple(item.id for item in identity.persons if item.is_archive_owner)
            relationship_snapshots: tuple[RelationshipSnapshot, ...] = ()
            relationship_histories: tuple[RelationshipHistory, ...] = ()
            relationship_quarantines: tuple[Any, ...] = ()
            if owner_ids and candidates.relationship_periods:
                relationships = RelationshipHistoryCompiler().compile(
                    generation_id=generation_id,
                    archive_owner_person_id=owner_ids[0],
                    significant_person_ids=candidates.significant_person_ids,
                    periods=candidates.relationship_periods,
                    people=identity.persons,
                    observations=observation_result.observations,
                    events=memory.events,
                    facts=(*memory.facts, *projects.history_facts),
                    preferences=memory.preferences,
                    approvals=candidates.relationship_approvals,
                    observation_contexts=candidates.relationship_observation_contexts,
                    correlations=candidates.relationship_correlations,
                    created_at=selected.created_at,
                )
                relationship_snapshots = relationships.snapshots
                relationship_histories = relationships.histories
                relationship_quarantines = relationships.quarantines
            elif self.policy.require_archive_owner and source_people and not owner_ids:
                raise ValueError("connector metadata did not establish one archive owner")
            self._checkpoint(
                generation_id,
                CompilerStage.RELATIONSHIPS,
                input_hash,
                {
                    "snapshots": len(relationship_snapshots),
                    "quarantines": len(relationship_quarantines),
                },
            )

            synthesis = self._synthesis(
                generation_id,
                entries,
                episodes,
                observation_result.observations,
                identity.persons,
                projects.projects,
                candidates,
                selected.created_at,
            )
            interpretations: tuple[Interpretation, ...] = ()
            if synthesis is not None:
                validator = InterpretationValidator(self.validation_policy)
                valid: list[Interpretation] = []
                for candidate in synthesis.candidates:
                    result = validator.validate(
                        candidate,
                        generation_id=generation_id,
                        observations=observation_result.observations,
                        episodes=episodes.episodes,
                        correlations=candidates.episode_correlations,
                        people=identity.persons,
                        projects=projects.projects,
                        owner_review=candidates.owner_reviews.get(candidate.id),
                        validated_at=selected.created_at,
                    )
                    if result.interpretation is not None:
                        valid.append(result.interpretation)
                    for issue in result.issues:
                        if issue.blocking:
                            self.repository.quarantine(
                                generation_id,
                                stage="validation",
                                record_kind="interpretation_candidate",
                                candidate_id=candidate.id,
                                reason_code=issue.code,
                                detail={"detail": issue.detail},
                            )
                interpretations = tuple(valid)
            else:
                stale.append(CompilerStage.SYNTHESIS)
            self._checkpoint(
                generation_id,
                CompilerStage.SYNTHESIS,
                input_hash,
                {
                    "candidates": len(synthesis.candidates) if synthesis else 0,
                    "interpretations": len(interpretations),
                },
            )

            records: list[PersonalModelRecord] = [
                *episodes.episodes,
                *identity.persons,
                *observation_result.observations,
                *projects.projects,
                *projects.goals,
                *memory.facts,
                *memory.preferences,
                *memory.events,
                *projects.history_facts,
                *relationship_snapshots,
                *(synthesis.candidates if synthesis else ()),
                *interpretations,
            ]
            records = self._apply_readiness(records, memory.readiness)
            records, applied_matches, correction_conflicts = self._apply_record_overrides(
                records, overrides
            )
            offered_questions = self._accepted_offered_questions(
                records,
                self._offered_question_proposals(synthesis, interpretations),
            )
            all_conflicts = {*identity_conflicts, *project_conflicts, *correction_conflicts}
            self._checkpoint(
                generation_id,
                CompilerStage.OVERRIDES,
                input_hash,
                {"overrides": len(overrides), "conflicts": len(all_conflicts)},
            )

            self._assert_inputs_current(input_hash)
            staged_hashes = self._stage_records(
                generation_id,
                snapshot,
                records,
                prior_active=prior_active,
                no_reuse=self._override_affected_ids(overrides, applied_matches),
            )
            rows = coverage_rows(episodes)
            for start in range(0, len(rows), _COVERAGE_BATCH):
                if start:
                    time.sleep(_WRITE_PAUSE_SECONDS)
                self.state_store.store_coverage(generation_id, rows[start:start + _COVERAGE_BATCH])
            self.state_store.store_identity_memberships(generation_id, identity, source_people)
            self.state_store.store_project_memberships(generation_id, projects)
            self.state_store.store_action_handoffs(generation_id, projects.action_handoffs)
            self.state_store.store_relationship_histories(generation_id, relationship_histories)
            self.state_store.store_offered_questions(generation_id, offered_questions)
            self._checkpoint(
                generation_id, CompilerStage.PERSIST, input_hash, {"records": len(staged_hashes)}
            )

            self._say("checking the result")
            self._record_quarantines(
                generation_id,
                episodes,
                identity,
                observation_result,
                memory,
                projects,
                relationship_quarantines,
                synthesis,
            )
            self._replay_overrides(
                generation_id,
                overrides,
                records,
                applied_matches,
                all_conflicts,
                identity,
                projects,
            )
            readiness = PersonalModelReadinessEvaluator().evaluate(
                ReadinessInputs(
                    entries=entries,
                    episodes=episodes,
                    observations=observation_result.observations,
                    identity=identity,
                    source_people=source_people,
                    projects=projects,
                    project_candidates=candidates.projects,
                    atomic_claims=atomic_claims,
                    records=records,
                    lookup_fixtures=selected.lookup_fixtures,
                )
            )
            for gate in readiness.gates:
                self.repository.record_metric(
                    generation_id,
                    stage="readiness",
                    name=gate.gate,
                    value=gate.model_dump(mode="json"),
                )
            self.repository.record_metric(
                generation_id,
                stage="readiness",
                name="fact_records",
                value=[item.model_dump(mode="json") for item in readiness.fact_records],
            )
            readiness_detail = {
                "verdict_counts": readiness.verdict_counts,
                "gates": {
                    item.gate: {
                        "verdict": str(item.verdict),
                        "measured_value": item.measured_value,
                        "threshold": item.threshold,
                    }
                    for item in readiness.gates
                },
            }
            self._checkpoint(
                generation_id,
                CompilerStage.READINESS,
                input_hash,
                readiness_detail,
            )
            self._activation_gates(generation_id, records, stale, selected)
            self._checkpoint(
                generation_id,
                CompilerStage.VALIDATE,
                input_hash,
                {"passed": True, "readiness": readiness_detail},
            )
            record_counts = self._counts(records)
            self.repository.record_metric(
                generation_id, stage="compiler", name="record_counts", value=record_counts
            )
            self.repository.record_metric(
                generation_id,
                stage="compiler",
                name="readiness_counts",
                value=self._readiness_counts(records),
            )
            self.repository.record_metric(
                generation_id,
                stage="compiler",
                name="stale_stages",
                value=[str(item) for item in stale],
            )
            # This checkpoint has to precede the activation, not follow it:
            # every generation-scoped row becomes immutable the moment the
            # generation goes ``current``, so a row written afterwards is
            # rejected by the trigger.  It therefore records that the compiler
            # reached the switch, not that the switch landed -- a run killed in
            # between leaves ``activate: complete`` above a generation still at
            # ``staged``.  ``varyz model status`` reads the two together and
            # says so; nothing else should read this row alone.
            self._checkpoint(
                generation_id,
                CompilerStage.ACTIVATE,
                input_hash,
                {"requested": selected.activate, "validated": True},
            )
            # Released before the switch, not after it.  Activation re-reads and
            # re-hashes the whole archive to prove the inputs did not move under
            # the compile, which is a second copy of the largest thing the run
            # touches, asked for at the moment the first copy is still alive:
            # the generation itself, every episode, every observation and every
            # entry read at the top.  On 2026-09-08 that cost the run its
            # result.  The compiler wrote `activate: complete` at 02:16:18Z with
            # a 15 GB heap, and Windows answered the next allocation by trimming
            # the working set to a few hundred megabytes; three hours later the
            # process was still faulting its own heap back in at nine thousand
            # reads a second and had not reached the pointer swap.  Nothing
            # below this line reads any of these, and the counts they were kept
            # for have already been taken.
            del records, entries, snapshot, episodes, observation_result
            del identity, source_people, projects, candidates, atomic_claims
            gc.collect()
            # The switch re-checks every record first: on a slow laptop, minutes.
            self._say("checking every record, then switching to the new model")
            activated_generation = (
                self._activate_generation(generation_id, input_hash)
                if selected.activate
                else self.repository.get_generation(generation_id)
            )
            return CompileResult(
                generation_id=generation_id,
                status=CompileStatus.COMPLETE,
                active_generation_id=self.repository.active_generation_id(),
                activated=selected.activate,
                failed=False,
                stale_stages=tuple(str(item) for item in stale),
                record_counts=record_counts,
                reused_record_count=activated_generation.reused_record_count,
                quarantine_count=activated_generation.quarantine_count,
                readiness=readiness,
                observation_progress=observation_progress,
            )
        except Exception as exc:
            try:
                self.state_store.checkpoint(
                    generation_id,
                    CompilerStage.ACTIVATE,
                    CheckpointState.FAILED,
                    input_hash,
                    {"error_type": type(exc).__name__},
                )
                failed = self.repository.fail_generation(
                    generation_id, f"{type(exc).__name__}: {exc}"
                )
            except Exception:
                failed = self.repository.get_generation(generation_id)
            return CompileResult(
                generation_id=generation_id,
                status=CompileStatus.FAILED,
                active_generation_id=self.repository.active_generation_id(),
                activated=False,
                failed=True,
                stale_stages=tuple(str(item) for item in stale),
                record_counts={},
                reused_record_count=failed.reused_record_count,
                quarantine_count=failed.quarantine_count,
                readiness=readiness,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _activate_generation(self, generation_id: str, expected_input_hash: str) -> ModelGeneration:
        def checked(done: int, total: int) -> None:
            self._say(f"checking {done:,} of {total:,} records", done / total if total else None)

        if self._data_dir is None:
            return self.repository.activate_generation(generation_id, on_checked=checked)
        # The final input check and pointer swap are one writer session. Ingestion
        # may run during inference, but it cannot slip between these two operations.
        with WriteLock(self._data_dir):
            self._assert_inputs_current(expected_input_hash)
            return self.repository.activate_generation(generation_id, on_checked=checked)

    def _assert_inputs_current(self, expected_input_hash: str) -> None:
        current_snapshot = self.graph_reader.read_active()
        current_entries = tuple(
            sorted(
                self.evidence_reader.read_entries(current_snapshot),
                key=lambda item: item.id,
            )
        )
        self._validate_inputs(current_snapshot, current_entries)
        if self._input_hash(current_snapshot, current_entries) != expected_input_hash:
            raise RuntimeError(
                "personal-model inputs changed during compilation; retry against the "
                "current archive"
            )

    def _validate_inputs(self, snapshot: GraphSnapshot, entries: Sequence[Entry]) -> None:
        if len({item.id for item in entries}) != len(entries):
            raise ValueError("evidence reader returned duplicate entry ids")
        if snapshot.evidence_content_hash is not None:
            digest = hashlib.sha256()
            for ordinal, entry in enumerate(sorted(entries, key=lambda item: item.id)):
                if ordinal:
                    digest.update(b"\n")
                digest.update(f"{entry.id}:{entry.content_hash}".encode())
            if digest.hexdigest() != snapshot.evidence_content_hash:
                raise RuntimeError(
                    "active graph evidence projection is stale; rebuild the graph before "
                    "compiling the personal model"
                )
        if snapshot.evidence_entry_count is not None and snapshot.evidence_entry_count != len(
            entries
        ):
            raise RuntimeError(
                "active graph entry count is stale; rebuild the graph before compiling "
                "the personal model"
            )
        for claim in snapshot.atomic_claims:
            if claim.span.entry_id not in {item.id for item in entries}:
                raise ValueError(
                    f"atomic claim references evidence outside the pinned projection: {claim.id}"
                )
        for label, adapter in (
            ("candidate", self.candidate_adapter),
            ("observation", self.annotator),
            ("synthesis", self.synthesis_adapter),
        ):
            if adapter is None:
                continue
            locality = getattr(adapter, "execution_locality", None)
            if locality is None and hasattr(adapter, "descriptor"):
                locality = getattr(adapter.descriptor, "execution_locality", None)
            try:
                selected = ExecutionLocality(locality)
            except (TypeError, ValueError):
                selected = ExecutionLocality.EXTERNAL
            if not self.policy.user_present and selected == ExecutionLocality.EXTERNAL:
                raise ValueError(
                    f"background {label} adapter is not declared managed-local/offline"
                )
            if selected == ExecutionLocality.EXTERNAL and not self.policy.allow_cloud_egress:
                raise ValueError(
                    f"external {label} adapter requires explicit cloud-egress authorization"
                )

    def _input_hash(self, snapshot: GraphSnapshot, entries: Sequence[Entry]) -> str:
        descriptor = self.annotator.descriptor.model_dump(mode="json") if self.annotator else None
        return content_hash(
            {
                "graph": snapshot.model_dump(mode="json"),
                "entries": [self._entry_projection(item) for item in entries],
                "compiler": self.policy.model_dump(mode="json"),
                "identity": self.source_identity_adapter.version,
                "candidate_adapter": {
                    "name": self.candidate_adapter.name,
                    "version": self.candidate_adapter.version,
                    "model": self.candidate_adapter.model_name,
                    "prompt": self.candidate_adapter.prompt_version,
                },
                "annotator": descriptor,
                "synthesis": self._synthesis_descriptor(),
                "validation_policy": self.validation_policy.model_dump(mode="json"),
            }
        )

    @staticmethod
    def _entry_projection(entry: Entry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "content_hash": entry.content_hash,
            "source": entry.source,
            "source_account": entry.source_account,
            "external_id": entry.external_id,
            "occurred_start": entry.occurred_start,
            "thread_id": entry.thread_id,
            "authored_by_user": entry.authored_by_user,
            "sensitivity": str(entry.sensitivity),
            "conversation": entry.conversation.model_dump(mode="json"),
        }

    def _synthesis_descriptor(self) -> dict[str, Any] | None:
        if self.synthesis_adapter is None:
            return None
        return {
            key: getattr(self.synthesis_adapter, key)
            for key in ("name", "version", "model_name", "prompt_version")
        }

    def _checkpoint(
        self, generation_id: str, stage: CompilerStage, input_hash: str, detail: Mapping[str, Any]
    ) -> None:
        self.state_store.checkpoint(
            generation_id, stage, CheckpointState.COMPLETE, input_hash, detail
        )
        # Thirteen stages ran and only two of them said anything, so a compile
        # on a real archive went quiet for three quarters of an hour at a time.
        # Counts only: one of these details carries the whole annotation hash
        # list, which is megabytes.
        counts = ", ".join(
            f"{key} {value}"
            for key, value in detail.items()
            if isinstance(value, int) and not isinstance(value, bool)
        )
        log.info("stage %s complete%s", stage.value, f": {counts}" if counts else "")

    def _observations(
        self,
        generation_id: str,
        entries: Sequence[Entry],
        episodes: EpisodeBuildResult,
        identity: IdentityResolutionResult,
        request: CompileRequest,
    ) -> _ObservationStageResult:
        total = len(episodes.episodes)
        if self.annotator is None:
            if entries and self.policy.require_observation_annotator:
                raise RuntimeError(
                    "semantic observation annotator unavailable; prior active generation retained"
                )
            extraction = ObservationExtractionResult(annotator_version="unavailable")
            progress = ObservationProgress(
                episodes_annotated=0,
                episodes_total=total,
                episodes_remaining=total,
                entries_reached=0,
                annotation_calls=0,
                cache_hits=0,
            )
            self.state_store.store_observation_progress(
                generation_id,
                progress,
                request,
                annotated_episode_ids=(),
                reached_entry_ids=(),
                updated_at=self.clock(),
            )
            return _ObservationStageResult(extraction, progress, False)
        cached = _CachingAnnotator(
            self.annotator,
            self.state_store,
            max_annotation_calls=request.max_annotation_calls,
            cached_only=request.cached_observations_only,
            deadline=request.observation_deadline,
            clock=self.clock,
        )
        exhausted_by: str | None = None
        try:
            result = ObservationExtractor(cached).extract(
                episodes,
                entries,
                entity_resolutions=identity.source_entity_resolutions,
                generation_id=generation_id,
                created_at=request.created_at,
                batch_size=self.policy.observation_batch_size,
                prefilter=True,
                order=self.policy.observation_order,
                defer_repeat_windows=self.policy.observation_defer_repeat_windows,
                budget=self.policy.observation_budget,
            )
        except ObservationBudgetExhausted as exc:
            exhausted_by = exc.reason
            result = exc.partial_result or ObservationExtractionResult(
                annotator_version=self.annotator.descriptor.version,
            )
        skipped_episode_ids = {item.episode_id for item in result.skips}
        completed_episode_ids = cached.annotated_episode_ids | skipped_episode_ids
        reached_entry_ids = set(cached.reached_entry_ids)
        reached_entry_ids.update(
            entry_id for item in result.skips for entry_id in item.target_entry_ids
        )
        annotated = len(completed_episode_ids)
        progress = ObservationProgress(
            episodes_annotated=annotated,
            episodes_total=total,
            episodes_remaining=total - annotated,
            entries_reached=len(reached_entry_ids),
            annotation_calls=cached.misses,
            cache_hits=cached.hits,
            budget_exhausted_by=exhausted_by,
        )
        self.state_store.store_observation_progress(
            generation_id,
            progress,
            request,
            annotated_episode_ids=tuple(completed_episode_ids),
            reached_entry_ids=tuple(reached_entry_ids),
            updated_at=self.clock(),
        )
        self.repository.record_metric(
            generation_id,
            stage="observations",
            name="cache",
            value={
                "hits": cached.hits,
                "misses": cached.misses,
                "episodes_annotated": annotated,
                "episodes_total": total,
                "budget_exhausted_by": exhausted_by,
            },
        )
        self.repository.record_metric(
            generation_id,
            stage="observations",
            name="prefilter_audit",
            value=[item.model_dump(mode="json") for item in result.skips],
        )
        return _ObservationStageResult(result, progress, exhausted_by is not None)

    def _synthesis(
        self,
        generation_id: str,
        entries: Sequence[Entry],
        episodes: EpisodeBuildResult,
        observations: Sequence[Observation],
        people: Sequence[Person],
        projects: Sequence[Project],
        candidates: CompilerCandidateBatch,
        created_at: datetime,
    ) -> SynthesisResult | None:
        if self.synthesis_adapter is None or not observations:
            return None
        return self.synthesis_adapter.synthesize(
            generation_id=generation_id,
            observations=observations,
            episodes=episodes,
            correlations=candidates.episode_correlations,
            people=people,
            projects=projects,
            source_scopes=tuple(
                sorted({f"{item.source}:{item.source_account}" for item in entries})
            ),
            created_at=created_at,
        )

    def _identity_overrides(
        self, overrides: Sequence[ModelOverride]
    ) -> tuple[tuple[IdentityOverride, ...], set[str]]:
        translated: list[IdentityOverride] = []
        conflicts: set[str] = set()
        for item in overrides:
            if item.target_kind != OverrideTargetKind.PERSON or item.action not in {
                OverrideAction.MERGE,
                OverrideAction.SPLIT,
            }:
                continue
            payload = (
                SplitPayload.model_validate_json(canonical_json(item.payload))
                if item.action == OverrideAction.SPLIT
                else MergePayload.model_validate_json(canonical_json(item.payload))
            )
            if isinstance(payload, SplitPayload):
                groups = payload.member_groups
                if groups is None:
                    conflicts.add(item.id)
                    continue
                action = IdentityOverrideAction.SPLIT
            else:
                left = self.state_store.previous_members("person", item.target_record_id)
                right = self.state_store.previous_members("person", payload.into_id)
                if not left or not right:
                    conflicts.add(item.id)
                    continue
                groups = (tuple(sorted({*left, *right})),)
                action = IdentityOverrideAction.MERGE
            translated.append(
                IdentityOverride(
                    sequence=item.sequence, override_id=item.id, action=action, member_groups=groups
                )
            )
        return tuple(translated), conflicts

    def _project_overrides(
        self, overrides: Sequence[ModelOverride]
    ) -> tuple[tuple[ProjectHierarchyOverride, ...], set[str]]:
        translated: list[ProjectHierarchyOverride] = []
        conflicts: set[str] = set()
        for item in overrides:
            if item.target_kind == OverrideTargetKind.PROJECT and item.action in {
                OverrideAction.MERGE,
                OverrideAction.SPLIT,
            }:
                if item.action == OverrideAction.SPLIT:
                    payload = SplitPayload.model_validate_json(canonical_json(item.payload))
                    groups = payload.member_groups
                    if groups is None:
                        conflicts.add(item.id)
                        continue
                    action = ProjectOverrideAction.SPLIT
                else:
                    payload = MergePayload.model_validate_json(canonical_json(item.payload))
                    left = self.state_store.previous_members("project", item.target_record_id)
                    right = self.state_store.previous_members("project", payload.into_id)
                    if not left or not right:
                        conflicts.add(item.id)
                        continue
                    groups = (tuple(sorted({*left, *right})),)
                    action = ProjectOverrideAction.MERGE
                translated.append(
                    ProjectHierarchyOverride(
                        sequence=item.sequence,
                        override_id=item.id,
                        action=action,
                        project_member_groups=groups,
                    )
                )
            elif item.target_kind == OverrideTargetKind.GOAL and item.action in {
                OverrideAction.REPARENT,
                OverrideAction.RECLASSIFY,
            }:
                members = self.state_store.previous_members("goal", item.target_record_id)
                if len(members) != 1:
                    conflicts.add(item.id)
                    continue
                if item.action == OverrideAction.REPARENT:
                    payload = ReparentPayload.model_validate_json(canonical_json(item.payload))
                    parent_candidates: list[str] = []
                    for parent in payload.parent_goal_ids:
                        choices = self.state_store.previous_members("goal", parent)
                        if len(choices) != 1:
                            conflicts.add(item.id)
                            break
                        parent_candidates.append(choices[0])
                    else:
                        translated.append(
                            ProjectHierarchyOverride(
                                sequence=item.sequence,
                                override_id=item.id,
                                action=ProjectOverrideAction.REPARENT,
                                goal_candidate_id=members[0],
                                new_parent_goal_candidate_ids=tuple(parent_candidates),
                            )
                        )
                else:
                    payload = ReclassifyPayload.model_validate_json(canonical_json(item.payload))
                    translated.append(
                        ProjectHierarchyOverride(
                            sequence=item.sequence,
                            override_id=item.id,
                            action=ProjectOverrideAction.RECLASSIFY,
                            goal_candidate_id=members[0],
                            new_node_kind=ProposedNodeKind(payload.kind.value),
                        )
                    )
        return tuple(translated), conflicts

    def _apply_record_overrides(
        self, records: Sequence[PersonalModelRecord], overrides: Sequence[ModelOverride]
    ) -> tuple[list[PersonalModelRecord], dict[str, str], set[str]]:
        by_id = {item.id: item for item in records}
        matches: dict[str, str] = {}
        conflicts: set[str] = set()
        user = ProducerMetadata(kind=ProducerKind.USER, name="owner-correction", version="1")
        superseded = {
            item.supersedes_override_id
            for item in overrides
            if item.action == OverrideAction.VERDICT and item.supersedes_override_id is not None
        }
        for item in overrides:
            if item.id in superseded:
                continue
            if item.target_kind in {
                OverrideTargetKind.PERSON,
                OverrideTargetKind.PROJECT,
                OverrideTargetKind.GOAL,
            }:
                continue
            record = by_id.get(item.target_record_id)
            if record is None or record_kind(record) != str(item.target_kind):
                continue
            try:
                update: dict[str, Any] = {
                    "producer": user,
                    "derivation": DerivationKind.USER_CONFIRMED,
                }
                if isinstance(record, Fact):
                    if item.action in {OverrideAction.CONFIRM, OverrideAction.EDIT}:
                        update.update(
                            value=FactValuePayload.model_validate_json(
                                canonical_json(item.payload)
                            ).value,
                            status=FactStatus.ACTIVE,
                            readiness=Readiness.READY,
                        )
                    elif item.action == OverrideAction.REJECT:
                        update.update(status=FactStatus.REJECTED, readiness=Readiness.REJECTED)
                    elif item.action == OverrideAction.SUPERSEDE:
                        replacement = SupersedePayload.model_validate_json(
                            canonical_json(item.payload)
                        ).replacement_record_id
                        if replacement not in by_id:
                            raise ValueError("replacement fact is absent")
                        replacement_record = by_id[replacement]
                        if not isinstance(replacement_record, Fact):
                            raise ValueError("replacement record is not a fact")
                        replacement_update = replacement_record.model_copy(
                            update={
                                "supersedes_fact_ids": tuple(
                                    sorted(
                                        {
                                            *replacement_record.supersedes_fact_ids,
                                            record.id,
                                        }
                                    )
                                )
                            }
                        )
                        by_id[replacement] = Fact.model_validate(
                            replacement_update.model_dump(mode="python"), strict=True
                        )
                        update.update(status=FactStatus.SUPERSEDED, readiness=Readiness.SUPERSEDED)
                elif isinstance(record, Preference):
                    if item.action in {OverrideAction.CONFIRM, OverrideAction.EDIT}:
                        update.update(
                            object=PreferenceValuePayload.model_validate_json(
                                canonical_json(item.payload)
                            ).object,
                            status=FactStatus.ACTIVE,
                            readiness=Readiness.READY,
                        )
                    elif item.action == OverrideAction.REJECT:
                        update.update(status=FactStatus.REJECTED, readiness=Readiness.REJECTED)
                    elif item.action == OverrideAction.SUPERSEDE:
                        replacement = SupersedePayload.model_validate_json(
                            canonical_json(item.payload)
                        ).replacement_record_id
                        if replacement not in by_id:
                            raise ValueError("replacement preference is absent")
                        replacement_record = by_id[replacement]
                        if not isinstance(replacement_record, Preference):
                            raise ValueError("replacement record is not a preference")
                        replacement_update = replacement_record.model_copy(
                            update={
                                "supersedes_preference_ids": tuple(
                                    sorted(
                                        {
                                            *replacement_record.supersedes_preference_ids,
                                            record.id,
                                        }
                                    )
                                )
                            }
                        )
                        by_id[replacement] = Preference.model_validate(
                            replacement_update.model_dump(mode="python"), strict=True
                        )
                        update.update(status=FactStatus.SUPERSEDED, readiness=Readiness.SUPERSEDED)
                elif isinstance(record, Interpretation):
                    if item.action == OverrideAction.VERDICT:
                        struck_thesis = (
                            item.struck_value.get("thesis")
                            if isinstance(item.struck_value, dict)
                            else None
                        )
                        # The evidence anchor deliberately survives prose regeneration. The
                        # struck thesis prevents that durability from blessing a new reading.
                        if struck_thesis != record.thesis:
                            continue
                        payload = InterpretationVerdictPayload.model_validate_json(
                            canonical_json(item.payload)
                        )
                        update = {
                            "owner_verdict": payload.verdict,
                            "owner_kept_part": payload.kept_part,
                        }
                        if payload.verdict == InterpretationVerdict.CONFIRM:
                            update["readiness"] = record.readiness
                        elif payload.verdict == InterpretationVerdict.PARTIAL:
                            update.update(
                                thesis=payload.kept_part,
                                readiness=Readiness.LIMITED,
                            )
                        else:
                            update["readiness"] = Readiness.REJECTED
                    elif item.action == OverrideAction.EDIT:
                        payload = InterpretationEditPayload.model_validate_json(
                            canonical_json(item.payload)
                        )
                        update.update(thesis=payload.thesis, readiness=Readiness.READY)
                        if payload.derivation_summary is not None:
                            update["derivation_summary"] = payload.derivation_summary
                    elif item.action in {OverrideAction.DISPUTE, OverrideAction.DELETE}:
                        update.update(readiness=Readiness.REJECTED)
                    elif item.action == OverrideAction.RESTORE:
                        update.update(readiness=Readiness.LIMITED)
                else:
                    continue
                by_id[record.id] = record.model_copy(update=update)
                # Revalidate after model_copy, which intentionally skips validation.
                by_id[record.id] = type(record).model_validate(
                    by_id[record.id].model_dump(mode="python"), strict=True
                )
                matches[item.id] = record.id
            except (ValueError, TypeError):
                conflicts.add(item.id)
        return [by_id[item.id] for item in records], matches, conflicts

    @staticmethod
    def _override_affected_ids(
        overrides: Sequence[ModelOverride], applied_matches: Mapping[str, str]
    ) -> frozenset[str]:
        affected = set(applied_matches.values())
        for item in overrides:
            if item.id not in applied_matches or item.action != OverrideAction.SUPERSEDE:
                continue
            payload = SupersedePayload.model_validate_json(canonical_json(item.payload))
            affected.add(payload.replacement_record_id)
        return frozenset(affected)

    @staticmethod
    def _apply_readiness(
        records: Sequence[PersonalModelRecord], readiness_reports: Sequence[Any]
    ) -> list[PersonalModelRecord]:
        exact_ready = {item.record_id for item in readiness_reports if item.eligible}
        promoted: list[PersonalModelRecord] = []
        for record in records:
            readiness = record.readiness
            if isinstance(record, Fact):
                if record.status == FactStatus.REJECTED:
                    readiness = Readiness.REJECTED
                elif record.status == FactStatus.SUPERSEDED:
                    readiness = Readiness.SUPERSEDED
                elif record.id in exact_ready or (
                    record.derivation == DerivationKind.USER_CONFIRMED
                ):
                    readiness = Readiness.READY
                else:
                    readiness = Readiness.LIMITED
            elif isinstance(record, Preference):
                if record.status == FactStatus.REJECTED:
                    readiness = Readiness.REJECTED
                elif record.status == FactStatus.SUPERSEDED:
                    readiness = Readiness.SUPERSEDED
                elif record.derivation in {
                    DerivationKind.EXPLICIT,
                    DerivationKind.USER_CONFIRMED,
                }:
                    readiness = Readiness.READY
                else:
                    readiness = Readiness.LIMITED
            elif isinstance(record, (Person, Project, Goal, RelationshipSnapshot)) or (
                record_kind(record) == "event"
            ):
                readiness = Readiness.LIMITED
            if readiness != record.readiness:
                candidate = record.model_copy(update={"readiness": readiness})
                record = type(record).model_validate(
                    candidate.model_dump(mode="python"), strict=True
                )
            promoted.append(record)
        return promoted

    def _stage_records(
        self,
        generation_id: str,
        snapshot: GraphSnapshot,
        records: Sequence[PersonalModelRecord],
        *,
        prior_active: str | None,
        no_reuse: frozenset[str],
    ) -> dict[str, str]:
        if len({item.id for item in records}) != len(records):
            raise ValueError("compiler produced duplicate canonical record ids")
        staged: dict[str, str] = {}
        staging_order = _staging_order(records)
        common = compiler_input_dependency(
            "compiler_contract",
            {
                "compiler": self.policy.compiler_version,
                "policy": self.policy.version,
                "candidate": self.candidate_adapter.version,
                "source_preferences": PinnedGraphCandidateAdapter.version,
                "synthesis": self._synthesis_descriptor(),
                "validation_policy": self.validation_policy.model_dump(mode="json"),
            },
        )
        total = len(staging_order)

        def say(done: int) -> None:
            self._say(f"saving {done:,} of {total:,} records", done / total)

        self._write_all(staging_order, lambda record: self._stage_one(
            generation_id, snapshot, record, staged, common,
            prior_active=prior_active, no_reuse=no_reuse,
        ), say)
        return staged

    def _write_all(
        self, items: Sequence[Any], write: Callable[[Any], object],
        say: Callable[[int], None] | None = None,
    ) -> None:
        """Write the items in short transactions, so other writers never wait long."""
        done, said = 0, -_WRITE_BATCH
        while done < len(items):
            if say is not None and done - said >= _WRITE_BATCH:
                say(done)
                said = done
            deadline = time.monotonic() + _WRITE_BATCH_SECONDS
            with self._write_batch():
                for item in items[done:done + _WRITE_BATCH]:
                    write(item)
                    done += 1
                    if time.monotonic() >= deadline:
                        break
            if done < len(items):
                time.sleep(_WRITE_PAUSE_SECONDS)

    def _stage_one(
        self,
        generation_id: str,
        snapshot: GraphSnapshot,
        record: PersonalModelRecord,
        staged: dict[str, str],
        common: ModelDependency,
        *,
        prior_active: str | None,
        no_reuse: frozenset[str],
    ) -> None:
        dependencies = list(
            required_record_dependencies(self.repository.conn, snapshot.generation_id, record)
        )
        dependencies.append(common)
        for kind, target in sorted(required_model_references(record)):
            if target not in staged:
                raise ValueError(f"{record.id} depends on unstaged {kind} {target}")
            dependencies.append(model_record_dependency(kind, target, staged[target]))
        unique = {(item.kind, item.id): item for item in dependencies}
        dep_tuple = tuple(unique[key] for key in sorted(unique))
        expected = compute_dependency_hash(
            record,
            dep_tuple,
            compiler_version=self.policy.compiler_version,
            policy_version=self.policy.version,
        )
        reused = False
        if prior_active is not None and record.id not in no_reuse:
            reused = self.repository.clone_record_if_unchanged(
                source_generation_id=prior_active,
                target_generation_id=generation_id,
                record_id=record.id,
                expected_dependency_hash=expected,
            )
        staged[record.id] = (
            expected if reused else self.repository.stage_record(record, dependencies=dep_tuple)
        )

    def _replay_overrides(
        self,
        generation_id: str,
        overrides: Sequence[ModelOverride],
        records: Sequence[PersonalModelRecord],
        applied_matches: Mapping[str, str],
        conflicts: set[str],
        identity: IdentityResolutionResult,
        projects: ProjectCompilation,
    ) -> None:
        by_id = {item.id: item for item in records}
        person_mapping = dict(identity.source_identity_to_person_id)
        superseded = {
            item.supersedes_override_id
            for item in overrides
            if item.action == OverrideAction.VERDICT and item.supersedes_override_id is not None
        }
        for item in overrides:
            if item.id in superseded:
                self.repository.record_override_replay(
                    generation_id,
                    item.id,
                    outcome=ReplayOutcome.NOT_APPLICABLE,
                    detail="a later interpretation verdict supersedes this ledger row",
                )
                continue
            if item.id in conflicts:
                self.repository.record_override_replay(
                    generation_id,
                    item.id,
                    outcome=ReplayOutcome.CONFLICT,
                    detail="override could not be replayed against durable membership",
                )
                continue
            matched = applied_matches.get(item.id)
            if matched is None and item.id in identity.applied_override_ids:
                translated = self._identity_overrides((item,))[0][0]
                first = translated.member_groups[0][0]
                matched = person_mapping.get(first)
            if matched is None and item.id in projects.applied_override_ids:
                if item.target_kind == OverrideTargetKind.PROJECT:
                    members = self.state_store.previous_members("project", item.target_record_id)
                    matched = (
                        projects.project_candidate_to_project_id.get(members[0])
                        if members
                        else None
                    )
                elif item.target_kind == OverrideTargetKind.GOAL:
                    members = self.state_store.previous_members("goal", item.target_record_id)
                    matched = (
                        projects.goal_candidate_to_goal_id.get(members[0]) if members else None
                    )
            if (
                matched is None
                and item.action == OverrideAction.VERDICT
                and (target := by_id.get(item.target_record_id)) is not None
                and isinstance(target, Interpretation)
            ):
                struck_thesis = (
                    item.struck_value.get("thesis") if isinstance(item.struck_value, dict) else None
                )
                if struck_thesis != target.thesis:
                    self.repository.record_override_replay(
                        generation_id,
                        item.id,
                        outcome=ReplayOutcome.ORPHANED,
                        detail="the evidence anchor remains but its interpretation thesis changed",
                    )
                    continue
            if (
                matched is None
                and item.target_record_id in by_id
                and record_kind(by_id[item.target_record_id]) == str(item.target_kind)
            ):
                matched = item.target_record_id
            if matched is not None:
                self.repository.record_override_replay(
                    generation_id, item.id, outcome=ReplayOutcome.APPLIED, matched_record_id=matched
                )
            else:
                self.repository.record_override_replay(
                    generation_id,
                    item.id,
                    outcome=ReplayOutcome.ORPHANED,
                    detail="stable override target and durable membership are absent",
                )

    def _activation_gates(
        self,
        generation_id: str,
        records: Sequence[PersonalModelRecord],
        stale: Sequence[str],
        request: CompileRequest,
    ) -> None:
        if len(records) < self.policy.minimum_records:
            raise ValueError("personal-model record floor not met")
        if stale and not self.policy.allow_literal_only_activation:
            raise RuntimeError(
                f"semantic stages are stale: {', '.join(str(item) for item in stale)}"
            )
        if self.policy.fail_on_quarantine:
            row = self.repository.conn.execute(
                "SELECT COUNT(*) AS count FROM personal_model_quarantines WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if row["count"]:
                raise ValueError("compiler policy blocks activation with quarantines")
        if request.required_applied_override_id is not None:
            replay = self.repository.conn.execute(
                """SELECT outcome FROM personal_model_override_replays
                   WHERE generation_id=? AND override_id=?""",
                (generation_id, request.required_applied_override_id),
            ).fetchone()
            if replay is None or replay["outcome"] != str(ReplayOutcome.APPLIED):
                raise RuntimeError("the requested correction could not be applied canonically")
        if request.required_record_ids:
            present = {item.id for item in records}
            missing = sorted(set(request.required_record_ids) - present)
            if missing:
                raise RuntimeError(
                    "correction rebuild would discard existing canonical records; "
                    "a semantic rebuild is required"
                )

    def _offered_question_proposals(
        self,
        synthesis: SynthesisResult | None,
        interpretations: Sequence[Interpretation],
    ) -> tuple[OfferedQuestionProposal, ...]:
        if synthesis is None or self.synthesis_adapter is None:
            return ()
        propose = getattr(self.synthesis_adapter, "offered_question_proposals", None)
        if propose is None:
            return ()
        return tuple(
            propose(
                synthesis=synthesis,
                interpretations=interpretations,
            )
        )

    @staticmethod
    def _accepted_offered_questions(
        records: Sequence[PersonalModelRecord],
        proposals: Sequence[OfferedQuestionProposal],
    ) -> tuple[OfferedQuestion, ...]:
        if len({item.question_id for item in proposals}) != len(proposals):
            raise ValueError("offered question proposal ids must be unique")
        visible_by_candidate = {
            item.candidate_id: item
            for item in records
            if isinstance(item, Interpretation)
            and item.readiness in {Readiness.READY, Readiness.LIMITED}
        }
        accepted: list[OfferedQuestion] = []
        for proposal in sorted(proposals, key=lambda item: item.question_id):
            interpretation = visible_by_candidate.get(proposal.interpretation_candidate_id)
            if interpretation is None:
                continue
            accepted.append(
                OfferedQuestion(
                    question_id=proposal.question_id,
                    interpretation_id=interpretation.id,
                    display_text=proposal.display_text,
                    normalized_text=proposal.normalized_text,
                    offered_at=proposal.offered_at,
                    state=(
                        "withdrawn" if interpretation.owner_verdict is not None else proposal.state
                    ),
                )
            )
        return tuple(accepted)

    def _record_quarantines(
        self,
        generation_id: str,
        episodes: EpisodeBuildResult,
        identity: IdentityResolutionResult,
        observations: ObservationExtractionResult,
        memory: Any,
        projects: ProjectCompilation,
        relationship_quarantines: Sequence[Any],
        synthesis: SynthesisResult | None,
    ) -> None:
        groups: tuple[tuple[str, Sequence[Any]], ...] = (
            ("episodes", episodes.diagnostics),
            ("identity", identity.quarantines),
            ("observations", observations.quarantines),
            ("memory", memory.quarantines),
            ("projects", projects.quarantines),
            ("relationships", relationship_quarantines),
            ("synthesis", synthesis.diagnostics if synthesis else ()),
        )
        pending = [(stage, item) for stage, items in groups for item in items]

        def write(pair: tuple[str, Any]) -> None:
            stage, item = pair
            payload = (
                item.model_dump(mode="json")
                if isinstance(item, BaseModel)
                else getattr(item, "__dict__", {"detail": str(item)})
            )
            reason = payload.get("code") or payload.get("reason") or type(item).__name__
            self.repository.quarantine(
                generation_id, stage=stage, reason_code=str(reason), detail=payload
            )

        self._write_all(pending, write)

    @staticmethod
    def _counts(records: Iterable[PersonalModelRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in records:
            kind = record_kind(item)
            counts[kind] = counts.get(kind, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _readiness_counts(records: Iterable[PersonalModelRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in records:
            key = str(item.readiness)
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


class CorrectionApplicationService:
    """Append one correction and synchronously materialize a canonical snapshot.

    The ledger append is durable even when rebuilding fails. The active pointer
    changes only after the ordinary compiler gates pass, so UI, query, CLI and
    MCP all observe the same result and never need a read-time correction overlay.
    """

    def __init__(
        self,
        *,
        repository: PersonalModelRepository,
        compiler: PersonalModelCompiler,
    ) -> None:
        self.repository = repository
        self.compiler = compiler

    def apply(
        self,
        *,
        target_kind: OverrideTargetKind | str,
        target_record_id: str,
        action: OverrideAction | str,
        payload: dict[str, Any],
        actor: str = "owner",
        struck_value: Any | None = None,
        supersedes_override_id: str | None = None,
        override_id: str | None = None,
        generation_id: str | None = None,
        created_at: datetime | None = None,
    ) -> CorrectionApplicationResult:
        active_generation_id = self.repository.active_generation_id()
        required_record_ids: tuple[str, ...] = ()
        if active_generation_id is not None:
            required_record_ids = tuple(
                row["record_id"]
                for row in self.repository.conn.execute(
                    """SELECT record_id FROM personal_model_records
                       WHERE generation_id=? ORDER BY record_id""",
                    (active_generation_id,),
                ).fetchall()
            )
        override = self.repository.append_override(
            target_kind=target_kind,
            target_record_id=target_record_id,
            action=action,
            payload=payload,
            actor=actor,
            struck_value=struck_value,
            supersedes_override_id=supersedes_override_id,
            override_id=override_id,
        )
        result = self.compiler.compile(
            CompileRequest(
                generation_id=generation_id,
                created_at=created_at or datetime.now(UTC),
                required_applied_override_id=override.id,
                required_record_ids=required_record_ids,
            )
        )
        if not result.failed and result.activated:
            disposition = CorrectionDisposition.APPLIED
        else:
            replay = self.repository.conn.execute(
                """SELECT outcome FROM personal_model_override_replays
                   WHERE generation_id=? AND override_id=?""",
                (result.generation_id, override.id),
            ).fetchone()
            disposition = (
                CorrectionDisposition.REBUILD_NEEDED
                if replay is not None and replay["outcome"] in {"orphaned", "conflict"}
                else CorrectionDisposition.FAILED
            )
        return CorrectionApplicationResult(
            disposition=disposition,
            override_id=override.id,
            override_sequence=override.sequence,
            generation_id=result.generation_id,
            active_generation_id=result.active_generation_id,
            error=result.error,
        )


def _quarantines_by_code(result: ObservationExtractionResult) -> dict[str, int]:
    """How the observation stage lost what it lost, by reason, largest first.

    The checkpoint recorded two numbers -- kept and lost -- and on this archive
    they read 1428 and 21924, which says the stage is failing without saying
    how.  One reason turned out to be almost all of it, and finding that took
    replaying the annotation cache offline against episodes rebuilt by hand.
    A histogram is cheap, is written where every run already writes, and would
    have made the same finding readable from ``varyz model status``.

    Counts against a closed vocabulary of this module's own quarantine codes.
    No candidate text, no episode id, nothing an annotator wrote.
    """

    counted = Counter(item.code.value for item in result.quarantines)
    return dict(counted.most_common())


def build_personal_model_compiler(
    conn: sqlite3.Connection,
    _cfg: Any | None = None,
    *,
    annotator: SemanticAnnotator | None = None,
    candidate_adapter: CandidateAdapter | None = None,
    synthesis_adapter: SynthesisAdapter | None = None,
    validation_policy: ValidationPolicy | None = None,
    policy: CompilerPolicy | None = None,
) -> PersonalModelCompiler:
    """Compose the local SQLite compiler used by CLI/UI runtimes.

    Without configured semantic adapters this is an honest literal-only build:
    it compiles explicit allowlisted graph claims and marks semantic stages stale.
    It never falls back to a cloud provider.
    """

    selected_policy = policy or CompilerPolicy(
        require_observation_annotator=annotator is not None,
        allow_literal_only_activation=True,
    )
    repository = PersonalModelRepository(conn)
    return PersonalModelCompiler(
        graph_reader=SQLiteActiveGraphReader(conn),
        evidence_reader=SQLiteEvidenceReader(
            conn,
            excluded_sources=tuple(getattr(getattr(_cfg, "graph", None), "excluded_sources", ())),
        ),
        repository=repository,
        state_store=SQLiteCompilerStateStore(conn),
        annotator=annotator,
        candidate_adapter=candidate_adapter,
        synthesis_adapter=synthesis_adapter,
        validation_policy=validation_policy,
        policy=selected_policy,
        data_dir=getattr(_cfg, "data_dir", None),
    )


def build_correction_application_service(
    conn: sqlite3.Connection,
    cfg: Any | None = None,
    *,
    provider_builder: Any | None = None,
    **compiler_options: Any,
) -> CorrectionApplicationService:
    if cfg is None:
        compiler = build_personal_model_compiler(conn, cfg, **compiler_options)
    else:
        from varyz.personal_model.semantic_runtime import build_local_semantic_compiler

        compiler = build_local_semantic_compiler(
            conn,
            cfg,
            provider_builder=provider_builder,
            **compiler_options,
        ).compiler
    return CorrectionApplicationService(
        repository=compiler.repository,
        compiler=compiler,
    )


__all__ = [
    "ActiveGraphReader",
    "CandidateAdapter",
    "CheckpointState",
    "CompileRequest",
    "CompileResult",
    "CorrectionApplicationResult",
    "CorrectionApplicationService",
    "CorrectionDisposition",
    "CompilerCandidateBatch",
    "CompilerPolicy",
    "CompilerStage",
    "CompilerStateStore",
    "DeterministicSourceIdentityAdapter",
    "EmptyDeterministicCandidateAdapter",
    "EvidenceReader",
    "ExecutionLocality",
    "ExplicitClaimProjection",
    "GraphSnapshot",
    "ModelRepository",
    "PersonalModelCompiler",
    "PersonalModelCompileAlreadyRunning",
    "PersonalModelCompileClaim",
    "PinnedGraphCandidateAdapter",
    "OfferedQuestion",
    "SQLiteActiveGraphReader",
    "SQLiteCompilerStateStore",
    "SQLiteEvidenceReader",
    "SynthesisAdapter",
    "build_correction_application_service",
    "build_personal_model_compiler",
    "incremental_target_entry_ids",
    "personal_model_compile_claim_path",
    "read_personal_model_compile_claim",
]

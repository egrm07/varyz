"""Generational persistence and durable corrections for the personal model.

The repository is the write boundary between compiler candidates and the model
read by Mirror/MCP. Records are validated against the pure domain contracts,
staged as canonical JSON, integrity-checked, and activated with one pointer swap.
Activated snapshots and owner overrides are append-only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from varyz.domain import ConversationMetadata, iso, new_id, utc_now
from varyz.personal_model.models import (
    ConversationEpisode,
    DerivedRecord,
    Event,
    Fact,
    FactValue,
    Goal,
    GoalKind,
    Interpretation,
    InterpretationCandidate,
    InterpretationVerdict,
    Observation,
    Person,
    Preference,
    Project,
    RelationshipSnapshot,
)


class GenerationState(StrEnum):
    STAGED = "staged"
    CURRENT = "current"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class OverrideTargetKind(StrEnum):
    PERSON = "person"
    PROJECT = "project"
    FACT = "fact"
    PREFERENCE = "preference"
    GOAL = "goal"
    INTERPRETATION = "interpretation"


class OverrideAction(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    CONFIRM = "confirm"
    EDIT = "edit"
    REJECT = "reject"
    SUPERSEDE = "supersede"
    REPARENT = "reparent"
    RECLASSIFY = "reclassify"
    DISPUTE = "dispute"
    DELETE = "delete"
    RESTORE = "restore"
    VERDICT = "verdict"


class ReplayOutcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLICABLE = "not_applicable"
    ORPHANED = "orphaned"
    CONFLICT = "conflict"


class ExternalDependencyKind(StrEnum):
    ENTRY = "entry"
    GRAPH_CLAIM = "graph_claim"
    COMPILER_INPUT = "compiler_input"


class OverridePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MergePayload(OverridePayload):
    into_id: str = Field(min_length=1)

    @field_validator("into_id")
    @classmethod
    def into_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("merge target must not be blank")
        return value


class SplitPayload(OverridePayload):
    resulting_record_ids: tuple[str, ...] = Field(min_length=2)
    # Durable source/candidate memberships make a split replayable after the
    # generated record ids change. Older ledger rows remain readable but a
    # compiler must treat a split without groups as a review conflict.
    member_groups: tuple[tuple[str, ...], ...] | None = None

    @model_validator(mode="after")
    def unique_results(self) -> SplitPayload:
        if any(not item.strip() for item in self.resulting_record_ids):
            raise ValueError("split result ids must be non-blank")
        if len(set(self.resulting_record_ids)) != len(self.resulting_record_ids):
            raise ValueError("split result ids must be unique")
        if self.member_groups is not None:
            if len(self.member_groups) < 2 or any(not group for group in self.member_groups):
                raise ValueError("split member_groups require at least two non-empty groups")
            members = tuple(item for group in self.member_groups for item in group)
            if any(not item.strip() for item in members):
                raise ValueError("split member keys must be non-blank")
            if len(set(members)) != len(members):
                raise ValueError("split member keys must be unique")
        return self


class FactValuePayload(OverridePayload):
    value: FactValue


class PreferenceValuePayload(OverridePayload):
    object: FactValue


class InterpretationEditPayload(OverridePayload):
    thesis: str = Field(min_length=1)
    derivation_summary: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("thesis", "derivation_summary")
    @classmethod
    def interpretation_text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("interpretation edit text must not be blank")
        return value


class InterpretationVerdictPayload(OverridePayload):
    verdict: InterpretationVerdict
    kept_part: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def partial_names_exact_kept_part(self) -> InterpretationVerdictPayload:
        if self.verdict == InterpretationVerdict.PARTIAL:
            if self.kept_part is None or not self.kept_part.strip():
                raise ValueError("a partial verdict requires the exact kept part")
        elif self.kept_part is not None:
            raise ValueError("only a partial verdict may carry a kept part")
        return self


class ReasonPayload(OverridePayload):
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("override reason must not be blank")
        return value


class SupersedePayload(OverridePayload):
    replacement_record_id: str = Field(min_length=1)

    @field_validator("replacement_record_id")
    @classmethod
    def replacement_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("replacement record id must not be blank")
        return value


class ReparentPayload(OverridePayload):
    parent_goal_ids: tuple[str, ...]

    @field_validator("parent_goal_ids")
    @classmethod
    def unique_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("parent goal ids must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError("parent goal ids must be unique")
        return value


class ReclassifyPayload(OverridePayload):
    kind: GoalKind


OVERRIDE_PAYLOAD_TYPES: dict[
    tuple[OverrideTargetKind, OverrideAction], type[OverridePayload]
] = {
    (OverrideTargetKind.PERSON, OverrideAction.MERGE): MergePayload,
    (OverrideTargetKind.PERSON, OverrideAction.SPLIT): SplitPayload,
    (OverrideTargetKind.PROJECT, OverrideAction.MERGE): MergePayload,
    (OverrideTargetKind.PROJECT, OverrideAction.SPLIT): SplitPayload,
    (OverrideTargetKind.FACT, OverrideAction.CONFIRM): FactValuePayload,
    (OverrideTargetKind.FACT, OverrideAction.EDIT): FactValuePayload,
    (OverrideTargetKind.FACT, OverrideAction.REJECT): ReasonPayload,
    (OverrideTargetKind.FACT, OverrideAction.SUPERSEDE): SupersedePayload,
    (OverrideTargetKind.PREFERENCE, OverrideAction.CONFIRM): PreferenceValuePayload,
    (OverrideTargetKind.PREFERENCE, OverrideAction.EDIT): PreferenceValuePayload,
    (OverrideTargetKind.PREFERENCE, OverrideAction.REJECT): ReasonPayload,
    (OverrideTargetKind.PREFERENCE, OverrideAction.SUPERSEDE): SupersedePayload,
    (OverrideTargetKind.GOAL, OverrideAction.REPARENT): ReparentPayload,
    (OverrideTargetKind.GOAL, OverrideAction.RECLASSIFY): ReclassifyPayload,
    (OverrideTargetKind.INTERPRETATION, OverrideAction.DISPUTE): ReasonPayload,
    (OverrideTargetKind.INTERPRETATION, OverrideAction.EDIT): InterpretationEditPayload,
    (OverrideTargetKind.INTERPRETATION, OverrideAction.DELETE): ReasonPayload,
    (OverrideTargetKind.INTERPRETATION, OverrideAction.RESTORE): ReasonPayload,
    (OverrideTargetKind.INTERPRETATION, OverrideAction.VERDICT): InterpretationVerdictPayload,
}


@dataclass(frozen=True)
class ModelDependency:
    """One stable compiler input.

    ``model_record`` distinguishes dependencies that must resolve to a record of
    ``kind`` in the same target generation from graph/config/evidence inputs.
    """

    kind: str
    id: str
    hash: str
    model_record: bool = False

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.id.strip() or not self.hash.strip():
            raise ValueError("dependency kind, id, and hash must be non-blank")
        if self.model_record and self.kind not in RECORD_TYPES:
            raise ValueError(f"unknown personal-model dependency kind: {self.kind}")
        if not self.model_record and self.kind not in ExternalDependencyKind:
            raise ValueError(f"unknown external dependency kind: {self.kind}")


@dataclass(frozen=True)
class ModelGeneration:
    id: str
    graph_generation_id: str
    compiler_version: str
    policy_version: str
    input_content_hash: str
    state: GenerationState
    override_sequence_max: int
    created_at: str
    completed_at: str | None
    activated_at: str | None
    failed_at: str | None
    failure_reason: str | None
    record_count: int
    reused_record_count: int
    quarantine_count: int
    metrics: dict[str, Any]


@dataclass(frozen=True)
class ModelOverride:
    sequence: int
    id: str
    target_kind: OverrideTargetKind
    target_record_id: str
    action: OverrideAction
    payload: dict[str, Any]
    struck_value: Any | None
    supersedes_override_id: str | None
    actor: str
    created_at: str


type PersonalModelRecord = (
    ConversationEpisode
    | Observation
    | Person
    | Project
    | Goal
    | Fact
    | Preference
    | Event
    | RelationshipSnapshot
    | InterpretationCandidate
    | Interpretation
)

RECORD_TYPES: dict[str, type[DerivedRecord]] = {
    "conversation_episode": ConversationEpisode,
    "observation": Observation,
    "person": Person,
    "project": Project,
    "goal": Goal,
    "fact": Fact,
    "preference": Preference,
    "event": Event,
    "relationship_snapshot": RelationshipSnapshot,
    "interpretation_candidate": InterpretationCandidate,
    "interpretation": Interpretation,
}
TYPE_TO_RECORD_KIND = {model: kind for kind, model in RECORD_TYPES.items()}

ALLOWED_OVERRIDE_ACTIONS: dict[OverrideTargetKind, frozenset[OverrideAction]] = {
    OverrideTargetKind.PERSON: frozenset({OverrideAction.MERGE, OverrideAction.SPLIT}),
    OverrideTargetKind.PROJECT: frozenset({OverrideAction.MERGE, OverrideAction.SPLIT}),
    OverrideTargetKind.FACT: frozenset(
        {
            OverrideAction.CONFIRM,
            OverrideAction.EDIT,
            OverrideAction.REJECT,
            OverrideAction.SUPERSEDE,
        }
    ),
    OverrideTargetKind.PREFERENCE: frozenset(
        {
            OverrideAction.CONFIRM,
            OverrideAction.EDIT,
            OverrideAction.REJECT,
            OverrideAction.SUPERSEDE,
        }
    ),
    OverrideTargetKind.GOAL: frozenset(
        {OverrideAction.REPARENT, OverrideAction.RECLASSIFY}
    ),
    OverrideTargetKind.INTERPRETATION: frozenset(
        {
            OverrideAction.DISPUTE,
            OverrideAction.EDIT,
            OverrideAction.DELETE,
            OverrideAction.RESTORE,
            OverrideAction.VERDICT,
        }
    ),
}


#: How many ids one ``IN (...)`` may name. SQLite caps a statement's host
#: parameters, and activation walks a whole generation: the integrity check
#: asked after every entry its 125,967 records cite at once -- 45,601 of them
#: -- and the pointer swap died on "too many SQL variables" every time,
#: after the compile had done all eight hours of its work. Nothing read the
#: failure, because the activate checkpoint is written before the swap and
#: said "complete" regardless.
_IN_CHUNK = 400
_DISCARD_RECORD_ROWID_SPAN = 1_000


def in_chunks(values: set[str]) -> list[tuple[str, ...]]:
    """Split ids into groups SQLite will bind in one `IN` clause.

    Public because the Mirror projection reads the same tables under the same
    ceiling, and two chunking rules in one codebase is one too many.
    """
    ordered = sorted(values)
    return [
        tuple(ordered[start : start + _IN_CHUNK])
        for start in range(0, len(ordered), _IN_CHUNK)
    ]


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal, StrEnum)):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used for hashes and storage."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def entry_dependency(conn: sqlite3.Connection, entry_id: str) -> ModelDependency:
    """Bind a record to every stored entry projection the personal model consumes.

    ``entries.content_hash`` intentionally covers only title/body for ingestion
    idempotency.  Episode reconstruction also depends on source identity,
    conversation structure, ordering, attribution, time, and sensitivity.  Keep
    those semantics separate: a metadata-only connector backfill remains an
    unchanged ingest while still invalidating stale derived records.
    """
    row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        raise KeyError(f"evidence entry does not exist: {entry_id}")
    try:
        conversation = ConversationMetadata.model_validate_json(
            row["conversation_metadata_json"] or "{}"
        ).model_dump(mode="json")
    except (ValueError, TypeError):
        # Match dao.row_to_entry: corrupt optional metadata is read as unknown.
        conversation = ConversationMetadata().model_dump(mode="json")
    material = {
        "kind": row["kind"],
        "source": row["source"],
        "source_account": row["source_account"],
        "external_id": row["external_id"],
        "content_hash": row["content_hash"],
        "title": row["title"],
        "body": row["body"],
        "occurred_start": row["occurred_start"],
        "occurred_end": row["occurred_end"],
        "tz": row["tz"],
        "time_precision": row["time_precision"],
        "captured_at": row["captured_at"],
        "thread_id": row["thread_id"],
        "conversation": conversation,
        "authored_by_user": bool(row["authored_by_user"]),
        "sensitivity": row["sensitivity"],
        "significance": row["significance"],
    }
    return ModelDependency(
        kind=ExternalDependencyKind.ENTRY,
        id=entry_id,
        hash=content_hash(material),
    )


def graph_claim_dependency(
    conn: sqlite3.Connection, graph_generation_id: str, claim_id: str
) -> ModelDependency:
    """Bind a record to the semantic content of a claim in its pinned graph."""
    row = conn.execute(
        "SELECT * FROM claims WHERE id=? AND graph_generation_id=?",
        (claim_id, graph_generation_id),
    ).fetchone()
    if row is None:
        raise KeyError(
            f"claim {claim_id} does not exist in graph generation {graph_generation_id}"
        )
    column_names = tuple(row.keys())
    material = {
        key: row[key]
        for key in column_names
        if key not in {"created_at", "updated_at"}
    }
    return ModelDependency(
        kind=ExternalDependencyKind.GRAPH_CLAIM,
        id=claim_id,
        hash=content_hash(material),
    )


def compiler_input_dependency(name: str, value: Any) -> ModelDependency:
    if not name.strip():
        raise ValueError("compiler input name must be non-blank")
    return ModelDependency(
        kind=ExternalDependencyKind.COMPILER_INPUT,
        id=name,
        hash=content_hash(value),
    )


def model_record_dependency(
    kind: str, record_id: str, dependency_hash: str
) -> ModelDependency:
    return ModelDependency(
        kind=kind,
        id=record_id,
        hash=dependency_hash,
        model_record=True,
    )


def required_model_references(record: DerivedRecord) -> set[tuple[str, str]]:
    """Every other record this one names, by kind and id.

    This is the one definition of that set.  It used to exist twice -- the
    compiler decided which model dependencies to write, the repository decided
    which ones activation required -- and the two lists were not the same.  A
    project or a goal carries evidence naming episodes and observations,
    activation demanded a dependency row for each, and the compiler wrote none,
    so a generation that found any project died at the last stage after the
    whole archive had been read: ``record ... lacks model dependency
    membership``.  Nothing caught it because no generation had ever reached
    that stage with a project in it.

    Mutual references are the one thing it leaves out.  A contradiction is
    symmetric -- each of the two facts names the other -- so it cannot be a
    dependency of either without making each record's hash depend on itself.
    ``_validate_domain_references`` requires those targets to exist all the
    same.
    """

    references: set[tuple[str, str]] = set()

    def add(kind: str, record_id: str | None) -> None:
        if record_id is not None:
            references.add((kind, record_id))

    for evidence in record.evidence:
        # Episode evidence names its containing episode for provenance.  That
        # self-link is not a cross-record dependency (and cannot carry a
        # non-circular dependency hash).
        if not (
            isinstance(record, ConversationEpisode)
            and evidence.episode_id == record.id
        ):
            add("conversation_episode", evidence.episode_id)
        add("observation", evidence.observation_id)
    if isinstance(record, ConversationEpisode):
        for record_id in record.participant_person_ids:
            add("person", record_id)
    elif isinstance(record, Observation):
        add("conversation_episode", record.episode_id)
        add("person", record.speaker_person_id)
        if record.subject_kind is not None and str(record.subject_kind) in RECORD_TYPES:
            add(str(record.subject_kind), record.subject_id)
        for record_id in record.addressee_person_ids:
            add("person", record_id)
    elif isinstance(record, Person):
        add("person", record.merged_into_person_id)
    elif isinstance(record, Project):
        add("project", record.merged_into_project_id)
        for record_id in record.participant_person_ids:
            add("person", record_id)
    elif isinstance(record, Goal):
        for record_id in record.project_ids:
            add("project", record_id)
        for record_id in (*record.parent_goal_ids, *record.dependency_goal_ids):
            add("goal", record_id)
    elif isinstance(record, Fact):
        if str(record.subject.kind) in RECORD_TYPES:
            add(str(record.subject.kind), record.subject.id)
        add("person", record.asserted_by_person_id)
        # Supersession points backwards in time and is a dependency.  A
        # contradiction is not: two facts that disagree each name the other,
        # so neither can carry the other's dependency hash -- the same reason
        # an episode's self-link is left out above.  Their existence is still
        # required, by _validate_domain_references, which checks presence and
        # kind without needing a hash.
        for record_id in record.supersedes_fact_ids:
            add("fact", record_id)
    elif isinstance(record, Preference):
        if str(record.subject.kind) in RECORD_TYPES:
            add(str(record.subject.kind), record.subject.id)
        add("person", record.asserted_by_person_id)
        for record_id in record.supersedes_preference_ids:
            add("preference", record_id)
    elif isinstance(record, Event):
        for record_id in record.participant_ids:
            add("person", record_id)
        for record_id in record.project_ids:
            add("project", record_id)
    elif isinstance(record, RelationshipSnapshot):
        add("person", record.archive_owner_person_id)
        add("person", record.other_person_id)
        for record_id in record.shared_event_ids:
            add("event", record_id)
        for record_id in record.commitment_observation_ids:
            add("observation", record_id)
    elif isinstance(record, (InterpretationCandidate, Interpretation)):
        add("project", record.scope.project_id)
        if record.scope.relationship_person_ids is not None:
            for record_id in record.scope.relationship_person_ids:
                add("person", record_id)
        for assignment in record.evidence_assignments:
            add("conversation_episode", assignment.episode_id)
            for record_id in assignment.observation_ids:
                add("observation", record_id)
        if isinstance(record, Interpretation):
            add("interpretation_candidate", record.candidate_id)
    return references


def required_record_dependencies(
    conn: sqlite3.Connection,
    graph_generation_id: str,
    record: DerivedRecord,
) -> tuple[ModelDependency, ...]:
    """Construct the non-negotiable evidence/claim dependency set for a record."""
    entry_membership = {reference.entry_id for reference in record.evidence}
    if isinstance(record, Observation):
        entry_membership.update(span.entry_id for span in record.spans)
    entry_ids = sorted(entry_membership)
    claim_ids = sorted(
        {
            *record.claim_ids,
            *(reference.claim_id for reference in record.evidence if reference.claim_id),
        }
    )
    dependencies = [entry_dependency(conn, entry_id) for entry_id in entry_ids]
    dependencies.extend(
        graph_claim_dependency(conn, graph_generation_id, claim_id)
        for claim_id in claim_ids
    )
    return tuple(dependencies)


def record_kind(record: DerivedRecord) -> str:
    try:
        return TYPE_TO_RECORD_KIND[type(record)]
    except KeyError as exc:
        raise TypeError(f"unsupported personal-model record: {type(record).__name__}") from exc


def compute_dependency_hash(
    record: DerivedRecord,
    dependencies: tuple[ModelDependency, ...] = (),
    *,
    compiler_version: str,
    policy_version: str,
) -> str:
    """Hash only stable inputs, never generation id or display prose."""
    duplicate_keys = [(item.kind, item.id) for item in dependencies]
    if len(set(duplicate_keys)) != len(duplicate_keys):
        raise ValueError("record dependencies must be unique by kind and id")
    material = {
        "record_kind": record_kind(record),
        "record_id": record.id,
        "evidence": [item.model_dump(mode="json") for item in record.evidence],
        "claim_ids": sorted(record.claim_ids),
        "dependencies": [
            {
                "kind": item.kind,
                "id": item.id,
                "hash": item.hash,
                "model_record": item.model_record,
            }
            for item in sorted(dependencies, key=lambda item: (item.kind, item.id))
        ],
        "producer": record.producer.model_dump(mode="json"),
        "compiler_version": compiler_version,
        "policy_version": policy_version,
    }
    return content_hash(material)


def _row_to_generation(row: sqlite3.Row) -> ModelGeneration:
    return ModelGeneration(
        id=row["id"],
        graph_generation_id=row["graph_generation_id"],
        compiler_version=row["compiler_version"],
        policy_version=row["policy_version"],
        input_content_hash=row["input_content_hash"],
        state=GenerationState(row["state"]),
        override_sequence_max=row["override_sequence_max"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        activated_at=row["activated_at"],
        failed_at=row["failed_at"],
        failure_reason=row["failure_reason"],
        record_count=row["record_count"],
        reused_record_count=row["reused_record_count"],
        quarantine_count=row["quarantine_count"],
        metrics=json.loads(row["metrics_json"]),
    )


def _row_to_override(row: sqlite3.Row) -> ModelOverride:
    return ModelOverride(
        sequence=row["sequence"],
        id=row["id"],
        target_kind=OverrideTargetKind(row["target_kind"]),
        target_record_id=row["target_record_id"],
        action=OverrideAction(row["action"]),
        payload=json.loads(row["payload_json"]),
        struck_value=(
            json.loads(row["struck_value_json"])
            if row["struck_value_json"] is not None
            else None
        ),
        supersedes_override_id=row["supersedes_override_id"],
        actor=row["actor"],
        created_at=row["created_at"],
    )


class _JoinedTransaction:
    """The repository's connection while a batch is open: its writes join the batch.

    Every write method commits with ``with self.conn``. Through this the
    ``with`` neither commits nor rolls back; the batch does both, once.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, *exc_info: object) -> None:
        return None


class PersonalModelRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @contextmanager
    def batched(self) -> Iterator[None]:
        """Commit the writes made inside as one transaction, or roll them all back.

        A compile stages tens of thousands of records. One commit each made
        the disk sync that many times, which on a laptop was most of the
        compile and most of its battery.
        """
        if isinstance(self.conn, _JoinedTransaction):
            yield
            return
        outer = self.conn
        self.conn = _JoinedTransaction(outer)  # type: ignore[assignment]
        try:
            with outer:
                yield
        finally:
            self.conn = outer

    def create_generation(
        self,
        graph_generation_id: str,
        *,
        compiler_version: str,
        policy_version: str,
        input_content_hash: str,
        generation_id: str | None = None,
    ) -> ModelGeneration:
        for label, value in (
            ("graph_generation_id", graph_generation_id),
            ("compiler_version", compiler_version),
            ("policy_version", policy_version),
            ("input_content_hash", input_content_hash),
        ):
            if not value.strip():
                raise ValueError(f"{label} must be non-blank")
        graph = self.conn.execute(
            "SELECT state FROM graph_runs WHERE id=?", (graph_generation_id,)
        ).fetchone()
        if graph is None:
            raise KeyError(f"graph generation does not exist: {graph_generation_id}")
        if graph["state"] not in {"active", "superseded"}:
            raise ValueError("personal model requires an activated graph generation")
        override_max = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM personal_model_overrides"
        ).fetchone()[0]
        model_generation_id = generation_id or new_id()
        now = iso(utc_now())
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_generations
                   (id, graph_generation_id, compiler_version, policy_version,
                    input_content_hash, state, override_sequence_max, created_at)
                   VALUES (?,?,?,?,?,'staged',?,?)""",
                (
                    model_generation_id,
                    graph_generation_id,
                    compiler_version,
                    policy_version,
                    input_content_hash,
                    override_max,
                    now,
                ),
            )
        return self.get_generation(model_generation_id)

    def get_generation(self, generation_id: str) -> ModelGeneration:
        row = self.conn.execute(
            "SELECT * FROM personal_model_generations WHERE id=?", (generation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(generation_id)
        return _row_to_generation(row)

    def _generation_header(self, generation_id: str) -> ModelGeneration:
        """Read current guard/hash inputs without loading the diagnostic payload.

        A reused record checks its source and target generations, and staging
        checks the target again. Loading their megabyte-sized metrics on every
        record made diagnostics part of the compile's inner loop. Keep these
        reads fresh for state transitions; only the unused metrics are omitted.
        User-facing reads still go through get_generation and include them.
        """
        row = self.conn.execute(
            """SELECT id, graph_generation_id, compiler_version, policy_version,
                      input_content_hash, state, override_sequence_max, created_at,
                      completed_at, activated_at, failed_at, failure_reason,
                      record_count, reused_record_count, quarantine_count,
                      '{}' AS metrics_json
               FROM personal_model_generations WHERE id=?""",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(generation_id)
        return _row_to_generation(row)

    def active_generation_id(self) -> str | None:
        row = self.conn.execute(
            "SELECT generation_id FROM personal_model_active_generation WHERE singleton=1"
        ).fetchone()
        return row["generation_id"] if row is not None else None

    def active_generation(self) -> ModelGeneration | None:
        generation_id = self.active_generation_id()
        return self.get_generation(generation_id) if generation_id is not None else None

    def stage_record(
        self,
        record: PersonalModelRecord,
        *,
        dependencies: tuple[ModelDependency, ...] = (),
        reused_from_generation_id: str | None = None,
    ) -> str:
        generation = self._require_staged(record.generation_id)
        self._require_standard_dependencies(generation, record, dependencies)
        kind = record_kind(record)
        model_type = RECORD_TYPES[kind]
        payload = record.model_dump(mode="json")
        encoded = canonical_json(payload)
        # Validate the exact representation that will be read later.
        model_type.model_validate_json(encoded, strict=True)
        json_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        dependency_hash = compute_dependency_hash(
            record,
            dependencies,
            compiler_version=generation.compiler_version,
            policy_version=generation.policy_version,
        )
        entity_kind, entity_id, predicate, concept = self._query_projection(record)
        now = iso(record.created_at)
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_records
                   (generation_id, record_id, record_kind, record_json,
                    canonical_json_hash, dependency_hash, entity_kind, entity_id,
                    normalized_predicate, normalized_concept, readiness, valid_from,
                    valid_to, sensitivity, created_at, reused_from_generation_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.generation_id,
                    record.id,
                    kind,
                    encoded,
                    json_hash,
                    dependency_hash,
                    entity_kind,
                    entity_id,
                    predicate,
                    concept,
                    str(record.readiness),
                    iso(record.temporal.valid_from),
                    iso(record.temporal.valid_to),
                    str(record.sensitivity),
                    now,
                    reused_from_generation_id,
                ),
            )
            for ordinal, reference in enumerate(record.evidence):
                self.conn.execute(
                    """INSERT INTO personal_model_record_evidence
                       (generation_id, record_id, ordinal, entry_id, role, claim_id,
                        episode_id, observation_id, independence_group_id, reference_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record.generation_id,
                        record.id,
                        ordinal,
                        reference.entry_id,
                        str(reference.role),
                        reference.claim_id,
                        reference.episode_id,
                        reference.observation_id,
                        reference.independence_group_id,
                        canonical_json(reference.model_dump(mode="json")),
                    ),
                )
            for ordinal, claim_id in enumerate(record.claim_ids):
                self.conn.execute(
                    """INSERT INTO personal_model_record_claims
                       (generation_id, record_id, ordinal, claim_id) VALUES (?,?,?,?)""",
                    (record.generation_id, record.id, ordinal, claim_id),
                )
            for ordinal, dependency in enumerate(
                sorted(dependencies, key=lambda item: (item.kind, item.id))
            ):
                self.conn.execute(
                    """INSERT INTO personal_model_record_dependencies
                       (generation_id, record_id, ordinal, dependency_kind,
                        dependency_id, dependency_hash, target_generation_id)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        record.generation_id,
                        record.id,
                        ordinal,
                        dependency.kind,
                        dependency.id,
                        dependency.hash,
                        record.generation_id if dependency.model_record else None,
                    ),
                )
            self.conn.execute(
                """UPDATE personal_model_generations
                   SET record_count=record_count+1,
                       reused_record_count=reused_record_count+?
                   WHERE id=?""",
                (int(reused_from_generation_id is not None), record.generation_id),
            )
        return dependency_hash

    def get_record(
        self, record_id: str, *, generation_id: str | None = None
    ) -> PersonalModelRecord:
        selected_generation = generation_id or self.active_generation_id()
        if selected_generation is None:
            raise LookupError("no active personal-model generation")
        row = self.conn.execute(
            """SELECT record_kind, record_json, canonical_json_hash
               FROM personal_model_records WHERE generation_id=? AND record_id=?""",
            (selected_generation, record_id),
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        actual_hash = hashlib.sha256(row["record_json"].encode("utf-8")).hexdigest()
        if actual_hash != row["canonical_json_hash"]:
            raise RuntimeError(f"stored personal-model record failed hash check: {record_id}")
        model_type = RECORD_TYPES[row["record_kind"]]
        return model_type.model_validate_json(row["record_json"], strict=True)

    def list_records(
        self,
        *,
        generation_id: str | None = None,
        kind: str | None = None,
        readiness: str | None = None,
    ) -> list[PersonalModelRecord]:
        selected_generation = generation_id or self.active_generation_id()
        if selected_generation is None:
            return []
        where = ["generation_id=?"]
        params: list[Any] = [selected_generation]
        if kind is not None:
            if kind not in RECORD_TYPES:
                raise ValueError(f"unknown personal-model record kind: {kind}")
            where.append("record_kind=?")
            params.append(kind)
        if readiness is not None:
            where.append("readiness=?")
            params.append(readiness)
        rows = self.conn.execute(
            f"""SELECT record_id FROM personal_model_records
                WHERE {' AND '.join(where)} ORDER BY record_kind, record_id""",
            params,
        ).fetchall()
        return [
            self.get_record(row["record_id"], generation_id=selected_generation)
            for row in rows
        ]

    def read_records(
        self,
        kinds: Collection[str] = (),
        *,
        generation_id: str | None = None,
        readiness: Collection[str] | None = None,
    ) -> list[tuple[str, str, DerivedRecord]]:
        """Every record of the named kinds, read in one sweep.

        `get_record` and `list_records` answer for one record at a time, which
        is the right shape for a lookup and the wrong one for a projection.
        The Mirror reads five of the eleven kinds and reads them all at once,
        and a point query per record made building the page one round trip for
        every canonical record in the generation -- a hundred thousand of them
        on a database measured in tens of gigabytes, to show a few hundred.
        Same hash check and same strict parse; one scan, and only the kinds
        the caller asked for. An empty `kinds` means every kind.
        """
        selected_generation = generation_id or self.active_generation_id()
        if selected_generation is None:
            return []
        unknown = sorted(set(kinds) - set(RECORD_TYPES))
        if unknown:
            raise ValueError(f"unknown personal-model record kind: {unknown[0]}")
        where = ["generation_id=?"]
        params: list[Any] = [selected_generation]
        if kinds:
            wanted = sorted(set(kinds))
            where.append(f"record_kind IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
        if readiness is not None:
            allowed = sorted(set(readiness))
            if not allowed:
                return []
            where.append(f"readiness IN ({','.join('?' * len(allowed))})")
            params.extend(allowed)
        rows = self.conn.execute(
            f"""SELECT record_id, record_kind, record_json, canonical_json_hash
                FROM personal_model_records
                WHERE {' AND '.join(where)} ORDER BY record_kind, record_id""",
            params,
        ).fetchall()
        records: list[tuple[str, str, DerivedRecord]] = []
        for row in rows:
            actual_hash = hashlib.sha256(
                row["record_json"].encode("utf-8")
            ).hexdigest()
            if actual_hash != row["canonical_json_hash"]:
                raise RuntimeError(
                    "stored personal-model record failed hash check: "
                    f"{row['record_id']}"
                )
            model_type = RECORD_TYPES[row["record_kind"]]
            records.append(
                (
                    str(row["record_id"]),
                    str(row["record_kind"]),
                    model_type.model_validate_json(row["record_json"], strict=True),
                )
            )
        return records

    def clone_record_if_unchanged(
        self,
        *,
        source_generation_id: str,
        target_generation_id: str,
        record_id: str,
        expected_dependency_hash: str,
    ) -> bool:
        if not expected_dependency_hash.strip():
            raise ValueError("expected dependency hash must be non-blank")
        source = self._generation_header(source_generation_id)
        if source.state not in {GenerationState.CURRENT, GenerationState.SUPERSEDED}:
            raise ValueError("records may be reused only from an activated generation")
        target = self._require_staged(target_generation_id)
        row = self.conn.execute(
            """SELECT record_kind, record_json, dependency_hash
               FROM personal_model_records WHERE generation_id=? AND record_id=?""",
            (source_generation_id, record_id),
        ).fetchone()
        if row is None:
            return False
        dependency_rows = self.conn.execute(
            """SELECT dependency_kind, dependency_id, dependency_hash,
                      target_generation_id
               FROM personal_model_record_dependencies
               WHERE generation_id=? AND record_id=? ORDER BY ordinal""",
            (source_generation_id, record_id),
        ).fetchall()
        dependencies = tuple(
            ModelDependency(
                kind=item["dependency_kind"],
                id=item["dependency_id"],
                hash=item["dependency_hash"],
                model_record=item["target_generation_id"] is not None,
            )
            for item in dependency_rows
        )
        model_type = RECORD_TYPES[row["record_kind"]]
        source_record = model_type.model_validate_json(row["record_json"], strict=True)
        payload = source_record.model_dump(mode="python")
        payload["generation_id"] = target_generation_id
        cloned = model_type.model_validate(payload, strict=True)
        if not self._standard_dependencies_match(target, cloned, dependencies):
            return False
        for dependency in dependencies:
            if not dependency.model_record:
                continue
            current = self.conn.execute(
                """SELECT record_kind, dependency_hash FROM personal_model_records
                   WHERE generation_id=? AND record_id=?""",
                (target_generation_id, dependency.id),
            ).fetchone()
            if (
                current is None
                or current["record_kind"] != dependency.kind
                or current["dependency_hash"] != dependency.hash
            ):
                return False
        target_hash = compute_dependency_hash(
            cloned,
            dependencies,
            compiler_version=target.compiler_version,
            policy_version=target.policy_version,
        )
        if row["dependency_hash"] != target_hash:
            return False
        if target_hash != expected_dependency_hash:
            return False
        self.stage_record(
            cloned,
            dependencies=dependencies,
            reused_from_generation_id=source_generation_id,
        )
        return True

    def quarantine(
        self,
        generation_id: str,
        *,
        stage: str,
        reason_code: str,
        record_kind: str | None = None,
        candidate_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        self._require_staged(generation_id)
        if not stage.strip() or not reason_code.strip():
            raise ValueError("quarantine stage and reason must be non-blank")
        if record_kind is not None and record_kind not in RECORD_TYPES:
            raise ValueError(f"unknown personal-model record kind: {record_kind}")
        quarantine_id = new_id()
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_quarantines
                   (id, generation_id, stage, record_kind, candidate_id,
                    reason_code, detail_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    quarantine_id,
                    generation_id,
                    stage,
                    record_kind,
                    candidate_id,
                    reason_code,
                    canonical_json(detail or {}),
                    iso(utc_now()),
                ),
            )
            self.conn.execute(
                """UPDATE personal_model_generations
                   SET quarantine_count=quarantine_count+1 WHERE id=?""",
                (generation_id,),
            )
        return quarantine_id

    def record_metric(
        self, generation_id: str, *, stage: str, name: str, value: Any
    ) -> None:
        self._require_staged(generation_id)
        if not stage.strip() or not name.strip():
            raise ValueError("metric stage and name must be non-blank")
        encoded = canonical_json(value)
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_run_metrics
                   (generation_id, stage, metric_name, metric_json, recorded_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(generation_id, stage, metric_name) DO UPDATE SET
                     metric_json=excluded.metric_json,
                     recorded_at=excluded.recorded_at""",
                (generation_id, stage, name, encoded, iso(utc_now())),
            )
            self._refresh_metrics_json(generation_id)

    def append_override(
        self,
        *,
        target_kind: OverrideTargetKind | str,
        target_record_id: str,
        action: OverrideAction | str,
        payload: dict[str, Any] | None = None,
        struck_value: Any | None = None,
        supersedes_override_id: str | None = None,
        actor: str = "owner",
        override_id: str | None = None,
    ) -> ModelOverride:
        kind = OverrideTargetKind(target_kind)
        selected_action = OverrideAction(action)
        if selected_action not in ALLOWED_OVERRIDE_ACTIONS[kind]:
            raise ValueError(f"{selected_action} is not valid for {kind}")
        if not target_record_id.strip() or not actor.strip():
            raise ValueError("override target and actor must be non-blank")
        if not isinstance(payload, dict):
            raise TypeError("override payload must be an object")
        payload_type = OVERRIDE_PAYLOAD_TYPES[(kind, selected_action)]
        validated_payload = payload_type.model_validate_json(
            canonical_json(payload), strict=True
        )
        override_payload = validated_payload.model_dump(mode="json")
        if isinstance(validated_payload, MergePayload) and (
            validated_payload.into_id == target_record_id
        ):
            raise ValueError("an identity cannot merge into itself")
        if isinstance(validated_payload, SplitPayload) and (
            target_record_id in validated_payload.resulting_record_ids
        ):
            raise ValueError("a split result must use new stable record ids")
        if kind == OverrideTargetKind.INTERPRETATION and selected_action == OverrideAction.VERDICT:
            prior_verdict = self.conn.execute(
                """SELECT id, struck_value_json FROM personal_model_overrides prior
                   WHERE target_kind='interpretation' AND target_record_id=?
                     AND action='verdict'
                     AND NOT EXISTS (
                       SELECT 1 FROM personal_model_overrides later
                       WHERE later.supersedes_override_id=prior.id)
                   ORDER BY sequence DESC LIMIT 1""",
                (target_record_id,),
            ).fetchone()
            if supersedes_override_id is None and prior_verdict is not None:
                supersedes_override_id = str(prior_verdict["id"])
                if prior_verdict["struck_value_json"] is not None:
                    struck_value = json.loads(prior_verdict["struck_value_json"])
            if not isinstance(struck_value, dict) or not isinstance(
                struck_value.get("thesis"), str
            ):
                raise ValueError("an interpretation verdict requires the struck thesis")
        if supersedes_override_id is not None:
            prior = self.conn.execute(
                """SELECT target_kind, target_record_id
                   FROM personal_model_overrides WHERE id=?""",
                (supersedes_override_id,),
            ).fetchone()
            if prior is None:
                raise KeyError(supersedes_override_id)
            if (prior["target_kind"], prior["target_record_id"]) != (
                str(kind),
                target_record_id,
            ):
                raise ValueError("an override may supersede only the same stable target")
        new_override_id = override_id or new_id()
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_overrides
                   (id, target_kind, target_record_id, action, payload_json,
                    struck_value_json, supersedes_override_id, actor, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    new_override_id,
                    str(kind),
                    target_record_id,
                    str(selected_action),
                    canonical_json(override_payload),
                    canonical_json(struck_value) if struck_value is not None else None,
                    supersedes_override_id,
                    actor,
                    iso(utc_now()),
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM personal_model_overrides WHERE id=?", (new_override_id,)
        ).fetchone()
        return _row_to_override(row)

    def overrides_for_generation(self, generation_id: str) -> list[ModelOverride]:
        generation = self.get_generation(generation_id)
        rows = self.conn.execute(
            """SELECT * FROM personal_model_overrides
               WHERE sequence <= ? ORDER BY sequence""",
            (generation.override_sequence_max,),
        ).fetchall()
        return [_row_to_override(row) for row in rows]

    def record_override_replay(
        self,
        generation_id: str,
        override_id: str,
        *,
        outcome: ReplayOutcome | str,
        matched_record_id: str | None = None,
        detail: str = "",
    ) -> None:
        generation = self._require_staged(generation_id)
        selected_outcome = ReplayOutcome(outcome)
        expected = self.conn.execute(
            """SELECT o.id,
                      (SELECT COUNT(*) FROM personal_model_overrides earlier
                       WHERE earlier.sequence <= o.sequence
                         AND earlier.sequence <= ?) AS replay_order
               FROM personal_model_overrides o
               WHERE o.sequence <= ?
                 AND NOT EXISTS (
                   SELECT 1 FROM personal_model_override_replays replay
                   WHERE replay.generation_id=? AND replay.override_id=o.id)
               ORDER BY o.sequence LIMIT 1""",
            (
                generation.override_sequence_max,
                generation.override_sequence_max,
                generation_id,
            ),
        ).fetchone()
        if expected is None:
            raise ValueError("generation has no pending override to replay")
        if expected["id"] != override_id:
            raise ValueError(
                f"overrides must replay in ledger order; expected {expected['id']}"
            )
        override = self.conn.execute(
            """SELECT target_kind, target_record_id FROM personal_model_overrides
               WHERE id=?""",
            (override_id,),
        ).fetchone()
        matched = None
        if matched_record_id is not None:
            matched = self.conn.execute(
                """SELECT record_kind FROM personal_model_records
                   WHERE generation_id=? AND record_id=?""",
                (generation_id, matched_record_id),
            ).fetchone()
            if matched is None:
                raise ValueError("override replay matched_record_id does not exist")
            if matched["record_kind"] != override["target_kind"]:
                raise ValueError("override replay matched record has the wrong target kind")
        if selected_outcome == ReplayOutcome.APPLIED and matched is None:
            raise ValueError("applied override replay requires a matched record")
        target_exists = self.conn.execute(
            """SELECT 1 FROM personal_model_records
               WHERE generation_id=? AND record_id=? AND record_kind=?""",
            (generation_id, override["target_record_id"], override["target_kind"]),
        ).fetchone()
        is_reworded_verdict = (
            override is not None
            and override["target_kind"] == str(OverrideTargetKind.INTERPRETATION)
            and self.conn.execute(
                "SELECT action FROM personal_model_overrides WHERE id=?", (override_id,)
            ).fetchone()["action"] == str(OverrideAction.VERDICT)
        )
        if selected_outcome == ReplayOutcome.ORPHANED and matched_record_id is not None:
            raise ValueError("an orphaned override cannot carry a matched record")
        if (
            selected_outcome == ReplayOutcome.ORPHANED
            and target_exists is not None
            and not is_reworded_verdict
        ):
            raise ValueError("an override is orphaned only when its stable target is absent")
        if selected_outcome in {ReplayOutcome.ORPHANED, ReplayOutcome.CONFLICT} and not detail:
            raise ValueError("orphaned or conflicting override replay requires detail")
        with self.conn:
            self.conn.execute(
                """INSERT INTO personal_model_override_replays
                   (generation_id, override_id, replay_order, outcome,
                    matched_record_id, detail, replayed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    override_id,
                    expected["replay_order"],
                    str(selected_outcome),
                    matched_record_id,
                    detail,
                    iso(utc_now()),
                ),
            )
            if selected_outcome in {ReplayOutcome.ORPHANED, ReplayOutcome.CONFLICT}:
                self.conn.execute(
                    """INSERT INTO personal_model_quarantines
                       (id, generation_id, stage, candidate_id, reason_code,
                        detail_json, created_at)
                       VALUES (?,?,'override_replay',?,?,?,?)""",
                    (
                        new_id(),
                        generation_id,
                        override_id,
                        f"override_{selected_outcome}",
                        canonical_json({"override_id": override_id, "detail": detail}),
                        iso(utc_now()),
                    ),
                )
                self.conn.execute(
                    """UPDATE personal_model_generations
                       SET quarantine_count=quarantine_count+1 WHERE id=?""",
                    (generation_id,),
                )

    def activate_generation(
        self, generation_id: str, *, on_checked: Callable[[int, int], None] | None = None,
    ) -> ModelGeneration:
        """Check every record, then make the generation current.

        ``on_checked(done, total)`` hears how far the check has got: on a slow
        laptop it takes minutes, and setup shows it.
        """
        self._require_staged(generation_id)
        now = iso(utc_now())
        with self.conn:
            self._validate_generation_integrity(generation_id, on_checked)
            previous = self.active_generation_id()
            if previous is not None:
                self.conn.execute(
                    "DELETE FROM personal_model_active_generation WHERE singleton=1"
                )
                self.conn.execute(
                    """UPDATE personal_model_generations SET state='superseded'
                       WHERE id=? AND state='current'""",
                    (previous,),
                )
            self._refresh_metrics_json(generation_id)
            self.conn.execute(
                """UPDATE personal_model_generations
                   SET state='current', completed_at=?, activated_at=?,
                       record_count=(SELECT COUNT(*) FROM personal_model_records
                                     WHERE generation_id=?),
                       reused_record_count=(SELECT COUNT(*) FROM personal_model_records
                                            WHERE generation_id=?
                                              AND reused_from_generation_id IS NOT NULL),
                       quarantine_count=(SELECT COUNT(*) FROM personal_model_quarantines
                                         WHERE generation_id=?),
                       failure_reason=NULL, failed_at=NULL
                   WHERE id=? AND state='staged'""",
                (now, now, generation_id, generation_id, generation_id, generation_id),
            )
            self.conn.execute(
                """INSERT INTO personal_model_active_generation
                   (singleton, generation_id, activated_at) VALUES (1,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     generation_id=excluded.generation_id,
                     activated_at=excluded.activated_at""",
                (generation_id, now),
            )
        return self.get_generation(generation_id)

    def fail_generation(self, generation_id: str, reason: str) -> ModelGeneration:
        self._require_staged(generation_id)
        if not reason.strip():
            raise ValueError("failure reason must be non-blank")
        now = iso(utc_now())
        with self.conn:
            self._refresh_metrics_json(generation_id)
            self.conn.execute(
                """UPDATE personal_model_generations
                   SET state='failed', completed_at=?, failed_at=?, failure_reason=?,
                       record_count=(SELECT COUNT(*) FROM personal_model_records
                                     WHERE generation_id=?),
                       reused_record_count=(SELECT COUNT(*) FROM personal_model_records
                                            WHERE generation_id=?
                                              AND reused_from_generation_id IS NOT NULL),
                       quarantine_count=(SELECT COUNT(*) FROM personal_model_quarantines
                                         WHERE generation_id=?)
                   WHERE id=? AND state='staged'""",
                (
                    now,
                    now,
                    reason,
                    generation_id,
                    generation_id,
                    generation_id,
                    generation_id,
                ),
            )
        return self.get_generation(generation_id)

    def discard_generation(self, generation_id: str) -> int:
        """Delete a generation that can never be read again, and everything it staged.

        A compile that dies leaves its whole staging behind -- 120,608 records
        and four million dependency rows, for one of the failures here -- and
        nothing ever removed it, so the archive grew by most of a model every
        time a run failed.  Only a generation that never activated may go: an
        activated one is the owner's history and, through
        ``clone_record_if_unchanged``, the one thing a later compile is allowed
        to reuse.

        Records go first in bounded rowid windows.  A real generation carries
        millions of evidence and dependency rows, and asking SQLite to cascade
        all of them from the generation header in one transaction can make the
        write-ahead log fill the disk that this cleanup is meant to recover.
        Each committed window is safe to resume after interruption.  Deleting
        the empty generation header then cascades the smaller generation-scoped
        rows: memberships, aliases, quarantines, checkpoints and metrics.

        Returns how many records went, counted rather than read off
        ``record_count``, which a generation that failed before persisting
        never filled in.
        """

        generation = self.get_generation(generation_id)
        if generation.state in {GenerationState.CURRENT, GenerationState.SUPERSEDED}:
            raise ValueError(
                "an activated generation is history and a source for reuse, "
                f"so it is not discardable: {generation_id}"
            )
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM personal_model_records WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        bounds = self.conn.execute(
            "SELECT MIN(rowid) AS first, MAX(rowid) AS last "
            "FROM personal_model_records WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if bounds["first"] is not None:
            first = int(bounds["first"])
            last = int(bounds["last"])
            for lower in range(first, last + 1, _DISCARD_RECORD_ROWID_SPAN):
                with self.conn:
                    self.conn.execute(
                        "DELETE FROM personal_model_records "
                        "WHERE generation_id=? AND rowid>=? AND rowid<?",
                        (
                            generation_id,
                            lower,
                            lower + _DISCARD_RECORD_ROWID_SPAN,
                        ),
                    )
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with self.conn:
            self.conn.execute(
                "DELETE FROM personal_model_generations WHERE id=?", (generation_id,)
            )
        return int(row["total"])

    def _require_staged(self, generation_id: str) -> ModelGeneration:
        generation = self._generation_header(generation_id)
        if generation.state != GenerationState.STAGED:
            raise ValueError(f"personal-model generation is not staged: {generation_id}")
        return generation

    def _refresh_metrics_json(self, generation_id: str) -> None:
        rows = self.conn.execute(
            """SELECT stage, metric_name, metric_json FROM personal_model_run_metrics
               WHERE generation_id=? ORDER BY stage, metric_name""",
            (generation_id,),
        ).fetchall()
        metrics: dict[str, dict[str, Any]] = {}
        for row in rows:
            metrics.setdefault(row["stage"], {})[row["metric_name"]] = json.loads(
                row["metric_json"]
            )
        self.conn.execute(
            "UPDATE personal_model_generations SET metrics_json=? WHERE id=?",
            (canonical_json(metrics), generation_id),
        )

    @staticmethod
    def _query_projection(
        record: DerivedRecord,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        if isinstance(record, Person):
            return "person", record.id, None, None
        if isinstance(record, Project):
            return "project", record.id, None, None
        if isinstance(record, Goal):
            return "goal", record.id, None, None
        if isinstance(record, Event):
            return "event", record.id, record.event_kind.casefold(), None
        if isinstance(record, (Fact, Preference)):
            return str(record.subject.kind), record.subject.id, record.predicate, None
        if isinstance(record, Observation):
            return (
                str(record.subject_kind) if record.subject_kind is not None else None,
                record.subject_id,
                str(record.kind),
                None,
            )
        if isinstance(record, (InterpretationCandidate, Interpretation)):
            return None, None, None, record.concept_key
        if isinstance(record, RelationshipSnapshot):
            return "person", record.other_person_id, "relationship", None
        return None, None, None, None

    def _validate_generation_integrity(
        self, generation_id: str, on_checked: Callable[[int, int], None] | None = None,
    ) -> None:
        generation = self._require_staged(generation_id)
        total = 0
        if on_checked is not None:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM personal_model_records WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        # Iterated, not fetched: a real generation is 120,000 records and
        # 1.87 GB of record JSON, and holding every raw row alive while every
        # parsed model is also alive doubles the peak for no reason.  The
        # cursor lets each row's JSON go as soon as it has been parsed.
        cursor = self.conn.execute(
            """SELECT record_id, record_kind, record_json, canonical_json_hash,
                      dependency_hash
               FROM personal_model_records WHERE generation_id=?""",
            (generation_id,),
        )
        records: dict[str, tuple[str, DerivedRecord, str]] = {}
        for row in cursor:
            if on_checked is not None and len(records) % 500 == 0:
                on_checked(len(records), total)
            encoded = row["record_json"]
            if canonical_json(json.loads(encoded)) != encoded:
                raise ValueError(f"record JSON is not canonical: {row['record_id']}")
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["canonical_json_hash"]:
                raise ValueError(f"record JSON hash mismatch: {row['record_id']}")
            model = RECORD_TYPES[row["record_kind"]].model_validate_json(encoded, strict=True)
            if model.generation_id != generation_id or model.id != row["record_id"]:
                raise ValueError(f"record identity does not match storage key: {row['record_id']}")
            dependencies = self._dependencies_for(generation_id, row["record_id"])
            expected_hash = compute_dependency_hash(
                model,
                dependencies,
                compiler_version=generation.compiler_version,
                policy_version=generation.policy_version,
            )
            if expected_hash != row["dependency_hash"]:
                raise ValueError(f"dependency hash mismatch: {row['record_id']}")
            # Checked here rather than in a second pass below.  The dependency
            # rows are already in hand -- 34 per record, four million for the
            # generation -- and reading them again only to check something else
            # about them cost a second full pass over the table.
            self._require_standard_dependencies(generation, model, dependencies)
            records[row["record_id"]] = (row["record_kind"], model, row["dependency_hash"])

        self._validate_graph_membership(generation, records)
        self._validate_model_dependencies(generation_id, records)
        self._validate_domain_references(records)
        self._validate_override_replay(generation)

    def _dependencies_for(
        self, generation_id: str, record_id: str
    ) -> tuple[ModelDependency, ...]:
        rows = self.conn.execute(
            """SELECT dependency_kind, dependency_id, dependency_hash,
                      target_generation_id
               FROM personal_model_record_dependencies
               WHERE generation_id=? AND record_id=? ORDER BY ordinal""",
            (generation_id, record_id),
        ).fetchall()
        return tuple(
            ModelDependency(
                kind=row["dependency_kind"],
                id=row["dependency_id"],
                hash=row["dependency_hash"],
                model_record=row["target_generation_id"] is not None,
            )
            for row in rows
        )

    def _require_standard_dependencies(
        self,
        generation: ModelGeneration,
        record: DerivedRecord,
        dependencies: tuple[ModelDependency, ...],
    ) -> None:
        try:
            required = required_record_dependencies(
                self.conn, generation.graph_generation_id, record
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        required_map = {(str(item.kind), item.id): item.hash for item in required}
        supplied_map = {
            (str(item.kind), item.id): item.hash
            for item in dependencies
            if not item.model_record
            and item.kind
            in {ExternalDependencyKind.ENTRY, ExternalDependencyKind.GRAPH_CLAIM}
        }
        if required_map != supplied_map:
            missing = sorted(set(required_map) - set(supplied_map))
            unexpected = sorted(set(supplied_map) - set(required_map))
            stale = sorted(
                key
                for key in set(required_map) & set(supplied_map)
                if required_map[key] != supplied_map[key]
            )
            raise ValueError(
                "record evidence dependencies are incomplete or stale: "
                f"missing={missing}, unexpected={unexpected}, stale={stale}"
            )

    def _standard_dependencies_match(
        self,
        generation: ModelGeneration,
        record: DerivedRecord,
        dependencies: tuple[ModelDependency, ...],
    ) -> bool:
        try:
            self._require_standard_dependencies(generation, record, dependencies)
        except ValueError:
            return False
        return True

    def _validate_graph_membership(
        self,
        generation: ModelGeneration,
        records: dict[str, tuple[str, DerivedRecord, str]],
    ) -> None:
        entry_ids = {
            reference.entry_id
            for _, record, _ in records.values()
            for reference in record.evidence
        }
        if entry_ids:
            found = set()
            for chunk in in_chunks(entry_ids):
                placeholders = ",".join("?" for _ in chunk)
                found.update(
                    row["id"]
                    for row in self.conn.execute(
                        f"SELECT id FROM entries WHERE id IN ({placeholders})", chunk
                    )
                )
            missing = sorted(entry_ids - found)
            if missing:
                raise ValueError(f"personal-model evidence entries do not exist: {missing}")
        claim_ids = {
            claim_id
            for _, record, _ in records.values()
            for claim_id in (
                *record.claim_ids,
                *(ref.claim_id for ref in record.evidence if ref.claim_id is not None),
            )
        }
        if claim_ids:
            found = set()
            for chunk in in_chunks(claim_ids):
                placeholders = ",".join("?" for _ in chunk)
                found.update(
                    row["id"]
                    for row in self.conn.execute(
                        f"""SELECT id FROM claims WHERE graph_generation_id=?
                             AND id IN ({placeholders})""",
                        (generation.graph_generation_id, *chunk),
                    )
                )
            missing = sorted(claim_ids - found)
            if missing:
                raise ValueError(
                    "personal-model claims are absent from the pinned graph generation: "
                    f"{missing}"
                )

    def _validate_model_dependencies(
        self, generation_id: str, records: dict[str, tuple[str, DerivedRecord, str]]
    ) -> None:
        rows = self.conn.execute(
            """SELECT record_id, dependency_kind, dependency_id, dependency_hash,
                      target_generation_id
               FROM personal_model_record_dependencies
               WHERE generation_id=? AND target_generation_id IS NOT NULL""",
            (generation_id,),
        ).fetchall()
        supplied: dict[str, set[tuple[str, str]]] = {}
        for row in rows:
            target = records.get(row["dependency_id"])
            if target is None:
                raise ValueError(
                    f"model dependency is missing for {row['record_id']}: "
                    f"{row['dependency_id']}"
                )
            if target[0] != row["dependency_kind"]:
                raise ValueError(
                    f"model dependency kind mismatch for {row['record_id']}: "
                    f"expected {row['dependency_kind']}, found {target[0]}"
                )
            if target[2] != row["dependency_hash"]:
                raise ValueError(
                    f"model dependency hash is stale for {row['record_id']}: "
                    f"{row['dependency_id']}"
                )
            supplied.setdefault(row["record_id"], set()).add(
                (row["dependency_kind"], row["dependency_id"])
            )
        for record_id, (_, record, _) in records.items():
            required = required_model_references(record)
            missing = sorted(required - supplied.get(record_id, set()))
            if missing:
                raise ValueError(
                    f"record {record_id} lacks model dependency membership: {missing}"
                )

    @staticmethod
    def _validate_domain_references(
        records: dict[str, tuple[str, DerivedRecord, str]]
    ) -> None:
        def require(source: str, target: str | None, expected_kind: str) -> None:
            if target is None:
                return
            found = records.get(target)
            if found is None:
                raise ValueError(f"{source} references missing {expected_kind}: {target}")
            if found[0] != expected_kind:
                raise ValueError(
                    f"{source} references {target} as {expected_kind}, found {found[0]}"
                )

        def require_subject(source: str, kind: str, target: str) -> None:
            if kind in {"person", "project", "goal", "event"}:
                require(source, target, kind)

        episodes = {
            record_id: record
            for record_id, (_, record, _) in records.items()
            if isinstance(record, ConversationEpisode)
        }
        observations = {
            record_id: record
            for record_id, (_, record, _) in records.items()
            if isinstance(record, Observation)
        }

        for record_id, (kind, record, _) in records.items():
            for reference in record.evidence:
                if reference.episode_id is not None:
                    require(record_id, reference.episode_id, "conversation_episode")
                    episode = episodes[reference.episode_id]
                    if reference.entry_id not in {
                        turn.entry_id for turn in episode.turns
                    }:
                        raise ValueError(
                            f"{record_id} evidence entry is outside episode "
                            f"{reference.episode_id}: {reference.entry_id}"
                        )
                if reference.observation_id is not None:
                    require(record_id, reference.observation_id, "observation")
                    observation = observations[reference.observation_id]
                    observation_entries = {
                        *(span.entry_id for span in observation.spans),
                        *(item.entry_id for item in observation.evidence),
                    }
                    if reference.entry_id not in observation_entries:
                        raise ValueError(
                            f"{record_id} evidence entry is outside observation "
                            f"{reference.observation_id}: {reference.entry_id}"
                        )
                    if (
                        reference.episode_id is not None
                        and observation.episode_id != reference.episode_id
                    ):
                        raise ValueError(
                            f"{record_id} evidence observation belongs to a different episode"
                        )
            if isinstance(record, ConversationEpisode):
                turn_entries = {turn.entry_id for turn in record.turns}
                evidence_entries = {reference.entry_id for reference in record.evidence}
                missing_turn_evidence = sorted(turn_entries - evidence_entries)
                if missing_turn_evidence:
                    raise ValueError(
                        f"episode {record_id} turns lack evidence references: "
                        f"{missing_turn_evidence}"
                    )
                for target in record.participant_person_ids:
                    require(record_id, target, "person")
            elif isinstance(record, Observation):
                require(record_id, record.episode_id, "conversation_episode")
                episode_entries = {
                    turn.entry_id for turn in episodes[record.episode_id].turns
                }
                observation_entries = {
                    *(span.entry_id for span in record.spans),
                    *(reference.entry_id for reference in record.evidence),
                }
                outside_entries = sorted(observation_entries - episode_entries)
                if outside_entries:
                    raise ValueError(
                        f"observation {record_id} evidence is outside its episode: "
                        f"{outside_entries}"
                    )
                require(record_id, record.speaker_person_id, "person")
                if record.subject_kind is not None and record.subject_id is not None:
                    require_subject(record_id, str(record.subject_kind), record.subject_id)
                for target in record.addressee_person_ids:
                    require(record_id, target, "person")
            elif isinstance(record, Person):
                require(record_id, record.merged_into_person_id, "person")
            elif isinstance(record, Project):
                require(record_id, record.merged_into_project_id, "project")
                for target in record.participant_person_ids:
                    require(record_id, target, "person")
            elif isinstance(record, Goal):
                for target in record.project_ids:
                    require(record_id, target, "project")
                for target in (*record.parent_goal_ids, *record.dependency_goal_ids):
                    require(record_id, target, "goal")
            elif isinstance(record, Fact):
                require_subject(record_id, str(record.subject.kind), record.subject.id)
                require(record_id, record.asserted_by_person_id, "person")
                for target in (*record.supersedes_fact_ids, *record.contradicted_by_fact_ids):
                    require(record_id, target, "fact")
            elif isinstance(record, Preference):
                require_subject(record_id, str(record.subject.kind), record.subject.id)
                require(record_id, record.asserted_by_person_id, "person")
                for target in (
                    *record.supersedes_preference_ids,
                    *record.contradicted_by_preference_ids,
                ):
                    require(record_id, target, "preference")
            elif isinstance(record, Event):
                for target in record.participant_ids:
                    require(record_id, target, "person")
                for target in record.project_ids:
                    require(record_id, target, "project")
            elif isinstance(record, RelationshipSnapshot):
                require(record_id, record.archive_owner_person_id, "person")
                require(record_id, record.other_person_id, "person")
                for target in record.shared_event_ids:
                    require(record_id, target, "event")
                for target in record.commitment_observation_ids:
                    require(record_id, target, "observation")
            elif isinstance(record, (InterpretationCandidate, Interpretation)):
                if record.scope.project_id is not None:
                    require(record_id, record.scope.project_id, "project")
                if record.scope.relationship_person_ids is not None:
                    for target in record.scope.relationship_person_ids:
                        require(record_id, target, "person")
                for assignment in record.evidence_assignments:
                    require(record_id, assignment.episode_id, "conversation_episode")
                    for target in assignment.observation_ids:
                        require(record_id, target, "observation")
                        if observations[target].episode_id != assignment.episode_id:
                            raise ValueError(
                                f"{record_id} assigns observation {target} to the wrong episode"
                            )
                if isinstance(record, Interpretation):
                    require(record_id, record.candidate_id, "interpretation_candidate")
            else:  # pragma: no cover - RECORD_TYPES makes this unreachable
                raise TypeError(f"unvalidated record kind: {kind}")

    def _validate_override_replay(self, generation: ModelGeneration) -> None:
        expected = self.conn.execute(
            """SELECT id, sequence FROM personal_model_overrides
               WHERE sequence <= ? ORDER BY sequence""",
            (generation.override_sequence_max,),
        ).fetchall()
        replayed = self.conn.execute(
            """SELECT replay.override_id, replay.replay_order, replay.outcome,
                      replay.detail, replay.matched_record_id,
                      override.target_kind, override.target_record_id, override.action
               FROM personal_model_override_replays replay
               JOIN personal_model_overrides override ON override.id=replay.override_id
               WHERE replay.generation_id=? ORDER BY replay.replay_order""",
            (generation.id,),
        ).fetchall()
        if len(expected) != len(replayed):
            missing = [
                row["id"]
                for row in expected
                if row["id"] not in {item["override_id"] for item in replayed}
            ]
            raise ValueError(f"personal-model overrides were not replayed: {missing}")
        for order, (override, replay) in enumerate(zip(expected, replayed, strict=True), 1):
            if replay["override_id"] != override["id"] or replay["replay_order"] != order:
                raise ValueError("personal-model overrides were replayed out of ledger order")
            if replay["outcome"] in {"orphaned", "conflict"} and not replay["detail"]:
                raise ValueError("orphaned override replay lacks an audit detail")
            if replay["outcome"] == "conflict":
                raise ValueError(
                    f"conflicting personal-model override blocks activation: {override['id']}"
                )
            matched_id = replay["matched_record_id"]
            if replay["outcome"] == "applied" and matched_id is None:
                raise ValueError("applied override replay lacks a matched record")
            if matched_id is not None:
                matched = self.conn.execute(
                    """SELECT record_kind FROM personal_model_records
                       WHERE generation_id=? AND record_id=?""",
                    (generation.id, matched_id),
                ).fetchone()
                if matched is None or matched["record_kind"] != replay["target_kind"]:
                    raise ValueError("override replay matched record no longer resolves")
            if replay["outcome"] == "orphaned":
                target = self.conn.execute(
                    """SELECT 1 FROM personal_model_records
                       WHERE generation_id=? AND record_id=? AND record_kind=?""",
                    (
                        generation.id,
                        replay["target_record_id"],
                        replay["target_kind"],
                    ),
                ).fetchone()
                if target is not None and not (
                    replay["target_kind"] == "interpretation"
                    and replay["action"] == "verdict"
                ):
                    raise ValueError("orphaned override target exists in the generation")


__all__ = [
    "ALLOWED_OVERRIDE_ACTIONS",
    "ExternalDependencyKind",
    "GenerationState",
    "ModelDependency",
    "ModelGeneration",
    "ModelOverride",
    "OverrideAction",
    "OverrideTargetKind",
    "PersonalModelRepository",
    "ReplayOutcome",
    "InterpretationVerdictPayload",
    "canonical_json",
    "compiler_input_dependency",
    "compute_dependency_hash",
    "content_hash",
    "entry_dependency",
    "graph_claim_dependency",
    "model_record_dependency",
    "record_kind",
    "required_record_dependencies",
]

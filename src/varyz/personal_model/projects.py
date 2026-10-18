"""Evidence-bound compilation of projects and goal hierarchy.

This module does not discover projects or goals from prose.  It accepts closed,
schema-constrained proposals, checks every proposal against pinned observations or
atomic claims, resolves project identity conservatively, and emits staged domain
records.  Display wording is deliberately excluded from record identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import combinations
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from varyz.domain import Sensitivity, TimePrecision
from varyz.personal_model.memory import AtomicClaimInput
from varyz.personal_model.models import (
    AliasKind,
    AttributionState,
    DerivationKind,
    EntityAlias,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    Fact,
    FactStatus,
    FactValueKind,
    Goal,
    GoalKind,
    GoalStatus,
    Observation,
    ProducerKind,
    ProducerMetadata,
    Project,
    Readiness,
    RecordState,
    SubjectReference,
    TemporalScope,
    TextFactValue,
    VerificationState,
    derived_record_id,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ProjectIdentifierKind(StrEnum):
    REPOSITORY = "repository"
    PATH = "path"
    PRODUCT = "product"
    SOURCE_NATIVE = "source_native"


class ProposedNodeKind(StrEnum):
    INTENDED_OUTCOME = "intended_outcome"
    GOAL = "goal"
    WORKSTREAM = "workstream"
    SUBGOAL = "subgoal"
    COMMITMENT = "commitment"
    ACTION = "action"
    ERRAND = "errand"
    SETUP_STEP = "setup_step"
    CONVERSATION_MOVE = "conversation_move"
    IMPLEMENTATION_DETAIL = "implementation_detail"


class ProjectOverrideAction(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    REPARENT = "reparent"
    RECLASSIFY = "reclassify"


class ProjectDiagnosticCode(StrEnum):
    UNKNOWN_EVIDENCE = "unknown_evidence"
    UNRESOLVED_ATTRIBUTION = "unresolved_attribution"
    ORPHANED_OVERRIDE = "orphaned_override"
    OVERRIDE_CONFLICT = "override_conflict"
    SUPERSEDED_OVERRIDE = "superseded_override"
    UNNAMED_PROJECT = "unnamed_project"
    UNNAMED_GOAL = "unnamed_goal"
    UNKNOWN_PROJECT = "unknown_project"
    UNKNOWN_GOAL = "unknown_goal"
    CYCLIC_HIERARCHY = "cyclic_hierarchy"
    INVALID_PARENT = "invalid_parent"


class HierarchyRejectionReason(StrEnum):
    ACTION_NOT_PERSONAL_GOAL = "action_not_personal_goal"
    ERRAND_NOT_PERSONAL_GOAL = "errand_not_personal_goal"
    SETUP_NOT_PERSONAL_GOAL = "setup_not_personal_goal"
    CONVERSATION_MOVE_NOT_PERSONAL_GOAL = "conversation_move_not_personal_goal"
    IMPLEMENTATION_DETAIL_NOT_PERSONAL_GOAL = "implementation_detail_not_personal_goal"
    COMMITMENT_REQUIRES_ACTION_LAYER = "commitment_requires_action_layer"


class ProjectIdentifier(_FrozenModel):
    kind: ProjectIdentifierKind
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project identifier cannot be blank")
        return value

    @property
    def canonical(self) -> str:
        value = unicodedata.normalize("NFKC", self.value.strip())
        if self.kind in {ProjectIdentifierKind.REPOSITORY, ProjectIdentifierKind.PATH}:
            value = value.replace("\\", "/").rstrip("/").casefold()
        elif self.kind == ProjectIdentifierKind.PRODUCT:
            value = normalize_project_alias(value)
        return f"{self.kind.value}:{value}"


class ProjectEvidenceCandidate(_FrozenModel):
    """A proposed project mention grounded in already resolved evidence."""

    candidate_id: str = Field(min_length=1)
    canonical_value_key: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    display_name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    identifiers: tuple[ProjectIdentifier, ...] = ()
    purpose: str | None = None
    life_area: str | None = None
    participant_person_ids: tuple[str, ...] = ()
    explicit_self_reference: bool = False
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    observed_at: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("project observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def evidence_and_uniqueness(self) -> ProjectEvidenceCandidate:
        if not self.observation_ids and not self.claim_ids:
            raise ValueError("project candidates require pinned evidence")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("project observation ids must be unique")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("project claim ids must be unique")
        if len(set(self.participant_person_ids)) != len(self.participant_person_ids):
            raise ValueError("project participant ids must be unique")
        canonical_identifiers = tuple(item.canonical for item in self.identifiers)
        if len(set(canonical_identifiers)) != len(canonical_identifiers):
            raise ValueError("project identifiers must be unique")
        if (
            not self.candidate_id.strip()
            or not self.display_name.strip()
            or any(not alias.strip() for alias in self.aliases)
            or any(not item.strip() for item in self.participant_person_ids)
            or any(not item.strip() for item in (*self.observation_ids, *self.claim_ids))
        ):
            raise ValueError("project display names and aliases cannot be blank")
        if self.purpose is not None and not self.purpose.strip():
            raise ValueError("project purpose cannot be blank")
        if self.life_area is not None and not self.life_area.strip():
            raise ValueError("project life area cannot be blank")
        return self


class GoalEvidenceCandidate(_FrozenModel):
    """A proposed hierarchy node; operational kinds are explicitly filtered."""

    candidate_id: str = Field(min_length=1)
    canonical_value_key: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    title: str = Field(min_length=1)
    node_kind: ProposedNodeKind
    status: GoalStatus = GoalStatus.UNKNOWN
    status_literal: str | None = None
    continuity_key: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    project_candidate_ids: tuple[str, ...] = ()
    parent_goal_candidate_ids: tuple[str, ...] = ()
    dependency_goal_candidate_ids: tuple[str, ...] = ()
    intended_outcome: str | None = None
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    observed_at: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED
    explicit_personal_goal: bool = False

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("goal observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def evidence_and_uniqueness(self) -> GoalEvidenceCandidate:
        if not self.observation_ids and not self.claim_ids:
            raise ValueError("goal candidates require pinned evidence")
        for label, values in (
            ("observation", self.observation_ids),
            ("claim", self.claim_ids),
            ("project", self.project_candidate_ids),
            ("parent", self.parent_goal_candidate_ids),
            ("dependency", self.dependency_goal_candidate_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"goal {label} ids must be unique")
            if any(not value.strip() for value in values):
                raise ValueError(f"goal {label} ids cannot be blank")
        if not self.candidate_id.strip() or not self.title.strip():
            raise ValueError("goal candidate id and title cannot be blank")
        if self.candidate_id in self.parent_goal_candidate_ids:
            raise ValueError("a goal candidate cannot parent itself")
        if self.candidate_id in self.dependency_goal_candidate_ids:
            raise ValueError("a goal candidate cannot depend on itself")
        if self.continuity_key is not None and not self.continuity_key.strip():
            raise ValueError("continuity_key cannot be blank")
        if self.status != GoalStatus.UNKNOWN and not self.status_literal:
            raise ValueError("a known goal status requires its exact source literal")
        if self.status == GoalStatus.UNKNOWN and self.status_literal is not None:
            raise ValueError("an unknown goal status cannot carry an exact literal")
        return self


class ProjectHierarchyOverride(_FrozenModel):
    """Append-only, replayable correction over source candidate membership."""

    sequence: int = Field(ge=1)
    override_id: str = Field(min_length=1)
    action: ProjectOverrideAction
    project_member_groups: tuple[tuple[str, ...], ...] = ()
    goal_candidate_id: str | None = None
    new_parent_goal_candidate_ids: tuple[str, ...] = ()
    new_node_kind: ProposedNodeKind | None = None

    @model_validator(mode="after")
    def fields_match_action(self) -> ProjectHierarchyOverride:
        groups = self.project_member_groups
        flat = tuple(item for group in groups for item in group)
        if any(not item.strip() for item in flat) or len(set(flat)) != len(flat):
            raise ValueError("override project members must be non-blank and unique")
        if self.action == ProjectOverrideAction.MERGE:
            if len(groups) != 1 or len(groups[0]) < 2:
                raise ValueError("merge requires one group of at least two project candidates")
        elif self.action == ProjectOverrideAction.SPLIT:
            if len(groups) < 2 or any(not group for group in groups):
                raise ValueError("split requires at least two non-empty groups")
        elif groups:
            raise ValueError("goal hierarchy overrides cannot contain project groups")
        if self.action == ProjectOverrideAction.REPARENT:
            if self.goal_candidate_id is None or self.new_node_kind is not None:
                raise ValueError("reparent requires only a goal candidate and new parent ids")
        elif self.action == ProjectOverrideAction.RECLASSIFY:
            if self.goal_candidate_id is None or self.new_node_kind is None:
                raise ValueError("reclassify requires a goal candidate and node kind")
            if self.new_parent_goal_candidate_ids:
                raise ValueError("reclassify cannot also reparent")
        elif (
            self.goal_candidate_id is not None
            or self.new_parent_goal_candidate_ids
            or self.new_node_kind
        ):
            raise ValueError("project identity overrides cannot contain goal fields")
        if len(set(self.new_parent_goal_candidate_ids)) != len(self.new_parent_goal_candidate_ids):
            raise ValueError("new parent ids must be unique")
        return self


@dataclass(frozen=True)
class ProjectReviewCandidate:
    left_candidate_id: str
    right_candidate_id: str
    score: float
    features: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ProjectDiagnostic:
    code: ProjectDiagnosticCode
    detail: str
    candidate_ids: tuple[str, ...] = ()
    override_id: str | None = None


@dataclass(frozen=True)
class HierarchyRejection:
    candidate_id: str
    title: str
    reason: HierarchyRejectionReason
    evidence_membership: tuple[str, ...]


@dataclass(frozen=True)
class ActionHandoff:
    """A non-goal leaf retained for the observation commitment/action layer."""

    candidate_id: str
    title: str
    node_kind: ProposedNodeKind
    observation_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    source_entry_ids: tuple[str, ...]
    project_ids: tuple[str, ...]
    parent_goal_ids: tuple[str, ...]
    target_layer: str = "observation_commitment_action"


@dataclass(frozen=True)
class ProjectPurposeChange:
    project_id: str
    previous_purpose: str
    current_purpose: str
    changed_at: datetime | None
    previous_evidence_membership: tuple[str, ...]
    current_evidence_membership: tuple[str, ...]


@dataclass(frozen=True)
class GoalStatusChange:
    goal_id: str
    previous_status: GoalStatus
    current_status: GoalStatus
    changed_at: datetime | None
    previous_evidence_membership: tuple[str, ...]
    current_evidence_membership: tuple[str, ...]


@dataclass(frozen=True)
class ProjectCompilation:
    projects: tuple[Project, ...]
    goals: tuple[Goal, ...]
    review_candidates: tuple[ProjectReviewCandidate, ...]
    quarantines: tuple[ProjectDiagnostic, ...]
    rejections: tuple[HierarchyRejection, ...]
    action_handoffs: tuple[ActionHandoff, ...]
    history_facts: tuple[Fact, ...]
    purpose_changes: tuple[ProjectPurposeChange, ...]
    status_changes: tuple[GoalStatusChange, ...]
    project_candidate_to_project_id: Mapping[str, str] = field(repr=False)
    goal_candidate_to_goal_id: Mapping[str, str] = field(repr=False)
    applied_override_ids: tuple[str, ...] = ()
    unmatched_override_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_candidate_to_project_id",
            MappingProxyType(dict(self.project_candidate_to_project_id)),
        )
        object.__setattr__(
            self,
            "goal_candidate_to_goal_id",
            MappingProxyType(dict(self.goal_candidate_to_goal_id)),
        )


@dataclass(frozen=True)
class ProjectPolicy:
    version: str = "project-hierarchy-v1"
    auto_merge_threshold: float = 0.75
    review_threshold: float = 0.20
    continuity_window: timedelta = timedelta(days=180)

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("project policy version cannot be blank")
        if not 0 <= self.review_threshold <= self.auto_merge_threshold <= 1:
            raise ValueError("project thresholds must be ordered in [0, 1]")
        if self.continuity_window <= timedelta(0):
            raise ValueError("continuity window must be positive")


class _DisjointSet:
    def __init__(self, keys: Iterable[str]):
        self.parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self.parent[key]
        if parent != key:
            self.parent[key] = self.find(parent)
        return self.parent[key]

    def members(self, key: str) -> set[str]:
        root = self.find(key)
        return {item for item in self.parent if self.find(item) == root}

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            first, second = sorted((left_root, right_root))
            self.parent[second] = first


def normalize_project_alias(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.strip().casefold())
    value = "".join(char for char in decomposed if not unicodedata.combining(char))
    words = re.findall(r"[\w]+", value, flags=re.UNICODE)
    while words and words[-1] in {"app", "application", "project", "product"}:
        words.pop()
    return " ".join(words)


# A word shaped like an account rather than a name: a phone number however it
# was punctuated, a WhatsApp JID, a content hash, a Telegram account with no
# username.  These reach a project proposal because that is all the archive
# knows some people by, and a chat with such a person is what the proposal was
# read out of.
# What a platform prints in place of a digit when it will show a number but not
# all of it.  Two people in this archive are called `+34∙∙∙∙∙∙∩62` and
# `+33∙∙∙∙∙∙∩43`, and the bullets were enough content for the number to read
# as a name: both sorted above every person on "People", which is the one thing
# the sort there exists to prevent.
_MASKED_DIGIT = "∙•·∗*×"

_ACCOUNT_SHAPED = re.compile(
    r"^(?:\+?\d[\d" + _MASKED_DIGIT + r" ()\-]{4,}|\d+@[a-z0-9.]+|[0-9a-f]{16,}|user\d{6,})$",
    re.IGNORECASE,
)

# Words that carry no name of their own.  The list is not general English: it is
# the connective half of the two phrasings a proposal falls back on when it has
# no name to give -- "Relationship with <account>" and "No specific name 5".
_NAMES_NOTHING = frozenset(
    {
        "a", "an", "and", "name", "no", "of", "relationship", "specific", "the", "with",
        # The other half: what a connector writes in the name column when it
        # has nobody to put there. A transcript files its two speakers as
        # `assistant` and `user`, a Telegram export writes `unknown` for a
        # deleted account, and the owner's own side is `me`. The archive holds
        # nine such people and three such projects, and because a transcript
        # role carries every message in the file they outweigh everyone real:
        # "Your goals" opened with `assistant`, and "what am I working on?"
        # answered with it. A word here only removes a name made of nothing
        # but these, so `Agent Smith` and `unknown unknowns` keep theirs.
        "agent", "anonymous", "assistant", "bot", "me", "none", "null",
        "someone", "system", "unknown", "user", "you",
    }
)


def is_a_relationship_label(value: str | None) -> bool:
    """Does this name a relationship rather than something the user set out to do?

    `Relationship with <name>` is a label varyz writes over a thread, not a
    goal anybody wrote down. `names_something` above refuses it where the tail
    is an account, which is most of them; this is the rest, where the tail is a
    person the archive did resolve.
    """
    if not value:
        return False
    words = unicodedata.normalize("NFKC", value).casefold().replace("_", " ").split()
    return bool(words) and words[0] == "relationship" and "with" in words[1:]


def identifies_an_account(value: str | None) -> bool:
    """Is this the account itself -- a phone number, a jid, a numbered handle?

    Not a name, and `names_something` refuses it for that reason.  But it is
    not nothing either: it tells one account from another, which is the whole
    job a name has on a page whose purpose is to let somebody recognise a
    stranger and merge them into the person they already know.  The word a
    connector writes when it has nobody to put in the name column -- `unknown`,
    `me`, `assistant` -- fails both tests, and that is the difference worth
    acting on.
    """

    value = unicodedata.normalize("NFKC", (value or "").strip())
    return bool(_ACCOUNT_SHAPED.match(value))


def names_something(value: str | None) -> bool:
    """Does this name a project or a person, or only the account it was read from?

    A name has to have one word in it that could tell this one from
    another.  A phone number cannot, and neither can "Relationship with" in
    front of one, nor the "No specific name 5" a proposal falls back on when it
    has nothing to call the thing at all.
    Neither can the word a connector writes when it has no name to write:
    `assistant`, `user`, `unknown`, `me`.

    One word that is neither is enough, so anything with real content in it
    survives -- `AUTOMATIC1111`, `brownies 2.0`, `Ab1cd`, `SAN7.14`.  A
    name that is only digits does not, which is the one honest name this
    refuses: a project called `1984` reads as an account here.

    Words are separated by the underscore as well, because half the archive
    writes the same two phrasings in snake case: `relationship_with_34600000001`
    read as one long word with letters in it and was admitted as a goal, on a
    generation where the spaced spelling of the identical name was refused.
    Splitting further only ever removes a name every part of which says
    nothing, so `kurisu_u` and `optimization_task` keep their names.
    """

    value = unicodedata.normalize("NFKC", (value or "").strip())
    # Whole first, because a phone number is often written with the spaces in
    # it and word by word each half of `+34 000 000 000` looks like nothing.
    if _ACCOUNT_SHAPED.match(value):
        return False
    for word in re.split(r"[\s,;:/|_]+", value):
        word = word.strip(".,'\"()[]").lstrip("+")
        if not word or word.isdigit() or _ACCOUNT_SHAPED.match(word):
            continue
        if word.casefold() in _NAMES_NOTHING:
            continue
        return True
    return False


def _tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    normalized = normalize_project_alias(value)
    return {token for token in normalized.split() if len(token) > 2}


def _fingerprint(item: BaseModel) -> str:
    return hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest()


def _pair(left: str, right: str) -> tuple[str, str]:
    first, second = sorted((left, right))
    return first, second


def _membership(item: ProjectEvidenceCandidate | GoalEvidenceCandidate) -> tuple[str, ...]:
    """Dependency membership: extractor records retained for provenance/reuse."""

    return tuple(sorted({*item.observation_ids, *item.claim_ids}))


def _source_membership(
    item: ProjectEvidenceCandidate | GoalEvidenceCandidate,
    observations: Mapping[str, BaseModel],
    claims: Mapping[str, BaseModel],
) -> tuple[str, ...]:
    """Stable semantic membership: underlying source entries, never extractor ids."""

    entry_ids: set[str] = set()
    for observation_id in item.observation_ids:
        observation = observations[observation_id]
        assert isinstance(observation, Observation)
        entry_ids.update(reference.entry_id for reference in observation.evidence)
        entry_ids.update(span.entry_id for span in observation.spans)
    for claim_id in item.claim_ids:
        claim = claims[claim_id]
        assert isinstance(claim, AtomicClaimInput)
        entry_ids.add(claim.span.entry_id)
    return tuple(sorted(entry_ids))


def _goal_kind(kind: ProposedNodeKind) -> GoalKind | None:
    mapping = {
        ProposedNodeKind.INTENDED_OUTCOME: GoalKind.INTENDED_OUTCOME,
        ProposedNodeKind.GOAL: GoalKind.GOAL,
        ProposedNodeKind.WORKSTREAM: GoalKind.WORKSTREAM,
        ProposedNodeKind.SUBGOAL: GoalKind.SUBGOAL,
    }
    return mapping.get(kind)


_REJECTION_BY_KIND = {
    ProposedNodeKind.COMMITMENT: HierarchyRejectionReason.COMMITMENT_REQUIRES_ACTION_LAYER,
    ProposedNodeKind.ACTION: HierarchyRejectionReason.ACTION_NOT_PERSONAL_GOAL,
    ProposedNodeKind.ERRAND: HierarchyRejectionReason.ERRAND_NOT_PERSONAL_GOAL,
    ProposedNodeKind.SETUP_STEP: HierarchyRejectionReason.SETUP_NOT_PERSONAL_GOAL,
    ProposedNodeKind.CONVERSATION_MOVE: (
        HierarchyRejectionReason.CONVERSATION_MOVE_NOT_PERSONAL_GOAL
    ),
    ProposedNodeKind.IMPLEMENTATION_DETAIL: (
        HierarchyRejectionReason.IMPLEMENTATION_DETAIL_NOT_PERSONAL_GOAL
    ),
}


class ProjectHierarchyCompiler:
    """Compile deterministic, precision-first project and goal records."""

    def __init__(self, policy: ProjectPolicy | None = None) -> None:
        self.policy = policy or ProjectPolicy()
        self._producer = ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="varyz-project-hierarchy-compiler",
            version=self.policy.version,
            rule_version=self.policy.version,
        )

    def compile(
        self,
        *,
        generation_id: str,
        observations: Sequence[Observation],
        atomic_claims: Sequence[AtomicClaimInput],
        project_candidates: Sequence[ProjectEvidenceCandidate],
        goal_candidates: Sequence[GoalEvidenceCandidate],
        created_at: datetime,
        overrides: Sequence[ProjectHierarchyOverride] = (),
    ) -> ProjectCompilation:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        observation_index = self._unique_index(observations, "observation")
        claim_index = self._unique_index(atomic_claims, "atomic claim")
        projects = self._candidate_index(project_candidates, "project")
        goals = self._candidate_index(goal_candidates, "goal")
        ordered_overrides = self._ordered_overrides(overrides)

        diagnostics: list[ProjectDiagnostic] = []
        valid_projects = self._evidence_valid_projects(
            projects, observation_index, claim_index, diagnostics
        )
        valid_goals = self._evidence_valid_goals(goals, observation_index, claim_index, diagnostics)

        (
            project_clusters,
            reviews,
            applied,
            unmatched,
            effective_project_overrides,
        ) = self._resolve_projects(
            valid_projects,
            ordered_overrides,
            observation_index,
            claim_index,
            diagnostics,
        )
        (
            project_records,
            project_mapping,
            purpose_changes,
            project_history_facts,
        ) = self._compile_projects(
            project_clusters,
            valid_projects,
            generation_id,
            created_at,
            observation_index,
            claim_index,
            effective_project_overrides,
            diagnostics,
        )
        (
            goal_records,
            goal_mapping,
            rejections,
            action_handoffs,
            status_changes,
            goal_history_facts,
            goal_applied,
            goal_unmatched,
        ) = self._compile_goals(
            valid_goals,
            project_mapping,
            ordered_overrides,
            generation_id,
            created_at,
            observation_index,
            claim_index,
            diagnostics,
        )
        return ProjectCompilation(
            projects=tuple(sorted(project_records, key=lambda item: item.id)),
            goals=tuple(sorted(goal_records, key=lambda item: item.id)),
            review_candidates=tuple(
                sorted(reviews, key=lambda item: (item.left_candidate_id, item.right_candidate_id))
            ),
            quarantines=tuple(
                sorted(
                    diagnostics,
                    key=lambda item: (item.code.value, item.candidate_ids, item.override_id or ""),
                )
            ),
            rejections=tuple(sorted(rejections, key=lambda item: item.candidate_id)),
            action_handoffs=tuple(sorted(action_handoffs, key=lambda item: item.candidate_id)),
            history_facts=tuple(
                sorted((*project_history_facts, *goal_history_facts), key=lambda item: item.id)
            ),
            purpose_changes=tuple(
                sorted(
                    purpose_changes,
                    key=lambda item: (item.project_id, item.changed_at or created_at),
                )
            ),
            status_changes=tuple(
                sorted(
                    status_changes, key=lambda item: (item.goal_id, item.changed_at or created_at)
                )
            ),
            project_candidate_to_project_id=project_mapping,
            goal_candidate_to_goal_id=goal_mapping,
            applied_override_ids=self._ordered_selected_override_ids(
                ordered_overrides, {*applied, *goal_applied}
            ),
            unmatched_override_ids=self._ordered_selected_override_ids(
                ordered_overrides, {*unmatched, *goal_unmatched}
            ),
        )

    @staticmethod
    def _ordered_selected_override_ids(
        overrides: Sequence[ProjectHierarchyOverride], selected: set[str]
    ) -> tuple[str, ...]:
        return tuple(item.override_id for item in overrides if item.override_id in selected)

    @staticmethod
    def _unique_index(items: Sequence[BaseModel], label: str) -> dict[str, BaseModel]:
        result: dict[str, BaseModel] = {}
        serialized: dict[str, str] = {}
        for item in items:
            item_id = str(item.id)
            material = item.model_dump_json()
            if item_id in serialized and serialized[item_id] != material:
                raise ValueError(f"conflicting duplicate {label} id: {item_id}")
            serialized[item_id] = material
            result[item_id] = item
        return result

    @staticmethod
    def _candidate_index(items: Sequence[BaseModel], label: str) -> dict[str, object]:
        result: dict[str, object] = {}
        serialized: dict[str, str] = {}
        for item in items:
            candidate_id = str(item.candidate_id)
            material = item.model_dump_json()
            if candidate_id in serialized and serialized[candidate_id] != material:
                raise ValueError(f"conflicting duplicate {label} candidate id: {candidate_id}")
            serialized[candidate_id] = material
            result[candidate_id] = item
        return result

    @staticmethod
    def _ordered_overrides(
        overrides: Sequence[ProjectHierarchyOverride],
    ) -> tuple[ProjectHierarchyOverride, ...]:
        ordered = tuple(sorted(overrides, key=lambda item: item.sequence))
        if len({item.sequence for item in ordered}) != len(ordered):
            raise ValueError("override sequences must be unique")
        if len({item.override_id for item in ordered}) != len(ordered):
            raise ValueError("override ids must be unique")
        return ordered

    @staticmethod
    def _evidence_valid_projects(
        candidates: Mapping[str, object],
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
        diagnostics: list[ProjectDiagnostic],
    ) -> dict[str, ProjectEvidenceCandidate]:
        result: dict[str, ProjectEvidenceCandidate] = {}
        for candidate_id in sorted(candidates):
            candidate = candidates[candidate_id]
            assert isinstance(candidate, ProjectEvidenceCandidate)
            missing = tuple(
                sorted(
                    {item for item in candidate.observation_ids if item not in observations}
                    | {item for item in candidate.claim_ids if item not in claims}
                )
            )
            if missing:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNKNOWN_EVIDENCE,
                        f"project candidate references absent evidence: {', '.join(missing)}",
                        (candidate_id,),
                    )
                )
                continue
            unresolved = tuple(
                observation_id
                for observation_id in candidate.observation_ids
                if isinstance(observations[observation_id], Observation)
                and (
                    observations[observation_id].speaker_attribution != AttributionState.RESOLVED
                    or observations[observation_id].subject_attribution != AttributionState.RESOLVED
                )
            )
            if unresolved:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNRESOLVED_ATTRIBUTION,
                        "project candidate depends on unresolved observation attribution",
                        (candidate_id, *unresolved),
                    )
                )
                continue
            result[candidate_id] = candidate
        return result

    @staticmethod
    def _evidence_valid_goals(
        candidates: Mapping[str, object],
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
        diagnostics: list[ProjectDiagnostic],
    ) -> dict[str, GoalEvidenceCandidate]:
        result: dict[str, GoalEvidenceCandidate] = {}
        for candidate_id in sorted(candidates):
            candidate = candidates[candidate_id]
            assert isinstance(candidate, GoalEvidenceCandidate)
            if not names_something(candidate.title):
                # The same account names that reached project proposals reach
                # goal titles: seventeen of the owner's hundred and twenty
                # compiled goals were "Relationship with <eleven digits>".  A goal
                # has no alias to fall back on, so there is nothing to call it.
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNNAMED_GOAL,
                        "goal candidate is titled only by the account it was read from",
                        (candidate_id,),
                    )
                )
                continue
            missing = tuple(
                sorted(
                    {item for item in candidate.observation_ids if item not in observations}
                    | {item for item in candidate.claim_ids if item not in claims}
                )
            )
            if missing:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNKNOWN_EVIDENCE,
                        f"goal candidate references absent evidence: {', '.join(missing)}",
                        (candidate_id,),
                    )
                )
                continue
            unresolved = tuple(
                observation_id
                for observation_id in candidate.observation_ids
                if isinstance(observations[observation_id], Observation)
                and (
                    observations[observation_id].speaker_attribution != AttributionState.RESOLVED
                    or observations[observation_id].subject_attribution != AttributionState.RESOLVED
                )
            )
            if unresolved:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNRESOLVED_ATTRIBUTION,
                        "goal candidate depends on unresolved observation attribution",
                        (candidate_id, *unresolved),
                    )
                )
                continue
            result[candidate_id] = candidate
        return result

    def _resolve_projects(
        self,
        candidates: Mapping[str, ProjectEvidenceCandidate],
        overrides: tuple[ProjectHierarchyOverride, ...],
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
        diagnostics: list[ProjectDiagnostic],
    ) -> tuple[
        tuple[tuple[str, ...], ...],
        list[ProjectReviewCandidate],
        list[str],
        list[str],
        dict[str, str],
    ]:
        keys = tuple(sorted(candidates))
        dsu = _DisjointSet(keys)
        forbidden: set[tuple[str, str]] = set()
        reviews: dict[tuple[str, str], ProjectReviewCandidate] = {}
        automatic_edges: list[tuple[str, str]] = []
        applied: list[str] = []
        unmatched: list[str] = []
        latest_by_member: dict[str, str] = {}
        effective_override_by_member: dict[str, str] = {}

        for left, right in combinations(keys, 2):
            score, features, conflict = self._project_score(
                candidates[left], candidates[right], observations, claims
            )
            pair = _pair(left, right)
            if conflict:
                forbidden.add(pair)
            elif score >= self.policy.auto_merge_threshold:
                automatic_edges.append(pair)
            elif score >= self.policy.review_threshold:
                reviews[pair] = ProjectReviewCandidate(
                    left,
                    right,
                    score,
                    features,
                    "plausible shared project; evidence is insufficient for an automatic merge",
                )

        project_overrides = tuple(
            item
            for item in overrides
            if item.action in {ProjectOverrideAction.MERGE, ProjectOverrideAction.SPLIT}
        )
        explicit_merges: list[tuple[str, str, ProjectHierarchyOverride]] = []
        for override in project_overrides:
            members = tuple(item for group in override.project_member_groups for item in group)
            missing = tuple(sorted(set(members) - set(keys)))
            if missing:
                unmatched.append(override.override_id)
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.ORPHANED_OVERRIDE,
                        f"override references absent project candidates: {', '.join(missing)}",
                        missing,
                        override.override_id,
                    )
                )
                continue
            applied.append(override.override_id)
            for member in members:
                prior = latest_by_member.get(member)
                if prior and prior != override.override_id:
                    diagnostics.append(
                        ProjectDiagnostic(
                            ProjectDiagnosticCode.SUPERSEDED_OVERRIDE,
                            f"override {prior} was superseded for this project candidate",
                            (member,),
                            prior,
                        )
                    )
                latest_by_member[member] = override.override_id
            if override.action == ProjectOverrideAction.SPLIT:
                for left_group, right_group in combinations(override.project_member_groups, 2):
                    for left in left_group:
                        for right in right_group:
                            forbidden.add(_pair(left, right))
                            reviews.pop(_pair(left, right), None)
            else:
                for left, right in combinations(override.project_member_groups[0], 2):
                    forbidden.discard(_pair(left, right))
                    explicit_merges.append((*_pair(left, right), override))
                    reviews.pop(_pair(left, right), None)

        for left, right, override in reversed(explicit_merges):
            if (
                latest_by_member.get(left) != override.override_id
                or latest_by_member.get(right) != override.override_id
            ):
                continue
            if not self._accept_project_edge(dsu, left, right, forbidden):
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.OVERRIDE_CONFLICT,
                        "project merge conflicts with a durable split",
                        (left, right),
                        override.override_id,
                    )
                )
            else:
                effective_override_by_member[left] = override.override_id
                effective_override_by_member[right] = override.override_id
        for left, right in sorted(automatic_edges):
            self._accept_project_edge(dsu, left, right, forbidden)

        override_by_id = {item.override_id: item for item in project_overrides}
        for member, override_id in latest_by_member.items():
            if override_by_id[override_id].action == ProjectOverrideAction.SPLIT:
                effective_override_by_member[member] = override_id

        clusters = {tuple(sorted(dsu.members(key))) for key in keys}
        return (
            tuple(sorted(clusters)),
            list(reviews.values()),
            applied,
            unmatched,
            effective_override_by_member,
        )

    def _project_score(
        self,
        left: ProjectEvidenceCandidate,
        right: ProjectEvidenceCandidate,
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
    ) -> tuple[float, tuple[str, ...], bool]:
        features: list[str] = []
        left_ids = defaultdict(set)
        right_ids = defaultdict(set)
        for item in left.identifiers:
            left_ids[item.kind].add(item.canonical)
        for item in right.identifiers:
            right_ids[item.kind].add(item.canonical)
        shared_ids = {value for values in left_ids.values() for value in values} & {
            value for values in right_ids.values() for value in values
        }
        conflicts = any(
            left_ids[kind] and right_ids[kind] and left_ids[kind].isdisjoint(right_ids[kind])
            for kind in set(left_ids) & set(right_ids)
        )
        if conflicts and not shared_ids:
            return 0.0, ("conflicting_stable_identifier",), True
        if shared_ids:
            return 1.0, ("shared_stable_identifier",), False
        if set(_source_membership(left, observations, claims)) & set(
            _source_membership(right, observations, claims)
        ):
            return 1.0, ("shared_evidence_membership",), False

        score = 0.0
        aliases_left = {
            normalize_project_alias(item) for item in (left.display_name, *left.aliases)
        } - {""}
        aliases_right = {
            normalize_project_alias(item) for item in (right.display_name, *right.aliases)
        } - {""}
        if aliases_left & aliases_right:
            score += 0.20
            features.append("shared_normalized_alias")
        if left.explicit_self_reference and right.explicit_self_reference:
            score += 0.15
            features.append("explicit_self_reference")
        left_purpose, right_purpose = _tokens(left.purpose), _tokens(right.purpose)
        if left_purpose and right_purpose:
            similarity = len(left_purpose & right_purpose) / len(left_purpose | right_purpose)
            if similarity >= 0.5:
                score += 0.30
                features.append("shared_purpose")
        if set(left.participant_person_ids) & set(right.participant_person_ids):
            score += 0.15
            features.append("shared_participant")
        if (
            left.observed_at
            and right.observed_at
            and abs(left.observed_at - right.observed_at) <= self.policy.continuity_window
        ):
            score += 0.10
            features.append("time_continuity")
        return min(score, 1.0), tuple(features), False

    @staticmethod
    def _accept_project_edge(
        dsu: _DisjointSet, left: str, right: str, forbidden: set[tuple[str, str]]
    ) -> bool:
        combined = dsu.members(left) | dsu.members(right)
        if any(_pair(first, second) in forbidden for first, second in combinations(combined, 2)):
            return False
        dsu.union(left, right)
        return True

    def _compile_projects(
        self,
        clusters: tuple[tuple[str, ...], ...],
        candidates: Mapping[str, ProjectEvidenceCandidate],
        generation_id: str,
        created_at: datetime,
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
        effective_overrides: Mapping[str, str],
        diagnostics: list[ProjectDiagnostic],
    ) -> tuple[list[Project], dict[str, str], list[ProjectPurposeChange], list[Fact]]:
        records: list[Project] = []
        mapping: dict[str, str] = {}
        changes: list[ProjectPurposeChange] = []
        history_facts: list[Fact] = []
        for members in clusters:
            items = tuple(candidates[item] for item in members)
            ordered = tuple(
                sorted(items, key=lambda item: (item.observed_at or created_at, item.candidate_id))
            )
            latest = ordered[-1]
            source_membership = tuple(
                sorted(
                    {
                        member
                        for item in items
                        for member in _source_membership(item, observations, claims)
                    }
                )
            )
            identifiers = tuple(
                sorted({identifier.canonical for item in items for identifier in item.identifiers})
            )
            value_keys = tuple(sorted({item.canonical_value_key for item in items}))
            canonical_subject = json.dumps(
                {"identity_tokens": identifiers or value_keys},
                sort_keys=True,
                separators=(",", ":"),
            )
            record_id = derived_record_id(
                "project",
                canonical_subject=canonical_subject,
                canonical_predicate="identity",
                evidence_membership=source_membership,
                identity_version=self.policy.version,
            )
            evidence, claim_ids = self._record_evidence(items, observations, claims)
            aliases = self._aliases(items, observations, claims)
            observed = tuple(item.observed_at for item in items if item.observed_at is not None)
            effective_ids = tuple(
                sorted(
                    {
                        effective_overrides[member]
                        for member in members
                        if member in effective_overrides
                    }
                )
            )
            identity_override_id = effective_ids[-1] if effective_ids else None
            display_name = self._display_name(ordered)
            if display_name is None:
                if identity_override_id is not None:
                    # The owner has merged or split this one by hand, so it is
                    # theirs whatever it ended up called.
                    display_name = latest.display_name
                else:
                    diagnostics.append(
                        ProjectDiagnostic(
                            ProjectDiagnosticCode.UNNAMED_PROJECT,
                            "project candidate is named only by the account it was read from",
                            members,
                        )
                    )
                    continue
            record = Project(
                id=record_id,
                generation_id=generation_id,
                evidence=evidence,
                claim_ids=claim_ids,
                temporal=TemporalScope(
                    observed_at=max(observed) if observed else None,
                    valid_from=min(observed) if observed else None,
                    time_precision=TimePrecision.EXACT if observed else TimePrecision.UNKNOWN,
                ),
                confidence=(
                    1.0
                    if identifiers or identity_override_id
                    else 0.9
                    if any(item.explicit_self_reference for item in items)
                    else 0.7
                ),
                sensitivity=self._sensitivity(items),
                derivation=(
                    DerivationKind.USER_CONFIRMED
                    if identity_override_id
                    else DerivationKind.EXPLICIT
                    if any(item.explicit_self_reference for item in items)
                    else DerivationKind.OBSERVED
                ),
                producer=self._producer,
                readiness=Readiness.STAGED,
                created_at=created_at,
                canonical_key=record_id,
                display_name=display_name,
                aliases=aliases,
                life_area=latest.life_area,
                participant_person_ids=tuple(
                    sorted({person for item in items for person in item.participant_person_ids})
                ),
                purpose=latest.purpose,
                state=RecordState.ACTIVE,
                identity_override_id=identity_override_id,
            )
            records.append(record)
            mapping.update({member: record.id for member in members})
            purpose_items = tuple(item for item in ordered if item.purpose)
            purpose_epochs: list[list[ProjectEvidenceCandidate]] = []
            for item in purpose_items:
                if not purpose_epochs or normalize_project_alias(
                    purpose_epochs[-1][-1].purpose or ""
                ) != normalize_project_alias(item.purpose or ""):
                    purpose_epochs.append([item])
                else:
                    purpose_epochs[-1].append(item)
            purpose_fact_ids = tuple(
                derived_record_id(
                    "fact",
                    canonical_subject=json.dumps(
                        {
                            "subject": record.id,
                            "value": normalize_project_alias(epoch[-1].purpose or ""),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    canonical_predicate="project.purpose",
                    evidence_membership=tuple(
                        sorted(
                            {
                                entry_id
                                for item in epoch
                                for entry_id in _source_membership(item, observations, claims)
                            }
                        )
                    ),
                    identity_version=self.policy.version,
                )
                for epoch in purpose_epochs
            )
            for index, epoch in enumerate(purpose_epochs):
                item = epoch[-1]
                item_evidence, item_claim_ids = self._record_evidence(
                    tuple(epoch), observations, claims
                )
                next_time = (
                    purpose_epochs[index + 1][0].observed_at
                    if index + 1 < len(purpose_epochs)
                    else None
                )
                epoch_times = tuple(
                    candidate.observed_at
                    for candidate in epoch
                    if candidate.observed_at is not None
                )
                history_facts.append(
                    Fact(
                        id=purpose_fact_ids[index],
                        generation_id=generation_id,
                        evidence=item_evidence,
                        claim_ids=item_claim_ids,
                        temporal=TemporalScope(
                            observed_at=max(epoch_times) if epoch_times else None,
                            valid_from=min(epoch_times) if epoch_times else None,
                            valid_to=next_time,
                            time_precision=(
                                TimePrecision.EXACT if item.observed_at else TimePrecision.UNKNOWN
                            ),
                        ),
                        confidence=record.confidence,
                        sensitivity=item.sensitivity,
                        derivation=(
                            DerivationKind.USER_CONFIRMED
                            if identity_override_id
                            else DerivationKind.EXPLICIT
                            if item.explicit_self_reference
                            else DerivationKind.OBSERVED
                        ),
                        producer=self._producer,
                        readiness=Readiness.STAGED,
                        created_at=created_at,
                        subject=SubjectReference(kind=EntityKind.PROJECT, id=record.id),
                        predicate="project.purpose",
                        value=TextFactValue(
                            kind=FactValueKind.TEXT,
                            exact_literal=item.purpose or "",
                            normalized=normalize_project_alias(item.purpose or ""),
                        ),
                        status=(
                            FactStatus.ACTIVE
                            if index == len(purpose_epochs) - 1
                            else FactStatus.SUPERSEDED
                        ),
                        supersedes_fact_ids=((purpose_fact_ids[index - 1],) if index > 0 else ()),
                    )
                )
            for previous, current in zip(purpose_items, purpose_items[1:], strict=False):
                if normalize_project_alias(previous.purpose or "") != normalize_project_alias(
                    current.purpose or ""
                ):
                    changes.append(
                        ProjectPurposeChange(
                            project_id=record.id,
                            previous_purpose=previous.purpose or "",
                            current_purpose=current.purpose or "",
                            changed_at=current.observed_at,
                            previous_evidence_membership=_membership(previous),
                            current_evidence_membership=_membership(current),
                        )
                    )
        return records, mapping, changes, history_facts

    def _compile_goals(
        self,
        candidates: Mapping[str, GoalEvidenceCandidate],
        project_mapping: Mapping[str, str],
        overrides: tuple[ProjectHierarchyOverride, ...],
        generation_id: str,
        created_at: datetime,
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
        diagnostics: list[ProjectDiagnostic],
    ) -> tuple[
        list[Goal],
        dict[str, str],
        list[HierarchyRejection],
        list[ActionHandoff],
        list[GoalStatusChange],
        list[Fact],
        list[str],
        list[str],
    ]:
        reparent: dict[str, ProjectHierarchyOverride] = {}
        reclassify: dict[str, ProjectHierarchyOverride] = {}
        applied: list[str] = []
        unmatched: list[str] = []
        for override in overrides:
            if override.action not in {
                ProjectOverrideAction.REPARENT,
                ProjectOverrideAction.RECLASSIFY,
            }:
                continue
            target = override.goal_candidate_id or ""
            references = {target, *override.new_parent_goal_candidate_ids}
            missing = tuple(sorted(references - set(candidates)))
            if missing:
                unmatched.append(override.override_id)
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.ORPHANED_OVERRIDE,
                        f"goal override references absent candidates: {', '.join(missing)}",
                        missing,
                        override.override_id,
                    )
                )
                continue
            destination = (
                reparent if override.action == ProjectOverrideAction.REPARENT else reclassify
            )
            prior = destination.get(target)
            if prior is not None:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.SUPERSEDED_OVERRIDE,
                        f"override {prior.override_id} was superseded for this goal",
                        (target,),
                        prior.override_id,
                    )
                )
            destination[target] = override
            applied.append(override.override_id)

        eligible: dict[str, GoalEvidenceCandidate] = {}
        for key, candidate in candidates.items():
            missing_projects = tuple(
                sorted(set(candidate.project_candidate_ids) - set(project_mapping))
            )
            if missing_projects:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNKNOWN_PROJECT,
                        "goal references absent or quarantined projects: "
                        + ", ".join(missing_projects),
                        (key,),
                    )
                )
                continue
            eligible[key] = candidate

        dsu = _DisjointSet(eligible)
        eligible_order = tuple(sorted(eligible))
        for left, right in combinations(eligible_order, 2):
            left_item, right_item = candidates[left], candidates[right]
            same_key = (
                left_item.continuity_key is not None
                and left_item.continuity_key == right_item.continuity_key
            )
            shared_evidence = bool(
                set(_source_membership(left_item, observations, claims))
                & set(_source_membership(right_item, observations, claims))
            )
            if same_key or shared_evidence:
                dsu.union(left, right)
        all_clusters = tuple(sorted({tuple(sorted(dsu.members(key))) for key in eligible_order}))

        rejections: list[HierarchyRejection] = []
        handoff_candidates: list[tuple[GoalEvidenceCandidate, ProposedNodeKind]] = []
        clusters: list[tuple[tuple[str, ...], GoalKind]] = []
        for members in all_clusters:
            items = tuple(candidates[key] for key in members)
            ordered = tuple(
                sorted(
                    items,
                    key=lambda item: (item.observed_at or created_at, item.candidate_id),
                )
            )
            cluster_overrides = tuple(
                reclassify[item.candidate_id] for item in ordered if item.candidate_id in reclassify
            )
            effective_node_kind = (
                max(cluster_overrides, key=lambda item: item.sequence).new_node_kind
                if cluster_overrides
                else ordered[-1].node_kind
            )
            assert effective_node_kind is not None
            kind = _goal_kind(effective_node_kind)
            if kind is None:
                if effective_node_kind in {
                    ProposedNodeKind.COMMITMENT,
                    ProposedNodeKind.ACTION,
                }:
                    handoff_candidates.extend((item, effective_node_kind) for item in ordered)
                rejections.extend(
                    HierarchyRejection(
                        candidate_id=item.candidate_id,
                        title=item.title,
                        reason=_REJECTION_BY_KIND[effective_node_kind],
                        evidence_membership=_membership(item),
                    )
                    for item in ordered
                )
                continue
            clusters.append((members, kind))

        goal_mapping: dict[str, str] = {}
        cluster_data: dict[
            str, tuple[tuple[str, ...], tuple[GoalEvidenceCandidate, ...], GoalKind]
        ] = {}
        status_changes: list[GoalStatusChange] = []
        history_facts: list[Fact] = []
        for members, kind in clusters:
            items = tuple(candidates[key] for key in members)
            ordered = tuple(
                sorted(items, key=lambda item: (item.observed_at or created_at, item.candidate_id))
            )
            source_membership = tuple(
                sorted(
                    {
                        member
                        for item in items
                        for member in _source_membership(item, observations, claims)
                    }
                )
            )
            project_ids = tuple(
                sorted(
                    {
                        project_mapping[project]
                        for item in items
                        for project in item.project_candidate_ids
                        if project in project_mapping
                    }
                )
            )
            record_id = derived_record_id(
                "goal",
                canonical_subject=json.dumps(
                    {
                        "projects": project_ids or ("unassigned",),
                        "semantic_keys": tuple(
                            sorted(
                                {item.continuity_key or item.canonical_value_key for item in items}
                            )
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                canonical_predicate=kind.value,
                evidence_membership=source_membership,
                identity_version=self.policy.version,
            )
            for member in members:
                goal_mapping[member] = record_id
            cluster_data[record_id] = (members, ordered, kind)
            known_status = tuple(item for item in ordered if item.status != GoalStatus.UNKNOWN)
            for previous, current in zip(known_status, known_status[1:], strict=False):
                if previous.status != current.status:
                    status_changes.append(
                        GoalStatusChange(
                            goal_id=record_id,
                            previous_status=previous.status,
                            current_status=current.status,
                            changed_at=current.observed_at,
                            previous_evidence_membership=_membership(previous),
                            current_evidence_membership=_membership(current),
                        )
                    )

        desired_edges: list[tuple[str, str, str, str]] = []
        for source_id, (members, items, kind) in cluster_data.items():
            del kind
            cluster_reparent_overrides = tuple(
                reparent[item.candidate_id] for item in items if item.candidate_id in reparent
            )
            parent_candidate_ids: set[str] = (
                set(
                    max(
                        cluster_reparent_overrides,
                        key=lambda item: item.sequence,
                    ).new_parent_goal_candidate_ids
                )
                if cluster_reparent_overrides
                else {parent for item in items for parent in item.parent_goal_candidate_ids}
            )
            dependency_candidate_ids: set[str] = set()
            for item in items:
                dependency_candidate_ids.update(item.dependency_goal_candidate_ids)
            for relation, target_ids in (
                ("parent", parent_candidate_ids),
                ("dependency", dependency_candidate_ids),
            ):
                for target_candidate_id in sorted(target_ids):
                    target_id = goal_mapping.get(target_candidate_id)
                    if target_id is None:
                        diagnostics.append(
                            ProjectDiagnostic(
                                ProjectDiagnosticCode.UNKNOWN_GOAL,
                                f"{relation} target is not an accepted goal",
                                (*members, target_candidate_id),
                            )
                        )
                        continue
                    desired_edges.append((source_id, target_id, relation, target_candidate_id))

        accepted_edges: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        graph: dict[str, set[str]] = defaultdict(set)
        for source_id, target_id, relation, target_candidate_id in sorted(desired_edges):
            if relation == "parent" and cluster_data[source_id][2] == GoalKind.INTENDED_OUTCOME:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.INVALID_PARENT,
                        "intended outcome cannot have a parent; parent edge was discarded",
                        (target_candidate_id,),
                    )
                )
                continue
            if source_id == target_id:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.INVALID_PARENT,
                        f"consolidated goal cannot {relation} itself",
                        (target_candidate_id,),
                    )
                )
                continue
            if self._reachable(graph, target_id, source_id):
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.CYCLIC_HIERARCHY,
                        f"discarded {relation} edge that would create a cycle",
                        (source_id, target_id),
                    )
                )
                continue
            graph[source_id].add(target_id)
            accepted_edges[source_id][relation].add(target_id)

        records: list[Goal] = []
        for record_id, (members, ordered, kind) in sorted(cluster_data.items()):
            latest = ordered[-1]
            parents = tuple(sorted(accepted_edges[record_id]["parent"]))
            corrected = any(
                item.candidate_id in reparent or item.candidate_id in reclassify for item in ordered
            )
            project_ids = tuple(
                sorted(
                    {
                        project_mapping[project]
                        for item in ordered
                        for project in item.project_candidate_ids
                        if project in project_mapping
                    }
                )
            )
            missing_projects = tuple(
                sorted(
                    {
                        project
                        for item in ordered
                        for project in item.project_candidate_ids
                        if project not in project_mapping
                    }
                )
            )
            if missing_projects:
                diagnostics.append(
                    ProjectDiagnostic(
                        ProjectDiagnosticCode.UNKNOWN_PROJECT,
                        "goal references absent or quarantined projects: "
                        + ", ".join(missing_projects),
                        members,
                    )
                )
            evidence, claim_ids = self._record_evidence(ordered, observations, claims)
            observed = tuple(item.observed_at for item in ordered if item.observed_at)
            goal_record = Goal(
                id=record_id,
                generation_id=generation_id,
                evidence=evidence,
                claim_ids=claim_ids,
                temporal=TemporalScope(
                    observed_at=max(observed) if observed else None,
                    valid_from=min(observed) if observed else None,
                    time_precision=TimePrecision.EXACT if observed else TimePrecision.UNKNOWN,
                ),
                confidence=(
                    1.0
                    if corrected or any(item.explicit_personal_goal for item in ordered)
                    else 0.75
                ),
                sensitivity=self._sensitivity(ordered),
                derivation=(
                    DerivationKind.USER_CONFIRMED
                    if corrected
                    else DerivationKind.EXPLICIT
                    if any(item.explicit_personal_goal for item in ordered)
                    else DerivationKind.OBSERVED
                ),
                producer=self._producer,
                readiness=Readiness.STAGED,
                created_at=created_at,
                canonical_key=record_id,
                title=latest.title,
                kind=kind,
                status=latest.status,
                project_ids=project_ids,
                parent_goal_ids=parents,
                dependency_goal_ids=tuple(sorted(accepted_edges[record_id]["dependency"])),
                intended_outcome=latest.intended_outcome,
            )
            records.append(goal_record)
            status_items = tuple(item for item in ordered if item.status != GoalStatus.UNKNOWN)
            status_epochs: list[list[GoalEvidenceCandidate]] = []
            for item in status_items:
                if not status_epochs or status_epochs[-1][-1].status != item.status:
                    status_epochs.append([item])
                else:
                    status_epochs[-1].append(item)
            status_fact_ids = tuple(
                derived_record_id(
                    "fact",
                    canonical_subject=json.dumps(
                        {
                            "subject": record_id,
                            "value": epoch[-1].status.value,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    canonical_predicate="goal.status",
                    evidence_membership=tuple(
                        sorted(
                            {
                                entry_id
                                for item in epoch
                                for entry_id in _source_membership(item, observations, claims)
                            }
                        )
                    ),
                    identity_version=self.policy.version,
                )
                for epoch in status_epochs
            )
            for index, epoch in enumerate(status_epochs):
                item = epoch[-1]
                item_evidence, item_claim_ids = self._record_evidence(
                    tuple(epoch), observations, claims
                )
                epoch_times = tuple(
                    candidate.observed_at
                    for candidate in epoch
                    if candidate.observed_at is not None
                )
                next_time = (
                    status_epochs[index + 1][0].observed_at
                    if index + 1 < len(status_epochs)
                    else None
                )
                history_facts.append(
                    Fact(
                        id=status_fact_ids[index],
                        generation_id=generation_id,
                        evidence=item_evidence,
                        claim_ids=item_claim_ids,
                        temporal=TemporalScope(
                            observed_at=max(epoch_times) if epoch_times else None,
                            valid_from=min(epoch_times) if epoch_times else None,
                            valid_to=next_time,
                            time_precision=(
                                TimePrecision.EXACT if epoch_times else TimePrecision.UNKNOWN
                            ),
                        ),
                        confidence=goal_record.confidence,
                        sensitivity=self._sensitivity(epoch),
                        derivation=(
                            DerivationKind.USER_CONFIRMED
                            if corrected
                            else DerivationKind.EXPLICIT
                            if item.explicit_personal_goal
                            else DerivationKind.OBSERVED
                        ),
                        producer=self._producer,
                        readiness=Readiness.STAGED,
                        created_at=created_at,
                        subject=SubjectReference(kind=EntityKind.GOAL, id=record_id),
                        predicate="goal.status",
                        value=TextFactValue(
                            kind=FactValueKind.TEXT,
                            exact_literal=item.status_literal or item.status.value,
                            normalized=item.status.value,
                        ),
                        status=(
                            FactStatus.ACTIVE
                            if index == len(status_epochs) - 1
                            else FactStatus.SUPERSEDED
                        ),
                        supersedes_fact_ids=((status_fact_ids[index - 1],) if index > 0 else ()),
                    )
                )

        action_handoffs = [
            ActionHandoff(
                candidate_id=item.candidate_id,
                title=item.title,
                node_kind=effective_kind,
                observation_ids=item.observation_ids,
                claim_ids=item.claim_ids,
                source_entry_ids=_source_membership(item, observations, claims),
                project_ids=tuple(
                    sorted(
                        project_mapping[project]
                        for project in item.project_candidate_ids
                        if project in project_mapping
                    )
                ),
                parent_goal_ids=tuple(
                    sorted(
                        goal_mapping[parent]
                        for parent in item.parent_goal_candidate_ids
                        if parent in goal_mapping
                    )
                ),
            )
            for item, effective_kind in handoff_candidates
        ]
        return (
            records,
            goal_mapping,
            rejections,
            action_handoffs,
            status_changes,
            history_facts,
            applied,
            unmatched,
        )

    @staticmethod
    def _reachable(graph: Mapping[str, set[str]], start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(sorted(graph.get(current, ())))
        return False

    @staticmethod
    def _record_evidence(
        items: Sequence[ProjectEvidenceCandidate | GoalEvidenceCandidate],
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
    ) -> tuple[tuple[EvidenceReference, ...], tuple[str, ...]]:
        references: dict[str, EvidenceReference] = {}
        claim_ids: set[str] = set()
        for item in items:
            for observation_id in item.observation_ids:
                observation = observations[observation_id]
                assert isinstance(observation, Observation)
                for reference in observation.evidence:
                    pinned = reference.model_copy(update={"observation_id": observation_id})
                    references[pinned.model_dump_json()] = pinned
                claim_ids.update(observation.claim_ids)
            for claim_id in item.claim_ids:
                claim = claims[claim_id]
                assert isinstance(claim, AtomicClaimInput)
                claim_ids.add(claim_id)
                reference = EvidenceReference(
                    entry_id=claim.span.entry_id,
                    role=EvidenceRole.SUPPORTS,
                    claim_id=claim_id,
                    span=claim.span,
                )
                references[reference.model_dump_json()] = reference
        return tuple(references[key] for key in sorted(references)), tuple(sorted(claim_ids))

    @staticmethod
    def _display_name(ordered: Sequence[ProjectEvidenceCandidate]) -> str | None:
        """What to call this project, or None if nothing here names it.

        The name used to be whichever proposal was seen last, and on a real
        archive that put a phone number on twenty-eight per cent of the
        compiled projects -- `<eleven digits>`, `<eleven digits>@s.whatsapp.net`,
        `Relationship with <eleven digits>`, `No specific name 5`.  Nine of them
        were carrying a perfectly good name on an earlier proposal in the same
        cluster -- a handle, three first names -- that recency
        threw away, and with `--include-sensitive` the rest crowded the real
        projects out of the answer to "what am I working on?".

        So: the most recent proposal that names something, then any alias that
        names something, and only then nothing at all.  Recency still decides
        between two real names, which is what it was there for.
        """

        for item in reversed(ordered):
            if names_something(item.display_name):
                return item.display_name
        for item in reversed(ordered):
            for alias in item.aliases:
                if names_something(alias):
                    return alias
        return None

    @staticmethod
    def _aliases(
        items: Sequence[ProjectEvidenceCandidate],
        observations: Mapping[str, BaseModel],
        claims: Mapping[str, BaseModel],
    ) -> tuple[EntityAlias, ...]:
        aliases: dict[tuple[str, str], EntityAlias] = {}
        for item in items:
            for literal in (item.display_name, *item.aliases):
                normalized = normalize_project_alias(literal)
                if not normalized:
                    continue
                entry_ids: set[str] = set()
                for observation_id in item.observation_ids:
                    observation = observations[observation_id]
                    assert isinstance(observation, Observation)
                    entry_ids.update(reference.entry_id for reference in observation.evidence)
                    entry_ids.update(span.entry_id for span in observation.spans)
                for claim_id in item.claim_ids:
                    claim = claims[claim_id]
                    assert isinstance(claim, AtomicClaimInput)
                    entry_ids.add(claim.span.entry_id)
                evidence_entry_ids = tuple(sorted(entry_ids))
                candidate = EntityAlias(
                    kind=AliasKind.NAME,
                    exact_value=literal,
                    normalized_value=normalized,
                    verification=VerificationState.AUTOMATIC
                    if item.explicit_self_reference
                    else VerificationState.CANDIDATE,
                    evidence_entry_ids=evidence_entry_ids,
                )
                aliases[(normalized, literal)] = candidate
        return tuple(aliases[key] for key in sorted(aliases))

    @staticmethod
    def _sensitivity(
        items: Sequence[ProjectEvidenceCandidate | GoalEvidenceCandidate],
    ) -> Sensitivity:
        rank = {
            Sensitivity.PUBLIC: 0,
            Sensitivity.PERSONAL: 1,
            Sensitivity.SENSITIVE: 2,
            Sensitivity.UNCLASSIFIED: 3,
        }
        return max((item.sensitivity for item in items), key=rank.__getitem__)


__all__ = [
    "ActionHandoff",
    "GoalEvidenceCandidate",
    "GoalStatusChange",
    "HierarchyRejection",
    "HierarchyRejectionReason",
    "ProjectCompilation",
    "ProjectDiagnostic",
    "ProjectDiagnosticCode",
    "ProjectEvidenceCandidate",
    "ProjectHierarchyCompiler",
    "ProjectHierarchyOverride",
    "ProjectIdentifier",
    "ProjectIdentifierKind",
    "ProjectOverrideAction",
    "ProjectPolicy",
    "ProjectPurposeChange",
    "ProjectReviewCandidate",
    "ProposedNodeKind",
    "names_something",
    "normalize_project_alias",
]

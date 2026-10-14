from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.domain import Sensitivity
from varyz.personal_model.memory import AtomicClaimInput, atomic_claim_content_hash
from varyz.personal_model.models import (
    AttributionState,
    DerivationKind,
    DialogueContext,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    GoalKind,
    GoalStatus,
    Observation,
    ObservationKind,
    ObservationModality,
    Polarity,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    SubjectReference,
    TextSpan,
)
from varyz.personal_model.projects import (
    GoalEvidenceCandidate,
    HierarchyRejectionReason,
    ProjectDiagnosticCode,
    ProjectEvidenceCandidate,
    ProjectHierarchyCompiler,
    ProjectHierarchyOverride,
    ProjectIdentifier,
    ProjectIdentifierKind,
    ProjectOverrideAction,
    ProposedNodeKind,
    identifies_an_account,
    names_something,
    normalize_project_alias,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
OWNER = SubjectReference(kind=EntityKind.PERSON, id="person-owner")


def claim(
    claim_id: str,
    literal: str | None = None,
    *,
    at: datetime | None = NOW,
    entry_id: str | None = None,
    revision: str | None = None,
):
    literal = literal or claim_id.replace("-", " ")
    return AtomicClaimInput(
        id=claim_id,
        subject=OWNER,
        asserted_by_person_id=OWNER.id,
        span=TextSpan(
            entry_id=entry_id or f"entry-{claim_id}",
            start=0,
            end=len(literal),
            exact_literal=literal,
            source_revision=revision,
        ),
        content_hash=atomic_claim_content_hash(literal, revision),
        occurred_at=at,
    )


def resolved_observation(
    observation_id: str,
    entry_id: str,
    literal: str,
    *,
    revision: str | None = None,
) -> Observation:
    span = TextSpan(
        entry_id=entry_id,
        start=0,
        end=len(literal),
        exact_literal=literal,
        source_revision=revision,
    )
    return Observation(
        id=observation_id,
        generation_id="generation-graph",
        evidence=(EvidenceReference(entry_id=entry_id, role=EvidenceRole.SUPPORTS, span=span),),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.EXPLICIT,
        producer=ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="test-observation",
            version="1",
            rule_version="1",
        ),
        readiness=Readiness.STAGED,
        created_at=NOW,
        episode_id="episode-1",
        kind=ObservationKind.STATEMENT,
        proposition=literal,
        spans=(span,),
        speaker_person_id=OWNER.id,
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=EntityKind.PROJECT,
        subject_id="source-project-varyz",
        subject_attribution=AttributionState.RESOLVED,
        dialogue_context=DialogueContext.VOLUNTEERED_REASONING,
        polarity=Polarity.POSITIVE,
        modality=ObservationModality.ASSERTED,
    )


def project(
    candidate_id: str,
    evidence: str,
    name: str = "Varyz",
    *,
    aliases: tuple[str, ...] = (),
    identifier: str | None = None,
    purpose: str | None = "build a private personal memory",
    participants: tuple[str, ...] = (OWNER.id,),
    at: datetime | None = NOW,
    explicit: bool = True,
) -> ProjectEvidenceCandidate:
    identifiers = (
        (
            ProjectIdentifier(
                kind=ProjectIdentifierKind.REPOSITORY,
                value=identifier,
            ),
        )
        if identifier
        else ()
    )
    return ProjectEvidenceCandidate(
        candidate_id=candidate_id,
        canonical_value_key=normalize_project_alias(name).replace(" ", "-") or candidate_id,
        display_name=name,
        aliases=aliases,
        identifiers=identifiers,
        purpose=purpose,
        life_area="work",
        participant_person_ids=participants,
        explicit_self_reference=explicit,
        claim_ids=(evidence,),
        observed_at=at,
    )


def goal(
    candidate_id: str,
    evidence: str,
    title: str,
    kind: ProposedNodeKind = ProposedNodeKind.GOAL,
    *,
    projects: tuple[str, ...] = ("project-1",),
    parents: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    continuity: str | None = None,
    status: GoalStatus = GoalStatus.ACTIVE,
    at: datetime | None = NOW,
) -> GoalEvidenceCandidate:
    return GoalEvidenceCandidate(
        candidate_id=candidate_id,
        canonical_value_key=candidate_id,
        title=title,
        node_kind=kind,
        status=status,
        status_literal=status.value if status != GoalStatus.UNKNOWN else None,
        continuity_key=continuity,
        project_candidate_ids=projects,
        parent_goal_candidate_ids=parents,
        dependency_goal_candidate_ids=dependencies,
        intended_outcome=title if kind == ProposedNodeKind.INTENDED_OUTCOME else None,
        claim_ids=(evidence,),
        observed_at=at,
        explicit_personal_goal=kind in {ProposedNodeKind.INTENDED_OUTCOME, ProposedNodeKind.GOAL},
    )


def compile_model(
    *,
    projects: tuple[ProjectEvidenceCandidate, ...],
    goals: tuple[GoalEvidenceCandidate, ...] = (),
    claims: tuple[AtomicClaimInput, ...],
    observations: tuple[Observation, ...] = (),
    overrides: tuple[ProjectHierarchyOverride, ...] = (),
):
    return ProjectHierarchyCompiler().compile(
        generation_id="generation-1",
        observations=observations,
        atomic_claims=claims,
        project_candidates=projects,
        goal_candidates=goals,
        created_at=NOW,
        overrides=overrides,
    )


def test_varyz_aliases_with_one_repository_identity_converge() -> None:
    claims = (claim("c1"), claim("c2"))
    candidates = (
        project("p1", "c1", "Varyz", identifier=r"C:\work\varyz"),
        project("p2", "c2", "varyz app", aliases=("Varyz",), identifier="c:/work/varyz/"),
    )

    result = compile_model(projects=candidates, claims=claims)

    assert len(result.projects) == 1
    assert set(result.project_candidate_to_project_id) == {"p1", "p2"}
    assert {alias.exact_value for alias in result.projects[0].aliases} == {"Varyz", "varyz app"}
    assert {item.entry_id for item in result.projects[0].evidence} == {"entry-c1", "entry-c2"}


def test_same_named_unrelated_projects_stay_split_and_are_reviewable() -> None:
    claims = (claim("c1"), claim("c2"))
    candidates = (
        project(
            "p1",
            "c1",
            "Atlas",
            purpose="a finance dashboard",
            participants=("person-a",),
            explicit=False,
        ),
        project(
            "p2",
            "c2",
            "Atlas",
            purpose="a school photography book",
            participants=("person-b",),
            at=NOW - timedelta(days=900),
            explicit=False,
        ),
    )

    result = compile_model(projects=candidates, claims=claims)

    assert len(result.projects) == 2
    assert len(result.review_candidates) == 1
    assert result.review_candidates[0].features == ("shared_normalized_alias",)


def test_contextual_alias_purpose_people_and_time_can_merge_without_name_only_rule() -> None:
    claims = (claim("c1"), claim("c2"))
    candidates = (
        project("p1", "c1", "Varyz", aliases=("life model",)),
        project(
            "p2",
            "c2",
            "Varyz app",
            aliases=("varyz",),
            at=NOW + timedelta(days=30),
        ),
    )

    result = compile_model(projects=candidates, claims=claims)

    assert len(result.projects) == 1


def test_repeated_implementation_tasks_and_actions_are_not_personal_goals() -> None:
    claims = tuple(claim(f"c{i}") for i in range(1, 5))
    candidates = (project("project-1", "c1", identifier="varyz"),)
    goals = (
        goal("g1", "c2", "Add an index", ProposedNodeKind.IMPLEMENTATION_DETAIL),
        goal("g2", "c3", "Add another index", ProposedNodeKind.IMPLEMENTATION_DETAIL),
        goal("g3", "c4", "Run tests", ProposedNodeKind.ACTION),
    )

    result = compile_model(projects=candidates, goals=goals, claims=claims)

    assert result.goals == ()
    assert {item.reason for item in result.rejections} == {
        HierarchyRejectionReason.IMPLEMENTATION_DETAIL_NOT_PERSONAL_GOAL,
        HierarchyRejectionReason.ACTION_NOT_PERSONAL_GOAL,
    }
    assert [item.candidate_id for item in result.action_handoffs] == ["g3"]
    assert result.action_handoffs[0].source_entry_ids == ("entry-c4",)
    assert result.action_handoffs[0].project_ids == (result.projects[0].id,)


def test_one_off_grocery_errand_is_rejected_even_when_repeated() -> None:
    claims = (claim("c1"), claim("c2"), claim("c3"))
    projects = (project("project-1", "c1", identifier="home"),)
    goals = (
        goal("g1", "c2", "Buy oat milk", ProposedNodeKind.ERRAND),
        goal("g2", "c3", "Buy oat milk", ProposedNodeKind.ERRAND),
    )

    result = compile_model(projects=projects, goals=goals, claims=claims)

    assert result.goals == ()
    assert [item.reason for item in result.rejections] == [
        HierarchyRejectionReason.ERRAND_NOT_PERSONAL_GOAL,
        HierarchyRejectionReason.ERRAND_NOT_PERSONAL_GOAL,
    ]
    assert result.action_handoffs == ()


def test_commitment_handoff_keeps_project_parent_and_source_mapping() -> None:
    claims = (claim("c1"), claim("c2"), claim("c3"))
    projects = (project("project-1", "c1", identifier="varyz"),)
    goals = (
        goal("work", "c2", "Memory compiler", ProposedNodeKind.WORKSTREAM),
        goal(
            "commitment",
            "c3",
            "Finish the schema review",
            ProposedNodeKind.COMMITMENT,
            parents=("work",),
        ),
    )

    result = compile_model(projects=projects, goals=goals, claims=claims)

    assert len(result.goals) == len(result.action_handoffs) == 1
    handoff = result.action_handoffs[0]
    assert handoff.node_kind == ProposedNodeKind.COMMITMENT
    assert handoff.project_ids == (result.projects[0].id,)
    assert handoff.parent_goal_ids == (result.goals[0].id,)
    assert handoff.source_entry_ids == ("entry-c3",)


def test_multiple_goals_and_a_goal_served_by_multiple_projects() -> None:
    claims = tuple(claim(f"c{i}") for i in range(1, 7))
    projects = (
        project("project-1", "c1", "Varyz", identifier="varyz"),
        project("project-2", "c2", "Memoir", identifier="memoir"),
    )
    goals = (
        goal("outcome", "c3", "Remember my life", ProposedNodeKind.INTENDED_OUTCOME),
        goal("search", "c4", "Search my life", parents=("outcome",)),
        goal(
            "understand",
            "c5",
            "Understand people",
            projects=("project-1", "project-2"),
            parents=("outcome",),
        ),
        goal(
            "migration",
            "c6",
            "Migrate archive",
            ProposedNodeKind.WORKSTREAM,
            parents=("search",),
            dependencies=("understand",),
        ),
    )

    result = compile_model(projects=projects, goals=goals, claims=claims)

    assert len(result.goals) == 4
    multi = next(item for item in result.goals if item.title == "Understand people")
    assert len(multi.project_ids) == 2
    workstream = next(item for item in result.goals if item.title == "Migrate archive")
    assert workstream.kind == GoalKind.WORKSTREAM
    assert len(workstream.parent_goal_ids) == len(workstream.dependency_goal_ids) == 1


def test_project_purpose_and_goal_status_changes_are_temporal_and_provenanced() -> None:
    claims = tuple(claim(f"c{i}") for i in range(1, 5))
    projects = (
        project(
            "p-old",
            "c1",
            identifier="varyz",
            purpose="search my archive",
            at=NOW - timedelta(days=90),
        ),
        project(
            "p-new",
            "c2",
            "Varyz personal model",
            identifier="varyz",
            purpose="understand my life and relationships",
            at=NOW,
        ),
    )
    goals = (
        goal(
            "g-old",
            "c3",
            "Build a life search",
            projects=("p-old",),
            continuity="life-model",
            status=GoalStatus.ACTIVE,
            at=NOW - timedelta(days=90),
        ),
        goal(
            "g-new",
            "c4",
            "Build a deep life model",
            projects=("p-new",),
            continuity="life-model",
            status=GoalStatus.COMPLETED,
            at=NOW,
        ),
    )

    result = compile_model(projects=projects, goals=goals, claims=claims)

    assert len(result.projects) == len(result.goals) == 1
    assert result.projects[0].purpose == "understand my life and relationships"
    assert result.purpose_changes[0].previous_evidence_membership == ("c1",)
    assert result.purpose_changes[0].current_evidence_membership == ("c2",)
    assert result.goals[0].status == GoalStatus.COMPLETED
    assert result.status_changes[0].previous_status == GoalStatus.ACTIVE
    assert result.status_changes[0].current_status == GoalStatus.COMPLETED
    assert {item.predicate for item in result.history_facts} == {
        "project.purpose",
        "goal.status",
    }
    assert len(result.history_facts) == 4
    active_facts = [item for item in result.history_facts if item.status.value == "active"]
    assert len(active_facts) == 2
    assert all(item.supersedes_fact_ids for item in active_facts)
    assert all(not item.id.startswith("observation") for item in result.history_facts)


def test_parent_and_dependency_cycles_are_not_emitted() -> None:
    claims = tuple(claim(f"c{i}") for i in range(1, 4))
    projects = (project("project-1", "c1", identifier="varyz"),)
    goals = (
        goal("g1", "c2", "First", parents=("g2",)),
        goal("g2", "c3", "Second", dependencies=("g1",)),
    )

    result = compile_model(projects=projects, goals=goals, claims=claims)

    edges = {item.id: {*item.parent_goal_ids, *item.dependency_goal_ids} for item in result.goals}
    assert not any(
        source in edges.get(target, set())
        for source, targets in edges.items()
        for target in targets
    )
    assert any(item.code == ProjectDiagnosticCode.CYCLIC_HIERARCHY for item in result.quarantines)


def test_project_merge_then_later_split_override_replays_deterministically() -> None:
    claims = (claim("c1"), claim("c2"))
    projects = (
        project("p1", "c1", "Atlas", purpose="finance", explicit=False),
        project("p2", "c2", "Atlas", purpose="photos", explicit=False),
    )
    merge = ProjectHierarchyOverride(
        sequence=1,
        override_id="merge-atlas",
        action=ProjectOverrideAction.MERGE,
        project_member_groups=(("p1", "p2"),),
    )
    split = ProjectHierarchyOverride(
        sequence=2,
        override_id="split-atlas",
        action=ProjectOverrideAction.SPLIT,
        project_member_groups=(("p1",), ("p2",)),
    )

    merged = compile_model(projects=projects, claims=claims, overrides=(merge,))
    split_result = compile_model(projects=projects, claims=claims, overrides=(merge, split))

    assert len(merged.projects) == 1
    assert merged.projects[0].derivation == DerivationKind.USER_CONFIRMED
    assert merged.projects[0].identity_override_id == "merge-atlas"
    assert len(split_result.projects) == 2
    assert split_result.applied_override_ids == ("merge-atlas", "split-atlas")
    assert any(
        item.code == ProjectDiagnosticCode.SUPERSEDED_OVERRIDE for item in split_result.quarantines
    )


def test_later_merge_can_supersede_an_earlier_split() -> None:
    claims = (claim("c1"), claim("c2"))
    projects = (
        project("p1", "c1", "Atlas", purpose="finance", explicit=False),
        project("p2", "c2", "Atlas", purpose="photos", explicit=False),
    )
    split = ProjectHierarchyOverride(
        sequence=1,
        override_id="split-atlas",
        action=ProjectOverrideAction.SPLIT,
        project_member_groups=(("p1",), ("p2",)),
    )
    merge = ProjectHierarchyOverride(
        sequence=2,
        override_id="merge-atlas",
        action=ProjectOverrideAction.MERGE,
        project_member_groups=(("p1", "p2"),),
    )

    result = compile_model(projects=projects, claims=claims, overrides=(split, merge))

    assert len(result.projects) == 1


def test_reparent_and_reclassify_overrides_apply_without_promoting_repetition() -> None:
    claims = tuple(claim(f"c{i}") for i in range(1, 5))
    projects = (project("project-1", "c1", identifier="varyz"),)
    goals = (
        goal("outcome", "c2", "Understand my life", ProposedNodeKind.INTENDED_OUTCOME),
        goal("work", "c3", "Build compiler", ProposedNodeKind.WORKSTREAM),
        goal("candidate", "c4", "Preserve birthdays", ProposedNodeKind.ACTION),
    )
    reparent = ProjectHierarchyOverride(
        sequence=1,
        override_id="parent-work",
        action=ProjectOverrideAction.REPARENT,
        goal_candidate_id="work",
        new_parent_goal_candidate_ids=("outcome",),
    )
    reclassify = ProjectHierarchyOverride(
        sequence=2,
        override_id="promote-birthdays",
        action=ProjectOverrideAction.RECLASSIFY,
        goal_candidate_id="candidate",
        new_node_kind=ProposedNodeKind.SUBGOAL,
    )

    result = compile_model(
        projects=projects,
        goals=goals,
        claims=claims,
        overrides=(reparent, reclassify),
    )

    assert result.rejections == ()
    work = next(item for item in result.goals if item.title == "Build compiler")
    promoted = next(item for item in result.goals if item.title == "Preserve birthdays")
    assert len(work.parent_goal_ids) == 1
    assert promoted.kind == GoalKind.SUBGOAL
    assert result.applied_override_ids == ("parent-work", "promote-birthdays")


def test_correction_targets_the_consolidated_goal_across_status_mentions() -> None:
    claims = tuple(claim(f"c{i}") for i in range(1, 6))
    projects = (project("project-1", "c1", identifier="varyz"),)
    goals = (
        goal("old-parent", "c2", "Old outcome", ProposedNodeKind.INTENDED_OUTCOME),
        goal("new-parent", "c3", "New outcome", ProposedNodeKind.INTENDED_OUTCOME),
        goal(
            "goal-old",
            "c4",
            "Ship Varyz",
            parents=("old-parent",),
            continuity="ship-varyz",
            at=NOW - timedelta(days=30),
        ),
        goal(
            "goal-new",
            "c5",
            "Release the personal model",
            parents=("old-parent",),
            continuity="ship-varyz",
            at=NOW,
        ),
    )
    overrides = (
        ProjectHierarchyOverride(
            sequence=1,
            override_id="reparent-consolidated",
            action=ProjectOverrideAction.REPARENT,
            goal_candidate_id="goal-old",
            new_parent_goal_candidate_ids=("new-parent",),
        ),
        ProjectHierarchyOverride(
            sequence=2,
            override_id="reclassify-consolidated",
            action=ProjectOverrideAction.RECLASSIFY,
            goal_candidate_id="goal-old",
            new_node_kind=ProposedNodeKind.SUBGOAL,
        ),
    )

    result = compile_model(projects=projects, goals=goals, claims=claims, overrides=overrides)

    consolidated = next(item for item in result.goals if item.title == "Release the personal model")
    new_parent = next(item for item in result.goals if item.title == "New outcome")
    assert consolidated.kind == GoalKind.SUBGOAL
    assert consolidated.parent_goal_ids == (new_parent.id,)
    assert consolidated.derivation == DerivationKind.USER_CONFIRMED


def test_orphan_override_is_diagnosed_without_mutating_valid_records() -> None:
    claims = (claim("c1"),)
    projects = (project("p1", "c1", identifier="varyz"),)
    override = ProjectHierarchyOverride(
        sequence=1,
        override_id="missing",
        action=ProjectOverrideAction.MERGE,
        project_member_groups=(("p1", "absent"),),
    )

    result = compile_model(projects=projects, claims=claims, overrides=(override,))

    assert len(result.projects) == 1
    assert result.unmatched_override_ids == ("missing",)
    assert result.quarantines[0].code == ProjectDiagnosticCode.ORPHANED_OVERRIDE


def test_input_order_does_not_change_records_or_decisions() -> None:
    claims = (claim("c1"), claim("c2"), claim("c3"))
    projects = (
        project("p1", "c1", identifier="varyz"),
        project("p2", "c2", "Varyz app", identifier="varyz"),
    )
    goals = (goal("g1", "c3", "Build personal model", projects=("p2",)),)

    forward = compile_model(projects=projects, goals=goals, claims=claims)
    reverse = compile_model(
        projects=tuple(reversed(projects)),
        goals=tuple(reversed(goals)),
        claims=tuple(reversed(claims)),
    )

    assert forward.projects == reverse.projects
    assert forward.goals == reverse.goals
    assert forward.project_candidate_to_project_id == reverse.project_candidate_to_project_id


def test_rewording_display_text_does_not_change_project_or_goal_identity() -> None:
    claims = (claim("c1"), claim("c2"))
    original_project = project("p1", "c1", "Varyz", identifier="varyz")
    rewritten_project = original_project.model_copy(
        update={"display_name": "My personal-model application"}
    )
    original_goal = goal("g1", "c2", "Build a deeper understanding", projects=("p1",))
    rewritten_goal = original_goal.model_copy(update={"title": "Understand my life deeply"})

    original = compile_model(projects=(original_project,), goals=(original_goal,), claims=claims)
    rewritten = compile_model(projects=(rewritten_project,), goals=(rewritten_goal,), claims=claims)

    assert original.projects[0].id == rewritten.projects[0].id
    assert original.goals[0].id == rewritten.goals[0].id


def test_new_extractor_ids_and_revisions_over_same_source_entry_keep_domain_ids() -> None:
    old_observation = resolved_observation(
        "project-observation-v1",
        "source-entry-project",
        "Varyz is my private memory",
        revision="r1",
    )
    new_observation = resolved_observation(
        "project-observation-v2",
        "source-entry-project",
        "Varyz is my private personal model",
        revision="r2",
    )
    old_claims = (
        claim(
            "goal-claim-v1",
            "I want Varyz to understand my life",
            entry_id="source-entry-goal",
            revision="r1",
        ),
    )
    new_claims = (
        claim(
            "goal-claim-v2",
            "I want a deep model of my life",
            entry_id="source-entry-goal",
            revision="r2",
        ),
    )
    old_project = project(
        "extractor-project-v1",
        "unused",
        "Varyz",
        identifier="varyz",
    ).model_copy(update={"claim_ids": (), "observation_ids": (old_observation.id,)})
    new_project = project(
        "extractor-project-v2",
        "unused",
        "Varyz personal model",
        identifier="varyz",
    ).model_copy(update={"claim_ids": (), "observation_ids": (new_observation.id,)})
    old_goal = goal(
        "extractor-goal-v1",
        "goal-claim-v1",
        "Understand my life",
        projects=("extractor-project-v1",),
        continuity="deep-life-model",
    ).model_copy(update={"canonical_value_key": "deep-life-model"})
    new_goal = goal(
        "extractor-goal-v2",
        "goal-claim-v2",
        "Build a deep personal model",
        projects=("extractor-project-v2",),
        continuity="deep-life-model",
    ).model_copy(update={"canonical_value_key": "deep-life-model"})

    old = compile_model(
        projects=(old_project,),
        goals=(old_goal,),
        claims=old_claims,
        observations=(old_observation,),
    )
    new = compile_model(
        projects=(new_project,),
        goals=(new_goal,),
        claims=new_claims,
        observations=(new_observation,),
    )

    assert old.projects[0].id == new.projects[0].id
    assert old.goals[0].id == new.goals[0].id
    assert old.projects[0].evidence[0].observation_id != new.projects[0].evidence[0].observation_id
    assert old.projects[0].evidence[0].span.source_revision == "r1"
    assert new.projects[0].evidence[0].span.source_revision == "r2"


def test_genuine_canonical_value_or_source_membership_change_changes_identity() -> None:
    same_entry_claim = claim("c1", entry_id="source-entry")
    other_entry_claim = claim("c2", entry_id="other-source-entry")
    base = project("p1", "c1", "Atlas", identifier=None, explicit=False)
    changed_value = base.model_copy(update={"canonical_value_key": "different-atlas"})
    changed_source = base.model_copy(update={"candidate_id": "p2", "claim_ids": ("c2",)})

    original = compile_model(projects=(base,), claims=(same_entry_claim,))
    value_changed = compile_model(projects=(changed_value,), claims=(same_entry_claim,))
    source_changed = compile_model(projects=(changed_source,), claims=(other_entry_claim,))

    assert original.projects[0].id != value_changed.projects[0].id
    assert original.projects[0].id != source_changed.projects[0].id

    goal_claim = claim("c3", entry_id="goal-source-entry")
    other_goal_claim = claim("c4", entry_id="other-goal-source-entry")
    project_claim = claim("c5", entry_id="project-source-entry")
    project_candidate = project("project-1", "c5", identifier="varyz")
    base_goal = goal("g1", "c3", "Understand my life").model_copy(
        update={"canonical_value_key": "understand-life"}
    )
    changed_goal_value = base_goal.model_copy(update={"canonical_value_key": "publish-life"})
    changed_goal_source = base_goal.model_copy(update={"candidate_id": "g2", "claim_ids": ("c4",)})

    original_goal = compile_model(
        projects=(project_candidate,),
        goals=(base_goal,),
        claims=(project_claim, goal_claim),
    )
    value_changed_goal = compile_model(
        projects=(project_candidate,),
        goals=(changed_goal_value,),
        claims=(project_claim, goal_claim),
    )
    source_changed_goal = compile_model(
        projects=(project_candidate,),
        goals=(changed_goal_source,),
        claims=(project_claim, other_goal_claim),
    )

    assert original_goal.goals[0].id != value_changed_goal.goals[0].id
    assert original_goal.goals[0].id != source_changed_goal.goals[0].id


def test_unknown_evidence_is_quarantined_and_not_compiled() -> None:
    candidate = project("p1", "missing", identifier="varyz")

    result = compile_model(projects=(candidate,), claims=())

    assert result.projects == ()
    assert result.quarantines[0].code == ProjectDiagnosticCode.UNKNOWN_EVIDENCE


def test_resolved_observation_provenance_is_carried_to_project_record() -> None:
    literal = "Varyz is my personal model"
    observation = Observation(
        id="observation-1",
        generation_id="generation-graph",
        evidence=(
            EvidenceReference(
                entry_id="entry-1",
                role=EvidenceRole.SUPPORTS,
                span=TextSpan(
                    entry_id="entry-1",
                    start=0,
                    end=len(literal),
                    exact_literal=literal,
                ),
            ),
        ),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.EXPLICIT,
        producer=ProducerMetadata(
            kind=ProducerKind.DETERMINISTIC,
            name="test-observation",
            version="1",
            rule_version="1",
        ),
        readiness=Readiness.STAGED,
        created_at=NOW,
        episode_id="episode-1",
        kind=ObservationKind.STATEMENT,
        proposition=literal,
        spans=(
            TextSpan(
                entry_id="entry-1",
                start=0,
                end=len(literal),
                exact_literal=literal,
            ),
        ),
        speaker_person_id=OWNER.id,
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=EntityKind.PROJECT,
        subject_id="source-project-varyz",
        subject_attribution=AttributionState.RESOLVED,
        dialogue_context=DialogueContext.VOLUNTEERED_REASONING,
        polarity=Polarity.POSITIVE,
        modality=ObservationModality.ASSERTED,
    )
    candidate = ProjectEvidenceCandidate(
        candidate_id="p1",
        canonical_value_key="varyz",
        display_name="Varyz",
        identifiers=(ProjectIdentifier(kind=ProjectIdentifierKind.PRODUCT, value="Varyz"),),
        explicit_self_reference=True,
        observation_ids=(observation.id,),
        observed_at=NOW,
    )

    result = compile_model(projects=(candidate,), observations=(observation,), claims=())

    reference = result.projects[0].evidence[0]
    assert reference.entry_id == "entry-1"
    assert reference.observation_id == observation.id
    assert reference.span is not None and reference.span.exact_literal == literal

    unresolved = observation.model_copy(
        update={
            "speaker_person_id": None,
            "speaker_attribution": AttributionState.UNKNOWN,
        }
    )
    quarantined = compile_model(projects=(candidate,), observations=(unresolved,), claims=())
    assert quarantined.projects == ()
    assert quarantined.quarantines[0].code == ProjectDiagnosticCode.UNRESOLVED_ATTRIBUTION


def test_a_project_the_archive_also_named_keeps_the_name_over_the_phone_number() -> None:
    """Recency picked the name, and on a real archive recency picked an account.

    Twenty-eight per cent of the compiled projects on the owner's archive were
    called things like `34900000001@s.whatsapp.net`, and nine of those were
    carrying a perfectly good name -- a handle, two first names -- on an
    earlier proposal in the same cluster.
    """

    claims = (claim("c1"), claim("c2"))
    candidates = (
        project(
            "p1",
            "c1",
            "Robin",
            identifier="c:/chats/robin",
            at=NOW - timedelta(days=3),
        ),
        project("p2", "c2", "34900000001@s.whatsapp.net", identifier="c:/chats/robin/", at=NOW),
    )

    result = compile_model(projects=candidates, claims=claims)

    assert len(result.projects) == 1
    assert result.projects[0].display_name == "Robin"
    # The account is still on the record; it just is not what the project is
    # called.
    assert "34900000001@s.whatsapp.net" in {
        alias.exact_value for alias in result.projects[0].aliases
    }


def test_a_project_nothing_names_is_quarantined_rather_than_called_by_an_account() -> None:
    """With `--include-sensitive` these crowded the real projects out.

    `varyz model lookup "What am I working on?"` answered with twenty rows that
    were almost all phone numbers, and Varyz, Adastra and SAN7 did not fit in
    the limit behind them.
    """

    claims = (claim("c1"), claim("c2"))
    candidates = (
        project(
            "p1",
            "c1",
            "34900000002",
            aliases=("Relationship with 34900000002",),
            purpose=None,
            explicit=False,
        ),
        project("p2", "c2", "No specific name 5", purpose=None, explicit=False),
    )

    result = compile_model(projects=candidates, claims=claims)

    assert result.projects == ()
    assert result.quarantines
    assert {item.code for item in result.quarantines} == {
        ProjectDiagnosticCode.UNNAMED_PROJECT
    }


def test_a_goal_titled_only_by_an_account_is_not_a_goal() -> None:
    """Seventeen of the owner's hundred and twenty goals were one of these.

    A goal has no alias to fall back on the way a project does, so there is
    nothing else to call it.
    """

    claims = (claim("c1"), claim("c2"), claim("c3"))
    candidates = (project("p1", "c1", "Varyz"),)
    goals = (
        goal("g1", "c2", "ship the alpha", projects=("p1",)),
        goal("g2", "c3", "Relationship with 34900000002", projects=("p1",)),
    )

    result = compile_model(projects=candidates, goals=goals, claims=claims)

    assert [item.title for item in result.goals] == ["ship the alpha"]
    assert [item.code for item in result.quarantines] == [ProjectDiagnosticCode.UNNAMED_GOAL]


def test_a_name_with_anything_in_it_is_a_name() -> None:
    for value in (
        "Varyz",
        "AUTOMATIC1111",
        "brownies 2.0",
        "Ab1cd",
        "SAN7.14",
        "ана",
        # Splitting on the underscore must not cost a name that has one.
        "kurisu_u",
        "optimization_task",
        "love_for_isabel",
    ):
        assert names_something(value), value
    for value in (
        "34900000002",
        "+34 900 000 002",
        "34900000001@s.whatsapp.net",
        "100000000000002@lid",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd",
        "user900000001",
        "Relationship with 34900000003",
        # The same name in snake case. Written this way it read as one long
        # word with letters in it, and was admitted on the same generation
        # where the spaced spelling of it was refused.
        "relationship_with_34900000003",
        "relationship_with_0123456789abcdef0123456789abcdef",
        "No specific name 5",
        "",
        None,
    ):
        assert not names_something(value), value


def test_a_number_a_platform_printed_half_of_is_still_a_number() -> None:
    """Two people in the archive are called `+34XXXXXXX62`, in bullets.

    A platform that will show a number but not all of it substitutes a mask
    character for the digits it holds back, and that was enough content for
    the number to read as a name: both sorted above every person on "People",
    which is the one thing the sort there exists to prevent.
    """

    for mask in ("∙", "•", "·", "*"):
        value = "+34" + mask * 7 + "62"
        assert not names_something(value), value
        assert identifies_an_account(value), value

    # A mask stands in for a digit, so it only ever costs a name that was a
    # number already. Nothing with a word in it is touched.
    for value in ("3·5 goals", "R2-D2", "*starred*", "SAN7.14"):
        assert names_something(value), value


def test_the_word_a_connector_writes_for_nobody_is_not_a_name() -> None:
    for value in (
        "assistant",
        "Assistant",
        "user",
        "unknown",
        "me",
        "agent",
        "system",
        "bot",
        "none",
        "someone",
        "you",
    ):
        assert not names_something(value), value
    # A placeholder word only costs a name made of nothing else.
    for value in ("Agent Smith", "unknown unknowns", "user research", "Me and Ana"):
        assert names_something(value), value

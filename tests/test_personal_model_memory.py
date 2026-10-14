from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from varyz.domain import Sensitivity, TimePrecision
from varyz.personal_model.memory import (
    AtomicClaimInput,
    EventCandidate,
    FactCandidate,
    MemoryCompiler,
    PinnedEntityReference,
    PreferenceBasis,
    PreferenceCandidate,
    QuarantineReason,
    atomic_claim_content_hash,
    normalize_date_literal,
)
from varyz.personal_model.models import (
    AttributionState,
    DateAmbiguityKind,
    DateFactValue,
    DerivationKind,
    DialogueContext,
    EntityKind,
    FactStatus,
    FactValueKind,
    MonthDayFactValue,
    Observation,
    ObservationKind,
    ObservationModality,
    Polarity,
    PreferenceValence,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    SubjectReference,
    TemporalScope,
    TextSpan,
)

NOW = datetime(2026, 8, 11, 10, tzinfo=UTC)
ANNA = SubjectReference(kind=EntityKind.PERSON, id="person-anna")
EGOR = SubjectReference(kind=EntityKind.PERSON, id="person-egor")
PROJECT = SubjectReference(kind=EntityKind.PROJECT, id="project-varyz")
PRODUCER = ProducerMetadata(
    kind=ProducerKind.DETERMINISTIC,
    name="test-observation",
    version="1",
    rule_version="1",
)


def observation(
    observation_id: str,
    text: str,
    *,
    subject: SubjectReference = ANNA,
    speaker: str = "person-anna",
    stated_at: datetime = NOW,
    revision: str | None = "r1",
    context: DialogueContext = DialogueContext.VOLUNTEERED_REASONING,
    modality: ObservationModality = ObservationModality.ASSERTED,
    polarity: Polarity = Polarity.POSITIVE,
    kind: ObservationKind = ObservationKind.STATEMENT,
    episode: str | None = None,
    entry_id: str | None = None,
    addressees: tuple[str, ...] = (),
) -> Observation:
    entry_id = entry_id or f"entry-{observation_id}"
    span = TextSpan(
        entry_id=entry_id,
        start=0,
        end=len(text),
        exact_literal=text,
        source_revision=revision,
    )
    return Observation(
        id=observation_id,
        generation_id="observations-g1",
        evidence=(),
        claim_ids=(f"source-claim-{observation_id}",),
        temporal=TemporalScope(
            stated_at=stated_at,
            time_precision=TimePrecision.EXACT,
            original_time_literal=stated_at.isoformat(),
        ),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.OBSERVED,
        producer=PRODUCER,
        readiness=Readiness.STAGED,
        created_at=stated_at,
        episode_id=episode or f"episode-{observation_id}",
        kind=kind,
        proposition=text,
        spans=(span,),
        speaker_person_id=speaker,
        speaker_source_key=f"id:{speaker}",
        speaker_attribution=AttributionState.RESOLVED,
        subject_kind=subject.kind,
        subject_id=subject.id,
        subject_surface=subject.id,
        subject_attribution=AttributionState.RESOLVED,
        addressee_person_ids=addressees,
        dialogue_context=context,
        polarity=polarity,
        modality=modality,
    )


def preference_candidate(
    observation_id: str,
    literal: str,
    *,
    predicate: str,
    valence: PreferenceValence = PreferenceValence.LIKE,
    basis: PreferenceBasis = PreferenceBasis.EXPLICIT,
    subject: SubjectReference = ANNA,
    asserted_by: str = "person-anna",
    extra_observation_ids: tuple[str, ...] = (),
) -> PreferenceCandidate:
    return PreferenceCandidate(
        kind="preference",
        subject=subject,
        predicate=predicate,
        exact_object_literal=literal,
        valence=valence,
        basis=basis,
        observation_ids=(observation_id, *extra_observation_ids),
        asserted_by_person_id=asserted_by,
    )


def compile_memory(
    observations: tuple[Observation, ...],
    candidates: tuple[FactCandidate | PreferenceCandidate | EventCandidate, ...],
    *,
    episode_independence_groups: dict[str, str] | None = None,
    atomic_claims: tuple[AtomicClaimInput, ...] = (),
    entity_references: tuple[PinnedEntityReference, ...] = (),
):
    return MemoryCompiler().compile(
        generation_id="memory-g1",
        observations=observations,
        candidates=candidates,
        created_at=NOW,
        atomic_claims=atomic_claims,
        entity_references=entity_references,
        episode_independence_groups=episode_independence_groups,
    )


def test_explicit_preferences_preserve_annas_peonies_and_sushi() -> None:
    peonies = observation("peonies", "Anna likes peonies.")
    sushi = observation("sushi", "Anna likes sushi.")

    result = compile_memory(
        (sushi, peonies),
        (
            preference_candidate("sushi", "sushi", predicate="preference.food"),
            preference_candidate("peonies", "peonies", predicate="preference.flower"),
        ),
    )

    assert not result.quarantines
    assert {
        (item.predicate, item.object.exact_literal, item.object.normalized)
        for item in result.preferences
    } == {
        ("preference.flower", "peonies", "peonies"),
        ("preference.food", "sushi", "sushi"),
    }
    assert all(item.readiness == Readiness.STAGED for item in result.preferences)


@pytest.mark.parametrize(
    ("text", "context", "modality", "expected_reason"),
    [
        (
            "Anna mentioned sushi at lunch.",
            DialogueContext.VOLUNTEERED_REASONING,
            ObservationModality.ASSERTED,
            QuarantineReason.MENTION_IS_NOT_PREFERENCE,
        ),
        (
            'Egor said "Anna likes sushi."',
            DialogueContext.QUOTATION,
            ObservationModality.QUOTED,
            QuarantineReason.CONTEXT_DISALLOWS_PREFERENCE,
        ),
        (
            "If Anna liked sushi, we could order it.",
            DialogueContext.HYPOTHETICAL,
            ObservationModality.HYPOTHETICAL,
            QuarantineReason.CONTEXT_DISALLOWS_PREFERENCE,
        ),
        (
            "Sure, Anna likes sushi, if you say so.",
            DialogueContext.PROMPTED_AGREEMENT,
            ObservationModality.ASSERTED,
            QuarantineReason.CONTEXT_DISALLOWS_PREFERENCE,
        ),
        (
            "Tell the agent that Anna likes sushi.",
            DialogueContext.INSTRUCTION,
            ObservationModality.ASSERTED,
            QuarantineReason.CONTEXT_DISALLOWS_PREFERENCE,
        ),
    ],
)
def test_mentions_quotes_hypotheticals_and_polite_or_instructed_agreement_are_not_preferences(
    text: str,
    context: DialogueContext,
    modality: ObservationModality,
    expected_reason: QuarantineReason,
) -> None:
    source = observation("unsafe", text, context=context, modality=modality)
    result = compile_memory(
        (source,),
        (preference_candidate("unsafe", "sushi", predicate="preference.food"),),
    )

    assert not result.preferences
    assert result.quarantines[0].reason == expected_reason


def test_negated_like_cannot_be_compiled_as_positive_preference() -> None:
    source = observation("negated", "Anna doesn't like sushi.", polarity=Polarity.NEGATIVE)

    wrong = compile_memory(
        (source,),
        (preference_candidate("negated", "sushi", predicate="preference.food"),),
    )
    correct = compile_memory(
        (source,),
        (
            preference_candidate(
                "negated",
                "sushi",
                predicate="preference.food",
                valence=PreferenceValence.DISLIKE,
            ),
        ),
    )

    assert wrong.quarantines[0].reason == QuarantineReason.MENTION_IS_NOT_PREFERENCE
    assert correct.preferences[0].valence == PreferenceValence.DISLIKE


def test_reported_preference_stays_with_subject_and_retains_reporter() -> None:
    report = observation(
        "report",
        "Anna likes sushi.",
        subject=ANNA,
        speaker="person-egor",
    )
    candidate = preference_candidate(
        "report",
        "sushi",
        predicate="preference.food",
        asserted_by="person-egor",
    )

    result = compile_memory((report,), (candidate,))
    wrongly_reassigned = compile_memory(
        (report,),
        (candidate.model_copy(update={"subject": EGOR, "asserted_by_person_id": "person-egor"}),),
    )

    assert result.preferences[0].subject == ANNA
    assert result.preferences[0].asserted_by_person_id == "person-egor"
    assert wrongly_reassigned.quarantines[0].reason == QuarantineReason.UNSUPPORTED_SUBJECT


def test_inferred_preference_requires_choice_or_reaction_across_two_episodes() -> None:
    first = observation(
        "choice-1",
        "Anna chose sushi again.",
        kind=ObservationKind.CHOICE,
        episode="episode-a",
    )
    second = observation(
        "choice-2",
        "Anna ordered sushi for dinner.",
        kind=ObservationKind.CHOICE,
        episode="episode-b",
    )
    candidate = preference_candidate(
        "choice-1",
        "sushi",
        predicate="preference.food",
        basis=PreferenceBasis.INFERRED,
        extra_observation_ids=("choice-2",),
    )

    result = compile_memory(
        (first, second),
        (candidate,),
        episode_independence_groups={
            "episode-a": "decision-a",
            "episode-b": "decision-b",
        },
    )
    insufficient = compile_memory(
        (first,),
        (candidate.model_copy(update={"observation_ids": ("choice-1",)}),),
        episode_independence_groups={"episode-a": "decision-a"},
    )

    assert result.preferences[0].derivation == DerivationKind.INFERRED
    assert result.preferences[0].confidence == 0.7
    assert insufficient.quarantines[0].reason == QuarantineReason.INSUFFICIENT_INFERRED_BASIS


def test_overlapping_subepisodes_from_one_decision_do_not_inflate_inferred_preference() -> None:
    first = observation(
        "overlap-1",
        "Anna chose sushi again.",
        kind=ObservationKind.CHOICE,
        episode="subepisode-a",
    )
    second = observation(
        "overlap-2",
        "Anna ordered sushi for dinner.",
        kind=ObservationKind.CHOICE,
        episode="subepisode-b",
    )
    candidate = preference_candidate(
        "overlap-1",
        "sushi",
        predicate="preference.food",
        basis=PreferenceBasis.INFERRED,
        extra_observation_ids=("overlap-2",),
    )

    result = compile_memory(
        (first, second),
        (candidate,),
        episode_independence_groups={
            "subepisode-a": "shared-decision",
            "subepisode-b": "shared-decision",
        },
    )

    assert not result.preferences
    assert result.quarantines[0].reason == QuarantineReason.INSUFFICIENT_INFERRED_BASIS


def test_date_normalizer_supports_full_month_day_and_multilingual_literals() -> None:
    english = normalize_date_literal("February 29, 2024")
    spanish = normalize_date_literal("29 de febrero de 2024")
    russian = normalize_date_literal("29 февраля 2024")
    unknown_year = normalize_date_literal("29 February")

    assert isinstance(english, DateFactValue) and english.value == date(2024, 2, 29)
    assert isinstance(spanish, DateFactValue) and spanish.value == date(2024, 2, 29)
    assert isinstance(russian, DateFactValue) and russian.value == date(2024, 2, 29)
    assert isinstance(unknown_year, MonthDayFactValue)
    assert (unknown_year.month, unknown_year.day) == (2, 29)


def test_numeric_date_without_locale_preserves_both_interpretations() -> None:
    value = normalize_date_literal("03/04/2025")

    assert isinstance(value, DateFactValue)
    assert value.value is None
    assert value.ambiguity is not None
    assert value.ambiguity.kind == DateAmbiguityKind.NUMERIC_ORDER
    assert set(value.ambiguity.candidates) == {date(2025, 3, 4), date(2025, 4, 3)}


def test_next_friday_uses_aware_source_timestamp_locale_and_timezone() -> None:
    # The local date, rather than the timestamp's displayed offset, controls resolution.
    source_time = datetime(2026, 8, 13, 21, 30, tzinfo=UTC)
    resolved = normalize_date_literal(
        "next Friday",
        source_timestamp=source_time,
        locale="en-GB",
        timezone="Europe/Madrid",
    )
    unresolved = normalize_date_literal("next Friday", source_timestamp=source_time)

    assert isinstance(resolved, DateFactValue) and resolved.value == date(2026, 8, 14)
    assert isinstance(unresolved, DateFactValue)
    assert unresolved.ambiguity is not None
    assert unresolved.ambiguity.kind == DateAmbiguityKind.RELATIVE


def birthday_candidate(observation_id: str, literal: str) -> FactCandidate:
    return FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.birth_date",
        value_kind=FactValueKind.DATE,
        exact_value_literal=literal,
        observation_ids=(observation_id,),
        asserted_by_person_id="person-anna",
        locale="en-GB",
        timezone="Europe/Madrid",
    )


def test_conflicting_birthdays_are_both_retained_and_contradicted() -> None:
    first = observation("birthday-a", "Anna's birthday is 2 March 1994.")
    second = observation("birthday-b", "Anna's birthday is 3 March 1994.")

    result = compile_memory(
        (first, second),
        (
            birthday_candidate("birthday-a", "2 March 1994"),
            birthday_candidate("birthday-b", "3 March 1994"),
        ),
    )

    assert len(result.facts) == 2
    assert {item.status for item in result.facts} == {FactStatus.CONTRADICTED}
    assert all(len(item.contradicted_by_fact_ids) == 1 for item in result.facts)
    assert all(not item.eligible for item in result.readiness)


def test_negated_or_uncertain_birthday_is_not_compiled_as_exact_fact() -> None:
    negated = observation(
        "not-birthday",
        "Anna's birthday is not 2 March 1994.",
        polarity=Polarity.NEGATIVE,
    )
    uncertain = observation(
        "maybe-birthday",
        "Anna's birthday may be 2 March 1994.",
        modality=ObservationModality.UNCERTAIN,
    )

    result = compile_memory(
        (negated, uncertain),
        (
            birthday_candidate("not-birthday", "2 March 1994"),
            birthday_candidate("maybe-birthday", "2 March 1994"),
        ),
    )

    assert not result.facts
    assert {item.reason for item in result.quarantines} == {
        QuarantineReason.CONTEXT_DISALLOWS_LITERAL_MEMORY
    }


def location_candidate(observation_id: str, location: str) -> FactCandidate:
    return FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.current_location",
        value_kind=FactValueKind.LOCATION,
        exact_value_literal=location,
        observation_ids=(observation_id,),
        asserted_by_person_id="person-anna",
    )


def test_new_current_location_supersedes_old_without_erasing_history() -> None:
    old_time = datetime(2024, 1, 1, 12, tzinfo=UTC)
    new_time = datetime(2025, 2, 1, 12, tzinfo=UTC)
    old = observation("old-home", "Anna lives in London.", stated_at=old_time)
    new = observation("new-home", "Anna moved to Madrid.", stated_at=new_time)

    result = compile_memory(
        (new, old),
        (location_candidate("new-home", "Madrid"), location_candidate("old-home", "London")),
    )
    by_value = {item.value.exact_literal: item for item in result.facts}

    assert by_value["Madrid"].status == FactStatus.ACTIVE
    assert by_value["London"].status == FactStatus.SUPERSEDED
    assert by_value["London"].temporal.valid_to == new_time
    assert by_value["London"].id in by_value["Madrid"].supersedes_fact_ids


def test_location_history_uses_each_real_change_boundary_instead_of_averaging() -> None:
    london = observation(
        "home-london",
        "Anna lives in London.",
        stated_at=datetime(2023, 1, 1, 12, tzinfo=UTC),
    )
    paris = observation(
        "home-paris",
        "Anna moved to Paris.",
        stated_at=datetime(2024, 1, 1, 12, tzinfo=UTC),
    )
    madrid = observation(
        "home-madrid",
        "Anna moved to Madrid.",
        stated_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
    )

    result = compile_memory(
        (madrid, london, paris),
        (
            location_candidate("home-madrid", "Madrid"),
            location_candidate("home-london", "London"),
            location_candidate("home-paris", "Paris"),
        ),
    )
    by_value = {item.value.exact_literal: item for item in result.facts}

    assert by_value["London"].temporal.valid_to == datetime(2024, 1, 1, 12, tzinfo=UTC)
    assert by_value["Paris"].temporal.valid_to == datetime(2025, 1, 1, 12, tzinfo=UTC)
    assert by_value["Madrid"].status == FactStatus.ACTIVE
    assert by_value["London"].id in by_value["Paris"].supersedes_fact_ids
    assert by_value["Paris"].id in by_value["Madrid"].supersedes_fact_ids


def test_preference_change_is_temporal_supersession_not_an_average() -> None:
    old = observation(
        "liked-sushi",
        "Anna likes sushi.",
        stated_at=datetime(2024, 1, 1, 12, tzinfo=UTC),
    )
    new = observation(
        "disliked-sushi",
        "Anna doesn't like sushi.",
        stated_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        polarity=Polarity.NEGATIVE,
    )
    liked = preference_candidate("liked-sushi", "sushi", predicate="preference.food")
    disliked = preference_candidate(
        "disliked-sushi",
        "sushi",
        predicate="preference.food",
        valence=PreferenceValence.DISLIKE,
    )

    result = compile_memory((new, old), (disliked, liked))
    by_valence = {item.valence: item for item in result.preferences}

    assert by_valence[PreferenceValence.LIKE].status == FactStatus.SUPERSEDED
    assert by_valence[PreferenceValence.DISLIKE].status == FactStatus.ACTIVE
    assert by_valence[PreferenceValence.LIKE].temporal.valid_to == datetime(
        2025, 1, 1, 12, tzinfo=UTC
    )
    assert by_valence[PreferenceValence.LIKE].strength == 0.75


def test_provider_normalization_and_invented_exact_fact_are_quarantined() -> None:
    source = observation("birthday", "Anna's name is Anna; her birthday is 2 March 1994.")
    invented = birthday_candidate("birthday", "3 March 1994")
    normalized = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.full_name",
        value_kind=FactValueKind.TEXT,
        exact_value_literal="Anna",
        proposed_normalized_value="anna smith",
        observation_ids=("birthday",),
        asserted_by_person_id="person-anna",
    )

    result = compile_memory((source,), (invented, normalized))

    assert not result.facts
    assert {item.reason for item in result.quarantines} == {
        QuarantineReason.NORMALIZATION_NOT_ALLOWED,
        QuarantineReason.UNSUPPORTED_LITERAL,
    }


def test_event_never_gets_birthday_label_without_birthday_evidence() -> None:
    source = observation("date-only", "Anna will be in Madrid on 2 March 2027.")
    candidate = EventCandidate(
        kind="event",
        event_kind="birthday",
        title_literal="Anna",
        time_literal="2 March 2027",
        observation_ids=("date-only",),
        asserted_by_person_id="person-anna",
        participant_ids=("person-anna",),
        location_literal="Madrid",
        locale="en-GB",
        timezone="Europe/Madrid",
    )

    result = compile_memory((source,), (candidate,))

    assert not result.events
    assert result.quarantines[0].reason == QuarantineReason.UNSUPPORTED_LITERAL


def test_hypothetical_event_is_not_stored_as_history() -> None:
    source = observation(
        "hypothetical-trip",
        "If Anna takes a trip to Madrid on 2 March 2027, it could be fun.",
        context=DialogueContext.HYPOTHETICAL,
        modality=ObservationModality.HYPOTHETICAL,
    )
    candidate = EventCandidate(
        kind="event",
        event_kind="trip",
        title_literal="trip to Madrid",
        time_literal="2 March 2027",
        observation_ids=("hypothetical-trip",),
        asserted_by_person_id="person-anna",
        participant_ids=("person-anna",),
        location_literal="Madrid",
    )

    result = compile_memory((source,), (candidate,))

    assert not result.events
    assert result.quarantines[0].reason == QuarantineReason.CONTEXT_DISALLOWS_LITERAL_MEMORY


def test_event_keeps_only_supported_people_projects_location_and_time() -> None:
    source = observation(
        "deadline",
        "Egor promised the Varyz deadline meeting with Anna in Madrid on 2 March 2027.",
        subject=PROJECT,
        speaker="person-egor",
        addressees=("person-anna",),
    )
    candidate = EventCandidate(
        kind="event",
        event_kind="meeting",
        title_literal="deadline meeting",
        time_literal="2 March 2027",
        observation_ids=("deadline",),
        asserted_by_person_id="person-egor",
        participant_ids=("person-egor", "person-anna"),
        project_ids=("project-varyz",),
        location_literal="Madrid",
        locale="en-GB",
        timezone="Europe/Madrid",
    )

    result = compile_memory((source,), (candidate,))

    event = result.events[0]
    assert event.participant_ids == ("person-anna", "person-egor")
    assert event.project_ids == ("project-varyz",)
    assert event.location is not None and event.location.exact_literal == "Madrid"
    assert event.temporal.valid_from is not None
    assert event.temporal.original_time_literal == "2 March 2027"


def test_reworded_observation_keeps_semantic_identity_across_new_ids_and_revision() -> None:
    original = observation(
        "preference-before",
        "Anna likes sushi.",
        revision="edit-1",
        entry_id="entry-stable-preference",
    )
    reworded = observation(
        "preference-after",
        "Anna really likes sushi.",
        revision="edit-2",
        entry_id="entry-stable-preference",
    )
    before = preference_candidate("preference-before", "sushi", predicate="preference.food")
    after = preference_candidate("preference-after", "sushi", predicate="preference.food")

    original_result = compile_memory((original,), (before,))
    reworded_result = compile_memory((reworded,), (after,))

    assert original_result.preferences[0].id == reworded_result.preferences[0].id
    assert original_result.preferences[0].evidence[0].observation_id != (
        reworded_result.preferences[0].evidence[0].observation_id
    )
    assert original_result.preferences[0].evidence[0].span.source_revision == "edit-1"
    assert reworded_result.preferences[0].evidence[0].span.source_revision == "edit-2"


def test_edited_birthday_keeps_fact_identity_when_entry_and_value_are_same() -> None:
    first = observation(
        "birthday-before",
        "Anna's birthday is 2 March 1994.",
        revision="revision-1",
        entry_id="entry-stable-birthday",
    )
    edited = observation(
        "birthday-after",
        "Anna was born on 2 March 1994.",
        revision="revision-2",
        entry_id="entry-stable-birthday",
    )

    first_result = compile_memory(
        (first,), (birthday_candidate("birthday-before", "2 March 1994"),)
    )
    edited_result = compile_memory(
        (edited,), (birthday_candidate("birthday-after", "2 March 1994"),)
    )

    assert first_result.facts[0].id == edited_result.facts[0].id


def test_reworded_event_keeps_identity_with_same_entry_kind_and_normalized_title() -> None:
    before = observation(
        "event-before",
        "Anna's birthday dinner is on 2 March 2027.",
        revision="r1",
        entry_id="entry-stable-event",
    )
    after = observation(
        "event-after",
        "On 2 March 2027, Anna has a birthday dinner.",
        revision="r2",
        entry_id="entry-stable-event",
    )

    def candidate(observation_id: str) -> EventCandidate:
        return EventCandidate(
            kind="event",
            event_kind="birthday",
            title_literal="birthday dinner",
            time_literal="2 March 2027",
            observation_ids=(observation_id,),
            asserted_by_person_id="person-anna",
            participant_ids=("person-anna",),
        )

    before_result = compile_memory((before,), (candidate("event-before"),))
    after_result = compile_memory((after,), (candidate("event-after"),))

    assert before_result.events[0].id == after_result.events[0].id


def atomic_claim(
    claim_id: str,
    *,
    text: str,
    entry_id: str,
    start: int,
    revision: str,
) -> AtomicClaimInput:
    span = TextSpan(
        entry_id=entry_id,
        start=start,
        end=start + len(text),
        exact_literal=text,
        source_revision=revision,
    )
    return AtomicClaimInput(
        id=claim_id,
        subject=ANNA,
        asserted_by_person_id="person-anna",
        span=span,
        content_hash=atomic_claim_content_hash(text, revision),
        occurred_at=NOW,
        sensitivity=Sensitivity.PERSONAL,
        dialogue_context=DialogueContext.DIRECT_ANSWER,
        modality=ObservationModality.ASSERTED,
        polarity=Polarity.POSITIVE,
    )


def test_atomic_claim_preserves_real_nonzero_source_offset() -> None:
    claim = atomic_claim(
        "age-claim",
        text="29 years old",
        entry_id="entry-age",
        start=8,
        revision="r1",
    )
    candidate = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.age",
        value_kind=FactValueKind.NUMBER,
        exact_value_literal="29 years",
        claim_ids=("age-claim",),
        asserted_by_person_id="person-anna",
    )

    result = compile_memory((), (candidate,), atomic_claims=(claim,))

    assert result.facts[0].evidence[0].span.start == 8
    assert result.facts[0].evidence[0].span.end == 20
    assert result.facts[0].value.value == 29
    assert result.facts[0].value.unit == "years"


def test_changed_claim_id_revision_and_wording_keep_same_entry_semantic_identity() -> None:
    before_claim = atomic_claim(
        "claim-before",
        text="29 years old",
        entry_id="entry-stable-age",
        start=8,
        revision="r1",
    )
    after_claim = atomic_claim(
        "claim-after",
        text="aged 29 years",
        entry_id="entry-stable-age",
        start=5,
        revision="r2",
    )
    before_candidate = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.age",
        value_kind=FactValueKind.NUMBER,
        exact_value_literal="29 years",
        claim_ids=("claim-before",),
        asserted_by_person_id="person-anna",
    )
    after_candidate = before_candidate.model_copy(update={"claim_ids": ("claim-after",)})

    before = compile_memory((), (before_candidate,), atomic_claims=(before_claim,))
    after = compile_memory((), (after_candidate,), atomic_claims=(after_claim,))

    assert before.facts[0].id == after.facts[0].id
    assert before.facts[0].claim_ids != after.facts[0].claim_ids


def test_number_boolean_and_entity_ref_values_are_deterministically_typed() -> None:
    height = observation("height", "Anna's height is 180 cm.")
    vegetarian = observation(
        "vegetarian",
        "Anna is not vegetarian.",
        polarity=Polarity.NEGATIVE,
    )
    partner = observation(
        "partner",
        "Anna's partner is Egor.",
        addressees=("person-egor",),
    )
    candidates = (
        FactCandidate(
            kind="fact",
            subject=ANNA,
            predicate="person.height_cm",
            value_kind=FactValueKind.NUMBER,
            exact_value_literal="180 cm",
            observation_ids=("height",),
            asserted_by_person_id="person-anna",
        ),
        FactCandidate(
            kind="fact",
            subject=ANNA,
            predicate="person.is_vegetarian",
            value_kind=FactValueKind.BOOLEAN,
            exact_value_literal="not vegetarian",
            observation_ids=("vegetarian",),
            asserted_by_person_id="person-anna",
        ),
        FactCandidate(
            kind="fact",
            subject=ANNA,
            predicate="person.partner",
            value_kind=FactValueKind.ENTITY_REF,
            exact_value_literal="Egor",
            observation_ids=("partner",),
            asserted_by_person_id="person-anna",
            entity_ref_kind=EntityKind.PERSON,
            entity_ref_id="person-egor",
        ),
    )
    entity_reference = PinnedEntityReference(
        entity=SubjectReference(kind=EntityKind.PERSON, id="person-egor"),
        span=TextSpan(
            entry_id="entry-partner",
            start=18,
            end=22,
            exact_literal="Egor",
            source_revision="r1",
        ),
        resolver_version="identity-v1",
    )

    result = compile_memory(
        (height, vegetarian, partner),
        candidates,
        entity_references=(entity_reference,),
    )
    by_predicate = {item.predicate: item.value for item in result.facts}

    assert by_predicate["person.height_cm"].value == 180
    assert by_predicate["person.height_cm"].unit == "cm"
    assert by_predicate["person.is_vegetarian"].value is False
    assert by_predicate["person.partner"].entity_id == "person-egor"


def test_malformed_number_and_unpinned_entity_ref_are_quarantined() -> None:
    height = observation("bad-height", "Anna's height is 1,80.")
    partner = observation("unresolved-partner", "Anna's partner is Egor.")
    malformed_number = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.height_cm",
        value_kind=FactValueKind.NUMBER,
        exact_value_literal="1,80",
        observation_ids=("bad-height",),
        asserted_by_person_id="person-anna",
    )
    unpinned_entity = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.partner",
        value_kind=FactValueKind.ENTITY_REF,
        exact_value_literal="Egor",
        observation_ids=("unresolved-partner",),
        asserted_by_person_id="person-anna",
        entity_ref_kind=EntityKind.PERSON,
        entity_ref_id="person-egor",
    )

    result = compile_memory((height, partner), (malformed_number, unpinned_entity))

    assert not result.facts
    assert {item.reason for item in result.quarantines} == {
        QuarantineReason.UNSUPPORTED_LITERAL,
        QuarantineReason.VALUE_KIND_NOT_ALLOWED,
    }


def test_typed_parser_rejects_wrong_unit_and_positive_boolean_cut_from_negation() -> None:
    height = observation("wrong-unit", "Anna's height is 180 kg.")
    vegetarian = observation(
        "negated-boolean",
        "Anna is not vegetarian.",
        polarity=Polarity.NEGATIVE,
    )
    wrong_unit = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.height_cm",
        value_kind=FactValueKind.NUMBER,
        exact_value_literal="180 kg",
        observation_ids=("wrong-unit",),
        asserted_by_person_id="person-anna",
    )
    cut_negation = FactCandidate(
        kind="fact",
        subject=ANNA,
        predicate="person.is_vegetarian",
        value_kind=FactValueKind.BOOLEAN,
        exact_value_literal="vegetarian",
        observation_ids=("negated-boolean",),
        asserted_by_person_id="person-anna",
    )

    result = compile_memory((height, vegetarian), (wrong_unit, cut_negation))

    assert not result.facts
    assert {item.reason for item in result.quarantines} == {
        QuarantineReason.CONTEXT_DISALLOWS_LITERAL_MEMORY,
        QuarantineReason.VALUE_KIND_NOT_ALLOWED,
    }


def test_raw_provider_payload_with_open_field_is_typed_quarantine() -> None:
    source = observation("raw", "Anna likes sushi.")
    raw = {
        "kind": "preference",
        "subject": {"kind": "person", "id": "person-anna"},
        "predicate": "preference.food",
        "exact_object_literal": "sushi",
        "valence": "like",
        "basis": "explicit",
        "observation_ids": ["raw"],
        "asserted_by_person_id": "person-anna",
        "provider_explanation": "unbounded prose is forbidden",
    }

    result = MemoryCompiler().compile_raw(
        generation_id="memory-g1",
        observations=(source,),
        candidates=(raw,),
        created_at=NOW,
    )

    assert not result.preferences
    assert result.quarantines[0].reason == QuarantineReason.MALFORMED_CANDIDATE


def test_valid_closed_raw_provider_payload_compiles() -> None:
    source = observation("raw-valid", "Anna likes sushi.")
    raw = {
        "kind": "preference",
        "subject": {"kind": "person", "id": "person-anna"},
        "predicate": "preference.food",
        "exact_object_literal": "sushi",
        "valence": "like",
        "basis": "explicit",
        "observation_ids": ["raw-valid"],
        "asserted_by_person_id": "person-anna",
    }

    result = MemoryCompiler().compile_raw(
        generation_id="memory-g1",
        observations=(source,),
        candidates=(raw,),
        created_at=NOW,
    )

    assert not result.quarantines
    assert result.preferences[0].object.exact_literal == "sushi"


def test_candidate_and_evidence_input_order_do_not_change_compilation() -> None:
    peonies = observation("order-peonies", "Anna likes peonies.")
    sushi = observation("order-sushi", "Anna likes sushi.")
    candidates = (
        preference_candidate("order-peonies", "peonies", predicate="preference.flower"),
        preference_candidate("order-sushi", "sushi", predicate="preference.food"),
    )

    forward = compile_memory((peonies, sushi), candidates)
    reverse = compile_memory(tuple(reversed((peonies, sushi))), tuple(reversed(candidates)))

    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_one_uncontradicted_explicit_exact_fact_is_reported_eligible_but_stays_staged() -> None:
    source = observation("one-birthday", "Anna's birthday is 2 March 1994.")

    result = compile_memory(
        (source,),
        (birthday_candidate("one-birthday", "2 March 1994"),),
    )

    assert result.facts[0].readiness == Readiness.STAGED
    assert result.readiness[0].eligible
    assert result.readiness[0].reasons == ()

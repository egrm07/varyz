"""Trimming the windows over a message to what the owner's day can pay for."""

from __future__ import annotations

from datetime import UTC, datetime

from varyz.domain import Sensitivity
from varyz.personal_model.episodes import EpisodeBuildResult, TargetEpisodeCoverage
from varyz.personal_model.models import (
    ContextCompleteness,
    ConversationEpisode,
    DerivationKind,
    EvidenceReference,
    EvidenceRole,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    SourceTurn,
)
from varyz.personal_model.observations import (
    AnnotationBudget,
    episodes_within_budget,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
PRODUCER = ProducerMetadata(
    kind=ProducerKind.LOCAL_MODEL,
    name="fixture-proposer",
    version="1",
    model_name="fixture-model",
    prompt_version="fixture-prompt-v1",
)


def _episode(episode_id: str, entry_ids: tuple[str, ...]) -> ConversationEpisode:
    return ConversationEpisode(
        id=episode_id,
        generation_id="generation-1",
        evidence=(
            EvidenceReference(
                entry_id=entry_ids[0],
                role=EvidenceRole.DEFINES_SCOPE,
                episode_id=episode_id,
                sensitivity=Sensitivity.PERSONAL,
            ),
        ),
        confidence=1.0,
        sensitivity=Sensitivity.PERSONAL,
        derivation=DerivationKind.OBSERVED,
        producer=PRODUCER,
        readiness=Readiness.STAGED,
        created_at=NOW,
        source="fixture-chat",
        source_thread_id="thread",
        turns=tuple(
            SourceTurn(entry_id=entry_id, order=index, occurred_at=None)
            for index, entry_id in enumerate(entry_ids)
        ),
        initiating_entry_id=entry_ids[0],
        target_entry_id=entry_ids[0],
        completeness=ContextCompleteness.COMPLETE,
        boundary_policy_version="fixture-v1",
    )


def _result(
    episodes: tuple[ConversationEpisode, ...],
    coverage: dict[str, tuple[str, ...]],
) -> EpisodeBuildResult:
    return EpisodeBuildResult(
        episodes=episodes,
        target_coverage=tuple(
            TargetEpisodeCoverage(
                target_entry_id=target,
                episode_ids=episode_ids,
                covered_entry_ids=(target,),
                omitted_entry_ids=(),
                quarantined=False,
            )
            for target, episode_ids in coverage.items()
        ),
        effective_boundary_policy_version="fixture-v1",
    )


def _archive(targets: int, windows: int) -> EpisodeBuildResult:
    """``targets`` messages, each read in ``windows`` episodes of its own."""

    episodes: list[ConversationEpisode] = []
    coverage: dict[str, tuple[str, ...]] = {}
    for target in range(targets):
        ids = []
        for window in range(windows):
            episode_id = f"e{target:03d}-{window}"
            ids.append(episode_id)
            # Later windows are longer, so "longest" and "first" differ.
            episodes.append(
                _episode(
                    episode_id,
                    (f"t{target:03d}",) + tuple(f"c{window}-{n}" for n in range(window)),
                )
            )
        coverage[f"t{target:03d}"] = tuple(ids)
    return _result(tuple(episodes), coverage)


def test_a_budget_that_covers_the_archive_keeps_every_window():
    """Two windows over a message share about one turn in seventy.

    They are different conversational moments, not one moment read twice, so
    when they can be afforded they are all worth reading.
    """

    result = _archive(targets=10, windows=3)

    kept, windows = episodes_within_budget(
        result, AnnotationBudget(hours=8.0, episodes_per_hour=1000.0)
    )

    assert len(kept) == 30
    assert windows == 0


def test_no_budget_at_all_keeps_every_window():
    result = _archive(targets=10, windows=3)

    assert episodes_within_budget(result, None) == (tuple(result.episodes), 0)
    assert episodes_within_budget(result, AnnotationBudget(hours=0)) == (
        tuple(result.episodes),
        0,
    )


def test_a_budget_that_does_not_cover_it_trims_the_windows_to_fit():
    result = _archive(targets=10, windows=3)

    # Twenty-five episodes buys two windows each, not three.
    kept, windows = episodes_within_budget(
        result, AnnotationBudget(hours=1.0, episodes_per_hour=25.0)
    )

    assert windows == 2
    assert len(kept) == 20


def test_every_message_keeps_at_least_one_window():
    """Below one window each, the budget stops the reading, not this.

    Coverage order makes what does get read a sample of the whole archive, so
    dropping messages here as well would only make the sample worse.
    """

    result = _archive(targets=10, windows=3)

    kept, windows = episodes_within_budget(
        result, AnnotationBudget(hours=1.0, episodes_per_hour=4.0)
    )

    assert windows == 1
    assert len(kept) == 10
    assert {episode.target_entry_id for episode in kept} == {
        f"t{index:03d}" for index in range(10)
    }


def test_the_windows_kept_are_the_longest_ones():
    """Context is the whole reason an episode is bigger than its target."""

    result = _archive(targets=4, windows=3)

    kept, _ = episodes_within_budget(
        result, AnnotationBudget(hours=1.0, episodes_per_hour=4.0)
    )

    assert {episode.id for episode in kept} == {f"e{index:03d}-2" for index in range(4)}


def test_a_window_shared_with_a_quiet_message_is_not_thrown_away():
    """The choice cannot be made a message at a time.

    One episode is usually the window for more than one message. Trimming a
    talkative message's third window must not take with it the only window a
    quiet message has.
    """

    episodes = (
        _episode("long", ("loud", "a", "b", "c")),
        _episode("mid", ("loud", "d", "e")),
        _episode("shared", ("loud", "quiet")),
    )
    result = _result(
        episodes,
        {"loud": ("long", "mid", "shared"), "quiet": ("shared",)},
    )

    kept, windows = episodes_within_budget(
        result, AnnotationBudget(hours=1.0, episodes_per_hour=2.0)
    )

    assert windows == 1
    # "long" is the loud message's longest window, and "shared" is kept anyway
    # because it is the only one the quiet message has.
    assert {episode.id for episode in kept} == {"long", "shared"}


def test_the_same_archive_and_budget_always_choose_the_same_reading():
    result = _archive(targets=6, windows=3)
    reversed_result = _result(
        tuple(reversed(result.episodes)),
        {row.target_entry_id: row.episode_ids for row in result.target_coverage},
    )
    budget = AnnotationBudget(hours=1.0, episodes_per_hour=8.0)

    first, windows = episodes_within_budget(result, budget)
    second, other = episodes_within_budget(reversed_result, budget)

    assert windows == other
    assert [episode.id for episode in first] == [episode.id for episode in second]


def test_an_episode_with_nothing_to_annotate_survives_the_budget():
    """A budget must not make a gap disappear; it still owes a quarantine."""

    episodes = (
        _episode("kept", ("a", "b", "c")),
        _episode("orphan", ("z",)),
    )
    result = _result(episodes, {"a": ("kept",)})

    kept, _ = episodes_within_budget(
        result, AnnotationBudget(hours=1.0, episodes_per_hour=1.0)
    )

    assert {episode.id for episode in kept} == {"kept", "orphan"}

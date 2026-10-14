"""Tests for the order episodes are offered to the annotator in.

A full pass takes tens of hours, so nearly every run is a prefix of one.  What
matters is therefore not only which episodes are sent but which are sent first:
a prefix in identifier order is a model of whichever conversations happened to
hash low, and a prefix in coverage order is a sample of the whole archive.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from varyz.domain import ConversationMetadata, Entry, Kind, Sensitivity
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
    EpisodeOrder,
    _spread_positions,
    breadth_ordered_episodes,
    coverage_ordered_episodes,
    first_read_first,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
PRODUCER = ProducerMetadata(
    kind=ProducerKind.LOCAL_MODEL,
    name="fixture-proposer",
    version="1",
    model_name="fixture-model",
    prompt_version="fixture-prompt-v1",
)


def entry(entry_id: str, *, at: str) -> Entry:
    body = f"body of {entry_id}"
    return Entry(
        id=entry_id,
        kind=Kind.MESSAGE,
        source="fixture-chat",
        source_account="owner@example.test",
        external_id=f"native-{entry_id}",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        body=body,
        occurred_start=at,
        ingested_at=NOW.isoformat(),
        thread_id="thread-1",
        conversation=ConversationMetadata(
            conversation_id="conversation-1",
            participant_ids=("owner", "anna"),
            sender_id="owner",
        ),
        sensitivity=Sensitivity.PERSONAL,
    )


def episode(episode_id: str, *, thread: str, entry_id: str) -> ConversationEpisode:
    return ConversationEpisode(
        id=episode_id,
        generation_id="generation-1",
        evidence=(
            EvidenceReference(
                entry_id=entry_id,
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
        source_thread_id=thread,
        turns=(SourceTurn(entry_id=entry_id, order=0, occurred_at=None),),
        initiating_entry_id=entry_id,
        target_entry_id=entry_id,
        completeness=ContextCompleteness.COMPLETE,
        boundary_policy_version="fixture-v1",
    )


def archive(
    sizes: dict[str, int],
) -> tuple[tuple[ConversationEpisode, ...], dict[str, Entry]]:
    """Build threads of the given sizes, timestamped in creation order."""

    episodes: list[ConversationEpisode] = []
    entries: dict[str, Entry] = {}
    for thread, size in sizes.items():
        for index in range(size):
            entry_id = f"{thread}-entry-{index:03d}"
            # Timestamps ascend within a thread; identifiers do not, so an
            # ordering that reads as chronological cannot be an id sort in
            # disguise.
            entries[entry_id] = entry(
                entry_id, at=f"2026-01-{index + 1:02d}T00:00:00+00:00"
            )
            episodes.append(
                episode(
                    f"{thread}-episode-{size - index:03d}",
                    thread=thread,
                    entry_id=entry_id,
                )
            )
    return tuple(episodes), entries


def test_every_prefix_samples_every_conversation() -> None:
    episodes, entries = archive({"big": 100, "small": 10, "tiny": 4})
    ordered = coverage_ordered_episodes(episodes, entries)

    assert len(ordered) == len(episodes)
    assert {item.id for item in ordered} == {item.id for item in episodes}

    for fraction in (0.1, 0.25, 0.5):
        prefix = ordered[: int(len(ordered) * fraction)]
        for thread, size in (("big", 100), ("small", 10), ("tiny", 4)):
            taken = sum(1 for item in prefix if item.source_thread_id == thread)
            expected = size * fraction
            # Rounding a four-episode thread onto a tenth of the archive can
            # only land within one episode of its share.
            assert abs(taken - expected) <= 1


def test_a_conversation_is_walked_in_time_order() -> None:
    episodes, entries = archive({"one": 12})
    ordered = coverage_ordered_episodes(episodes, entries)
    occurred = [entries[item.target_entry_id].occurred_start for item in ordered]
    assert occurred == sorted(occurred)


def test_ordering_is_stable_across_runs() -> None:
    episodes, entries = archive({"a": 7, "b": 3})
    first = coverage_ordered_episodes(episodes, entries)
    shuffled = tuple(reversed(episodes))
    second = coverage_ordered_episodes(shuffled, entries)
    assert [item.id for item in first] == [item.id for item in second]


def test_an_order_correlated_with_conversations_is_lopsided() -> None:
    """Why the guarantee has to be by construction rather than by luck.

    Episode ids are content hashes today, so sorting by them happens to be a
    random sample and happens to be roughly proportional.  Nothing enforces
    that: the moment an identifier carries any locality — a scheme change, a
    prefix, a source added to the front — a prefix of the run becomes a model
    of whichever conversations sort first.  Coverage order does not depend on
    what identifiers look like.
    """

    episodes, entries = archive({"big": 100, "small": 10, "tiny": 4})
    correlated = tuple(
        sorted(episodes, key=lambda item: (item.source_thread_id or "", item.id))
    )
    prefix = correlated[: len(correlated) // 10]
    assert {item.source_thread_id for item in prefix} == {"big"}

    ordered = coverage_ordered_episodes(episodes, entries)
    taken = ordered[: len(ordered) // 10]
    for thread, size in (("big", 100), ("small", 10), ("tiny", 4)):
        share = sum(1 for item in taken if item.source_thread_id == thread)
        assert abs(share - size / 10) <= 1


def test_missing_timestamps_do_not_lose_episodes() -> None:
    episodes, entries = archive({"a": 5})
    assert len(coverage_ordered_episodes(episodes, {})) == len(episodes)


def test_episode_order_values_are_stable() -> None:
    assert EpisodeOrder.COVERAGE == "coverage"
    assert EpisodeOrder.IDENTIFIER == "identifier"
    assert EpisodeOrder.BREADTH == "breadth"


def test_breadth_order_reaches_every_conversation_in_the_first_round() -> None:
    """The property coverage order does not have, and the reason this exists.

    A durable reading needs one disposition witnessed in two conversations, so
    what a stopped run can conclude is bounded by the conversations it reached.
    Proportional placement puts a small thread's only episode halfway down the
    walk; here every thread contributes before any thread contributes twice.
    """

    sizes = {"big": 100, "small": 10, "tiny": 4, "single": 1}
    episodes, entries = archive(sizes)
    ordered = breadth_ordered_episodes(episodes, entries)

    assert len(ordered) == len(episodes)
    assert {item.id for item in ordered} == {item.id for item in episodes}

    first_round = ordered[: len(sizes)]
    assert {item.source_thread_id for item in first_round} == set(sizes)

    # The same prefix in coverage order has not yet reached the one-episode
    # thread at all, which is the whole difference.
    proportional = coverage_ordered_episodes(episodes, entries)[: len(sizes)]
    assert {item.source_thread_id for item in proportional} != set(sizes)


def test_breadth_order_still_spreads_a_conversation_over_its_history() -> None:
    """Breadth must not be bought with a run of everyone's oldest messages."""

    episodes, entries = archive({"one": 64, "other": 64})
    ordered = breadth_ordered_episodes(episodes, entries)
    taken = [
        entries[item.target_entry_id].occurred_start
        for item in ordered
        if item.source_thread_id == "one"
    ][:8]
    span = [entries[f"one-entry-{index:03d}"].occurred_start for index in range(64)]
    # An eighth of the thread, and it reaches past the halfway point of its
    # history rather than pooling in the first eighth.
    assert max(taken) > span[len(span) // 2]


def test_breadth_order_is_stable_across_runs() -> None:
    episodes, entries = archive({"a": 7, "b": 3, "c": 1})
    first = breadth_ordered_episodes(episodes, entries)
    second = breadth_ordered_episodes(tuple(reversed(episodes)), entries)
    assert [item.id for item in first] == [item.id for item in second]


def test_breadth_order_keeps_every_episode_without_timestamps() -> None:
    episodes, entries = archive({"a": 5, "b": 2})
    assert len(breadth_ordered_episodes(episodes, {})) == len(episodes)


def test_a_message_is_read_once_before_any_message_is_read_twice() -> None:
    episodes, _ = archive({"a": 4})
    ordered = tuple(sorted(episodes, key=lambda item: item.id))
    # The second and fourth episodes cover only messages the first and third
    # already covered: a boundary message belonging to two windows.
    coverage = {
        ordered[0].id: ("entry-1", "entry-2"),
        ordered[1].id: ("entry-2",),
        ordered[2].id: ("entry-3",),
        ordered[3].id: ("entry-1", "entry-3"),
    }
    walked = first_read_first(ordered, coverage)
    assert [item.id for item in walked] == [
        ordered[0].id,
        ordered[2].id,
        ordered[1].id,
        ordered[3].id,
    ]


def test_deferring_repeats_loses_no_episode_and_keeps_relative_order() -> None:
    """A full pass must still read every window; only the order changes."""

    episodes, _ = archive({"a": 6})
    ordered = tuple(sorted(episodes, key=lambda item: item.id))
    coverage = {item.id: ("shared",) for item in ordered}
    walked = first_read_first(ordered, coverage)
    assert [item.id for item in walked] == [item.id for item in ordered]
    assert len(walked) == len(ordered)


def test_an_episode_with_no_coverage_keeps_its_place_to_be_quarantined() -> None:
    episodes, _ = archive({"a": 3})
    ordered = tuple(sorted(episodes, key=lambda item: item.id))
    coverage = {ordered[0].id: ("entry-1",), ordered[2].id: ("entry-1",)}
    walked = first_read_first(ordered, coverage)
    assert [item.id for item in walked][:2] == [ordered[0].id, ordered[1].id]


def test_spread_positions_is_a_permutation_that_opens_out() -> None:
    for count in (0, 1, 2, 3, 5, 8, 13, 64):
        spread = _spread_positions(count)
        assert sorted(spread) == list(range(count))
    # A prefix of a long thread covers both halves rather than only the first.
    half = _spread_positions(64)[:8]
    assert any(position >= 32 for position in half)

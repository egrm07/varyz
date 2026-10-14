"""Deterministic reconstruction of bounded source conversation episodes.

This module is deliberately structural.  It reads only normalized source metadata and
never interprets message text, invents relationships, or turns generated summaries into
evidence.  Semantic observation extraction belongs to the next compiler stage.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from varyz.domain import Entry, Sensitivity, TimePrecision
from varyz.personal_model.models import (
    ContextCompleteness,
    ConversationEpisode,
    DerivationKind,
    EvidenceReference,
    EvidenceRole,
    OmittedRange,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    ReplyEdge,
    SourceTurn,
    TemporalScope,
    conversation_episode_id,
)


class TopicContinuityPort(Protocol):
    """Optional, versioned topic boundary decision supplied by another adapter.

    ``None`` means that the adapter cannot decide.  The reconstructor treats that as
    unknown rather than as evidence of either continuity or a boundary.
    """

    version: str

    def continues(self, previous: Entry, current: Entry) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class EpisodePolicy:
    """Versioned structural and context-budget policy."""

    boundary_policy_version: str = "1"
    max_time_gap: timedelta = timedelta(hours=6)
    max_turns: int = 40
    max_characters: int = 24_000

    def __post_init__(self) -> None:
        if not self.boundary_policy_version.strip():
            raise ValueError("boundary_policy_version must not be blank")
        if self.max_time_gap < timedelta(0):
            raise ValueError("max_time_gap must not be negative")
        if self.max_turns < 1:
            raise ValueError("max_turns must be at least one")
        if self.max_characters < 1:
            raise ValueError("max_characters must be at least one")


class DiagnosticCode(StrEnum):
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    DUPLICATE_ENTRY_ID = "duplicate_entry_id"
    DUPLICATE_SOURCE_ORDER = "duplicate_source_order"
    INVALID_TIMESTAMP = "invalid_timestamp"
    OMITTED_CONTEXT = "omitted_context"
    QUARANTINED_TARGET = "quarantined_target"
    REPLY_CYCLE = "reply_cycle"
    TOPIC_PORT_ERROR = "topic_port_error"
    UNKNOWN_ORDERING = "unknown_ordering"
    UNRESOLVED_QUOTE_REFERENCE = "unresolved_quote_reference"
    UNRESOLVED_REPLY_REFERENCE = "unresolved_reply_reference"


class EpisodeDiagnostic(BaseModel):
    """Machine-readable reason an episode is incomplete, unknown, or absent."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    code: DiagnosticCode
    entry_ids: tuple[str, ...] = ()
    detail: str = Field(min_length=1)


class TargetEpisodeCoverage(BaseModel):
    """Stable mapping from a requested analysis target to canonical episode windows."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    target_entry_id: str = Field(min_length=1)
    episode_ids: tuple[str, ...] = ()
    covered_entry_ids: tuple[str, ...] = ()
    omitted_entry_ids: tuple[str, ...] = ()
    quarantined: bool = False

    @model_validator(mode="after")
    def valid_coverage(self) -> TargetEpisodeCoverage:
        for field_name, values in (
            ("episode_ids", self.episode_ids),
            ("covered_entry_ids", self.covered_entry_ids),
            ("omitted_entry_ids", self.omitted_entry_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
        if set(self.covered_entry_ids) & set(self.omitted_entry_ids):
            raise ValueError("covered and omitted entry ids must be disjoint")
        if self.quarantined:
            if self.episode_ids or self.covered_entry_ids:
                raise ValueError("a quarantined target cannot claim episode coverage")
        elif not self.episode_ids or self.target_entry_id not in self.covered_entry_ids:
            raise ValueError("a represented target requires an episode that covers it")
        return self


class EpisodeBuildResult(BaseModel):
    """Episode outputs and non-silent reconstruction diagnostics."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    episodes: tuple[ConversationEpisode, ...] = ()
    diagnostics: tuple[EpisodeDiagnostic, ...] = ()
    target_coverage: tuple[TargetEpisodeCoverage, ...] = ()
    quarantined_target_ids: tuple[str, ...] = ()
    effective_boundary_policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_result_links(self) -> EpisodeBuildResult:
        episode_ids = tuple(episode.id for episode in self.episodes)
        if len(set(episode_ids)) != len(episode_ids):
            raise ValueError("episode ids must be unique")
        target_ids = tuple(item.target_entry_id for item in self.target_coverage)
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("target coverage rows must be unique")
        episode_id_set = set(episode_ids)
        for item in self.target_coverage:
            if not set(item.episode_ids) <= episode_id_set:
                raise ValueError("target coverage references an absent episode")
        if len(set(self.quarantined_target_ids)) != len(self.quarantined_target_ids):
            raise ValueError("quarantined target ids must be unique")
        quarantined_from_coverage = {
            item.target_entry_id for item in self.target_coverage if item.quarantined
        }
        if quarantined_from_coverage != set(self.quarantined_target_ids):
            raise ValueError("quarantined targets and coverage rows disagree")
        return self


@dataclass(frozen=True, slots=True)
class _BoundaryKey:
    source: str
    account: str
    thread_id: str | None
    conversation_id: str | None
    isolated_entry_id: str | None


@dataclass(frozen=True, slots=True)
class _ResolvedBoundary:
    key: _BoundaryKey
    entries: tuple[Entry, ...]
    position: dict[str, int]
    edges: tuple[ReplyEdge, ...]
    unresolved_entry_ids: frozenset[str]
    ordering_unknown: bool
    participant_completeness_unknown: bool


_SENSITIVITY_ORDER = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.UNCLASSIFIED: 3,
}


class EpisodeReconstructor:
    """Build target-centred staged episodes from stored :class:`Entry` records."""

    def __init__(
        self,
        policy: EpisodePolicy | None = None,
        *,
        topic_continuity: TopicContinuityPort | None = None,
    ) -> None:
        self.policy = policy or EpisodePolicy()
        self.topic_continuity = topic_continuity
        topic_version = topic_continuity.version if topic_continuity is not None else "none"
        if not topic_version.strip():
            raise ValueError("topic continuity version must not be blank")
        policy_material = json.dumps(
            {
                "declared": self.policy.boundary_policy_version,
                "max_gap_seconds": self.policy.max_time_gap.total_seconds(),
                "max_turns": self.policy.max_turns,
                "max_characters": self.policy.max_characters,
                "topic_version": topic_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(policy_material.encode("utf-8")).hexdigest()[:16]
        self.effective_policy_version = (
            f"{self.policy.boundary_policy_version}+{topic_version}+{fingerprint}"
        )

    def build(
        self,
        entries: Iterable[Entry],
        *,
        generation_id: str,
        created_at: datetime,
        target_entry_ids: Iterable[str] | None = None,
    ) -> EpisodeBuildResult:
        """Reconstruct episodes deterministically, independent of input iteration order.

        Every requested target is either represented by at least one staged episode or is
        listed in ``quarantined_target_ids`` with a diagnostic.  Supplying no targets asks
        for one target-centred reconstruction per unique input entry.
        """

        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if not generation_id.strip():
            raise ValueError("generation_id must not be blank")

        diagnostics: list[EpisodeDiagnostic] = []
        unique_entries, duplicate_ids = self._deduplicate(entries, diagnostics)
        entry_by_id = {entry.id: entry for entry in unique_entries}
        requested_targets = (
            sorted(set(target_entry_ids))
            if target_entry_ids is not None
            else sorted(set(entry_by_id) | set(duplicate_ids))
        )
        quarantined: set[str] = set()
        coverage_by_target: dict[str, TargetEpisodeCoverage] = {}

        for target_id in requested_targets:
            if target_id not in entry_by_id or target_id in duplicate_ids:
                quarantined.add(target_id)
                diagnostics.append(
                    EpisodeDiagnostic(
                        code=DiagnosticCode.QUARANTINED_TARGET,
                        entry_ids=(target_id,),
                        detail=(
                            "target is absent from the input or has conflicting duplicate records"
                        ),
                    )
                )

        boundaries: dict[_BoundaryKey, list[Entry]] = defaultdict(list)
        for entry in unique_entries:
            boundaries[self._boundary_key(entry)].append(entry)

        built: list[ConversationEpisode] = []
        for key in sorted(boundaries, key=self._boundary_sort_key):
            resolved, boundary_diagnostics = self._resolve_boundary(key, boundaries[key])
            diagnostics.extend(boundary_diagnostics)
            boundary_target_ids = [
                target_id
                for target_id in requested_targets
                if target_id in resolved.position and target_id not in quarantined
            ]
            if not boundary_target_ids:
                continue
            components, component_diagnostics = self._components(resolved)
            diagnostics.extend(component_diagnostics)
            component_by_id = {
                entry.id: component for component in components for entry in component
            }
            for target_id in boundary_target_ids:
                target = entry_by_id[target_id]
                if self._entry_characters(target) > self.policy.max_characters:
                    quarantined.add(target_id)
                    component_ids = tuple(
                        entry.id for entry in component_by_id[target_id]
                    )
                    coverage_by_target[target_id] = TargetEpisodeCoverage(
                        target_entry_id=target_id,
                        omitted_entry_ids=component_ids,
                        quarantined=True,
                    )
                    diagnostics.append(
                        EpisodeDiagnostic(
                            code=DiagnosticCode.QUARANTINED_TARGET,
                            entry_ids=(target_id,),
                            detail="target turn alone exceeds the episode character budget",
                        )
                    )
                    continue
                episodes, target_diagnostics = self._build_target(
                    resolved,
                    component_by_id[target_id],
                    target_id=target_id,
                    generation_id=generation_id,
                    created_at=created_at,
                )
                built.extend(episodes)
                diagnostics.extend(target_diagnostics)

                covered_ids = {
                    turn.entry_id for episode in episodes for turn in episode.turns
                }
                component_ids = tuple(
                    entry.id for entry in component_by_id[target_id]
                )
                coverage_by_target[target_id] = TargetEpisodeCoverage(
                    target_entry_id=target_id,
                    episode_ids=tuple(sorted({episode.id for episode in episodes})),
                    covered_entry_ids=tuple(
                        entry_id for entry_id in component_ids if entry_id in covered_ids
                    ),
                    omitted_entry_ids=tuple(
                        entry_id for entry_id in component_ids if entry_id not in covered_ids
                    ),
                )

        canonical_by_id: dict[str, ConversationEpisode] = {}
        for episode in built:
            existing = canonical_by_id.get(episode.id)
            if existing is not None and existing != episode:
                raise RuntimeError(
                    "episode identity collision: equal membership/policy produced unequal records"
                )
            canonical_by_id[episode.id] = episode
        canonical_episodes = tuple(
            canonical_by_id[episode_id] for episode_id in sorted(canonical_by_id)
        )
        for target_id in requested_targets:
            if target_id not in coverage_by_target:
                coverage_by_target[target_id] = TargetEpisodeCoverage(
                    target_entry_id=target_id,
                    omitted_entry_ids=(target_id,),
                    quarantined=True,
                )
        diagnostics.sort(key=lambda item: (item.code.value, item.entry_ids, item.detail))
        return EpisodeBuildResult(
            episodes=canonical_episodes,
            diagnostics=tuple(diagnostics),
            target_coverage=tuple(
                coverage_by_target[target_id] for target_id in requested_targets
            ),
            quarantined_target_ids=tuple(sorted(quarantined)),
            effective_boundary_policy_version=self.effective_policy_version,
        )

    def iter_build_results(
        self,
        boundary_entries: Iterable[Iterable[Entry]],
        *,
        generation_id: str,
        created_at: datetime,
    ) -> Iterator[EpisodeBuildResult]:
        """Build and release one canonical conversation boundary at a time.

        The grouping is explicit because an arbitrary entry iterator cannot know that a
        boundary is complete.  Database and connector adapters can supply that guarantee
        without forcing this domain layer to retain the archive or depend on storage.
        """

        for entries in boundary_entries:
            group = tuple(entries)
            if not group:
                continue
            keys = {self._boundary_key(entry) for entry in group}
            if len(keys) != 1:
                raise ValueError("a streamed episode group must contain one boundary")
            yield self.build(
                group,
                generation_id=generation_id,
                created_at=created_at,
            )

    def _deduplicate(
        self, entries: Iterable[Entry], diagnostics: list[EpisodeDiagnostic]
    ) -> tuple[tuple[Entry, ...], frozenset[str]]:
        grouped: dict[str, list[Entry]] = defaultdict(list)
        for entry in entries:
            grouped[entry.id].append(entry)
        unique: list[Entry] = []
        conflicts: set[str] = set()
        for entry_id in sorted(grouped):
            candidates = grouped[entry_id]
            canonical = sorted(
                candidates,
                key=lambda item: json.dumps(
                    item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ),
            )
            distinct = {
                json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                for item in canonical
            }
            if len(distinct) > 1:
                conflicts.add(entry_id)
                diagnostics.append(
                    EpisodeDiagnostic(
                        code=DiagnosticCode.DUPLICATE_ENTRY_ID,
                        entry_ids=(entry_id,),
                        detail="conflicting records share one entry id",
                    )
                )
                continue
            unique.append(canonical[0])
        return tuple(unique), frozenset(conflicts)

    @staticmethod
    def _boundary_key(entry: Entry) -> _BoundaryKey:
        conversation_id = entry.conversation.conversation_id
        isolated = None if entry.thread_id or conversation_id else entry.id
        return _BoundaryKey(
            source=entry.source,
            account=entry.source_account,
            thread_id=entry.thread_id,
            conversation_id=conversation_id,
            isolated_entry_id=isolated,
        )

    @staticmethod
    def _boundary_sort_key(key: _BoundaryKey) -> tuple[str, str, str, str, str]:
        return (
            key.source,
            key.account,
            key.thread_id or "",
            key.conversation_id or "",
            key.isolated_entry_id or "",
        )

    def _resolve_boundary(
        self, key: _BoundaryKey, entries: Sequence[Entry]
    ) -> tuple[_ResolvedBoundary, list[EpisodeDiagnostic]]:
        diagnostics: list[EpisodeDiagnostic] = []
        ordered, ordering_unknown, ordering_diagnostics = self._order_entries(entries)
        diagnostics.extend(ordering_diagnostics)
        position = {entry.id: index for index, entry in enumerate(ordered)}

        native_ids: dict[str, list[str]] = defaultdict(list)
        for entry in ordered:
            native_ids[entry.id].append(entry.id)
            native_ids[entry.external_id].append(entry.id)

        edges: list[ReplyEdge] = []
        unresolved: set[str] = set()
        for entry in ordered:
            references = (
                (entry.conversation.reply_to_id, False),
                (entry.conversation.quote_target_id, True),
            )
            for reference, is_quote in references:
                if not reference:
                    continue
                candidates = sorted(set(native_ids.get(reference, ())))
                if len(candidates) == 1 and candidates[0] != entry.id:
                    edges.append(
                        ReplyEdge(
                            from_entry_id=entry.id,
                            to_entry_id=candidates[0],
                            is_quote=is_quote,
                        )
                    )
                    continue
                unresolved.add(entry.id)
                if len(candidates) > 1:
                    code = DiagnosticCode.AMBIGUOUS_REFERENCE
                    detail = f"native reference {reference!r} matches multiple turns"
                else:
                    code = (
                        DiagnosticCode.UNRESOLVED_QUOTE_REFERENCE
                        if is_quote
                        else DiagnosticCode.UNRESOLVED_REPLY_REFERENCE
                    )
                    detail = f"native reference {reference!r} is absent from this conversation"
                diagnostics.append(
                    EpisodeDiagnostic(code=code, entry_ids=(entry.id,), detail=detail)
                )

        participant_unknown = any(
            entry.conversation.participants_complete is not True for entry in ordered
        )
        return (
            _ResolvedBoundary(
                key=key,
                entries=ordered,
                position=position,
                edges=tuple(
                    sorted(
                        set(edges),
                        key=lambda edge: (
                            position[edge.from_entry_id],
                            position[edge.to_entry_id],
                            edge.is_quote,
                        ),
                    )
                ),
                unresolved_entry_ids=frozenset(unresolved),
                ordering_unknown=ordering_unknown,
                participant_completeness_unknown=participant_unknown,
            ),
            diagnostics,
        )

    def _order_entries(
        self, entries: Sequence[Entry]
    ) -> tuple[tuple[Entry, ...], bool, list[EpisodeDiagnostic]]:
        diagnostics: list[EpisodeDiagnostic] = []
        parsed_times: dict[str, datetime | None] = {}
        for entry in entries:
            parsed, invalid = self._parse_timestamp(entry.occurred_start)
            parsed_times[entry.id] = parsed
            if invalid:
                diagnostics.append(
                    EpisodeDiagnostic(
                        code=DiagnosticCode.INVALID_TIMESTAMP,
                        entry_ids=(entry.id,),
                        detail=(
                            "occurred_start is invalid or lacks a timezone; "
                            "time remains unknown"
                        ),
                    )
                )

        source_orders: dict[tuple[str, str], list[str]] = defaultdict(list)
        for entry in entries:
            order = entry.conversation.source_order
            if order is not None:
                source_orders[self._canonical_order(order)].append(entry.id)
        duplicate_orders = {
            order: ids for order, ids in source_orders.items() if len(ids) > 1
        }
        for order, ids in sorted(duplicate_orders.items()):
            diagnostics.append(
                EpisodeDiagnostic(
                    code=DiagnosticCode.DUPLICATE_SOURCE_ORDER,
                    entry_ids=tuple(sorted(ids)),
                    detail=f"source order {order[1]!r} is duplicated; deterministic tie-break used",
                )
            )

        all_have_order = all(entry.conversation.source_order is not None for entry in entries)
        all_have_time = all(parsed_times[entry.id] is not None for entry in entries)
        ordering_unknown = bool(duplicate_orders) or not (all_have_order or all_have_time)
        if ordering_unknown:
            diagnostics.append(
                EpisodeDiagnostic(
                    code=DiagnosticCode.UNKNOWN_ORDERING,
                    entry_ids=tuple(sorted(entry.id for entry in entries)),
                    detail="source ordering is incomplete or ambiguous; stable fallback order used",
                )
            )

        if all_have_order:
            ordered = sorted(
                entries,
                key=lambda entry: (
                    self._canonical_order(entry.conversation.source_order),
                    self._time_sort_key(parsed_times[entry.id]),
                    entry.external_id,
                    entry.id,
                ),
            )
        elif all_have_time:
            ordered = sorted(
                entries,
                key=lambda entry: (
                    self._time_sort_key(parsed_times[entry.id]),
                    self._canonical_order(entry.conversation.source_order),
                    entry.external_id,
                    entry.id,
                ),
            )
        else:
            ordered = sorted(
                entries,
                key=lambda entry: (
                    self._time_sort_key(parsed_times[entry.id]),
                    self._canonical_order(entry.conversation.source_order),
                    entry.external_id,
                    entry.id,
                ),
            )
        return tuple(ordered), ordering_unknown, diagnostics

    @staticmethod
    def _canonical_order(value: int | str | None) -> tuple[str, str]:
        if value is None:
            return ("2-missing", "")
        if isinstance(value, int):
            return ("0-integer", f"{value:+030d}")
        return ("1-string", value)

    @staticmethod
    def _time_sort_key(value: datetime | None) -> tuple[int, str]:
        return (0, value.isoformat()) if value is not None else (1, "")

    @staticmethod
    def _parse_timestamp(value: str | None) -> tuple[datetime | None, bool]:
        if value is None:
            return None, False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, True
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, True
        return parsed, False

    def _components(
        self, boundary: _ResolvedBoundary
    ) -> tuple[tuple[tuple[Entry, ...], ...], list[EpisodeDiagnostic]]:
        diagnostics: list[EpisodeDiagnostic] = []
        parent = {entry.id: entry.id for entry in boundary.entries}

        def find(entry_id: str) -> str:
            while parent[entry_id] != entry_id:
                parent[entry_id] = parent[parent[entry_id]]
                entry_id = parent[entry_id]
            return entry_id

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        explicit_pairs = {
            frozenset((edge.from_entry_id, edge.to_entry_id)) for edge in boundary.edges
        }
        for edge in boundary.edges:
            union(edge.from_entry_id, edge.to_entry_id)

        for previous, current in zip(boundary.entries, boundary.entries[1:], strict=False):
            if frozenset((previous.id, current.id)) in explicit_pairs:
                continue
            continues = self._within_time_gap(previous, current)
            if continues and self.topic_continuity is not None:
                try:
                    topic_decision = self.topic_continuity.continues(previous, current)
                except Exception as exc:  # adapters must not make reconstruction disappear
                    topic_decision = None
                    diagnostics.append(
                        EpisodeDiagnostic(
                            code=DiagnosticCode.TOPIC_PORT_ERROR,
                            entry_ids=(previous.id, current.id),
                            detail=f"topic continuity adapter failed: {type(exc).__name__}",
                        )
                    )
                if topic_decision is False:
                    continues = False
            if continues:
                union(previous.id, current.id)

        grouped: dict[str, list[Entry]] = defaultdict(list)
        for entry in boundary.entries:
            grouped[find(entry.id)].append(entry)
        components = tuple(
            tuple(group)
            for _, group in sorted(
                grouped.items(), key=lambda item: boundary.position[item[1][0].id]
            )
        )
        return components, diagnostics

    def _within_time_gap(self, previous: Entry, current: Entry) -> bool:
        previous_time, _ = self._parse_timestamp(previous.occurred_start)
        current_time, _ = self._parse_timestamp(current.occurred_start)
        if previous_time is None or current_time is None:
            return True
        return abs(current_time - previous_time) <= self.policy.max_time_gap

    def _build_target(
        self,
        boundary: _ResolvedBoundary,
        component: tuple[Entry, ...],
        *,
        target_id: str,
        generation_id: str,
        created_at: datetime,
    ) -> tuple[list[ConversationEpisode], list[EpisodeDiagnostic]]:
        diagnostics: list[EpisodeDiagnostic] = []
        component_ids = {entry.id for entry in component}
        component_edges = tuple(
            edge
            for edge in boundary.edges
            if edge.from_entry_id in component_ids and edge.to_entry_id in component_ids
        )
        ancestors, descendants, cycle = self._reply_context(
            target_id, component_edges, boundary.position
        )
        if cycle:
            diagnostics.append(
                EpisodeDiagnostic(
                    code=DiagnosticCode.REPLY_CYCLE,
                    entry_ids=tuple(sorted(cycle)),
                    detail=(
                        "reply metadata contains a cycle; traversal stopped without "
                        "inventing a root"
                    ),
                )
            )

        target_position = boundary.position[target_id]
        required = [target_id]
        required.extend(
            sorted(ancestors, key=lambda item: abs(boundary.position[item] - target_position))
        )
        required.extend(
            sorted(descendants, key=lambda item: boundary.position[item] - target_position)
        )
        required_set = set(required)
        ordinary = sorted(
            (entry.id for entry in component if entry.id not in required_set),
            key=lambda item: (
                abs(boundary.position[item] - target_position),
                boundary.position[item],
            ),
        )
        priority = tuple(dict.fromkeys((*required, *ordinary)))
        component_by_id = {entry.id: entry for entry in component}

        if self._fits(component):
            selections = [set(component_ids)]
            correlation_id = None
        else:
            correlation_id = self._correlation_id(component)
            selections = self._budgeted_selections(priority, target_id, component_by_id)

        episodes: list[ConversationEpisode] = []
        for selected in selections:
            ordered_selected = tuple(entry for entry in component if entry.id in selected)
            omitted = tuple(entry for entry in component if entry.id not in selected)
            omitted_ranges = self._omitted_ranges(component, selected)
            if omitted:
                diagnostics.append(
                    EpisodeDiagnostic(
                        code=DiagnosticCode.OMITTED_CONTEXT,
                        entry_ids=(target_id, *(entry.id for entry in omitted)),
                        detail="episode budget omitted source turns; omitted ranges are recorded",
                    )
                )
            episodes.append(
                self._episode_contract(
                    boundary,
                    ordered_selected,
                    generation_id=generation_id,
                    created_at=created_at,
                    correlation_id=correlation_id,
                    omitted_ranges=omitted_ranges,
                )
            )
        return episodes, diagnostics

    @staticmethod
    def _reply_context(
        target_id: str,
        edges: Sequence[ReplyEdge],
        position: dict[str, int],
    ) -> tuple[set[str], set[str], set[str]]:
        parents: dict[str, list[str]] = defaultdict(list)
        children: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            parents[edge.from_entry_id].append(edge.to_entry_id)
            children[edge.to_entry_id].append(edge.from_entry_id)
        for values in (*parents.values(), *children.values()):
            values.sort(key=position.__getitem__)

        ancestors: set[str] = set()
        descendants: set[str] = set()
        cycle: set[str] = set()

        def traverse(start: str, adjacency: dict[str, list[str]], output: set[str]) -> None:
            queue = deque([(start, frozenset((start,)))])
            while queue:
                current, path = queue.popleft()
                for linked in adjacency.get(current, ()):
                    if linked in path:
                        cycle.update(path)
                        cycle.add(linked)
                        continue
                    if linked not in output and linked != target_id:
                        output.add(linked)
                        queue.append((linked, path | {linked}))

        traverse(target_id, parents, ancestors)
        traverse(target_id, children, descendants)
        return ancestors, descendants, cycle

    def _fits(self, entries: Sequence[Entry]) -> bool:
        return len(entries) <= self.policy.max_turns and sum(
            self._entry_characters(entry) for entry in entries
        ) <= self.policy.max_characters

    def _budgeted_selections(
        self, priority: tuple[str, ...], target_id: str, entries: dict[str, Entry]
    ) -> list[set[str]]:
        remaining = [entry_id for entry_id in priority if entry_id != target_id]
        selections: list[set[str]] = []
        target_chars = self._entry_characters(entries[target_id])
        while remaining:
            selected = {target_id}
            used_chars = target_chars
            deferred: list[str] = []
            for entry_id in remaining:
                size = self._entry_characters(entries[entry_id])
                if (
                    len(selected) < self.policy.max_turns
                    and used_chars + size <= self.policy.max_characters
                ):
                    selected.add(entry_id)
                    used_chars += size
                else:
                    deferred.append(entry_id)
            selections.append(selected)
            if len(deferred) == len(remaining):
                break
            remaining = deferred
        return selections or [{target_id}]

    def _episode_contract(
        self,
        boundary: _ResolvedBoundary,
        entries: tuple[Entry, ...],
        *,
        generation_id: str,
        created_at: datetime,
        correlation_id: str | None,
        omitted_ranges: tuple[OmittedRange, ...],
    ) -> ConversationEpisode:
        ids = tuple(entry.id for entry in entries)
        episode_id = conversation_episode_id(
            ids, boundary_policy_version=self.effective_policy_version
        )
        selected = set(ids)
        edges = tuple(
            edge
            for edge in boundary.edges
            if edge.from_entry_id in selected and edge.to_entry_id in selected
        )
        timestamps = {entry.id: self._parse_timestamp(entry.occurred_start)[0] for entry in entries}
        known_timestamps = sorted(value for value in timestamps.values() if value is not None)
        precisions = {entry.time_precision for entry in entries}
        precision = (
            next(iter(precisions))
            if len(known_timestamps) == len(entries) and len(precisions) == 1
            else TimePrecision.UNKNOWN
        )
        if omitted_ranges or self._has_reply_cycle(edges) or any(
            entry.id in boundary.unresolved_entry_ids for entry in entries
        ):
            completeness = ContextCompleteness.INCOMPLETE
        elif (
            boundary.ordering_unknown
            or boundary.participant_completeness_unknown
            or boundary.key.isolated_entry_id is not None
        ):
            completeness = ContextCompleteness.UNKNOWN
        else:
            completeness = ContextCompleteness.COMPLETE

        participants = self._participant_keys(entries)
        sensitivity = max(
            (entry.sensitivity for entry in entries), key=_SENSITIVITY_ORDER.__getitem__
        )
        evidence = tuple(
            EvidenceReference(
                entry_id=entry.id,
                role=EvidenceRole.MENTIONS,
                episode_id=episode_id,
                sensitivity=entry.sensitivity,
            )
            for entry in entries
        )
        return ConversationEpisode(
            id=episode_id,
            generation_id=generation_id,
            evidence=evidence,
            claim_ids=(),
            temporal=TemporalScope(
                valid_from=known_timestamps[0] if known_timestamps else None,
                valid_to=known_timestamps[-1] if known_timestamps else None,
                time_precision=precision,
            ),
            confidence=1.0,
            sensitivity=sensitivity,
            derivation=DerivationKind.OBSERVED,
            producer=ProducerMetadata(
                kind=ProducerKind.DETERMINISTIC,
                name="conversation-episode-reconstructor",
                version="1",
                rule_version=self.effective_policy_version,
            ),
            readiness=Readiness.STAGED,
            created_at=created_at,
            source=boundary.key.source,
            source_thread_id=boundary.key.conversation_id or boundary.key.thread_id,
            turns=tuple(
                SourceTurn(
                    entry_id=entry.id,
                    source_turn_id=entry.external_id,
                    speaker_source_key=self._speaker_key(entry),
                    order=boundary.position[entry.id],
                    occurred_at=timestamps[entry.id],
                    edited=self._edited_state(entry),
                )
                for entry in entries
            ),
            participant_source_keys=participants,
            participant_person_ids=(),
            reply_edges=edges,
            initiating_entry_id=ids[0],
            # The target stored on the canonical record is a membership-derived anchor.
            # Requested analysis targets live in EpisodeBuildResult.target_coverage so
            # they cannot create conflicting records with one membership-derived id.
            target_entry_id=ids[0],
            completeness=completeness,
            boundary_policy_version=self.effective_policy_version,
            correlation_id=correlation_id,
            omitted_ranges=omitted_ranges,
        )

    @staticmethod
    def _has_reply_cycle(edges: Sequence[ReplyEdge]) -> bool:
        adjacency: dict[str, list[str]] = defaultdict(list)
        nodes: set[str] = set()
        for edge in edges:
            adjacency[edge.from_entry_id].append(edge.to_entry_id)
            nodes.update((edge.from_entry_id, edge.to_entry_id))
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(linked) for linked in adjacency.get(node, ())):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in sorted(nodes) if node not in visited)

    @staticmethod
    def _edited_state(entry: Entry) -> bool | None:
        state = entry.conversation.edit_state.value
        if state == "edited":
            return True
        if state == "original":
            return False
        return None

    @staticmethod
    def _speaker_key(entry: Entry) -> str | None:
        if entry.conversation.sender_id:
            return f"id:{entry.conversation.sender_id}"
        if entry.conversation.sender_label:
            return f"label:{entry.conversation.sender_label}"
        return None

    @staticmethod
    def _participant_keys(entries: Sequence[Entry]) -> tuple[str, ...]:
        keys: set[str] = set()
        for entry in entries:
            conversation = entry.conversation
            keys.update(f"id:{value}" for value in conversation.participant_ids)
            keys.update(f"label:{value}" for value in conversation.participant_labels)
            if conversation.sender_id:
                keys.add(f"id:{conversation.sender_id}")
            elif conversation.sender_label:
                keys.add(f"label:{conversation.sender_label}")
            keys.update(f"id:{value}" for value in conversation.recipient_ids)
            keys.update(f"label:{value}" for value in conversation.recipient_labels)
            keys.update(f"id:{value}" for value in conversation.addressee_ids)
            keys.update(f"label:{value}" for value in conversation.addressee_labels)
        return tuple(sorted(keys))

    @staticmethod
    def _entry_characters(entry: Entry) -> int:
        return len(entry.body)

    def _correlation_id(self, component: Sequence[Entry]) -> str:
        material = json.dumps(
            {
                "membership": [entry.id for entry in component],
                "boundary_policy_version": self.effective_policy_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(f"episode-correlation:{material}".encode()).hexdigest()

    @staticmethod
    def _omitted_ranges(
        component: Sequence[Entry], selected: set[str]
    ) -> tuple[OmittedRange, ...]:
        ranges: list[OmittedRange] = []
        index = 0
        while index < len(component):
            if component[index].id in selected:
                index += 1
                continue
            start = index
            while index < len(component) and component[index].id not in selected:
                index += 1
            ranges.append(
                OmittedRange(
                    after_entry_id=component[start - 1].id if start > 0 else None,
                    before_entry_id=component[index].id if index < len(component) else None,
                    omitted_count=index - start,
                    reason="episode turn/character budget",
                )
            )
        return tuple(ranges)


__all__ = [
    "DiagnosticCode",
    "EpisodeBuildResult",
    "EpisodeDiagnostic",
    "EpisodePolicy",
    "EpisodeReconstructor",
    "TargetEpisodeCoverage",
    "TopicContinuityPort",
]

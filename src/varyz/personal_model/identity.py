"""Precision-first, provider-neutral person identity resolution.

Source identities are the atoms in this module.  Exact, verified identifiers may
connect those atoms; names only create review candidates.  The resolver consumes
already-approved local archive metadata and never enriches a person from the web.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import combinations
from types import MappingProxyType

from varyz.domain import Sensitivity
from varyz.personal_model.models import (
    AliasKind,
    DerivationKind,
    EntityAlias,
    EntityKind,
    EvidenceReference,
    EvidenceRole,
    Person,
    ProducerKind,
    ProducerMetadata,
    Readiness,
    VerificationState,
)
from varyz.personal_model.observations import SourceEntityResolution
from varyz.personal_model.projects import names_something


class IdentityDecisionKind(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    UNCERTAIN = "uncertain"


class IdentityOverrideAction(StrEnum):
    MERGE = "merge"
    SPLIT = "split"


class IdentityDiagnosticCode(StrEnum):
    CONFLICTING_DURABLE_IDENTIFIERS = "conflicting_durable_identifiers"
    CONFLICTING_ARCHIVE_OWNER_ATTRIBUTION = "conflicting_archive_owner_attribution"
    ORPHANED_OVERRIDE = "orphaned_override"
    INVALID_OVERRIDE = "invalid_override"
    OVERRIDE_CONFLICT = "override_conflict"
    SUPERSEDED_OVERRIDE = "superseded_override"


def normalize_alias(kind: AliasKind, value: str) -> str:
    """Normalize for lookup while retaining the exact literal on ``EntityAlias``.

    Diacritics are removed only from the lookup form.  In particular, equal
    normalized names remain weak evidence and are never accepted identity edges.
    """

    literal = value.strip()
    if kind == AliasKind.PHONE:
        digits = re.sub(r"\D", "", literal)
        if digits.startswith("00"):
            digits = digits[2:]
        return f"+{digits}" if digits else ""
    if kind == AliasKind.EMAIL:
        return unicodedata.normalize("NFKC", literal).casefold()
    if kind == AliasKind.SOURCE_ACCOUNT_ID:
        # Provider-native ids are opaque and may be case-sensitive.
        return unicodedata.normalize("NFKC", literal)
    if kind == AliasKind.USERNAME:
        literal = literal.removeprefix("@")
    decomposed = unicodedata.normalize("NFKD", literal.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.split())


def source_identity_key(source: str, source_account: str, source_key: str) -> str:
    """Return an unambiguous stable key for one source-scoped identity."""

    return json.dumps(
        (source.strip(), source_account.strip(), source_key.strip()),
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class SourceAlias:
    kind: AliasKind
    exact_value: str
    evidence_entry_ids: tuple[str, ...]
    verified_durable: bool = False
    # Global for email/phone by default; source-scoped for source ids/usernames.
    identifier_scope: str | None = None

    def __post_init__(self) -> None:
        if not self.exact_value.strip():
            raise ValueError("an alias exact value cannot be blank")
        if not self.evidence_entry_ids or any(not item.strip() for item in self.evidence_entry_ids):
            raise ValueError("an alias requires non-blank evidence entry ids")
        if len(set(self.evidence_entry_ids)) != len(self.evidence_entry_ids):
            raise ValueError("alias evidence entry ids must be unique")
        if self.verified_durable and self.kind == AliasKind.NAME:
            raise ValueError("a name cannot be declared a durable identity identifier")


@dataclass(frozen=True)
class SourcePerson:
    """One person representation established by an approved archive source."""

    source: str
    source_account: str
    source_key: str
    display_name: str
    aliases: tuple[SourceAlias, ...]
    is_archive_owner: bool = False
    source_key_is_durable: bool = False
    # Label-shaped keys must be explicitly allowed before literal extraction can use them.
    authorized_text_source_keys: tuple[str, ...] = ()
    sensitivity: Sensitivity = Sensitivity.UNCLASSIFIED

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.source_key.strip() or not self.display_name.strip():
            raise ValueError("source, source_key, and display_name must be non-blank")
        if not self.aliases:
            raise ValueError("a source person requires at least one evidenced alias")
        if any(not item.strip() for item in self.authorized_text_source_keys):
            raise ValueError("authorized text source keys must be non-blank")
        if len(set(self.authorized_text_source_keys)) != len(self.authorized_text_source_keys):
            raise ValueError("authorized text source keys must be unique")

    @property
    def identity_key(self) -> str:
        return source_identity_key(self.source, self.source_account, self.source_key)


@dataclass(frozen=True)
class IdentityOverride:
    """Durable source-membership override suitable for append-only replay.

    A merge has exactly one member group containing at least two source identity
    keys.  A split has two or more groups; every cross-group pair is forced apart.
    Storing source membership is essential: display names and generated person ids
    are not sufficient to replay a split after rebuilding source data.
    """

    sequence: int
    override_id: str
    action: IdentityOverrideAction
    member_groups: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.override_id.strip():
            raise ValueError("an identity override requires a positive sequence and id")
        if self.action == IdentityOverrideAction.MERGE:
            if len(self.member_groups) != 1 or len(self.member_groups[0]) < 2:
                raise ValueError("a merge override requires one group of at least two members")
        elif len(self.member_groups) < 2 or any(not group for group in self.member_groups):
            raise ValueError("a split override requires at least two non-empty groups")
        flattened = tuple(item for group in self.member_groups for item in group)
        if any(not item.strip() for item in flattened) or len(set(flattened)) != len(flattened):
            raise ValueError("override source memberships must be non-blank and unique")


@dataclass(frozen=True)
class IdentityDecision:
    left_source_identity: str
    right_source_identity: str
    decision: IdentityDecisionKind
    reason: str
    automatic: bool
    override_id: str | None = None


@dataclass(frozen=True)
class IdentityReviewCandidate:
    left_source_identity: str
    right_source_identity: str
    reason: str
    normalized_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityDiagnostic:
    code: IdentityDiagnosticCode
    detail: str
    source_identity_keys: tuple[str, ...] = ()
    override_id: str | None = None


@dataclass(frozen=True)
class IdentityResolutionResult:
    persons: tuple[Person, ...]
    decisions: tuple[IdentityDecision, ...]
    review_candidates: tuple[IdentityReviewCandidate, ...]
    quarantines: tuple[IdentityDiagnostic, ...]
    source_entity_resolutions: tuple[SourceEntityResolution, ...]
    source_identity_to_person_id: Mapping[str, str] = field(repr=False)
    applied_override_ids: tuple[str, ...] = ()
    unmatched_override_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_identity_to_person_id",
            MappingProxyType(dict(self.source_identity_to_person_id)),
        )


@dataclass(frozen=True)
class IdentityPolicy:
    version: str = "1"
    producer_name: str = "precision-first-person-resolver"

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.producer_name.strip():
            raise ValueError("identity policy version and producer name must be non-blank")


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
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


class PersonResolver:
    """Compile source person representations into deterministic canonical people."""

    def __init__(self, policy: IdentityPolicy | None = None):
        self.policy = policy or IdentityPolicy()

    def resolve(
        self,
        source_people: Iterable[SourcePerson],
        *,
        generation_id: str,
        created_at: datetime,
        overrides: Iterable[IdentityOverride] = (),
    ) -> IdentityResolutionResult:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        people = self._source_index(source_people)
        ordered_overrides = self._ordered_overrides(overrides)
        keys = tuple(sorted(people))
        dsu = _DisjointSet(keys)
        decisions: dict[tuple[str, str], IdentityDecision] = {}
        reviews: dict[tuple[str, str], IdentityReviewCandidate] = {}
        diagnostics: list[IdentityDiagnostic] = []
        forbidden: set[tuple[str, str]] = set()
        applied: list[str] = []
        unmatched: list[str] = []
        latest_override_by_member: dict[str, tuple[int, str]] = {}

        # Owner attribution is source-established, not inferred from a display name,
        # but it remains an automatic edge: a later user split must be able to defeat it.
        owner_keys = tuple(key for key in keys if people[key].is_archive_owner)
        automatic_edges: list[tuple[str, str, str]] = []
        for left, right in combinations(keys, 2):
            shared = self._shared_durable(people[left], people[right])
            conflicts = self._durable_conflicts(people[left], people[right])
            pair = _pair(left, right)
            both_owner = people[left].is_archive_owner and people[right].is_archive_owner
            if both_owner and not conflicts:
                automatic_edges.append((left, right, "explicit archive-owner attribution"))
                continue
            if conflicts:
                # This is a component constraint as well as a direct-pair check:
                # an otherwise accepted A-B-C chain cannot hide an A-C conflict.
                forbidden.add(pair)
            if both_owner:
                # Owner attribution is a source's claim, not a proof, and a
                # source can be wrong about it: a git checkout whose local
                # `user.email` is somebody else's address marks that person's
                # commits as the owner's, and this merged their address into the
                # owner's record with no trace.  Two identifiers that disagree
                # inside one namespace cannot both be one person's, so the pair
                # goes to review rather than being joined on the attribution
                # that is itself in question.
                detail = (
                    "both attributed to the archive owner but conflicting "
                    f"{', '.join(conflicts)}"
                )
                diagnostics.append(
                    IdentityDiagnostic(
                        IdentityDiagnosticCode.CONFLICTING_ARCHIVE_OWNER_ATTRIBUTION,
                        detail,
                        pair,
                    )
                )
                decisions[pair] = IdentityDecision(
                    left, right, IdentityDecisionKind.UNCERTAIN, detail, True
                )
                reviews[pair] = IdentityReviewCandidate(left, right, detail, tuple(shared))
                continue
            if shared and conflicts:
                detail = f"shared {', '.join(shared)} but conflicting {', '.join(conflicts)}"
                diagnostics.append(
                    IdentityDiagnostic(
                        IdentityDiagnosticCode.CONFLICTING_DURABLE_IDENTIFIERS,
                        detail,
                        pair,
                    )
                )
                decisions[pair] = IdentityDecision(
                    left, right, IdentityDecisionKind.UNCERTAIN, detail, True
                )
                reviews[pair] = IdentityReviewCandidate(left, right, detail, tuple(shared))
            elif shared:
                automatic_edges.append((left, right, f"verified exact {', '.join(shared)}"))
            else:
                same_names = self._shared_names(people[left], people[right])
                if same_names:
                    reason = "same normalized name; names alone never establish identity"
                    decisions[pair] = IdentityDecision(
                        left, right, IdentityDecisionKind.UNCERTAIN, reason, True
                    )
                    reviews[pair] = IdentityReviewCandidate(left, right, reason, same_names)

        # Overrides are interpreted against durable source membership, never prose.
        explicit_same: list[tuple[str, str, IdentityOverride]] = []
        for override in ordered_overrides:
            members = tuple(item for group in override.member_groups for item in group)
            missing = tuple(sorted(set(members) - set(keys)))
            if missing:
                unmatched.append(override.override_id)
                diagnostics.append(
                    IdentityDiagnostic(
                        IdentityDiagnosticCode.ORPHANED_OVERRIDE,
                        f"override references absent source identities: {', '.join(missing)}",
                        missing,
                        override.override_id,
                    )
                )
                continue
            applied.append(override.override_id)
            for member in members:
                latest_override_by_member[member] = (override.sequence, override.override_id)
            if override.action == IdentityOverrideAction.MERGE:
                for left, right in combinations(override.member_groups[0], 2):
                    pair = _pair(left, right)
                    self._audit_superseded_decision(decisions, diagnostics, pair, override)
                    forbidden.discard(pair)
                    explicit_same.append((*pair, override))
                    decisions[pair] = IdentityDecision(
                        *pair,
                        IdentityDecisionKind.SAME,
                        "user merge override",
                        False,
                        override.override_id,
                    )
                    reviews.pop(pair, None)
            else:
                for left_group, right_group in combinations(override.member_groups, 2):
                    for left in left_group:
                        for right in right_group:
                            pair = _pair(left, right)
                            self._audit_superseded_decision(decisions, diagnostics, pair, override)
                            forbidden.add(pair)
                            decisions[pair] = IdentityDecision(
                                *pair,
                                IdentityDecisionKind.DIFFERENT,
                                "user split override",
                                False,
                                override.override_id,
                            )
                            reviews.pop(pair, None)

        # Explicit decisions take precedence, then conservative automatic links.
        for left, right, override in explicit_same:
            current = decisions.get(_pair(left, right))
            if (
                current is None
                or current.decision != IdentityDecisionKind.SAME
                or current.override_id != override.override_id
            ):
                continue
            if not self._accept_edge(dsu, left, right, forbidden):
                diagnostics.append(
                    IdentityDiagnostic(
                        IdentityDiagnosticCode.OVERRIDE_CONFLICT,
                        "merge would violate a later durable split",
                        _pair(left, right),
                        override.override_id,
                    )
                )
        for left, right, reason in sorted(automatic_edges):
            pair = _pair(left, right)
            if self._accept_edge(dsu, left, right, forbidden):
                if pair not in forbidden and decisions.get(pair) is None:
                    decisions[pair] = IdentityDecision(
                        left, right, IdentityDecisionKind.SAME, reason, True
                    )
            else:
                if decisions.get(pair) is None:
                    decisions[pair] = IdentityDecision(
                        left,
                        right,
                        IdentityDecisionKind.UNCERTAIN,
                        f"{reason}, but a split constraint prevents the merge",
                        True,
                    )

        clusters: dict[str, tuple[str, ...]] = {}
        for key in keys:
            clusters.setdefault(dsu.find(key), tuple())
        for root in tuple(clusters):
            clusters[root] = tuple(sorted(dsu.members(root)))
        self._assert_no_false_merges(clusters.values(), forbidden, decisions)

        # A split can reveal mutually inconsistent source-owner assertions.  Keep one
        # deterministic canonical owner and quarantine every disputed attribution rather
        # than silently exposing two archive owners as significant people.
        canonical_owner_key = min(owner_keys) if owner_keys else None
        if canonical_owner_key is not None:
            canonical_owner_root = dsu.find(canonical_owner_key)
            for owner_key in owner_keys:
                if dsu.find(owner_key) == canonical_owner_root:
                    continue
                diagnostics.append(
                    IdentityDiagnostic(
                        IdentityDiagnosticCode.CONFLICTING_ARCHIVE_OWNER_ATTRIBUTION,
                        "two source-established owner aliases stayed in separate clusters, "
                        "by a user split or by durable identifiers that disagree; "
                        "this source attribution is quarantined as non-owner",
                        (owner_key,),
                        latest_override_by_member.get(owner_key, (0, ""))[1] or None,
                    )
                )

        canonical_people: list[Person] = []
        mapping: dict[str, str] = {}
        resolutions: list[SourceEntityResolution] = []
        for members in sorted(clusters.values()):
            person = self._person(
                members,
                people,
                generation_id,
                created_at,
                latest_override_by_member,
                canonical_owner_key,
            )
            canonical_people.append(person)
            for member in members:
                mapping[member] = person.id
                source_person = people[member]
                allowed_keys = set(source_person.authorized_text_source_keys)
                if source_person.source_key_is_durable or source_person.source_key in allowed_keys:
                    allowed_keys.add(source_person.source_key)
                for key in sorted(allowed_keys):
                    resolutions.append(
                        SourceEntityResolution(
                            source=source_person.source,
                            source_account=source_person.source_account,
                            source_key=key,
                            kind=EntityKind.PERSON,
                            entity_id=person.id,
                        )
                    )

        return IdentityResolutionResult(
            persons=tuple(sorted(canonical_people, key=lambda item: item.id)),
            decisions=tuple(decisions[key] for key in sorted(decisions)),
            review_candidates=tuple(reviews[key] for key in sorted(reviews)),
            quarantines=tuple(diagnostics),
            source_entity_resolutions=tuple(
                sorted(
                    resolutions,
                    key=lambda item: (
                        item.source,
                        item.source_account,
                        item.source_key,
                        item.entity_id,
                    ),
                )
            ),
            source_identity_to_person_id=mapping,
            applied_override_ids=tuple(applied),
            unmatched_override_ids=tuple(unmatched),
        )

    @staticmethod
    def _source_index(source_people: Iterable[SourcePerson]) -> dict[str, SourcePerson]:
        result: dict[str, SourcePerson] = {}
        for person in source_people:
            prior = result.get(person.identity_key)
            if prior is not None and prior != person:
                raise ValueError(f"conflicting source person for {person.identity_key}")
            result[person.identity_key] = person
        return result

    @staticmethod
    def _ordered_overrides(overrides: Iterable[IdentityOverride]) -> tuple[IdentityOverride, ...]:
        ordered = tuple(sorted(overrides, key=lambda item: item.sequence))
        sequences = tuple(item.sequence for item in ordered)
        ids = tuple(item.override_id for item in ordered)
        if len(set(sequences)) != len(sequences) or len(set(ids)) != len(ids):
            raise ValueError("override sequences and ids must be unique")
        return ordered

    @staticmethod
    def _audit_superseded_decision(
        decisions: dict[tuple[str, str], IdentityDecision],
        diagnostics: list[IdentityDiagnostic],
        pair: tuple[str, str],
        override: IdentityOverride,
    ) -> None:
        prior = decisions.get(pair)
        if prior is None or prior.override_id is None or prior.override_id == override.override_id:
            return
        diagnostics.append(
            IdentityDiagnostic(
                IdentityDiagnosticCode.SUPERSEDED_OVERRIDE,
                f"override {prior.override_id} was superseded by later override "
                f"{override.override_id} for this identity pair",
                pair,
                prior.override_id,
            )
        )

    @staticmethod
    def _identifier_map(person: SourcePerson) -> dict[tuple[AliasKind, str], set[str]]:
        result: dict[tuple[AliasKind, str], set[str]] = {}
        if person.source_key_is_durable:
            source_scope = source_identity_key(person.source, person.source_account, "account")
            result.setdefault((AliasKind.SOURCE_ACCOUNT_ID, source_scope), set()).add(
                normalize_alias(AliasKind.SOURCE_ACCOUNT_ID, person.source_key)
            )
        for alias in person.aliases:
            if not alias.verified_durable:
                continue
            default_scope = (
                "global"
                if alias.kind in {AliasKind.EMAIL, AliasKind.PHONE}
                else source_identity_key(person.source, person.source_account, "account")
            )
            scope = alias.identifier_scope or default_scope
            normalized = normalize_alias(alias.kind, alias.exact_value)
            if normalized:
                result.setdefault((alias.kind, scope), set()).add(normalized)
        return result

    def _shared_durable(self, left: SourcePerson, right: SourcePerson) -> tuple[str, ...]:
        left_ids, right_ids = self._identifier_map(left), self._identifier_map(right)
        shared: list[str] = []
        namespaces = sorted(set(left_ids) & set(right_ids), key=lambda item: (item[0], item[1]))
        for namespace in namespaces:
            values = left_ids[namespace] & right_ids[namespace]
            shared.extend(
                f"{namespace[0].value}:{namespace[1]}:{value}" for value in sorted(values)
            )
        return tuple(shared)

    def _durable_conflicts(self, left: SourcePerson, right: SourcePerson) -> tuple[str, ...]:
        left_ids, right_ids = self._identifier_map(left), self._identifier_map(right)
        conflicts = []
        namespaces = sorted(set(left_ids) & set(right_ids), key=lambda item: (item[0], item[1]))
        for namespace in namespaces:
            if left_ids[namespace].isdisjoint(right_ids[namespace]):
                conflicts.append(f"{namespace[0].value}:{namespace[1]}")
        return tuple(conflicts)

    @staticmethod
    def _shared_names(left: SourcePerson, right: SourcePerson) -> tuple[str, ...]:
        def names(person: SourcePerson) -> set[str]:
            return {
                normalize_alias(alias.kind, alias.exact_value)
                for alias in person.aliases
                if alias.kind == AliasKind.NAME
            }

        return tuple(sorted(names(left) & names(right) - {""}))

    @staticmethod
    def _accept_edge(
        dsu: _DisjointSet,
        left: str,
        right: str,
        forbidden: set[tuple[str, str]],
    ) -> bool:
        combined = dsu.members(left) | dsu.members(right)
        if any(_pair(first, second) in forbidden for first, second in combinations(combined, 2)):
            return False
        dsu.union(left, right)
        return True

    def _person(
        self,
        members: tuple[str, ...],
        source_people: dict[str, SourcePerson],
        generation_id: str,
        created_at: datetime,
        latest_override_by_member: dict[str, tuple[int, str]],
        canonical_owner_key: str | None,
    ) -> Person:
        inputs = tuple(source_people[key] for key in members)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"members": members, "identity_policy_version": self.policy.version},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        aliases: dict[tuple[object, ...], EntityAlias] = {}
        evidence_ids: set[str] = set()
        for source_person in inputs:
            for alias in source_person.aliases:
                evidence_ids.update(alias.evidence_entry_ids)
                normalized = normalize_alias(alias.kind, alias.exact_value)
                entity_alias = EntityAlias(
                    kind=alias.kind,
                    exact_value=alias.exact_value,
                    normalized_value=normalized,
                    source=source_person.source,
                    source_account=source_person.source_account,
                    verification=(
                        VerificationState.AUTOMATIC
                        if alias.verified_durable
                        else VerificationState.CANDIDATE
                    ),
                    evidence_entry_ids=tuple(sorted(alias.evidence_entry_ids)),
                )
                key = (
                    entity_alias.kind,
                    entity_alias.exact_value,
                    entity_alias.normalized_value,
                    entity_alias.source,
                    entity_alias.source_account,
                    entity_alias.verification,
                    entity_alias.evidence_entry_ids,
                )
                aliases[key] = entity_alias
        if not evidence_ids:
            raise ValueError("a canonical person requires source evidence")
        # The name on a person's card is the first thing anybody reads, and the
        # source identities in one cluster do not agree on it: an account id, a
        # phone number, and the word a connector writes when an export does not
        # say who sent a message all sit beside a real name.  Taking the lowest
        # identity key took whichever of those sorted first -- for the archive
        # owner that was Telegram's "me", and the Mirror titled his own page
        # with it, though the same person was `Egor` in every git commit he had
        # ever written.  Prefer a source identity that names somebody.  Where
        # none does the old order still decides, so a person known only by a
        # number is named the same way as before.
        named = tuple(item for item in inputs if names_something(item.display_name))
        display_source = min(named or inputs, key=lambda item: item.identity_key)
        if canonical_owner_key in members:
            owner_source = source_people[canonical_owner_key]
            if not named or names_something(owner_source.display_name):
                display_source = owner_source
        override_markers = {
            latest_override_by_member[member]
            for member in members
            if member in latest_override_by_member
        }
        override_id = max(override_markers)[1] if override_markers else None
        sensitivity = max(
            (item.sensitivity for item in inputs),
            key=lambda item: {
                Sensitivity.PUBLIC: 0,
                Sensitivity.PERSONAL: 1,
                Sensitivity.SENSITIVE: 2,
                Sensitivity.UNCLASSIFIED: 3,
            }[item],
        )
        return Person(
            id=f"person:{fingerprint}",
            generation_id=generation_id,
            evidence=tuple(
                EvidenceReference(entry_id=item, role=EvidenceRole.MENTIONS)
                for item in sorted(evidence_ids)
            ),
            confidence=1.0 if canonical_owner_key in members else 0.95,
            sensitivity=sensitivity,
            derivation=DerivationKind.EXPLICIT,
            producer=ProducerMetadata(
                kind=ProducerKind.DETERMINISTIC,
                name=self.policy.producer_name,
                version=self.policy.version,
                rule_version=self.policy.version,
            ),
            readiness=Readiness.STAGED,
            created_at=created_at,
            canonical_key=f"person-source-membership:{fingerprint}",
            display_name=display_source.display_name,
            aliases=tuple(aliases[key] for key in sorted(aliases, key=lambda value: str(value))),
            is_archive_owner=canonical_owner_key in members,
            identity_override_id=override_id,
        )

    @staticmethod
    def _assert_no_false_merges(
        clusters: Iterable[tuple[str, ...]],
        forbidden: set[tuple[str, str]],
        decisions: dict[tuple[str, str], IdentityDecision],
    ) -> None:
        for members in clusters:
            for left, right in combinations(members, 2):
                pair = _pair(left, right)
                if pair in forbidden:
                    raise AssertionError("identity cluster violates a split constraint")
                decision = decisions.get(pair)
                if decision is not None and decision.decision == IdentityDecisionKind.DIFFERENT:
                    raise AssertionError("identity cluster contains a DIFFERENT decision")


def _pair(left: str, right: str) -> tuple[str, str]:
    if left == right:
        raise ValueError("identity decisions require two different source identities")
    return tuple(sorted((left, right)))  # type: ignore[return-value]


__all__ = [
    "IdentityDecision",
    "IdentityDecisionKind",
    "IdentityDiagnostic",
    "IdentityDiagnosticCode",
    "IdentityOverride",
    "IdentityOverrideAction",
    "IdentityPolicy",
    "IdentityResolutionResult",
    "IdentityReviewCandidate",
    "PersonResolver",
    "SourceAlias",
    "SourcePerson",
    "normalize_alias",
    "source_identity_key",
]

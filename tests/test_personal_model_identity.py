from __future__ import annotations

from datetime import UTC, datetime

import pytest

from varyz.personal_model.identity import (
    IdentityDecisionKind,
    IdentityDiagnosticCode,
    IdentityOverride,
    IdentityOverrideAction,
    IdentityPolicy,
    PersonResolver,
    SourceAlias,
    SourcePerson,
    normalize_alias,
)
from varyz.personal_model.models import AliasKind, EntityKind

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def alias(
    value: str,
    entry: str,
    *,
    kind: AliasKind = AliasKind.NAME,
    durable: bool = False,
    scope: str | None = None,
) -> SourceAlias:
    return SourceAlias(
        kind=kind,
        exact_value=value,
        evidence_entry_ids=(entry,),
        verified_durable=durable,
        identifier_scope=scope,
    )


def source_person(
    source: str,
    source_key: str,
    name: str,
    *aliases: SourceAlias,
    account: str = "primary",
    owner: bool = False,
    durable_source_key: bool = True,
    authorized_text_keys: tuple[str, ...] = (),
) -> SourcePerson:
    return SourcePerson(
        source=source,
        source_account=account,
        source_key=source_key,
        display_name=name,
        aliases=aliases or (alias(name, f"entry-{source}-{source_key}"),),
        is_archive_owner=owner,
        source_key_is_durable=durable_source_key,
        authorized_text_source_keys=authorized_text_keys,
    )


def resolve(*people: SourcePerson, overrides: tuple[IdentityOverride, ...] = ()):
    return PersonResolver(IdentityPolicy(version="test-v1")).resolve(
        people,
        generation_id="generation-1",
        created_at=NOW,
        overrides=overrides,
    )


def test_archive_owner_aliases_across_connectors_form_one_canonical_owner() -> None:
    telegram = source_person(
        "telegram",
        "id:tg-1",
        "Egor",
        alias("Егор", "tg-entry"),
        owner=True,
    )
    whatsapp = source_person(
        "whatsapp",
        "id:wa-9",
        "Egor",
        alias("Egor", "wa-entry"),
        owner=True,
    )

    result = resolve(whatsapp, telegram)

    assert len(result.persons) == 1
    assert result.persons[0].is_archive_owner is True
    assert sum(person.is_archive_owner for person in result.persons) == 1
    assert set(result.source_identity_to_person_id.values()) == {result.persons[0].id}
    assert result.decisions[0].decision == IdentityDecisionKind.SAME


def test_verified_owner_identifier_absorbs_an_unmarked_self_representation() -> None:
    owner = source_person(
        "telegram",
        "id:self",
        "Egor",
        alias("egor@example.com", "self-email", kind=AliasKind.EMAIL, durable=True),
        owner=True,
    )
    unmarked_self = source_person(
        "email",
        "id:sender",
        "E. K.",
        alias("EGOR@example.com", "mail-email", kind=AliasKind.EMAIL, durable=True),
    )

    result = resolve(owner, unmarked_self)

    assert len(result.persons) == 1
    assert result.persons[0].is_archive_owner is True


def test_user_split_outranks_two_source_owner_attributions_without_two_owners() -> None:
    first = source_person("telegram", "id:self", "Egor", owner=True)
    second = source_person("whatsapp", "id:self", "Egor", owner=True)
    split = IdentityOverride(
        sequence=7,
        override_id="owner-split",
        action=IdentityOverrideAction.SPLIT,
        member_groups=((first.identity_key,), (second.identity_key,)),
    )

    result = resolve(first, second, overrides=(split,))

    assert len(result.persons) == 2
    assert sum(person.is_archive_owner for person in result.persons) == 1
    assert result.decisions[0].decision == IdentityDecisionKind.DIFFERENT
    assert any(
        item.code == IdentityDiagnosticCode.CONFLICTING_ARCHIVE_OWNER_ATTRIBUTION
        for item in result.quarantines
    )


def test_same_anna_label_in_different_accounts_never_auto_merges() -> None:
    first = source_person("telegram", "label:Anna", "Anna", account="one", durable_source_key=False)
    second = source_person(
        "whatsapp", "label:Anna", "ANNA", account="two", durable_source_key=False
    )

    result = resolve(first, second)

    assert len(result.persons) == 2
    assert [item.decision for item in result.decisions] == [IdentityDecisionKind.UNCERTAIN]
    assert len(result.review_candidates) == 1
    assert result.source_entity_resolutions == ()


@pytest.mark.parametrize(
    ("kind", "left", "right"),
    [
        (AliasKind.EMAIL, "Anna@Example.com", "anna@example.com"),
        (AliasKind.PHONE, "+34 (600) 123-456", "0034 600 123 456"),
    ],
)
def test_verified_global_phone_or_email_can_merge(kind: AliasKind, left: str, right: str) -> None:
    first = source_person(
        "telegram",
        "id:1",
        "Anna",
        alias(left, "one", kind=kind, durable=True),
    )
    second = source_person(
        "whatsapp",
        "id:2",
        "Anya",
        alias(right, "two", kind=kind, durable=True),
    )

    result = resolve(first, second)

    assert len(result.persons) == 1
    assert result.decisions[0].decision == IdentityDecisionKind.SAME
    assert result.decisions[0].automatic is True


def test_verified_source_account_ids_only_match_inside_one_connected_account() -> None:
    same_account_a = source_person(
        "telegram",
        "label:A",
        "A",
        alias("User-44", "one", kind=AliasKind.SOURCE_ACCOUNT_ID, durable=True),
        durable_source_key=False,
    )
    same_account_b = source_person(
        "telegram",
        "id:44",
        "A renamed",
        alias("User-44", "two", kind=AliasKind.SOURCE_ACCOUNT_ID, durable=True),
    )
    other_account = source_person(
        "telegram",
        "id:44",
        "Someone else",
        alias("User-44", "three", kind=AliasKind.SOURCE_ACCOUNT_ID, durable=True),
        account="secondary",
    )

    result = resolve(same_account_a, same_account_b, other_account)

    assert len(result.persons) == 2
    assert (
        result.source_identity_to_person_id[same_account_a.identity_key]
        == (result.source_identity_to_person_id[same_account_b.identity_key])
    )
    assert (
        result.source_identity_to_person_id[other_account.identity_key]
        != (result.source_identity_to_person_id[same_account_a.identity_key])
    )


def test_source_account_identifier_comparison_is_case_sensitive() -> None:
    upper = source_person(
        "provider",
        "label:upper",
        "Upper",
        alias("ABC", "upper", kind=AliasKind.SOURCE_ACCOUNT_ID, durable=True),
        durable_source_key=False,
    )
    lower = source_person(
        "provider",
        "label:lower",
        "Lower",
        alias("abc", "lower", kind=AliasKind.SOURCE_ACCOUNT_ID, durable=True),
        durable_source_key=False,
    )

    assert len(resolve(upper, lower).persons) == 2


def test_conflicting_verified_identifiers_stay_split_and_quarantined() -> None:
    first = source_person(
        "telegram",
        "id:1",
        "Anna",
        alias("+34600123456", "one-phone", kind=AliasKind.PHONE, durable=True),
        alias("anna.one@example.com", "one-email", kind=AliasKind.EMAIL, durable=True),
    )
    second = source_person(
        "whatsapp",
        "id:2",
        "Anna",
        alias("+34600123456", "two-phone", kind=AliasKind.PHONE, durable=True),
        alias("anna.two@example.com", "two-email", kind=AliasKind.EMAIL, durable=True),
    )

    result = resolve(first, second)

    assert len(result.persons) == 2
    assert result.decisions[0].decision == IdentityDecisionKind.UNCERTAIN
    assert result.quarantines[0].code == IdentityDiagnosticCode.CONFLICTING_DURABLE_IDENTIFIERS


def test_conflict_cannot_be_bypassed_by_a_transitive_identifier_chain() -> None:
    first = source_person(
        "telegram",
        "id:a",
        "A",
        alias("shared@example.com", "a-email", kind=AliasKind.EMAIL, durable=True),
        alias("+34111", "a-phone", kind=AliasKind.PHONE, durable=True),
    )
    bridge = source_person(
        "email",
        "id:b",
        "B",
        alias("shared@example.com", "b-email", kind=AliasKind.EMAIL, durable=True),
        alias("+34222", "b-phone", kind=AliasKind.PHONE, durable=True),
        alias("bridge", "b-user", kind=AliasKind.USERNAME, durable=True, scope="network"),
    )
    third = source_person(
        "whatsapp",
        "id:c",
        "C",
        alias("bridge", "c-user", kind=AliasKind.USERNAME, durable=True, scope="network"),
        alias("+34333", "c-phone", kind=AliasKind.PHONE, durable=True),
    )

    result = resolve(first, bridge, third)

    person_memberships: dict[str, set[str]] = {}
    for source_key, person_id in result.source_identity_to_person_id.items():
        person_memberships.setdefault(person_id, set()).add(source_key)
    assert max(map(len, person_memberships.values())) < 3


def test_multilingual_case_and_diacritic_normalization_only_creates_review() -> None:
    assert normalize_alias(AliasKind.NAME, "  ÁNNA  ") == "anna"
    assert normalize_alias(AliasKind.NAME, "ЁГОР") == "егор"
    first = source_person("one", "label:1", "Ánna", durable_source_key=False)
    second = source_person("two", "label:2", "ANNA", durable_source_key=False)

    result = resolve(first, second)

    assert len(result.persons) == 2
    assert result.review_candidates[0].normalized_aliases == ("anna",)


def test_merge_then_later_durable_split_override_replays_without_rewriting_aliases() -> None:
    first = source_person("telegram", "id:1", "Anna")
    second = source_person("whatsapp", "id:2", "Anya")
    merge = IdentityOverride(
        sequence=1,
        override_id="merge-1",
        action=IdentityOverrideAction.MERGE,
        member_groups=((first.identity_key, second.identity_key),),
    )
    split = IdentityOverride(
        sequence=2,
        override_id="split-2",
        action=IdentityOverrideAction.SPLIT,
        member_groups=((first.identity_key,), (second.identity_key,)),
    )

    merged = resolve(first, second, overrides=(merge,))
    split_result = resolve(first, second, overrides=(split, merge))

    assert len(merged.persons) == 1
    assert len(split_result.persons) == 2
    assert split_result.decisions[0].decision == IdentityDecisionKind.DIFFERENT
    assert split_result.decisions[0].override_id == "split-2"
    assert split_result.applied_override_ids == ("merge-1", "split-2")
    assert {a.exact_value for p in split_result.persons for a in p.aliases} == {"Anna", "Anya"}
    assert any(
        item.code == IdentityDiagnosticCode.SUPERSEDED_OVERRIDE and item.override_id == "merge-1"
        for item in split_result.quarantines
    )


def test_split_then_later_merge_override_wins_by_ledger_sequence_and_is_audited() -> None:
    first = source_person("telegram", "id:1", "Anna")
    second = source_person("whatsapp", "id:2", "Anya")
    split = IdentityOverride(
        sequence=3,
        override_id="split-first",
        action=IdentityOverrideAction.SPLIT,
        member_groups=((first.identity_key,), (second.identity_key,)),
    )
    merge = IdentityOverride(
        sequence=4,
        override_id="merge-later",
        action=IdentityOverrideAction.MERGE,
        member_groups=((first.identity_key, second.identity_key),),
    )

    result = resolve(first, second, overrides=(merge, split))

    assert len(result.persons) == 1
    assert result.decisions[0].decision == IdentityDecisionKind.SAME
    assert result.decisions[0].override_id == "merge-later"
    assert result.applied_override_ids == ("split-first", "merge-later")
    assert any(
        item.code == IdentityDiagnosticCode.SUPERSEDED_OVERRIDE
        and item.override_id == "split-first"
        for item in result.quarantines
    )


def test_deleted_source_membership_leaves_override_unmatched_and_visible() -> None:
    remaining = source_person("telegram", "id:1", "Anna")
    absent_key = source_person("whatsapp", "id:2", "Anya").identity_key
    override = IdentityOverride(
        sequence=1,
        override_id="merge-missing",
        action=IdentityOverrideAction.MERGE,
        member_groups=((remaining.identity_key, absent_key),),
    )

    result = resolve(remaining, overrides=(override,))

    assert result.applied_override_ids == ()
    assert result.unmatched_override_ids == ("merge-missing",)
    assert result.quarantines[0].code == IdentityDiagnosticCode.ORPHANED_OVERRIDE


def test_input_and_override_order_do_not_change_result() -> None:
    first = source_person(
        "telegram",
        "id:1",
        "Anna",
        alias("anna@example.com", "one", kind=AliasKind.EMAIL, durable=True),
    )
    second = source_person(
        "email",
        "id:2",
        "Anya",
        alias("ANNA@example.com", "two", kind=AliasKind.EMAIL, durable=True),
    )
    third = source_person("whatsapp", "id:3", "Boris")
    forward = resolve(first, second, third)
    reverse = resolve(third, second, first)

    assert [item.model_dump(mode="json") for item in forward.persons] == [
        item.model_dump(mode="json") for item in reverse.persons
    ]
    assert forward.decisions == reverse.decisions
    assert forward.source_identity_to_person_id == reverse.source_identity_to_person_id


def test_the_owner_is_named_by_a_name_and_not_by_the_word_me() -> None:
    """A Telegram export writes `me` for the owner's own side of a chat."""

    telegram = source_person(
        "telegram",
        "label:me",
        "me",
        alias("egor@example.com", "tg-entry", kind=AliasKind.EMAIL, durable=True),
        owner=True,
        durable_source_key=False,
    )
    git = source_person(
        "git",
        "id:egor@example.com",
        "Egor",
        alias("egor@example.com", "git-entry", kind=AliasKind.EMAIL, durable=True),
    )

    result = resolve(telegram, git)

    assert len(result.persons) == 1
    assert result.persons[0].is_archive_owner
    assert result.persons[0].display_name == "Egor"


def test_a_person_known_only_by_an_account_keeps_that_account_as_a_name() -> None:
    unnamed = source_person("telegram", "id:user6000000001", "user6000000001")

    result = resolve(unnamed)

    assert result.persons[0].display_name == "user6000000001"


def test_display_name_rewording_does_not_change_stable_person_identity() -> None:
    original = source_person("telegram", "id:1", "Anna")
    renamed = source_person("telegram", "id:1", "Anya", alias("Anya", "entry-telegram-id:1"))

    original_result = resolve(original)
    renamed_result = resolve(renamed)

    assert original_result.persons[0].id == renamed_result.persons[0].id
    assert original_result.persons[0].canonical_key == renamed_result.persons[0].canonical_key
    assert original_result.persons[0].display_name != renamed_result.persons[0].display_name


def test_observation_resolution_includes_structured_ids_but_text_only_when_authorized() -> None:
    structured = source_person("telegram", "id:44", "Anna")
    label_unapproved = source_person("whatsapp", "label:Anna", "Anna", durable_source_key=False)
    label_approved = source_person(
        "instagram",
        "label:Анна",
        "Анна",
        durable_source_key=False,
        authorized_text_keys=("label:Анна",),
    )

    result = resolve(structured, label_unapproved, label_approved)
    mappings = {
        (item.source, item.source_account, item.source_key, item.kind)
        for item in result.source_entity_resolutions
    }

    assert ("telegram", "primary", "id:44", EntityKind.PERSON) in mappings
    assert ("instagram", "primary", "label:Анна", EntityKind.PERSON) in mappings
    assert ("whatsapp", "primary", "label:Anna", EntityKind.PERSON) not in mappings


def test_name_only_uncertain_edge_cannot_bridge_durable_clusters() -> None:
    first = source_person(
        "one",
        "id:1",
        "Anna",
        alias("Anna", "a-name"),
        alias("one@example.com", "a-email", kind=AliasKind.EMAIL, durable=True),
    )
    same_as_first = source_person(
        "two",
        "id:2",
        "Anya",
        alias("ONE@example.com", "b-email", kind=AliasKind.EMAIL, durable=True),
    )
    name_only = source_person("three", "label:3", "ÁNNA", durable_source_key=False)

    result = resolve(first, same_as_first, name_only)

    assert len(result.persons) == 2
    assert (
        result.source_identity_to_person_id[first.identity_key]
        == (result.source_identity_to_person_id[same_as_first.identity_key])
    )
    assert (
        result.source_identity_to_person_id[name_only.identity_key]
        != (result.source_identity_to_person_id[first.identity_key])
    )


def test_invalid_split_without_durable_membership_is_rejected() -> None:
    with pytest.raises(ValueError, match="split override"):
        IdentityOverride(
            sequence=1,
            override_id="bad",
            action=IdentityOverrideAction.SPLIT,
            member_groups=(("only-one",),),
        )


def test_two_owner_marked_git_addresses_go_to_review_rather_than_merging() -> None:
    """A checkout configured with somebody else's address still says "owner".

    `git config user.email` is the only owner signal a repository carries, so a
    folder whose local config holds a colleague's address marks that person's
    commits as the archive owner's.  Owner attribution used to be an
    unconditional merge edge -- it skipped the durable-conflict check entirely --
    and so a stranger's address joined the owner's record with nothing in the
    run naming it.  One address per namespace: they cannot both be one person's.
    """

    mine = source_person(
        "git",
        "id:owner@example.com",
        "Egor",
        alias(
            "owner@example.com",
            "commit-1",
            kind=AliasKind.SOURCE_ACCOUNT_ID,
            durable=True,
            scope="git:primary",
        ),
        owner=True,
    )
    theirs = source_person(
        "git",
        "id:someone-else@example.com",
        "Someone Else",
        alias(
            "someone-else@example.com",
            "commit-2",
            kind=AliasKind.SOURCE_ACCOUNT_ID,
            durable=True,
            scope="git:primary",
        ),
        owner=True,
    )

    result = resolve(mine, theirs)

    assert len(result.persons) == 2
    assert sum(person.is_archive_owner for person in result.persons) == 1
    assert result.decisions[0].decision == IdentityDecisionKind.UNCERTAIN
    assert result.review_candidates, "the disputed pair must be offered for review"
    assert any(
        item.code == IdentityDiagnosticCode.CONFLICTING_ARCHIVE_OWNER_ATTRIBUTION
        for item in result.quarantines
    )


def test_owner_attribution_still_merges_when_nothing_durable_disagrees() -> None:
    """The fix must not cost the owner the identities that genuinely are theirs."""

    telegram = source_person("telegram", "id:tg-1", "Egor", owner=True)
    git = source_person(
        "git",
        "id:owner@example.com",
        "Egor",
        alias(
            "owner@example.com",
            "commit-1",
            kind=AliasKind.SOURCE_ACCOUNT_ID,
            durable=True,
            scope="git:primary",
        ),
        owner=True,
    )

    result = resolve(telegram, git)

    assert len(result.persons) == 1
    assert result.persons[0].is_archive_owner is True

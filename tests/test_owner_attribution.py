"""A message written *to* the owner cannot become one of the owner's values.

The defect this pins is real and was found in the live archive: a whatsapp entry
headed `From: me` and flagged `authored_by_user=1`, whose body is somebody
else's affectionate letter to the owner — second person throughout, his name
used to call him. Varyz filed its belief under the archive's owner, so another
person's declaration was displayed back to him as something *he* believes. The
fixture below is written to that shape rather than quoted from the message,
which is not this repository's to carry.

Nothing in that text distinguishes it from something he wrote — the only tell
is the vocative name, and until the owner tells varyz what he is called there
is nothing to match. So the guard is off by default and every test here that
does not configure a name asserts the old behaviour is untouched.
"""

from __future__ import annotations

import pytest

from varyz.config import Config
from varyz.graph.extract import (
    DeterministicExtractor,
    _is_me,
    addresses_owner,
    owner_version_suffix,
)

LETTER = (
    "I read the thing you sent, it's lovely. Thank you. You matter a lot to me "
    "Egor, I really do need you around. You know how little I ask anyone for, "
    "but you're the one person I don't have to keep my guard up with."
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (LETTER, True),
        ("Egor, you're like a librarian who found the keys to the archive.", True),
        ("Dear Egor, you should keep your academic trajectory.", True),
        # Second person, but never his name: an ordinary message to anybody.
        ("You mean so much to me, I really need you.", False),
        # His name, but nobody is being spoken to: him mentioned in passing, or
        # signing something he wrote.
        ("Egor pushed the release this morning.", False),
        ("— Egor", False),
        # Both, and still his. These are the shapes that cost real claims when
        # any mention counted: his own planning note weighing his own habits,
        # and him relaying a message about himself.
        ("The policy scores 'would Egor actually build this?' before you ship.", False),
        ("Tell Egor I said hi, would you?", False),
        # A letter he was sent, quoted inside a message he wrote. The vocative
        # is real and belongs to somebody else's text; the sentences around it
        # are his and stay his.
        ("She said: Dear Egor, you should reapply. I think she's guessing.", False),
    ],
)
def test_a_message_that_names_the_owner_and_speaks_to_him_is_not_his(body, expected):
    assert addresses_owner(body, ["Egor"]) is expected


def test_the_guard_is_inert_until_the_owner_says_what_they_are_called():
    assert addresses_owner(LETTER, []) is False
    assert Config().owner.names == []


def test_the_authorship_flag_still_wins_for_everything_else():
    """The flag is asked first and stays first.

    Name-matching alone once filed 3,849 of his own Telegram messages under a
    stranger, so this must narrow the flag and nothing more.
    """
    assert _is_me(
        "Egor Menshikov",
        "",
        authored_by_user=True,
        body="I decided to go to a university overseas.",
        owner_names=["Egor"],
    )
    assert _is_me("me", "", body=LETTER, owner_names=["Egor"])


def test_a_letter_addressed_to_the_owner_loses_its_claim_to_being_his():
    assert not _is_me(
        "me", "", authored_by_user=True, body=LETTER, owner_names=["Egor"]
    )
    # Told nothing, varyz cannot tell, and says so by leaving the entry alone.
    assert _is_me("me", "", authored_by_user=True, body=LETTER, owner_names=[])


def test_the_extractor_carries_the_names_it_is_given():
    assert DeterministicExtractor().owner_names == ()
    assert DeterministicExtractor(["Egor", "Егор"]).owner_names == ("Egor", "Егор")


def test_naming_the_owner_makes_the_rebuild_a_different_reading():
    """Otherwise filling in `[owner] names` and rebuilding does nothing.

    The generation key and the extraction cache scope are both built from the
    extractor's version, so a reader that has learnt the owner's name has to say
    so there — a run that looks identical to one already done is skipped, and
    every misattributed claim survives the fix meant to remove it.
    """
    assert owner_version_suffix([]) == ""
    assert owner_version_suffix(["  ", ""]) == ""
    # Order and case are how a name is typed, not which name it is.
    assert owner_version_suffix(["Egor", "Егор"]) == owner_version_suffix(
        ["  егор ", "EGOR"]
    )
    assert owner_version_suffix(["Egor"]) != owner_version_suffix(["Егор"])

    plain = DeterministicExtractor()
    named = DeterministicExtractor(["Egor"])
    assert named.version != plain.version
    assert named.version.startswith(plain.version)


def test_a_letter_to_the_owner_yields_no_claim_of_his_at_all(conn):
    """End to end: the graph must not hold the belief as his.

    Built twice over the same entry — once told his name and once not — so the
    assertion is about the guard and not about what the patterns happen to find.
    """
    from varyz.db import dao
    from varyz.domain import Kind, NormalizedEntry, Sensitivity
    from varyz.evidence import SQLiteEvidenceReader
    from varyz.graph.extract import GraphBuildService
    from varyz.graph.identity import self_entity_id
    from varyz.graph.repository import GraphRepository

    body = f"From: me\nChat: +34\n\n{LETTER} I believe you deserve everything."
    with conn:
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id="letter",
                body=body,
                authored_by_user=True,
            ),
            source="whatsapp",
            sensitivity=Sensitivity.PERSONAL,
        )

    def claims_attributed_to_self(owner_names):
        GraphBuildService(
            GraphRepository(conn),
            SQLiteEvidenceReader(conn),
            DeterministicExtractor(owner_names),
        ).rebuild()
        generation = conn.execute(
            "SELECT id FROM graph_runs WHERE state='active' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        # By id, not by label: the sender entity minted for the header
        # "From: me" is labelled "me", and the label column collates without
        # case, so comparing names would call the two the same person again.
        return conn.execute(
            """SELECT COUNT(*) FROM claims
               WHERE graph_generation_id = ? AND subject_entity_id = ?""",
            (generation, self_entity_id(conn)),
        ).fetchone()[0]

    assert claims_attributed_to_self([]) >= 1
    assert claims_attributed_to_self(["Egor"]) == 0

"""In a group chat the speaker is not the room.

Found in the live archive and measured before this was written: 6 Telegram
groups had become 6 "people", and 84 claims of the active generation's 1,118
were filed under whoever happened to speak in them first — everything said in
the room, attributed to one invented person. A thread id is the right handle
for a speaker only when the thread holds exactly one of them, which each
transport states in its own spelling: `@g.us` for WhatsApp, a negative id for
Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.evidence import SQLiteEvidenceReader
from varyz.graph import extract as extract_module
from varyz.graph.extract import DeterministicExtractor, GraphBuildService, _is_one_to_one
from varyz.graph.repository import GraphRepository
from varyz.graph.service import rebuild_graph

START = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)


class _Entry:
    """Just the two fields the room test reads."""

    def __init__(self, source: str, thread_id: str | None):
        self.source = source
        self.thread_id = thread_id


@pytest.mark.parametrize(
    ("source", "thread_id", "expected"),
    [
        # Telegram states it in the sign, and `canonical_chat_id` restates an
        # export's all-positive numbering to match MTProto so this holds for
        # both shapes of the same archive.
        ("telegram", "tg:7000000123", True),
        ("telegram", "tg:-4000000456", False),
        ("telegram", "tg:-1001234567890", False),
        # WhatsApp states it in the suffix.
        ("whatsapp", "wa:79161234567@s.whatsapp.net", True),
        ("whatsapp", "wa:120363041234567890@g.us", False),
        # Nothing to read it from. False on purpose: guessing wrong here merges
        # two people into one, which is the failure nobody can see afterwards.
        ("telegram", None, False),
        ("telegram", "", False),
        ("email", "thread-9", False),
        ("ai_conversations", "ai:codex:019c", False),
    ],
)
def test_a_transport_says_whether_a_thread_holds_one_person(source, thread_id, expected):
    assert _is_one_to_one(_Entry(source, thread_id)) is expected


def _speakers(conn, bodies, thread_id: str, source: str = "telegram") -> dict[str, str]:
    """Build a graph from one thread and return each entity's label by id."""
    for index, body in enumerate(bodies):
        when = START + timedelta(hours=index)
        with conn:
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=f"g{index}",
                    body=body,
                    occurred_start=when,
                    captured_at=when,
                    thread_id=thread_id,
                    authored_by_user=False,
                ),
                source=source,
                sensitivity=Sensitivity.PERSONAL,
            )
    GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild()
    run = GraphRepository(conn).active_run()
    return {
        row["id"]: row["canonical_label"]
        for row in conn.execute(
            "SELECT id, canonical_label FROM entities WHERE graph_generation_id = ?",
            (run.id,),
        )
    }


GROUP = (
    "From: Ruslan\nChat: Crew\n\nI decided to book the Tbilisi flights.",
    "From: Marta\nChat: Crew\n\nI promised to bring the projector.",
    "From: Ruslan\nChat: Crew\n\nI worked on the seating chart.",
)


def _aliases(conn) -> list[tuple[str, str]]:
    return [
        (row["namespace"], row["alias_value"])
        for row in conn.execute("SELECT namespace, alias_value FROM entity_aliases")
    ]


def test_nobody_in_a_group_is_filed_under_the_chat(conn):
    """The room's id is nobody's identity, and two speakers are not one person."""
    labels = _speakers(conn, GROUP, "tg:-4000000456")

    assert ("telegram_id", "-4000000456") not in _aliases(conn)
    assert "Ruslan" in labels.values()
    assert "Marta" in labels.values()


def test_a_group_speaker_is_named_but_never_verified_as_an_account(conn):
    """A display name is a guess, and it is stored as one.

    It is never auto-merged either, so the two things Ruslan said arrive as two
    identities with a merge candidate between them. That is the conservative
    direction on purpose: a split is visible in `varyz graph review` and
    mergeable, and the merge this replaced — every speaker in the room becoming
    the room — was neither.
    """
    _speakers(conn, GROUP, "tg:-4000000456")
    rows = conn.execute(
        """SELECT namespace, verification_state FROM entity_aliases
           WHERE alias_value = 'Ruslan'"""
    ).fetchall()

    assert [row["namespace"] for row in rows] == ["display_name", "display_name"]
    assert {row["verification_state"] for row in rows} == {"candidate"}


def _write(conn, bodies, thread_id: str, *, first_index: int = 0) -> None:
    for offset, body in enumerate(bodies):
        index = first_index + offset
        when = START + timedelta(hours=index)
        with conn:
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=f"g{index}",
                    body=body,
                    occurred_start=when,
                    captured_at=when,
                    thread_id=thread_id,
                    authored_by_user=False,
                ),
                source="telegram",
                sensitivity=Sensitivity.PERSONAL,
            )


def _active_aliases(conn) -> list[tuple[str, str]]:
    run = GraphRepository(conn).active_run()
    return [
        (row["namespace"], row["alias_value"])
        for row in conn.execute(
            "SELECT namespace, alias_value FROM entity_aliases WHERE graph_generation_id = ?",
            (run.id,),
        )
    ]


def _speaker_labels(conn) -> set[str]:
    run = GraphRepository(conn).active_run()
    return {
        row["canonical_label"]
        for row in conn.execute(
            """SELECT e.canonical_label FROM claims c
               JOIN entities e ON e.id = c.subject_entity_id
               WHERE c.graph_generation_id = ?""",
            (run.id,),
        )
    }


def test_a_fixed_identity_rule_reaches_answers_already_cached(conn, cfg, monkeypatch):
    """Who spoke is re-answered on reuse, not read back with the rest.

    This is the defect the group fix itself hit: correct code, a rebuild that
    reused 45,528 cached answers, and all 84 misattributed claims returned
    unchanged. Nothing but a full re-extraction through the model would have
    landed it, to recover something a regex already knows.
    """
    cfg.graph.extractor = "deterministic"
    monkeypatch.setattr(extract_module, "_is_one_to_one", lambda entry: True)
    _write(conn, GROUP, "tg:-4000000456")
    rebuild_graph(conn, cfg, version="1")

    assert ("telegram_id", "-4000000456") in _active_aliases(conn)

    # The rule is corrected, and nothing about the entries changes — so every
    # one of them is a cache hit on the next build.
    monkeypatch.undo()
    _write(
        conn,
        ("From: Marta\nChat: Crew\n\nI worked on the playlist.",),
        "tg:-4000000456",
        first_index=9,
    )
    result = rebuild_graph(conn, cfg, version="1")

    assert result.entries_reused == len(GROUP)
    assert ("telegram_id", "-4000000456") not in _active_aliases(conn)
    # And the claims moved with the alias rather than pointing at nobody.
    assert _speaker_labels(conn) == {"Ruslan", "Marta"}


def test_a_two_person_chat_is_still_filed_under_the_thread(conn):
    """The old behaviour, unchanged: with one other person the thread is them."""
    labels = _speakers(conn, GROUP[:1], "tg:7000000123")
    entity_id = next(eid for eid, label in labels.items() if label == "Ruslan")
    alias = conn.execute(
        "SELECT namespace, alias_value FROM entity_aliases WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()

    assert alias["namespace"] == "telegram_id"
    assert alias["alias_value"] == "7000000123"

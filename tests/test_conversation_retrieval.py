"""Conclusions reach an agent; the conversations behind them do not.

The point under test is a boundary, so most of these assert about what is
absent: no quote in the payload, no sensitive conclusion in a default answer,
no message text anywhere in what `understand` returns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.retrieval.understand import understand

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.pipeline.windows import build_windows
from varyz.retrieval import conversations

START = datetime(2026, 4, 2, 18, 0, tzinfo=UTC)

SECRET = "the sofa is coming on the fourteenth"


def _window(conn, thread: str = "move") -> str:
    for index, (authored, body) in enumerate(
        [(False, "when is the sofa arriving"), (True, SECRET)]
    ):
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=f"{thread}-{index}",
                body=body,
                occurred_start=START + timedelta(minutes=index),
                thread_id=thread,
                authored_by_user=authored,
            ),
            source="whatsapp",
            sensitivity=Sensitivity.SENSITIVE,
        )
    build_windows(conn)
    return conn.execute(
        "SELECT id FROM conversation_windows WHERE thread_id = ?", (thread,)
    ).fetchone()["id"]


def _conclusion(conn, window_id: str, text: str, *, sensitivity: str, seq: int = 0) -> None:
    with conn:
        conn.execute(
            "INSERT INTO conversation_window_conclusions"
            " (id, window_id, seq, text, kind, sensitivity, model, prompt_version,"
            "  content_hash, created_at)"
            " VALUES (?, ?, ?, ?, 'plan', ?, 'gemma', 'window-v1', 'h', '2026-04-02')",
            (f"{window_id}:{seq}", window_id, seq, text, sensitivity),
        )


def test_a_conclusion_is_found_by_the_words_it_was_written_in(conn):
    window_id = _window(conn)
    _conclusion(
        conn,
        window_id,
        "The sofa is being delivered on the fourteenth of April.",
        sensitivity="personal",
    )

    found, withheld = conversations.search(conn, "when is the sofa delivered", limit=5)

    assert [item.text for item in found] == [
        "The sofa is being delivered on the fourteenth of April."
    ]
    assert withheld == 0


def test_what_travels_is_the_conclusion_and_an_address_never_the_messages(conn):
    window_id = _window(conn)
    _conclusion(
        conn,
        window_id,
        "The sofa is being delivered on the fourteenth of April.",
        sensitivity="personal",
    )

    found, _ = conversations.search(conn, "sofa delivery", limit=5)
    payload = conversations.payload(found[0])

    assert payload["reference"] == window_id
    assert payload["turns"] == 2
    assert payload["quoted"] is False
    assert SECRET not in repr(payload)


def test_a_conclusion_from_a_sensitive_conversation_is_held_back(conn):
    window_id = _window(conn)
    _conclusion(
        conn,
        window_id,
        "The sofa is being delivered on the fourteenth of April.",
        sensitivity="sensitive",
    )

    found, withheld = conversations.search(conn, "sofa delivery", limit=5)

    assert found == []
    assert withheld == 1


def test_a_caller_allowed_to_read_sensitive_material_sees_it(conn):
    window_id = _window(conn)
    _conclusion(
        conn,
        window_id,
        "The sofa is being delivered on the fourteenth of April.",
        sensitivity="sensitive",
    )

    found, withheld = conversations.search(
        conn,
        "sofa delivery",
        limit=5,
        allowed=(Sensitivity.PUBLIC, Sensitivity.PERSONAL, Sensitivity.SENSITIVE),
    )

    assert len(found) == 1
    assert withheld == 0


def test_a_held_back_conclusion_does_not_take_an_allowed_ones_place(conn):
    window_id = _window(conn)
    _conclusion(conn, window_id, "The sofa arrives on the fourteenth.", sensitivity="sensitive")
    _conclusion(
        conn, window_id, "The sofa was ordered in March.", sensitivity="personal", seq=1
    )

    found, withheld = conversations.search(conn, "sofa", limit=1)

    assert [item.text for item in found] == ["The sofa was ordered in March."]
    assert withheld == 1


def test_an_archive_whose_conversations_were_never_read_answers_nothing(conn):
    _window(conn)

    assert conversations.search(conn, "sofa", limit=5) == ([], 0)


def test_understand_reports_conversation_conclusions_without_a_graph(conn):
    window_id = _window(conn)
    _conclusion(
        conn,
        window_id,
        "The sofa is being delivered on the fourteenth of April.",
        sensitivity="personal",
    )

    result = understand(conn, "when is the sofa delivered", limit=5, max_chars=400)

    assert result["error"] == "no_active_graph"
    assert result["abstained"] is False
    assert [item["conclusion"] for item in result["conversations"]] == [
        "The sofa is being delivered on the fourteenth of April."
    ]
    assert SECRET not in repr(result)


def test_understand_counts_a_withheld_conclusion_as_withheld(conn):
    window_id = _window(conn)
    _conclusion(
        conn,
        window_id,
        "The sofa is being delivered on the fourteenth of April.",
        sensitivity="sensitive",
    )

    result = understand(conn, "when is the sofa delivered", limit=5, max_chars=400)

    assert result["conversations"] == []
    assert result["withheld_sensitive"] == 1
    assert result["abstained"] is True

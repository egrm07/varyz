"""The partition that turns single messages back into conversations.

These tests hold the structural guarantees the later passes rely on: the same
archive always cuts the same way, a window is addressed by what is in it, and
re-running the partition never disturbs anything keyed to a window that has not
changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from varyz.db import dao
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.personal_model.episodes import EpisodePolicy
from varyz.pipeline.windows import (
    build_windows,
    partition,
    policy_version,
    window_text,
)

START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _message(
    conn,
    *,
    external_id: str,
    thread: str,
    minutes: float,
    body: str = "a message body",
    source: str = "ai_conversations",
    authored: bool = False,
) -> str:
    entry_id, _ = dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id=external_id,
            body=body,
            occurred_start=START + timedelta(minutes=minutes),
            thread_id=thread,
            authored_by_user=authored,
        ),
        source=source,
        sensitivity=Sensitivity.SENSITIVE,
    )
    return entry_id


def _entries(conn) -> list:
    return [dao.row_to_entry(row) for row in conn.execute("SELECT * FROM entries")]


def test_messages_close_together_in_one_thread_make_one_window(conn):
    for i in range(5):
        _message(conn, external_id=f"m{i}", thread="t1", minutes=i * 3)

    windows = partition(_entries(conn))

    assert len(windows) == 1
    assert windows[0].turns == 5


def test_chat_rows_an_older_archive_kept_are_not_read_as_conversations(conn):
    _message(conn, external_id="social", thread="social", minutes=0, source="whatsapp")
    _message(
        conn,
        external_id="agent",
        thread="agent",
        minutes=1,
        source="ai_conversations",
    )

    result = build_windows(conn)

    assert result["windows"] == 1
    entry_sources = conn.execute(
        """SELECT DISTINCT e.source FROM conversation_window_entries we
           JOIN entries e ON e.id=we.entry_id"""
    ).fetchall()
    assert [row[0] for row in entry_sources] == ["ai_conversations"]


def test_a_long_silence_ends_the_conversation(conn):
    _message(conn, external_id="before", thread="t1", minutes=0)
    _message(conn, external_id="after", thread="t1", minutes=60 * 7)

    windows = partition(_entries(conn))

    assert len(windows) == 2
    assert [w.turns for w in windows] == [1, 1]


def test_two_threads_never_share_a_window(conn):
    _message(conn, external_id="a", thread="t1", minutes=0)
    _message(conn, external_id="b", thread="t2", minutes=1)

    windows = partition(_entries(conn))

    assert len(windows) == 2
    assert {w.thread_id for w in windows} == {"t1", "t2"}


def test_a_long_conversation_is_capped_rather_than_unbounded(conn):
    policy = EpisodePolicy(max_turns=4)
    for i in range(10):
        _message(conn, external_id=f"m{i}", thread="t1", minutes=i)

    windows = partition(_entries(conn), policy=policy)

    assert [w.turns for w in windows] == [4, 4, 2]


def test_the_character_budget_also_ends_a_window(conn):
    policy = EpisodePolicy(max_characters=50)
    for i in range(4):
        _message(conn, external_id=f"m{i}", thread="t1", minutes=i, body="x" * 30)

    windows = partition(_entries(conn), policy=policy)

    assert all(w.characters <= 50 for w in windows)
    assert len(windows) == 4


def test_the_cut_does_not_depend_on_the_order_rows_arrive_in(conn):
    for i in range(8):
        _message(conn, external_id=f"m{i}", thread=f"t{i % 2}", minutes=i * 2)
    entries = _entries(conn)

    forward = partition(entries)
    backward = partition(list(reversed(entries)))

    assert [w.id for w in forward] == [w.id for w in backward]


def test_a_message_the_archive_cannot_place_in_time_is_left_out(conn):
    _message(conn, external_id="timed", thread="t1", minutes=0)
    dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.MESSAGE,
            external_id="untimed",
            body="no timestamp",
            thread_id="t1",
        ),
        source="ai_conversations",
    )

    windows = partition(_entries(conn))

    assert len(windows) == 1
    assert windows[0].turns == 1


def test_a_window_is_addressed_by_its_membership_and_policy(conn):
    for i in range(3):
        _message(conn, external_id=f"m{i}", thread="t1", minutes=i)
    entries = _entries(conn)

    default = partition(entries)[0]
    narrower = partition(entries, policy=EpisodePolicy(max_turns=2))

    assert default.id not in {w.id for w in narrower}
    assert default.policy_version == policy_version()


def test_building_twice_changes_nothing(conn):
    for i in range(6):
        _message(conn, external_id=f"m{i}", thread="t1", minutes=i)

    first = build_windows(conn)
    second = build_windows(conn)

    assert first["added"] == first["windows"] > 0
    assert second["added"] == 0
    assert second["changed"] == 0
    assert second["removed"] == 0


def test_an_edited_message_invalidates_only_its_own_window(conn):
    _message(conn, external_id="a", thread="t1", minutes=0)
    _message(conn, external_id="far", thread="t1", minutes=60 * 24)
    build_windows(conn)
    before = {
        row["id"]: row["content_hash"]
        for row in conn.execute("SELECT id, content_hash FROM conversation_windows")
    }

    _message(conn, external_id="a", thread="t1", minutes=0, body="edited afterwards")
    result = build_windows(conn)

    after = {
        row["id"]: row["content_hash"]
        for row in conn.execute("SELECT id, content_hash FROM conversation_windows")
    }
    assert result["changed"] == 1
    assert set(before) == set(after)
    assert sum(before[k] != after[k] for k in before) == 1


def test_a_window_that_no_longer_exists_takes_its_conclusions_with_it(conn):
    ids = [_message(conn, external_id=f"m{i}", thread="t1", minutes=i) for i in range(3)]
    build_windows(conn)
    window_id = conn.execute("SELECT id FROM conversation_windows").fetchone()["id"]
    with conn:
        conn.execute(
            "INSERT INTO conversation_window_conclusions"
            " (id, window_id, seq, text, kind, sensitivity, model, prompt_version,"
            "  content_hash, created_at)"
            " VALUES ('c1', ?, 0, 'a conclusion', 'fact', 'personal', 'm', 'v1', 'h', 'now')",
            (window_id,),
        )

    for entry_id in ids:
        dao.delete_entry(conn, entry_id)
    build_windows(conn)

    assert conn.execute("SELECT count(*) FROM conversation_windows").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM conversation_window_conclusions").fetchone()[0] == 0
    )


def test_the_text_a_model_reads_names_sides_and_not_people(conn):
    _message(conn, external_id="a", thread="t1", minutes=0, body="are you coming", authored=False)
    _message(conn, external_id="b", thread="t1", minutes=1, body="yes at six", authored=True)
    build_windows(conn)
    window_id = conn.execute("SELECT id FROM conversation_windows").fetchone()["id"]

    text = window_text(conn, window_id)

    assert text == "them: are you coming\nyou: yes at six"


def test_only_conversation_sources_are_windowed(conn):
    _message(conn, external_id="chat", thread="t1", minutes=0)
    dao.upsert_entry(
        conn,
        NormalizedEntry(
            kind=Kind.DOCUMENT,
            external_id="doc",
            body="a document that is not a conversation",
            occurred_start=START,
        ),
        source="documents",
    )

    build_windows(conn)

    sources = {row["source"] for row in conn.execute("SELECT source FROM conversation_windows")}
    assert sources == {"ai_conversations"}


def test_a_policy_with_no_budget_is_refused():
    with pytest.raises(ValueError):
        EpisodePolicy(max_turns=0)

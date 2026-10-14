from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_personal_model_compiler import add_graph, add_turn, compiler
from varyz.domain import Entry, Kind
from varyz.personal_model.compiler import CompileRequest, incremental_target_entry_ids

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def _entry(entry_id: str, body: str, occurred: datetime) -> Entry:
    return Entry(
        id=entry_id,
        kind=Kind.NOTE,
        source="fixture",
        external_id=entry_id,
        content_hash=f"hash-{entry_id}",
        body=body,
        occurred_start=occurred.isoformat(),
        ingested_at=occurred.isoformat(),
    )


def test_no_incremental_scope_preserves_the_full_compile_sentinel() -> None:
    entries = (_entry("old", "Project Orchid", NOW - timedelta(days=90)),)

    assert incremental_target_entry_ids(entries, CompileRequest(created_at=NOW)) is None


def test_recent_and_hot_topic_scopes_union_with_explicit_targets() -> None:
    entries = (
        _entry(
            "old-hot",
            "The Project Orchid OAuth callback is blocked.",
            NOW - timedelta(days=90),
        ),
        _entry("recent", "A routine recent note.", NOW - timedelta(hours=4)),
        _entry("old-cold", "Old unrelated note.", NOW - timedelta(days=90)),
    )
    request = CompileRequest(
        target_entry_ids=("manual",),
        last_n_days=2,
        hot_topics=("Project Orchid",),
        created_at=NOW,
    )

    assert incremental_target_entry_ids(entries, request) == ("manual", "old-hot", "recent")


def test_hot_topics_are_nonblank_and_unique_after_normalization() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        CompileRequest(hot_topics=("  ",), created_at=NOW)
    with pytest.raises(ValueError, match="unique"):
        CompileRequest(hot_topics=("Project  Orchid", "project orchid"), created_at=NOW)


def test_incremental_compile_works_with_zero_active_generation(conn) -> None:
    snapshot = add_graph(conn)
    old_question = add_turn(
        conn,
        "old-question",
        "What is old?",
        order=1,
        sender="owner",
        thread="old-thread",
    )
    old_answer = add_turn(
        conn,
        "old-answer",
        "Old context",
        order=2,
        sender="anna",
        reply_to="old-question",
        thread="old-thread",
    )
    recent_question = add_turn(
        conn,
        "recent-question",
        "What is recent?",
        order=3,
        sender="owner",
        thread="recent-thread",
    )
    recent_answer = add_turn(
        conn,
        "recent-answer",
        "Recent context",
        order=4,
        sender="anna",
        reply_to="recent-question",
        thread="recent-thread",
    )
    old_stamp = (NOW - timedelta(days=30)).isoformat()
    recent_stamp = (NOW - timedelta(hours=2)).isoformat()
    conn.execute(
        "UPDATE entries SET occurred_start=? WHERE id IN (?,?)",
        (old_stamp, old_question, old_answer),
    )
    conn.execute(
        "UPDATE entries SET occurred_start=? WHERE id IN (?,?)",
        (recent_stamp, recent_question, recent_answer),
    )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM personal_model_active_generation").fetchone()[0] == 0
    result = compiler(conn, snapshot).compile(
        CompileRequest(
            generation_id="incremental-no-active",
            last_n_days=2,
            created_at=NOW,
        )
    )

    assert result.failed is False
    assert result.active_generation_id == "incremental-no-active"
    covered = {
        row[0]
        for row in conn.execute(
            """SELECT target_entry_id FROM personal_model_target_episode_coverage
               WHERE generation_id=?""",
            (result.generation_id,),
        ).fetchall()
    }
    assert covered == {recent_question, recent_answer}

"""The owner's picks in Claude Code questions become that project's decisions.

The owner chose this on 2026-09-25: a pick is recorded, a newer pick to the same
question supersedes the older answer, and a withdrawn one never comes back.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from varyz.db.connection import open_db
from varyz.resolution.decision_capture import capture, picks_in_line
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller

CALLER = Caller(
    agent_id="varyz:autosync",
    display_name="Varyz autosync",
    purpose="capture_decisions",
    device_id="device:test",
    transport="local",
)
MARKET = "Which market should I target?"


def _line(
    answers: dict[str, str],
    *,
    call: str = "toolu_1",
    at: str = "2026-09-20T10:00:00Z",
    cwd: str = "C:\\projects\\shop",
    header: str = "Market",
    sidechain: bool = False,
    annotations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "questions": [
            {"question": question, "header": header, "options": [], "multiSelect": False}
            for question in answers
        ],
        "answers": answers,
    }
    if annotations is not None:
        result["annotations"] = annotations
    return {
        "type": "user",
        "isSidechain": sidechain,
        "sessionId": "session-1",
        "cwd": cwd,
        "timestamp": at,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call, "content": "answered"}],
        },
        "toolUseResult": result,
    }


def _write(path: Path, *records: dict[str, Any], newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(record) for record in records)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text + ("\n" if newline else ""))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


def _run(conn: sqlite3.Connection, tmp_path: Path, *, apply: bool = True):
    return capture(
        conn,
        {"approved": True, "paths": [str(tmp_path / "transcripts")]},
        offsets_path=tmp_path / "data" / "autosync" / "decision_capture.json",
        caller=CALLER,
        device_id="device:test",
        apply=apply,
    )


def _current(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (entry.question, entry.answer, entry.scope)
        for entry in SQLiteKnowledgeStore(conn).current()
    ]


def test_a_pick_is_read_with_its_project_and_time() -> None:
    [pick] = picks_in_line(
        _line(
            {MARKET: "UK + EU (Recommended)"},
            annotations={MARKET: {"notes": "skip Germany"}},
        )
    )
    assert pick.question == MARKET
    assert pick.answer == "UK + EU (note: skip Germany)"
    assert pick.project == "shop"
    assert pick.answered_at == "2026-09-20T10:00:00Z"
    assert pick.source == "claude_code_pick:toolu_1:0"


def test_a_time_without_an_offset_is_utc() -> None:
    from varyz.resolution.decision_capture import _parse_time

    assert _parse_time("2026-09-20T10:00:00") == _parse_time("2026-09-20T10:00:00Z")


def test_a_multi_select_pick_reads_as_a_list_of_options() -> None:
    record = _line({"Which styles?": "placeholder"})
    record["toolUseResult"]["answers"] = {"Which styles?": ["Dark (Recommended)", "Sharp"]}
    [pick] = picks_in_line(record)
    assert pick.answer == "Dark, Sharp"


@pytest.mark.parametrize(
    "record",
    [
        _line({MARKET: "Skip for now"}),
        _line({MARKET: "UK"}, sidechain=True),
        _line({MARKET: "UK"}, header="varyz"),  # relayed: answer_owner_question records it
        _line({"Where does that apply?": "Always, in all my projects"}),
        _line({"Should these apply here?": "Just for varyz"}),
        _line({"Should Varyz remember 'no glows' as a rule?": "All projects"}),
        {**_line({MARKET: "UK"}), "toolUseResult": "Error: the user declined"},
    ],
)
def test_what_is_not_a_decision_is_never_recorded(record: dict[str, Any]) -> None:
    assert picks_in_line(record) == []


def test_dry_run_writes_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _write(tmp_path / "transcripts" / "shop" / "s1.jsonl", _line({MARKET: "UK only"}))
    stats = _run(conn, tmp_path, apply=False)
    assert stats.recorded == 1
    assert _current(conn) == []
    assert not (tmp_path / "data" / "autosync" / "decision_capture.json").exists()


def test_a_pick_is_recorded_once_as_the_projects_decision(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    transcript = tmp_path / "transcripts" / "shop" / "s1.jsonl"
    # A resumed session copies the same call into a second file.
    _write(transcript, {"type": "assistant", "message": {"content": "…"}}, _line({MARKET: "UK"}))
    _write(tmp_path / "transcripts" / "shop" / "s2.jsonl", _line({MARKET: "UK"}))

    stats = _run(conn, tmp_path)
    assert (stats.recorded, stats.already_known) == (1, 1)
    [entry] = SQLiteKnowledgeStore(conn).current()
    assert (entry.question, entry.answer, entry.scope) == (MARKET, "UK", "shop")
    assert entry.basis.value == "user_confirmed"
    assert entry.stated_at.isoformat().startswith("2026-09-20T10:00:00")

    assert _run(conn, tmp_path).files_read == 0  # nothing new since the last offset
    (tmp_path / "data" / "autosync" / "decision_capture.json").unlink()
    again = _run(conn, tmp_path)
    assert (again.recorded, again.already_known) == (0, 2)


def test_a_newer_pick_supersedes_the_older_answer(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    transcript = tmp_path / "transcripts" / "shop" / "s1.jsonl"
    _write(transcript, _line({MARKET: "UK only"}, call="toolu_1", at="2026-09-20T10:00:00Z"))
    _run(conn, tmp_path)
    _write(transcript, _line({MARKET: "UK + EU"}, call="toolu_2", at="2026-09-22T10:00:00Z"))
    stats = _run(conn, tmp_path)
    assert (stats.recorded, stats.superseded) == (1, 1)
    assert _current(conn) == [(MARKET, "UK + EU", "shop")]


def test_an_older_pick_never_undoes_a_later_answer(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    transcripts = tmp_path / "transcripts" / "shop"
    # Files are read in name order, so the newer session is read first.
    newer = _line({MARKET: "UK + EU"}, call="t2", at="2026-09-22T10:00:00Z")
    older = _line({MARKET: "UK only"}, call="t1", at="2026-09-20T10:00:00Z")
    _write(transcripts / "a.jsonl", newer)
    _write(transcripts / "b.jsonl", older)
    stats = _run(conn, tmp_path)
    assert (stats.recorded, stats.older_than_current) == (1, 1)
    assert _current(conn) == [(MARKET, "UK + EU", "shop")]


def test_a_retracted_pick_never_comes_back(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _write(tmp_path / "transcripts" / "shop" / "s1.jsonl", _line({MARKET: "UK"}))
    _run(conn, tmp_path)
    [entry] = SQLiteKnowledgeStore(conn).current()
    SQLiteKnowledgeStore(conn).retract(entry.id, caller=CALLER, device_id="device:test")
    (tmp_path / "data" / "autosync" / "decision_capture.json").unlink()
    assert _run(conn, tmp_path).recorded == 0
    assert _current(conn) == []


def test_a_line_still_being_written_is_read_next_time(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    transcript = tmp_path / "transcripts" / "shop" / "s1.jsonl"
    _write(transcript, _line({MARKET: "UK"}), newline=False)
    assert _run(conn, tmp_path).picks_found == 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert _run(conn, tmp_path).recorded == 1


def test_nothing_is_read_without_the_connectors_approval(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _write(tmp_path / "transcripts" / "shop" / "s1.jsonl", _line({MARKET: "UK"}))
    for options in (
        {"paths": [str(tmp_path / "transcripts")]},
        {"approved": True, "adapters": ["codex"], "paths": [str(tmp_path / "transcripts")]},
    ):
        stats = capture(
            conn, options, offsets_path=tmp_path / "offsets.json", caller=CALLER,
            device_id="device:test", apply=True,
        )
        assert stats.files_read == 0


def test_autosync_records_picks_every_cycle(tmp_path: Path, monkeypatch) -> None:
    from tests.conftest import FakeEmbedder
    from varyz import autosync
    from varyz.config import load_config

    home_projects = tmp_path / "home" / ".claude" / "projects"
    _write(home_projects / "shop" / "s1.jsonl", _line({MARKET: "UK"}))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n\n'
        "[connectors.ai_conversations]\napproved = true\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    open_db(cfg.db_path).close()
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg, **_: FakeEmbedder())
    monkeypatch.setattr(
        autosync,
        "sync_connector",
        lambda *_args: SimpleNamespace(
            new=0, updated=0, unchanged=0, excluded=0, deleted=0, errors=0
        ),
    )

    result = autosync.run_once(config_path, trigger="test")
    assert "Decisions recorded from agents' questions: 1" in result.detail
    conn = open_db(cfg.db_path)
    try:
        assert _current(conn) == [(MARKET, "UK", "shop")]
    finally:
        conn.close()

"""The graph follows its own cadence, and one cycle cannot run forever.

Ingest is incremental and cheap; a rebuild restages the whole archive into a
new generation. These are the tests that the two are no longer forced to share
a schedule, and that a cycle which overruns stops at a checkpoint instead of
holding the write lock until somebody notices.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import FakeEmbedder
from varyz import autosync
from varyz.config import load_config
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry
from varyz.evidence import SQLiteEvidenceReader
from varyz.graph.cadence import rebuild_due
from varyz.graph.extract import DeterministicExtractor, GraphBuildService
from varyz.graph.repository import GraphRepository


def _entry(conn, external_id: str, body: str) -> None:
    with conn:
        dao.upsert_entry(
            conn,
            NormalizedEntry(kind=Kind.NOTE, external_id=external_id, body=body),
            source="manual",
            sensitivity="personal",
        )


def _rebuild(conn):
    return GraphBuildService(
        GraphRepository(conn), SQLiteEvidenceReader(conn), DeterministicExtractor()
    ).rebuild()


def _decide(conn, *, new: int = 50, age: int = 60):
    return rebuild_due(conn, min_new_entries=new, max_age_minutes=age)


def _backdate(conn, minutes: int) -> None:
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "UPDATE graph_runs SET activated_at=? WHERE state='active'", (stamp,)
        )


# --- the gate itself -------------------------------------------------------


def test_an_archive_with_no_graph_yet_is_always_due(conn):
    _entry(conn, "a", "I moved to Berlin.")

    decision = _decide(conn)

    assert decision.due and "no active generation" in decision.reason


def test_a_fresh_graph_with_nothing_new_is_not_worth_rebuilding(conn):
    _entry(conn, "a", "I moved to Berlin.")
    _rebuild(conn)

    decision = _decide(conn)

    assert not decision.due
    assert decision.new_entries == 0


def test_enough_new_entries_bring_the_rebuild_forward(conn):
    _entry(conn, "a", "I moved to Berlin.")
    _rebuild(conn)
    for index in range(3):
        _entry(conn, f"new-{index}", f"I visited Porto in 201{index}.")

    assert not _decide(conn, new=4).due
    decision = _decide(conn, new=3)
    assert decision.due and decision.new_entries == 3


def test_excluded_source_growth_does_not_schedule_a_graph_rebuild(conn):
    _entry(conn, "a", "I moved to Berlin.")
    _rebuild(conn)
    with conn:
        dao.upsert_entry(
            conn,
            NormalizedEntry(kind=Kind.MESSAGE, external_id="social", body="old chat"),
            source="WhatsApp",
            sensitivity="sensitive",
        )

    decision = rebuild_due(
        conn,
        min_new_entries=1,
        max_age_minutes=60,
        excluded_sources=(" WHATSAPP ", "telegram"),
    )

    assert not decision.due
    assert decision.new_entries == 0


def test_an_old_graph_is_rebuilt_even_when_nothing_arrived(conn):
    """The count misses edited entries. The age bound is what catches them."""
    _entry(conn, "a", "I moved to Berlin.")
    _rebuild(conn)
    _backdate(conn, minutes=90)

    decision = _decide(conn, age=60)

    assert decision.due and decision.new_entries == 0
    assert decision.age_minutes is not None and decision.age_minutes >= 90


def test_a_graph_with_no_activation_time_is_rebuilt_rather_than_trusted(conn):
    _entry(conn, "a", "I moved to Berlin.")
    _rebuild(conn)
    with conn:
        conn.execute("UPDATE graph_runs SET activated_at=NULL WHERE state='active'")

    assert _decide(conn).due


# --- the gate inside a cycle ----------------------------------------------


def _project(tmp_path: Path, *, graph_extra: str = "", autosync_extra: str = "") -> Path:
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "plan.md").write_text("# Lisbon\n\nBook the train on Friday.", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[storage]\n"
        'data_dir = "data"\n\n'
        "[graph]\n"
        'extractor = "deterministic"\n'
        + graph_extra
        + "\n[autosync]\n"
        + autosync_extra
        + "\n[connectors.documents]\n"
        "approved = true\n"
        f'paths = ["{docs.as_posix()}"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    open_db(cfg.db_path).close()
    return config_path, docs


def test_a_cycle_that_finds_new_entries_still_rebuilds(tmp_path, monkeypatch):
    config_path, docs = _project(tmp_path, graph_extra="rebuild_min_new_entries = 1\n")
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    assert autosync.run_once(config_path, trigger="test").graph_status == "updated"
    assert autosync.run_once(config_path, trigger="test").graph_status == "throttled"
    (docs / "porto.md").write_text("I moved to Porto in 2019.", encoding="utf-8")

    third = autosync.run_once(config_path, trigger="test")

    assert third.new == 1 and third.graph_status == "updated"


def test_a_throttled_cycle_is_not_a_failed_cycle(tmp_path, monkeypatch):
    config_path, _ = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    autosync.run_once(config_path, trigger="test")
    second = autosync.run_once(config_path, trigger="test")

    assert second.status == "ok" and second.errors == 0
    assert "deferred" in second.detail
    state = json.loads(autosync.state_path(cfg).read_text(encoding="utf-8"))
    assert state["status"] == "ok"


# --- the cycle deadline ----------------------------------------------------


class Clock:
    """A clock the test moves by hand, so no test has to wait for a deadline."""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


def test_a_rebuild_that_overruns_the_cycle_stops_at_a_checkpoint(tmp_path, monkeypatch):
    """The wedged daemon held the write lock for 3 h 38 min. This is the leash."""
    config_path, _ = _project(
        tmp_path,
        graph_extra="rebuild_min_new_entries = 1\n",
        autosync_extra="cycle_deadline_minutes = 30\n",
    )
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    clock = Clock()
    monkeypatch.setattr(autosync, "utc_now", clock)

    from varyz.graph import service as graph_service

    seen: list[bool] = []

    def overrunning_rebuild(conn, config, **kwargs):
        should_stop = kwargs["should_stop"]
        seen.append(should_stop())
        clock.advance(45)
        seen.append(should_stop())
        return SimpleNamespace(
            stopped=True,
            no_op=False,
            run_id="staging-run",
            entries_processed=12,
            entries_reused=0,
            entries_failed=0,
            claims=0,
        )

    monkeypatch.setattr(graph_service, "rebuild_graph", overrunning_rebuild)
    result = autosync.run_once(config_path, trigger="test")

    assert seen == [False, True]
    assert result.status == "partial" and result.graph_status == "deadline"
    assert "30-minute cycle deadline" in result.detail
    assert result.errors == 0


def test_a_zero_deadline_lets_a_long_cycle_finish(tmp_path, monkeypatch):
    config_path, _ = _project(
        tmp_path,
        graph_extra="rebuild_min_new_entries = 1\n",
        autosync_extra="cycle_deadline_minutes = 0\n",
    )
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    clock = Clock()
    monkeypatch.setattr(autosync, "utc_now", clock)

    from varyz.graph import service as graph_service

    seen: list[bool] = []

    def slow_rebuild(conn, config, **kwargs):
        clock.advance(600)
        seen.append(kwargs["should_stop"]())
        return SimpleNamespace(
            stopped=False,
            no_op=False,
            run_id="run",
            entries_processed=1,
            entries_reused=0,
            entries_failed=0,
            claims=0,
        )

    monkeypatch.setattr(graph_service, "rebuild_graph", slow_rebuild)
    result = autosync.run_once(config_path, trigger="test")

    assert seen == [False]
    assert result.status == "ok" and result.graph_status == "updated"

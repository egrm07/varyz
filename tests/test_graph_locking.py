"""Graph rebuild concurrency keeps model waits away from the archive writer lock."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import FakeEmbedder
from varyz import autosync
from varyz.config import Config, load_config
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.graph.repository import GraphRepository
from varyz.graph.service import (
    GraphRebuildAlreadyRunning,
    graph_rebuild_claim_path,
    read_graph_rebuild_claim,
    rebuild_graph,
)


class BlockingProvider:
    model = "blocking-fixture"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, _system: str, _user: str) -> str:
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release blocking provider")
        return '{"facets":[]}'


class RaisingProvider:
    model = "raising-fixture"

    def complete(self, _system: str, _user: str) -> str:
        raise RuntimeError("fixture inference failed")


def _message(conn, external_id: str, *, minute: int = 0) -> None:
    with conn:
        dao.upsert_entry(
            conn,
            NormalizedEntry(
                kind=Kind.MESSAGE,
                external_id=external_id,
                body="From: me\nChat: Test\n\nI think privacy matters.",
                occurred_start=datetime(2026, 8, 13, 10, minute, tzinfo=UTC),
                thread_id="wa:test",
            ),
            source="whatsapp",
            sensitivity=Sensitivity.PERSONAL,
        )


def _semantic(cfg: Config) -> None:
    cfg.graph.extractor = "contextual_facets"
    cfg.graph.semantic_provider = "local"
    cfg.graph.extraction_cache = False


def _start_blocked_rebuild(cfg: Config, provider: BlockingProvider):
    errors: list[BaseException] = []

    def run() -> None:
        conn = open_db(cfg.db_path)
        try:
            rebuild_graph(conn, cfg, provider=provider)
        except BaseException as exc:  # surfaced in the test thread after cleanup
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=run, name="blocked-graph-rebuild")
    thread.start()
    assert provider.started.wait(timeout=10)
    return thread, errors


def _autosync_project(tmp_path: Path) -> tuple[Path, Config]:
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "new.md").write_text("# New\n\nArchive this while graph inference waits.")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[storage]\n"
        'data_dir = "data"\n\n'
        "[graph]\n"
        'extractor = "contextual_facets"\n'
        'semantic_provider = "local"\n'
        "extraction_cache = false\n\n"
        "[autosync]\n"
        "semantic_graph = true\n\n"
        "[connectors.documents]\n"
        "approved = true\n"
        f'paths = ["{docs.as_posix()}"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    conn = open_db(cfg.db_path)
    _message(conn, "before-autosync")
    conn.close()
    return config_path, cfg


def test_ingestion_succeeds_while_graph_rebuild_waits_on_inference(
    tmp_path, monkeypatch
):
    config_path, cfg = _autosync_project(tmp_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg, **_: FakeEmbedder())
    provider = BlockingProvider()
    thread, errors = _start_blocked_rebuild(cfg, provider)

    try:
        result = autosync.run_once(config_path, trigger="test")
        conn = open_db(cfg.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        finally:
            conn.close()
        assert result.new == 1
        assert result.connectors[0].status == "ok"
        assert result.graph_status == "failed"
        assert "already running" in result.detail
    finally:
        provider.release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []


def test_second_rebuild_names_the_rebuild_that_already_owns_the_claim(cfg):
    _semantic(cfg)
    conn = open_db(cfg.db_path)
    _message(conn, "claim-holder")
    conn.close()
    provider = BlockingProvider()
    thread, errors = _start_blocked_rebuild(cfg, provider)

    try:
        holder = read_graph_rebuild_claim(cfg.data_dir)
        second = open_db(cfg.db_path)
        try:
            with pytest.raises(GraphRebuildAlreadyRunning) as caught:
                rebuild_graph(second, cfg, provider=RaisingProvider())
        finally:
            second.close()
        message = str(caught.value)
        assert holder["claim_id"] in message
        assert f"PID {holder['pid']}" in message
        assert holder["started_at"] in message
    finally:
        provider.release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []


def test_kill_switch_still_stops_a_real_rebuild_between_entries(cfg):
    _semantic(cfg)
    conn = open_db(cfg.db_path)
    _message(conn, "pause-one")
    _message(conn, "pause-two", minute=1)

    class PausingProvider:
        model = "pausing-fixture"

        def complete(self, _system: str, _user: str) -> str:
            autosync.pause(cfg)
            return '{"facets":[]}'

    result = rebuild_graph(
        conn,
        cfg,
        provider=PausingProvider(),
        should_stop=lambda: autosync.is_paused(cfg),
    )

    assert result.stopped is True
    assert result.entries_processed == 1
    assert GraphRepository(conn).active_run() is None
    conn.close()


def test_inference_failure_leaves_the_active_graph_untouched(cfg):
    cfg.graph.extractor = "deterministic"
    conn = open_db(cfg.db_path)
    _message(conn, "active-entry")
    active = rebuild_graph(conn, cfg)
    _message(conn, "failing-entry", minute=1)
    _semantic(cfg)
    cfg.graph.max_consecutive_failures = 1

    with pytest.raises(RuntimeError, match="consecutive extraction failures"):
        rebuild_graph(conn, cfg, provider=RaisingProvider())

    still_active = GraphRepository(conn).active_run()
    assert still_active is not None
    assert still_active.id == active.run_id
    failed = conn.execute(
        "SELECT COUNT(*) FROM graph_runs WHERE state='failed'"
    ).fetchone()[0]
    assert failed == 1
    conn.close()


def test_stale_claim_metadata_does_not_block_a_new_rebuild(cfg):
    cfg.graph.extractor = "deterministic"
    conn = open_db(cfg.db_path)
    _message(conn, "stale-claim")
    path = graph_rebuild_claim_path(cfg.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claim_id": "dead-rebuild",
                "pid": 999999999,
                "started_at": "2026-08-12T00:00:00+00:00",
                "version": "old",
            }
        ),
        encoding="utf-8",
    )

    result = rebuild_graph(conn, cfg)

    assert result.run_id is not None
    current = read_graph_rebuild_claim(cfg.data_dir)
    assert current["claim_id"] != "dead-rebuild"
    conn.close()

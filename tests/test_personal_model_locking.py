"""Personal-model inference never monopolizes the archive writer lock."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests.conftest import FakeEmbedder
from tests.test_personal_model_compiler import (
    NOW,
    EmptyAnnotator,
    add_graph,
    add_isolated_turns,
    add_turn,
    pin_graph_to_current_entries,
)
from varyz import autosync
from varyz.config import Config, load_config
from varyz.db.connection import open_db
from varyz.personal_model.compiler import (
    CompileRequest,
    CompilerPolicy,
    CompileStatus,
    PersonalModelCompileAlreadyRunning,
    build_personal_model_compiler,
    personal_model_compile_claim_path,
    read_personal_model_compile_claim,
)
from varyz.personal_model.observations import AnnotationBatchRequest
from varyz.personal_model.repository import PersonalModelRepository


class BlockingAnnotator(EmptyAnnotator):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def annotate(self, request: AnnotationBatchRequest) -> str:
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release blocking annotator")
        return super().annotate(request)


def _project(tmp_path: Path, *, documents: bool = False) -> tuple[Path, Config]:
    connector = ""
    if documents:
        docs = tmp_path / "notes"
        docs.mkdir()
        (docs / "new.md").write_text(
            "# New\n\nIngested while personal-model observation waits.",
            encoding="utf-8",
        )
        connector = (
            "\n[connectors.documents]\n"
            "approved = true\n"
            f'paths = ["{docs.as_posix()}"]\n'
        )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[storage]\n"
        'data_dir = "data"\n\n'
        "[graph]\n"
        'extractor = "deterministic"\n'
        + connector,
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    conn = open_db(cfg.db_path)
    add_graph(conn)
    add_turn(conn, "compile-source", "I choose privacy", order=1, sender="owner")
    pin_graph_to_current_entries(conn)
    conn.close()
    return config_path, cfg


def _start_blocked_compile(cfg: Config, annotator: BlockingAnnotator):
    results = []
    errors: list[BaseException] = []

    def run() -> None:
        conn = open_db(cfg.db_path)
        try:
            results.append(
                build_personal_model_compiler(conn, cfg, annotator=annotator).compile(
                    CompileRequest(generation_id="blocked-compile", created_at=NOW)
                )
            )
        except BaseException as exc:  # surfaced after the worker is joined
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=run, name="blocked-personal-model-compile")
    thread.start()
    assert annotator.started.wait(timeout=10)
    return thread, results, errors


def test_ingestion_succeeds_while_compile_waits_in_observation(
    tmp_path, monkeypatch
):
    config_path, cfg = _project(tmp_path, documents=True)
    conn = open_db(cfg.db_path)
    stable = build_personal_model_compiler(conn, cfg).compile(
        CompileRequest(generation_id="stable-before-block", created_at=NOW)
    )
    conn.close()
    assert stable.activated
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    annotator = BlockingAnnotator()
    thread, results, errors = _start_blocked_compile(cfg, annotator)

    try:
        result = autosync.run_once(config_path, trigger="test")
        conn = open_db(cfg.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        finally:
            conn.close()
        assert thread.is_alive()
        assert result.new == 1
        assert result.connectors[0].status == "ok"
    finally:
        annotator.release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0].failed
    assert results[0].active_generation_id == "stable-before-block"
    check = open_db(cfg.db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM personal_model_records WHERE generation_id=?",
            (results[0].generation_id,),
        ).fetchone()[0] == 0
    finally:
        check.close()


def test_second_compile_names_the_compile_that_owns_the_claim(tmp_path):
    _, cfg = _project(tmp_path)
    annotator = BlockingAnnotator()
    thread, results, errors = _start_blocked_compile(cfg, annotator)

    try:
        holder = read_personal_model_compile_claim(cfg.data_dir)
        second = open_db(cfg.db_path)
        try:
            with pytest.raises(PersonalModelCompileAlreadyRunning) as caught:
                build_personal_model_compiler(
                    second, cfg, annotator=EmptyAnnotator()
                ).compile(
                    CompileRequest(generation_id="refused-compile", created_at=NOW)
                )
        finally:
            second.close()
        message = str(caught.value)
        assert holder["claim_id"] in message
        assert f"PID {holder['pid']}" in message
        assert holder["started_at"] in message
        assert "blocked-compile" in message
    finally:
        annotator.release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0].activated


def test_partial_compile_releases_claim_for_the_next_bounded_run(tmp_path):
    _, cfg = _project(tmp_path)
    conn = open_db(cfg.db_path)
    add_isolated_turns(conn, 2)
    pin_graph_to_current_entries(conn)
    annotator = EmptyAnnotator()
    compiler = build_personal_model_compiler(conn, cfg, annotator=annotator)

    partial = compiler.compile(
        CompileRequest(
            generation_id="partial-compile",
            max_annotation_calls=1,
            created_at=NOW,
        )
    )
    resumed = compiler.compile(
        CompileRequest(
            generation_id="resumed-compile",
            max_annotation_calls=3,
            created_at=NOW,
        )
    )

    assert partial.status == CompileStatus.PARTIAL
    assert partial.observation_progress is not None
    assert partial.observation_progress.annotation_calls == 1
    assert resumed.status == CompileStatus.COMPLETE and resumed.activated
    assert resumed.observation_progress is not None
    assert resumed.observation_progress.cache_hits >= 1
    assert personal_model_compile_claim_path(cfg.data_dir).is_file()
    conn.close()


def test_required_annotator_failure_stages_nothing_and_preserves_active(tmp_path):
    _, cfg = _project(tmp_path)
    conn = open_db(cfg.db_path)
    stable = build_personal_model_compiler(conn, cfg).compile(
        CompileRequest(generation_id="stable-before-failure", created_at=NOW)
    )
    failed = build_personal_model_compiler(
        conn,
        cfg,
        policy=CompilerPolicy(
            require_observation_annotator=True,
            allow_literal_only_activation=True,
        ),
    ).compile(CompileRequest(generation_id="failed-annotation", created_at=NOW))

    assert stable.activated
    assert failed.failed and not failed.activated
    assert failed.active_generation_id == stable.generation_id
    assert PersonalModelRepository(conn).active_generation_id() == stable.generation_id
    assert conn.execute(
        "SELECT COUNT(*) FROM personal_model_records WHERE generation_id=?",
        (failed.generation_id,),
    ).fetchone()[0] == 0
    conn.close()


def test_stale_compile_claim_metadata_does_not_block_a_new_run(tmp_path):
    _, cfg = _project(tmp_path)
    path = personal_model_compile_claim_path(cfg.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\0"
        + json.dumps(
            {
                "claim_id": "dead-compile",
                "generation_id": "abandoned",
                "pid": 999999999,
                "started_at": "2026-08-12T00:00:00+00:00",
            }
        ).encode()
    )
    conn = open_db(cfg.db_path)

    result = build_personal_model_compiler(conn, cfg).compile(
        CompileRequest(generation_id="after-stale-claim", created_at=NOW)
    )

    assert result.activated
    assert read_personal_model_compile_claim(cfg.data_dir)["claim_id"] != "dead-compile"
    conn.close()

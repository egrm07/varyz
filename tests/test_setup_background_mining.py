# ruff: noqa: F811
"""Approval and overlapping setup work, using synthetic chats and offline workers."""

from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_setup_web import machine  # noqa: F401
from varyz import cli, setup_extract
from varyz.config import load_config
from varyz.resolution import headless_reader as reader
from varyz.resolution import local_reader, usage
from varyz.setup_web import SetupSession

OFFER = {
    "state": "offered",
    "turns": 1000,
    "runner": "codex",
    "plan": "ChatGPT",
    "known": True,
    "five_hour": 4.3,
    "weekly": 0.7,
    "seconds": 221,
}


def until(get, predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = get()
        if predicate(value):
            return value
        time.sleep(0.01)
    raise AssertionError("expected background transition did not happen")


@pytest.mark.parametrize("stop", [False, True])
def test_setup_finishes_while_mining_starts_by_itself_and_stops_on_request(
    machine, monkeypatch, stop
):
    download = threading.Event()
    mining = threading.Event()
    started = threading.Event()
    calls = []

    def sync(cfg, ids, on_progress=None, *, step=None, on_read=None):
        on_read()
        on_progress("Downloading search models: 1 MB of 100 MB", 0.01)
        assert download.wait(5)
        return []

    def start(cfg, path, offered):
        calls.append(offered)
        started.set()
        assert mining.wait(5)
        return {"at": time.time(), "turns": 1000, "runner": "codex", "seconds": 221}

    stopped = []
    monkeypatch.setattr(cli, "_sync_sources", sync)
    monkeypatch.setattr(setup_extract, "offer", lambda *_a, **_k: dict(OFFER))
    monkeypatch.setattr(setup_extract, "start", start)
    monkeypatch.setattr(setup_extract, "stop",
                        lambda cfg, *, runner=None: stopped.append(runner))
    monkeypatch.setattr(
        setup_extract,
        "progress",
        lambda *_a: {"state": "accepted", "phase": "reading", "fraction": 0.25},
    )
    session = SetupSession(machine.config)
    try:
        session.start(
            ["claude-code"],
            ["ai_conversations"],
            inference_mode="conversations",
            cloud_conversations=True,
        )
        # Once estimated it starts by itself, while the download still runs.
        assert started.wait(2)
        assert session.snapshot()["extract"]["state"] == "starting"
        if stop:
            before = time.monotonic()
            assert session.control_extract("stop") is None
            assert time.monotonic() - before < 0.5
            assert session.control_extract("stop") == "learning is not running"
        download.set()
        state = until(session.snapshot, lambda s: s["finished"])
        assert state["result"]["verdict"] == "GO"
        assert state["extract"]["state"] == ("declined" if stop else "starting")
        assert len(calls) == 1
        mining.set()
        if stop:
            # What began while it was stopped is cancelled, and stays stopped.
            until(lambda: stopped, bool)
            assert stopped == ["codex"]
            assert session.snapshot()["extract"]["state"] == "declined"
        else:
            until(session.snapshot, lambda s: s["extract"]["state"] == "accepted")
            assert not stopped
    finally:
        download.set()
        mining.set()


def test_usage_lookup_and_progress_never_block_setup_polling(machine, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def progress(*args):
        entered.set()
        assert release.wait(5)
        return {"state": "accepted", "phase": "reading", "fraction": 0.5}

    monkeypatch.setattr(setup_extract, "progress", progress)
    session = SetupSession(machine.config)
    session._extract = dict(OFFER, state="accepted", phase="reading")
    session._extract_started = {"at": time.time()}
    try:
        session.snapshot()
        assert entered.wait(1)
        before = time.monotonic()
        for _ in range(5):
            assert session.snapshot()["extract"]["state"] == "accepted"
        assert time.monotonic() - before < 0.5
    finally:
        release.set()


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[workflows]\nreader_codex_model = "gpt-6-luna"\n', encoding="utf-8")
    return path


def test_local_offer_uses_zero_subscription_allowance_and_starts_nothing_itself(
    config_file, monkeypatch
):
    cfg = load_config(config_file)
    cfg.workflows.inference_mode = "local"
    monkeypatch.setattr(local_reader, "_allowed", lambda cfg: True)
    monkeypatch.setattr(local_reader, "work_left", lambda cfg: (1000, 2))
    calls = []
    monkeypatch.setattr(local_reader, "request", lambda *a, **kw: calls.append(kw) or True)
    offered = setup_extract.offer(cfg, config_file)
    assert not calls
    assert offered["five_hour"] == offered["weekly"] == 0
    assert offered["seconds"] > 0
    started = setup_extract.start(cfg, config_file, offered)
    assert calls == [{"wait_model": True}]
    assert started["runner"] == "local"


def test_local_progress_waits_for_download_and_does_not_claim_partial_work_is_done(
    config_file,
    monkeypatch,
):
    cfg = load_config(config_file)
    status = {"state": "waiting_model"}
    monkeypatch.setattr(local_reader, "load_status", lambda cfg: status)
    monkeypatch.setattr(local_reader, "work_left", lambda cfg: (500, 2))
    started = {"at": 100, "turns": 1000, "runner": "local", "seconds": 2000}
    view = setup_extract.progress(cfg, started, 200)
    assert view["phase"] == "waiting_model"
    assert "account_weekly" not in view  # Local learning has no subscription usage.
    status.update(state="running", reading_at=100)
    view = setup_extract.progress(cfg, started, 200)
    assert view["phase"] == "reading" and view["fraction"] == 0.5
    status["state"] = "stopped"
    assert setup_extract.progress(cfg, started, 201)["phase"] == "stopped"


def test_unknown_usage_starts_the_approved_cloud_job(config_file, monkeypatch):
    cfg = load_config(config_file)
    reader.update_status(cfg, lambda s: s.update(pending=["full"], setup_runner="codex"))
    calls = []
    remaining = [100]
    monkeypatch.setattr(reader, "_work_left", lambda *a: (remaining[0], 0, 0, 0))

    def launch(*args):
        calls.append(args)
        remaining[0] = 0
        return SimpleNamespace(pid=999, stdout=io.StringIO(""), poll=lambda: 0)

    result = reader.supervise(cfg, config_file,
        runners={"codex": reader.Runner("codex", "fake", "fake-test-token")},
        read_usage=lambda name: None, launch=launch, clock=lambda: 100)
    assert len(calls) == 1
    assert result["state"] == "idle" and result["pending"] == []


def test_supervisor_stays_on_the_plan_the_owner_approved(config_file, monkeypatch):
    cfg = load_config(config_file)
    reader.update_status(cfg, lambda s: s.update(pending=["full"], setup_runner="codex"))
    checked = []

    def read_usage(name):
        checked.append(name)
        return None

    monkeypatch.setattr(reader, "_work_left", lambda *a: (0, 0, 0, 0))
    reader.supervise(
        cfg,
        config_file,
        runners={
            name: reader.Runner(name, "fake", "fake-test-token") for name in ["codex", "claude"]
        },
        read_usage=read_usage,
        launch=lambda *a: SimpleNamespace(pid=999, stdout=io.StringIO(""), poll=lambda: 0),
        clock=lambda: 100,
    )
    assert checked == ["codex"]


def test_mac_usage_reads_only_the_selected_keychain_account(tmp_path, monkeypatch):
    monkeypatch.setattr(usage.sys, "platform", "darwin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"claudeAiOauth": {"accessToken": "fake-test-token", "expiresAt": 2000}}
            ),
        )

    monkeypatch.setattr(usage.subprocess, "run", run)
    assert usage.claude_token(now=100) == "fake-test-token"
    assert len(seen) == 1 and seen[0][-2].startswith("Claude Code-credentials-")
    assert "fake-test-token" not in str(seen)


def test_background_launch_cannot_inherit_paid_api_credentials(config_file, monkeypatch):
    cfg = load_config(config_file)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("CODEX_API_KEY", "fake-test-key")
    envs = []

    def launch(cmd, env, cwd):
        envs.append(env)
        return SimpleNamespace(pid=999, stdout=io.StringIO(""), poll=lambda: 0, wait=lambda: 0)

    reader.run_one(
        cfg,
        reader.Runner("codex", "fake", "fake-test-token"),
        "full",
        ["fake"],
        gate=usage.Gate(30, 25),
        launch=launch,
        clock=lambda: 100,
        read_usage=lambda name: None,
    )
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"}.intersection(envs[0])


def test_source_import_callback_precedes_model_download(config_file, monkeypatch):
    cfg = load_config(config_file)
    from varyz.db.connection import open_db

    open_db(cfg.db_path).close()
    events = []
    monkeypatch.setattr(cli, "get_connector", lambda cid: cid)
    monkeypatch.setattr(cli, "sync_connector", lambda *a, **kw: events.append("import"))
    monkeypatch.setattr(cli, "_print_stats", lambda stats: None)
    monkeypatch.setattr(
        cli, "_fetch_search_models", lambda *a: events.append("download") or "ready"
    )
    monkeypatch.setattr(cli, "Embedder", lambda cfg, **_: None)
    monkeypatch.setattr(cli, "embed_pending", lambda *a, **kw: events.append("embed") or 0)
    assert (
        cli._sync_sources(cfg, ("ai_conversations",), on_read=lambda: events.append("offer")) == []
    )
    assert events == ["import", "offer", "download", "embed"]

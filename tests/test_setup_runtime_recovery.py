"""Discover existing runtimes and recover from setup failures without live AI or accounts."""

from __future__ import annotations

import threading
import time
import tomllib
from types import SimpleNamespace

import pytest

from tests.conftest import synthetic_executable as executable
from tests.test_setup_web import _request, machine, server  # noqa: F401
from varyz import codex_runtime, install, setup_extract
from varyz.config import load_config
from varyz.resolution import headless_reader
from varyz.setup_recovery import repair_prompt


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runtime.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    monkeypatch.setattr(codex_runtime.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(codex_runtime.shutil, "which", lambda name: None)
    monkeypatch.delenv("VARYZ_CODEX_PATH", raising=False)
    monkeypatch.setattr(codex_runtime.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=""))
    return tmp_path


def test_mac_app_runtime_needs_no_path_change(mac):
    path = executable(mac / "Applications/Codex.app/Contents/Resources/codex")
    assert codex_runtime.find_codex() == str(path)
    assert headless_reader._codex_executable() == str(path)


def test_editor_runtime_matches_platform_and_architecture(mac):
    executable(mac / ".vscode/extensions/openai.chatgpt-99/bin/windows-x86_64/codex.exe")
    executable(mac / ".vscode/extensions/openai.chatgpt-99/bin/macos-x86_64/codex")
    path = executable(mac / ".vscode/extensions/openai.chatgpt-2/bin/macos-aarch64/codex")
    assert codex_runtime.find_codex() == str(path)


def test_mac_app_in_a_custom_folder_is_found_via_bounded_metadata_lookup(mac, monkeypatch):
    path = executable(mac / "My apps/Codex.app/Contents/Resources/codex")
    seen = []

    def lookup(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=str(mac / "My apps/Codex.app") + "\n")

    monkeypatch.setattr(codex_runtime.subprocess, "run", lookup)
    assert codex_runtime.find_codex() == str(path)
    assert seen[0][0][0] == "/usr/bin/mdfind" and seen[0][1]["timeout"] == 3


def test_windows_npm_uses_native_binary_outside_path(mac, monkeypatch):
    monkeypatch.setattr(codex_runtime.sys, "platform", "win32")
    monkeypatch.setattr(codex_runtime.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("APPDATA", str(mac / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(mac / "Local"))
    path = executable(mac / "AppData/npm/node_modules/@openai/codex/node_modules/@openai/"
                      "codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
    assert codex_runtime.find_codex() == str(path)


def test_codex_settings_fallback_keeps_unrelated_settings_and_backup(tmp_path, monkeypatch):
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    original = ('# Keep this comment\nmodel = "existing"\n'
                '[mcp_servers.other]\ncommand = "other" # Keep this too\n')
    path.write_text(original, encoding="utf-8")
    cfg_path = tmp_path / "varyz.toml"
    cfg_path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    monkeypatch.setattr(codex_runtime, "find_codex", lambda: None)
    result = install.install_mcp(config_path=cfg_path, paths={"codex": path},
                                 agent_token="fake-test-token")
    assert result[0].changed and "skipped" not in result[0].detail
    parsed = tomllib.loads(path.read_text("utf-8"))
    assert parsed["model"] == "existing" and parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["varyz"]["env"]["VARYZ_AGENT_TOKEN"] == "fake-test-token"
    assert "# Keep this comment" in path.read_text("utf-8")
    assert "# Keep this too" in path.read_text("utf-8")
    assert path.with_suffix(".toml.varyz-backup").read_text("utf-8") == original
    assert not install.install_mcp(config_path=cfg_path, paths={"codex": path},
                                   agent_token="fake-test-token")[0].changed


def test_repair_prompt_contains_diagnostics_but_never_secret_values(tmp_path):
    secret = "AKIA" + "C" * 16
    prompt = repair_prompt(tmp_path / "my archive/config.toml",
                           [("Codex", "Failure: " + secret + " VARYZ_AGENT_TOKEN=opaque-value")])
    assert secret not in prompt and "opaque-value" not in prompt
    assert "Codex" in prompt and "my archive" in prompt
    assert "even if it is not on PATH" in prompt and "Back up settings" in prompt
    assert repair_prompt(tmp_path / "config.toml", []) is None


@pytest.mark.parametrize("action", ["retry", "skip"])
@pytest.mark.parametrize("phase", ["waiting", "agent", "failed", "stopped"])
def test_usage_wait_controls_are_async_and_preserve_setup_completion(
    server, monkeypatch, action, phase  # noqa: F811
):
    release = threading.Event()
    entered = threading.Event()
    session = server.session
    session.started = session.finished = True
    session._extract = {"state": "accepted", "plan": "Claude", "phase": phase}
    session._extract_started = {"runner": "claude", "at": time.time()}
    session._extract_seen = (time.time(), dict(session._extract))

    def worker(*args):
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(setup_extract, action if action == "retry" else "stop", worker)
    try:
        before = time.monotonic()
        status, _, _ = _request(server, "POST", "/api/extract/control", token=session.token,
                                body={"action": action})
        assert status == 200 and time.monotonic() - before < 0.5
        assert entered.wait(1)
        state = session.snapshot()
        assert state["finished"] and state["extract"]["phase"] == "checking"
        assert _request(server, "POST", "/api/extract/control", token=session.token,
                        body={"action": action})[0] == 409
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while session._extract_retrying and time.monotonic() < deadline:
            time.sleep(0.01)
    if action == "skip":
        assert session.snapshot()["extract"]["state"] == "declined"


def test_skip_drops_the_setup_job_and_retry_cannot_switch_approved_provider(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(path)
    headless_reader.update_status(cfg, lambda s: s.update(
        state="waiting_for_usage", pending=["full"], setup_runner="claude", next_check=9999999999))
    runners = {name: headless_reader.Runner(name, "fake", "fake-token")
               for name in ["claude", "codex"]}
    monkeypatch.setattr(headless_reader, "find_runners", lambda *a: runners)
    requested = []
    monkeypatch.setattr(headless_reader, "request",
                        lambda *a, **k: requested.append(k["runners"]) or True)
    assert setup_extract.retry(cfg, path)
    assert list(requested[0]) == ["claude"]
    assert headless_reader.load_status(cfg)["setup_runner"] == "claude"
    setup_extract.stop(cfg)
    status = headless_reader.load_status(cfg)
    assert status["state"] == "stopped" and status["pending"] == []


def test_invalid_learning_action_returns_a_client_error(server):  # noqa: F811
    for action in [None, {}, [], "other"]:
        status, _, _ = _request(server, "POST", "/api/extract/control",
                                token=server.session.token, body={"action": action})
        assert status == 400


def test_missing_cli_hands_approved_learning_to_the_first_agent_task(tmp_path):
    from tests.test_profile_reader import _shop
    from varyz.db.connection import open_db
    from varyz.resolution import profile_reader

    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(path)
    conn = open_db(cfg.db_path)
    try:
        _shop(conn)
        now = time.time()
        profile_reader.reading_taken(cfg, now)
        headless_reader.update_status(cfg, lambda s: s.update(
            pending=["full"], setup_runner="claude", setup_allow_agent_fallback=True))
        result = headless_reader.supervise(cfg, path,
            runners={}, read_usage=lambda _: pytest.fail("no runtime to check"),
            launch=lambda *a: pytest.fail("no runtime to launch"))
        assert result["state"] == "agent" and result["pending"] == []
        assert profile_reader.agent_reading_offer(cfg, conn, now) is not None
        assert not headless_reader.request(cfg, "full", path)
        view = setup_extract.progress(cfg, {"at": now, "turns": 3, "runner": "claude"}, now)
        assert view["phase"] == "agent" and view["reason"] == "cli_unavailable"
    finally:
        conn.close()


def test_no_cli_offers_estimates_and_only_hands_off_after_approval(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(path)
    monkeypatch.setattr(headless_reader, "_work_left", lambda *a: (1000, 0, 0, 0))
    offered = setup_extract.offer(cfg, path, runners={})
    assert offered["runner"] == "agent" and offered["five_hour"] > 0
    assert not headless_reader.load_status(cfg)
    started = setup_extract.start(cfg, path, offered,
        request=lambda *a, **k: pytest.fail("agent handoff must not launch a CLI"))
    assert started["runner"] == "agent"
    assert setup_extract.progress(cfg, started, time.time())["phase"] == "agent"


def test_explicit_retry_recovers_from_agent_fallback_without_losing_progress(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(path)
    headless_reader.hand_back(cfg, "cli_unavailable")
    runners = {"codex": headless_reader.Runner("codex", "fake", "fake-token")}
    monkeypatch.setattr(headless_reader, "find_runners", lambda *a: runners)
    launched = []
    monkeypatch.setattr(headless_reader, "spawn", lambda *a: launched.append(a))
    monkeypatch.setattr(headless_reader, "_running", lambda *a: False)
    assert setup_extract.retry(cfg, path)
    assert len(launched) == 1
    state = headless_reader.load_status(cfg)
    assert not state["setup_agent_fallback"] and state["pending"] == ["full"]
    started = {"runner": "agent", "at": time.time(), "turns": 3, "seconds": 30}
    view = setup_extract.progress(cfg, started, time.time(), work=(2, 0, 1, 0))
    assert view["phase"] == "reading" and view["turns_left"] == 2


def test_retry_with_no_runner_keeps_the_agent_fallback(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(path)
    headless_reader.hand_back(cfg, "cli_unavailable")
    monkeypatch.setattr(headless_reader, "find_runners", lambda *a: {})
    assert not setup_extract.retry(cfg, path)
    assert headless_reader.load_status(cfg)["setup_agent_fallback"]


def test_setup_notices_when_the_copied_prompt_finishes_learning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(path)
    headless_reader.hand_back(cfg, "cli_unavailable")
    started = {"runner": "agent", "at": time.time(), "turns": 3, "seconds": 30}
    assert setup_extract.progress(cfg, started, time.time(), work=(0, 1, 3, 1))["phase"] == "agent"
    assert setup_extract.progress(cfg, started, time.time(), work=(0, 0, 3, 1))["phase"] == "done"

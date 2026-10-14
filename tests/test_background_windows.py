"""Background work never opens a terminal window on Windows.

CREATE_NO_WINDOW launches a console program without a console. Children must
request the same behavior themselves. A GUI hook entry point also prevents a
console appearing before Python starts. Mocked flags below cover child launches;
the Windows integration test checks the entry point and its actual JSON pipes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from varyz import autosync, hooks, install, update
from varyz.config import Config
from varyz.connectors import git_repos
from varyz.resolution.local_classifier import LocalModel
from varyz.resolution.ollama_setup import binary_version

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


@pytest.fixture
def windows_flags(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", DETACHED_PROCESS, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    launches: list[dict[str, Any]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> SimpleNamespace:
        launches.append(kwargs)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return launches


def _hidden_console(kwargs: dict[str, Any]) -> bool:
    flags = int(kwargs.get("creationflags", 0))
    return bool(flags & CREATE_NO_WINDOW) and not flags & DETACHED_PROCESS


def _config_file(cfg: Config) -> Path:
    path = cfg.base_dir / "config.toml"
    path.write_text("", encoding="utf-8")
    return path


def test_on_use_refresh_runs_on_a_hidden_console(cfg, windows_flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    config_path = _config_file(cfg)
    monkeypatch.setattr(autosync, "is_due", lambda _cfg: True)
    monkeypatch.setattr(autosync, "_claim", lambda _cfg: True)
    assert autosync.maybe_trigger_on_use(cfg, config_path) == "started"
    assert _hidden_console(windows_flags[0])


def test_update_check_runs_on_a_hidden_console(cfg, windows_flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg.update.mode = "auto"
    assert update.maybe_check_on_use(cfg, _config_file(cfg)) == "started"
    assert _hidden_console(windows_flags[0])


def test_hook_started_daemon_runs_on_a_hidden_console(windows_flags):
    # Port 9 on loopback is the discard port: nothing listens there.
    hooks._start_daemon_if_down("127.0.0.1", 9, None)
    assert _hidden_console(windows_flags[0])


def test_classifier_warm_up_runs_on_a_hidden_console(windows_flags):
    LocalModel("tiny").warm_in_background()
    assert _hidden_console(windows_flags[0])


def test_git_connector_asks_for_no_window(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, raising=False)
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    git_repos._run_git(tmp_path, ["status"])
    assert calls[0]["creationflags"] == CREATE_NO_WINDOW


def test_connection_refresh_codex_commands_ask_for_no_window(monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda _name: "codex.exe")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    install._run_codex(["mcp", "remove", "varyz"])
    assert calls[0][0] == ["codex.exe", "mcp", "remove", "varyz"]
    assert _hidden_console(calls[0][1])


def test_ollama_version_probe_asks_for_no_window(monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="ollama version is 0.34.4", stderr="")

    assert binary_version(Path("ollama.exe"), run=fake_run) == (0, 34, 4)
    assert calls[0][0] == ["ollama.exe", "--version"]
    assert _hidden_console(calls[0][1])

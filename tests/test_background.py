"""`varyz uninstall` stops what the installation still runs, and only that."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeEmbedder
from varyz import background, cli
from varyz.background import Found
from varyz.resolution import headless_reader

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Embedder", lambda cfg, **_: FakeEmbedder())
    result = runner.invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.output
    return tmp_path
ENV = Path(os.sep, "home", "o", "tools", "varyzai")
PY = f"{ENV}{os.sep}python"
LAUNCHER = Path(os.sep, "home", "o", "bin", "varyz")


def _listing() -> list[Found]:
    return [
        Found(1, 0, "/sbin/init"),
        Found(10, 1, "terminal"),
        Found(11, 10, f"{LAUNCHER} uninstall"),                      # the caller's launcher
        Found(12, 11, f"{PY} -m varyz uninstall"),                   # the caller
        Found(20, 1, f"{PY} -m varyz rules read --kind full"),       # learning supervisor
        Found(21, 20, "claude -p read"),                             # its reader
        Found(22, 21, f"{LAUNCHER} mcp --config c.toml"),            # the reader's server
        Found(30, 1, f"{PY} -m varyz.resolution.local_reader c.toml --wait-model"),
        Found(40, 1, f"{LAUNCHER} setup --config c.toml"),           # a setup left behind
        Found(41, 40, f"{PY} -m varyz setup --config c.toml"),
        Found(50, 1, f"{PY} -m pytest tests"),                       # same folder, not Varyz
        Found(51, 1, "/usr/bin/python3 -m varyz mcp"),              # another installation
    ]


def test_finds_this_installations_processes_and_spares_the_caller():
    found = background.find(ENV, [LAUNCHER], listing=_listing(), me=12)

    # The outermost of each tree only: a tree stop takes its children.
    assert [process.pid for process in found] == [20, 30, 40]


def test_other_programs_in_the_same_environment_are_not_varyz():
    assert not background.matches(f"{PY} -m pytest tests", ENV, [LAUNCHER])
    assert not background.matches(f"{PY} -m varyzish", ENV, [LAUNCHER])
    assert background.matches(f'{PY} -m "varyz" daemon run', ENV, [LAUNCHER])
    assert background.matches(f"{PY} -m varyz.news --config c", ENV, [LAUNCHER])
    assert not background.matches("/usr/bin/python3 -m varyz mcp", ENV, [LAUNCHER])


def test_lines_name_what_runs_not_its_command():
    names = [background.describe(process) for process in
             background.find(ENV, [LAUNCHER], listing=_listing(), me=12)]

    assert names == ["preference learning (pid 20)", "local preference learning (pid 30)",
                     "setup (pid 40)"]
    watchdog = Found(60, 1, f"{PY} -m varyz.resolution.availability --config c --watch")
    assert background.describe(watchdog) == "Varyz service watchdog (pid 60)"


def _patch(monkeypatch, stopped: list[list[int]]) -> None:
    monkeypatch.setattr(background, "install_paths", lambda: (ENV, [LAUNCHER]))
    monkeypatch.setattr(background, "_listing", _listing)
    monkeypatch.setattr(background.os, "getpid", lambda: 12)
    monkeypatch.setattr(background, "stop", lambda found: stopped.append(
        [process.pid for process in found]) or [process.pid for process in found])
    monkeypatch.setattr("varyz.setup_flow.unregister_mcp", lambda **_kwargs: [])


def test_uninstall_stops_background_work_and_clears_queued_learning(project, monkeypatch):
    stopped: list[list[int]] = []
    _patch(monkeypatch, stopped)
    cfg = cli._cfg(project / "config.toml")
    headless_reader.update_status(cfg, lambda status: status.update(
        {"pending": ["full"], "state": "running"}))

    result = runner.invoke(cli.app, ["uninstall"])

    assert result.exit_code == 0, result.output
    assert stopped == [[20, 30, 40]]
    assert "Stopped background processes: preference learning (pid 20)" in result.output
    status = headless_reader.load_status(cfg)
    assert status["pending"] == [] and status["stop_run"] == "*"
    assert status["state"] == "stopped"


def test_uninstall_dry_run_names_them_and_stops_nothing(project, monkeypatch):
    stopped: list[list[int]] = []
    _patch(monkeypatch, stopped)
    cfg = cli._cfg(project / "config.toml")
    headless_reader.update_status(cfg, lambda status: status.update({"pending": ["full"]}))

    result = runner.invoke(cli.app, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert stopped == []
    assert "Would stop background processes" in result.output
    assert headless_reader.load_status(cfg)["pending"] == ["full"]


def test_uninstalling_one_client_leaves_background_work_alone(project, monkeypatch):
    stopped: list[list[int]] = []
    _patch(monkeypatch, stopped)

    result = runner.invoke(cli.app, ["uninstall", "--client", "cursor"])

    assert stopped == [] and "background processes" not in result.output

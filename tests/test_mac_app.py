"""The Varyz app autosync runs through on macOS, so macOS asks for a folder, not the disk.

The suite's guard stops installs from building it; the builder is taken here
before that guard replaces it.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from varyz import mac_app
from varyz.mac_app import build_app

on_a_mac = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("osacompile") is None,
    reason="builds a real app with the system's osacompile and codesign",
)


class FakeTools:
    """osacompile and codesign, as far as the builder can tell."""

    def __init__(self, *, fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, command, **kwargs):
        assert kwargs["timeout"] > 0 and kwargs["check"] is False
        self.calls.append(command)
        tool = Path(command[0]).name
        if tool == self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"{tool} broke")
        if tool == "osacompile":
            contents = Path(command[command.index("-o") + 1]) / "Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleExecutable": "applet",
                "CFBundleIdentifier": "com.apple.ScriptEditor.id.x",
                "NSCameraUsageDescription": "This script needs to access your camera.",
                "NSContactsUsageDescription": "This script needs to access your contacts.",
                "NSAppleEventsUsageDescription": "This script needs to control other apps.",
            }))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(mac_app.shutil, "which", lambda name: f"/usr/bin/{name}")
    return FakeTools()


def _info(app: Path) -> dict:
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def test_the_app_asks_for_folders_and_nothing_else(tmp_path, tools):
    app = build_app(tmp_path, "com.varyz.autosync.abc", run=tools)

    assert app == tmp_path / "Varyz.app"
    info = _info(app)
    assert info["CFBundleIdentifier"] == "com.varyz.autosync.abc"
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "Varyz"
    assert info["CFBundleVersion"] == mac_app.APP_VERSION
    assert info["LSUIElement"] is True
    reasons = {key for key in info if key.endswith("UsageDescription")}
    assert reasons == {
        "NSDocumentsFolderUsageDescription", "NSDesktopFolderUsageDescription",
        "NSDownloadsFolderUsageDescription", "NSRemovableVolumesUsageDescription",
        "NSNetworkVolumesUsageDescription",
    }
    assert {info[key] for key in reasons} == {mac_app.FOLDER_REASON}
    osacompile, codesign = tools.calls
    assert osacompile[-2:] == ["-e", mac_app.SCRIPT]
    # Signed after Info.plist changed, under the id macOS files the answer by.
    assert codesign[1:-1] == ["--force", "--sign", "-", "--identifier",
                              "com.varyz.autosync.abc"]
    assert not list(tmp_path.glob(".building*"))


def test_a_built_app_is_kept_so_macos_keeps_the_owner_s_answer(tmp_path, tools):
    app = build_app(tmp_path, "com.varyz.autosync.abc", run=tools)
    marker = app / "Contents" / "kept"
    marker.write_text("", encoding="utf-8")

    assert build_app(tmp_path, "com.varyz.autosync.abc", run=tools) == app
    assert len(tools.calls) == 2 and marker.is_file()

    # Another install's id is a different app to macOS: rebuilt.
    assert build_app(tmp_path, "com.varyz.autosync.def", run=tools) == app
    assert _info(app)["CFBundleIdentifier"] == "com.varyz.autosync.def"
    assert not marker.exists()


@pytest.mark.parametrize("fail", ["osacompile", "codesign"])
def test_a_failed_build_leaves_no_half_built_app(tmp_path, monkeypatch, fail):
    monkeypatch.setattr(mac_app.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert build_app(tmp_path, "com.varyz.autosync.abc", run=FakeTools(fail=fail)) is None
    assert list(tmp_path.iterdir()) == []


def test_without_the_system_tools_there_is_no_app(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_app.shutil, "which", lambda name: None)
    tools = FakeTools()

    assert build_app(tmp_path, "com.varyz.autosync.abc", run=tools) is None
    assert tools.calls == []


def test_the_app_is_ready_only_with_its_launcher(tmp_path, tools):
    build_app(tmp_path, "com.varyz.autosync.abc", run=tools)
    assert mac_app.ready(tmp_path) is None

    mac_app.write_launcher(tmp_path, ["/bin/true"], working_directory=tmp_path,
                           path="/usr/bin:/bin", log=tmp_path / "log", marker="Varyz")
    assert mac_app.ready(tmp_path) == tmp_path / "Varyz.app"


def test_the_launcher_sets_what_an_app_does_not_inherit(tmp_path):
    home = tmp_path / "it's here"
    command = ["/opt/py thon", "-m", "varyz.autosync_daemon", "--config", str(home / "c.toml")]

    launcher = mac_app.write_launcher(
        tmp_path, command, working_directory=home, path="/opt/homebrew/bin:/usr/bin",
        log=tmp_path / "launch log", marker="Managed by Varyz",
    )

    lines = launcher.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["#!/bin/sh", "# Managed by Varyz"]
    assert lines[2] == f"cd {shlex.quote(str(home))} || exit 1"
    assert lines[3] == "PATH=/opt/homebrew/bin:/usr/bin; export PATH"
    assert shlex.split(lines[4]) == [
        "exec", "/usr/sbin/taskpolicy", "-b", *command, ">>" + str(tmp_path / "launch log"),
        "2>&1",
    ]
    assert mac_app.launcher_runs(tmp_path, "varyz.autosync_daemon",
                                 shlex.quote(str(home / "c.toml")))
    assert not mac_app.launcher_runs(tmp_path, "something else")


def test_launchd_waits_for_the_app_and_a_hand_off_does_not():
    app = Path("/Users/o/.varyz/data/autosync/Varyz.app")

    assert mac_app.open_command(app, wait=True) == [
        "/usr/bin/open", "-W", "-n", "-g", "-a", str(app)]
    assert mac_app.open_command(app, wait=False) == [
        "/usr/bin/open", "-n", "-g", "-a", str(app)]


@on_a_mac
def test_the_app_built_on_this_mac_is_validly_signed(tmp_path):
    app = build_app(tmp_path, "com.varyz.autosync.test")

    assert app is not None
    verified = subprocess.run(
        ["codesign", "--verify", "--strict", str(app)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert verified.returncode == 0, verified.stderr
    shown = subprocess.run(
        ["codesign", "--display", "--verbose=2", str(app)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert "Identifier=com.varyz.autosync.test" in shown.stderr
    assert _info(app)["NSDocumentsFolderUsageDescription"] == mac_app.FOLDER_REASON


@on_a_mac
def test_the_launcher_runs_the_command_where_and_as_it_was_written(tmp_path):
    home = tmp_path / "it's a \"home\""
    home.mkdir()
    log = tmp_path / "launch log"
    command = [sys.executable, "-c",
               "import os, sys; print(os.getcwd()); print(os.environ['PATH']); "
               "print(sys.argv[1])", "one 'two' $three"]
    launcher = mac_app.write_launcher(tmp_path, command, working_directory=home,
                                      path="/usr/bin:/bin", log=log, marker="Varyz")

    subprocess.run(["/bin/sh", str(launcher)], env={}, timeout=60, check=True)

    assert log.read_text(encoding="utf-8").splitlines() == [
        str(home.resolve()), "/usr/bin:/bin", "one 'two' $three"]

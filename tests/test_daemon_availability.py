from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from varyz import cli
from varyz.config import load_config
from varyz.db.connection import FileLock
from varyz.resolution import availability
from varyz.resolution import client as transport


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "owner custom.toml"
    path.write_text('[resolution]\nport = 1207\n', encoding="utf-8")
    cfg = load_config(path)
    cfg.db_path.parent.mkdir(parents=True)
    cfg.db_path.touch()
    return path


def test_cli_pins_exact_config_filename(config_path):
    cfg = load_config(config_path)
    assert cli._resolution_client(cfg).config_path == config_path.resolve()
    assert "source_file" not in cfg.model_dump()


def test_refused_connection_recovers_once_without_replaying_uncertain_writes(
    config_path, monkeypatch,
):
    client = cli._resolution_client(load_config(config_path))
    requests = []
    starts = []

    def send(method, path, payload=None):
        requests.append((method, path))
        if len(requests) == 1:
            raise transport.DaemonUnavailableError("refused")
        if path == "/identity":
            return {"archive": str(client.archive)}
        return {"items": []}

    monkeypatch.setattr(client, "_send", send)
    monkeypatch.setattr(
        transport, "ensure_daemon", lambda c, p: starts.append(p) or SimpleNamespace(running=True)
    )
    assert client.pending() == ()
    assert starts == [config_path.resolve()]
    assert requests == [("GET", "/identity"), ("GET", "/identity"), ("GET", "/escalations")]


@pytest.mark.parametrize("path", ["/status", "/shutdown"])
def test_health_and_stop_do_not_restart_the_daemon(config_path, monkeypatch, path):
    client = cli._resolution_client(load_config(config_path))
    client._archive_checked = True
    monkeypatch.setattr(
        client, "_send", lambda *a: (_ for _ in ()).throw(transport.DaemonUnavailableError())
    )
    monkeypatch.setattr(transport, "ensure_daemon", lambda *a: pytest.fail("unexpected startup"))
    with pytest.raises(transport.DaemonUnavailableError):
        client._request("GET" if path == "/status" else "POST", path)


@pytest.mark.parametrize("error", [
    transport.ResolutionClientError("timeout"),
    transport.ResolutionClientError("HTTP 401"),
    transport.ArchiveMismatchError("another archive"),
])
def test_uncertain_writes_auth_and_archive_errors_are_not_retried(
    config_path, monkeypatch, error,
):
    client = cli._resolution_client(load_config(config_path))
    client._archive_checked = True
    calls = []

    def send(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(client, "_send", send)
    monkeypatch.setattr(transport, "ensure_daemon", lambda *a: pytest.fail("unexpected startup"))
    with pytest.raises(type(error)):
        client._request("POST", "/knowledge", {"statement": "a confirmed rule"})
    assert len(calls) == 1


@pytest.mark.parametrize("reason,expected", [
    (ConnectionRefusedError(10061, "refused"), transport.DaemonUnavailableError),
    (TimeoutError("uncertain delivery"), transport.ResolutionClientError),
])
def test_transport_distinguishes_refusal_from_timeout(monkeypatch, reason, expected):
    monkeypatch.setattr(
        transport, "urlopen", lambda *a, **k: (_ for _ in ()).throw(URLError(reason))
    )
    with pytest.raises(expected) as caught:
        transport.ResolutionClient()._send("POST", "/knowledge", {})
    assert type(caught.value) is expected


def test_pause_blocks_watchdog_cli_and_shared_starter(config_path, monkeypatch):
    cfg = load_config(config_path)
    availability.set_paused(cfg, True)
    monkeypatch.setattr(transport, "ensure_daemon", lambda *a: pytest.fail("unexpected startup"))
    assert availability.check_once(config_path) == "paused"
    client = cli._resolution_client(cfg)
    monkeypatch.setattr(
        client, "_send", lambda *a: (_ for _ in ()).throw(transport.DaemonUnavailableError())
    )
    with pytest.raises(transport.DaemonUnavailableError):
        client.pending()
    availability.set_paused(cfg, False)
    assert not availability.is_paused(cfg)


def test_shared_starter_respects_pause_before_spawning(config_path, monkeypatch):
    availability.set_paused(load_config(config_path), True)
    client = transport.ResolutionClient()
    monkeypatch.setattr(
        client, "status", lambda: (_ for _ in ()).throw(transport.DaemonUnavailableError())
    )
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert transport.ensure_daemon(client, config_path) is None


def test_check_recovers_stopped_daemon_and_records_only_status(config_path, monkeypatch):
    monkeypatch.setattr(
        transport.ResolutionClient, "status",
        lambda *a: (_ for _ in ()).throw(transport.DaemonUnavailableError()),
    )
    started = []
    monkeypatch.setattr(transport, "ensure_daemon", lambda c, p: started.append(p) or object())
    assert availability.check_once(config_path) == "recovered"
    assert started == [config_path]
    root = availability.directory(load_config(config_path))
    assert json.loads((root / "status.json").read_text())["status"] == "recovered"
    assert len((root / "availability.jsonl").read_text().splitlines()) == 1


def test_check_does_not_log_private_exception_messages(config_path, monkeypatch):
    monkeypatch.setattr(
        transport.ResolutionClient, "status",
        lambda *a: (_ for _ in ()).throw(transport.ArchiveMismatchError("private secret")),
    )
    monkeypatch.setattr(transport, "ensure_daemon", lambda *a: pytest.fail("unexpected startup"))
    assert availability.check_once(config_path) == "failed:ArchiveMismatchError"
    log = availability.directory(load_config(config_path)) / "availability.jsonl"
    assert "private secret" not in log.read_text()


def test_scheduled_check_is_disabled_without_installation(config_path, monkeypatch):
    monkeypatch.setattr(transport, "ensure_daemon", lambda *a: pytest.fail("unexpected startup"))
    assert availability.check_once(config_path, scheduled=True) == "disabled"


def test_overlapping_checks_do_not_start_duplicate_daemons(config_path):
    cfg = load_config(config_path)
    lock = FileLock(availability.directory(cfg) / "CHECK.lock")
    assert lock.acquire()
    try:
        assert availability.check_once(config_path) == "busy"
    finally:
        lock.release()


def test_startup_script_is_hidden_and_retries_a_crashed_worker(config_path):
    script = availability.startup_script(config_path, Path('C:/Python "quoted"/pythonw.exe'))
    assert availability.REGISTRATION in script
    assert ", 0, True)" in script
    assert "If result = 0 Then Exit Do" in script
    assert "WScript.Sleep 5000" in script
    assert "ENABLED" in script
    assert "--watch" in script
    assert "owner custom.toml" in script


def test_uninstall_disables_recovery_without_touching_archive(config_path, monkeypatch, tmp_path):
    cfg = load_config(config_path)
    root = availability.directory(cfg)
    root.mkdir(parents=True)
    (root / "ENABLED").touch()
    target = tmp_path / "startup.vbs"
    target.write_text(availability.REGISTRATION)
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    availability.uninstall(config_path)
    assert not target.exists()
    assert not (root / "ENABLED").exists()
    assert availability.is_paused(cfg)
    assert cfg.db_path.is_file()


def test_watch_continues_after_a_failed_check(config_path, monkeypatch):
    root = availability.directory(load_config(config_path))
    root.mkdir(parents=True)
    enabled = root / "ENABLED"
    enabled.touch()
    checks = []

    def check(path, *, scheduled=False):
        checks.append(path)
        if len(checks) == 2:
            enabled.unlink()
        return "failed:start" if len(checks) == 1 else "recovered"

    monkeypatch.setattr(availability, "check_once", check)
    monkeypatch.setattr(availability.time, "sleep", lambda seconds: None)
    assert availability.watch(config_path) == 0
    assert len(checks) == 2


def test_watch_exits_for_supervisor_to_reload_updated_code(config_path, monkeypatch):
    from varyz import autosync

    root = availability.directory(load_config(config_path))
    root.mkdir(parents=True)
    (root / "ENABLED").touch()
    fingerprints = iter(["old", "new"])
    monkeypatch.setattr(autosync, "code_fingerprint", lambda: next(fingerprints))
    monkeypatch.setattr(availability, "check_once", lambda *a, **k: pytest.fail("old code ran"))
    assert availability.watch(config_path) == 1


def test_install_dry_run_writes_nothing(config_path, monkeypatch, tmp_path):
    monkeypatch.setattr(availability.sys, "platform", "win32")
    monkeypatch.setattr(availability.sys, "executable", str(tmp_path / "python.exe"))
    (tmp_path / "pythonw.exe").touch()
    target = tmp_path / "startup" / "varyz.vbs"
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    monkeypatch.setattr(availability.shutil, "which", lambda p: "wscript.exe")
    monkeypatch.setattr(availability, "check_once", lambda *a, **k: pytest.fail("preflight ran"))
    assert availability.install(config_path, dry_run=True) == target
    assert not target.exists()
    assert not availability.directory(load_config(config_path)).exists()


def test_failed_registration_rolls_back_enable_marker(config_path, monkeypatch, tmp_path):
    monkeypatch.setattr(availability, "refresh_registration", lambda p: p)
    monkeypatch.setattr(availability.sys, "platform", "win32")
    monkeypatch.setattr(availability.sys, "executable", str(tmp_path / "python.exe"))
    (tmp_path / "pythonw.exe").touch()
    target = tmp_path / "startup" / "varyz.vbs"
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    monkeypatch.setattr(availability.shutil, "which", lambda p: "wscript.exe")
    monkeypatch.setattr(availability, "check_once", lambda p: "running")
    monkeypatch.setattr(
        availability.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("failed"))
    )
    with pytest.raises(OSError):
        availability.install(config_path)
    assert not target.exists()
    assert not (availability.directory(load_config(config_path)) / "ENABLED").exists()


def test_refresh_registration_uses_gui_launcher_without_starting(
    config_path, monkeypatch, tmp_path,
):
    from varyz import windows_hook

    target = tmp_path / "startup.vbs"
    target.write_text("' " + availability.REGISTRATION, encoding="utf-8")
    launcher = tmp_path / "varyz-watchdog.exe"
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    monkeypatch.setattr(windows_hook, "write_launchers", lambda *a: {"watchdog": launcher})
    monkeypatch.setattr(availability, "check_once", lambda *a: pytest.fail("started daemon"))
    monkeypatch.setattr(availability.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert availability.refresh_registration(config_path) == target
    script = target.read_text(encoding="utf-8")
    assert str(launcher) in script
    assert "pythonw.exe" not in script
    assert ", 0, True)" in script
    assert "If result = 0 Then Exit Do" in script


def test_refresh_registration_does_not_create_or_replace_foreign_entry(
    config_path, monkeypatch, tmp_path,
):
    target = tmp_path / "startup.vbs"
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    assert availability.refresh_registration(config_path) is None
    assert not target.exists()
    target.write_text("foreign", encoding="utf-8")
    with pytest.raises(ValueError, match="not owned"):
        availability.refresh_registration(config_path)
    assert target.read_text(encoding="utf-8") == "foreign"


def test_watch_startup_failure_records_only_exception_type(config_path, monkeypatch):
    monkeypatch.setattr(
        availability, "watch", lambda p: (_ for _ in ()).throw(PermissionError("private secret"))
    )
    assert availability.main(["--config", str(config_path), "--watch"]) == 1
    root = availability.directory(load_config(config_path))
    status = (root / "status.json").read_text(encoding="utf-8")
    assert "failed:startup:PermissionError" in status
    assert "private secret" not in status


def test_watch_startup_failure_still_retries_if_status_cannot_be_written(config_path, monkeypatch):
    monkeypatch.setattr(
        availability, "watch", lambda p: (_ for _ in ()).throw(PermissionError())
    )
    monkeypatch.setattr(
        availability, "_record", lambda *a: (_ for _ in ()).throw(PermissionError())
    )
    assert availability.main(["--config", str(config_path), "--watch"]) == 1


def test_on_demand_removes_only_owned_login_recovery_and_starts_nothing(
    config_path, monkeypatch, tmp_path,
):
    target = tmp_path / "startup.vbs"
    target.write_text("' " + availability.REGISTRATION, encoding="utf-8")
    monkeypatch.setattr(availability.sys, "platform", "win32")
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    monkeypatch.setattr(availability, "check_once", lambda *a, **k: pytest.fail("started"))
    monkeypatch.setattr(availability.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    cfg = load_config(config_path)
    root = availability.directory(cfg)
    root.mkdir(parents=True)
    (root / "ENABLED").touch()
    availability.set_paused(cfg, True)
    availability.use_on_demand(config_path)
    assert not target.exists()
    assert not (root / "ENABLED").exists()
    assert not availability.is_paused(cfg)
    assert availability.is_on_demand(cfg)
    assert cfg.db_path.exists()
    assert cfg.resolution.autostart
    availability.use_on_demand(config_path)  # Repeating the choice is safe.


def test_on_demand_preserves_foreign_startup_and_existing_mode(config_path, monkeypatch, tmp_path):
    target = tmp_path / "startup.vbs"
    target.write_text("foreign", encoding="utf-8")
    monkeypatch.setattr(availability.sys, "platform", "win32")
    monkeypatch.setattr(availability, "startup_path", lambda p: target)
    cfg = load_config(config_path)
    availability.set_paused(cfg, True)
    with pytest.raises(ValueError, match="not owned"):
        availability.use_on_demand(config_path)
    assert target.read_text(encoding="utf-8") == "foreign"
    assert availability.is_paused(cfg)
    assert not availability.is_on_demand(cfg)


def test_on_demand_first_request_still_starts_daemon(config_path, monkeypatch):
    cfg = load_config(config_path)
    root = availability.directory(cfg)
    root.mkdir(parents=True)
    (root / "ON_DEMAND").touch()
    status = object()
    client = transport.ResolutionClient()
    calls = iter([transport.DaemonUnavailableError(), status])

    def read_status():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    launches = []
    monkeypatch.setattr(client, "status", read_status)
    monkeypatch.setattr(
        transport.subprocess, "Popen",
        lambda command, **kwargs: launches.append((command, kwargs)) or SimpleNamespace(),
    )
    assert transport.ensure_daemon(client, config_path) is status
    assert len(launches) == 1
    assert launches[0][0][1:5] == ["-m", "varyz", "daemon", "run"]

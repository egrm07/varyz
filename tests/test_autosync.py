"""Autonomous refresh stays idempotent, observable, pausable, and offline-testable."""

from __future__ import annotations

import configparser
import json
import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import FakeEmbedder
from varyz import autosync, autosync_daemon
from varyz.config import load_config
from varyz.db.connection import open_db


def _project(
    tmp_path: Path,
    *,
    broken_source: bool = False,
    background_inference: bool = False,
    max_per_sync: int = 200,
) -> Path:
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "plan.md").write_text("# Lisbon\n\nBook the train on Friday.", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    extra = ""
    if broken_source:
        extra = (
            "\n[connectors.git]\n"
            "approved = true\n"
            f'paths = ["{(tmp_path / "missing-export").as_posix()}"]\n'
        )
    config_path.write_text(
        "[storage]\n"
        'data_dir = "data"\n\n'
        "[graph]\n"
        'extractor = "deterministic"\n\n'
        "[autosync]\n"
        f"semantic_graph = {str(background_inference).lower()}\n\n"
        "[conversations]\n"
        f"max_per_sync = {max_per_sync}\n\n"
        "[connectors.documents]\n"
        "approved = true\n"
        f'paths = ["{docs.as_posix()}"]\n' + extra,
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    open_db(cfg.db_path).close()
    return config_path


def test_dry_run_lists_every_configured_source_without_writes(tmp_path):
    config_path = _project(tmp_path, broken_source=True)
    cfg = load_config(config_path)
    result = autosync.run_once(config_path, trigger="test", dry_run=True)

    assert result.status == "dry_run"
    assert [value.connector for value in result.connectors] == ["documents", "git"]
    assert all(value.status == "planned" for value in result.connectors)
    assert not autosync.autosync_dir(cfg).exists()


def test_run_syncs_all_sources_embeds_and_writes_digest(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    first = autosync.run_once(config_path, trigger="test")
    second = autosync.run_once(config_path, trigger="test")

    assert first.status == "ok" and first.new == 1 and first.embedded > 0
    assert first.graph_status == "updated" and first.graph_entries == 1
    assert second.status == "ok" and second.new == 0 and second.unchanged == 1
    # Nothing arrived and the graph is minutes old, so the second cycle ingests
    # and stops there rather than restaging the archive to reach the same graph.
    assert second.graph_status == "throttled"
    state = json.loads(autosync.state_path(cfg).read_text(encoding="utf-8"))
    assert state["status"] == "ok" and state["graph_status"] == "throttled"
    assert state["connectors"][0]["connector"] == "documents"
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM graph_runs WHERE state='active'").fetchone()[0] == 1
    conn.close()
    assert len(autosync.log_path(cfg).read_text(encoding="utf-8").splitlines()) == 2
    report = autosync.report_path(cfg).read_text(encoding="utf-8")
    assert "# Varyz autosync" in report and "| documents | ok |" in report


def test_contextual_graph_never_starts_local_model_in_background_by_default(
    tmp_path, monkeypatch
):
    docs = tmp_path / "notes"
    docs.mkdir()
    (docs / "plan.md").write_text(
        "# Lisbon\n\nBook the train on Friday.", encoding="utf-8"
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[storage]\n"
        'data_dir = "data"\n\n'
        "[connectors.documents]\n"
        "approved = true\n"
        f'paths = ["{docs.as_posix()}"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    open_db(cfg.db_path).close()
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    from varyz.graph import service as graph_service

    def refuse(*_args, **_kwargs):
        pytest.fail("autosync attempted contextual graph extraction")

    monkeypatch.setattr(graph_service, "rebuild_graph", refuse)
    monkeypatch.setattr(
        autosync,
        "_release_local_model",
        lambda: pytest.fail("deferred autosync touched a model it does not own"),
    )

    result = autosync.run_once(config_path, trigger="test")

    assert result.status == "ok"
    assert result.new == 1
    assert result.graph_status == "deferred"
    assert "background local inference is disabled" in result.detail


def test_one_broken_source_does_not_stop_the_others(tmp_path, monkeypatch):
    config_path = _project(tmp_path, broken_source=True)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    result = autosync.run_once(config_path, trigger="test")

    assert result.status == "failed"
    by_source = {value.connector: value for value in result.connectors}
    assert by_source["documents"].new == 1
    assert by_source["git"].errors == 1
    assert "missing-export" in by_source["git"].detail
    assert result.graph_status == "updated"


def test_graph_update_releases_writer_lock_and_keeps_wal_readers_available(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    from varyz.graph import service as graph_service

    real_rebuild = graph_service.rebuild_graph

    def observed_rebuild(conn, config, **kwargs):
        from varyz.db.connection import WriteLock, connect

        with WriteLock(config.data_dir):
            pass
        reader = connect(config.db_path)
        try:
            assert reader.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        finally:
            reader.close()
        return real_rebuild(conn, config, **kwargs)

    monkeypatch.setattr(graph_service, "rebuild_graph", observed_rebuild)
    result = autosync.run_once(config_path, trigger="test")

    assert result.status == "ok"
    assert result.graph_status == "updated"


def test_graph_failure_is_reported_without_rolling_back_ingestion(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    from varyz.graph import service as graph_service

    monkeypatch.setattr(
        graph_service,
        "rebuild_graph",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("graph fixture")),
    )
    result = autosync.run_once(config_path, trigger="test")

    assert result.status == "failed"
    assert result.graph_status == "failed"
    assert "graph fixture" in result.detail
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    conn.close()


def test_graph_prune_failure_is_a_visible_warning_not_a_failed_cycle(
    tmp_path, monkeypatch
):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    from varyz.graph import prune as graph_prune

    monkeypatch.setattr(
        graph_prune,
        "prune_graph",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic prune failure")
        ),
    )

    result = autosync.run_once(config_path, trigger="test")

    assert result.status == "ok"
    assert result.graph_status == "updated"
    assert result.graph_pruned == 0
    assert result.warnings == ["Graph prune failed: RuntimeError: synthetic prune failure"]
    state = json.loads(autosync.state_path(cfg).read_text(encoding="utf-8"))
    assert state["status"] == "ok"
    assert state["warnings"] == result.warnings
    report = autosync.report_path(cfg).read_text(encoding="utf-8")
    assert "0 errors, 1 warning" in report
    assert "## Warnings" in report
    assert "Graph prune failed: RuntimeError: synthetic prune failure" in report


def test_pause_is_a_persistent_no_op_kill_switch(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    autosync.pause(cfg)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    result = autosync.run_once(config_path, trigger="scheduled")
    assert result.status == "paused"
    assert autosync.paused_path(cfg).is_file()

    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    conn.close()
    autosync.resume(cfg)
    assert not autosync.paused_path(cfg).exists()


def test_kill_switch_stops_a_run_already_in_flight(tmp_path, monkeypatch):
    """A graph rebuild runs for hours. A switch that only gates the next run is not one."""
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    from varyz.graph import service as graph_service

    seen: list[object] = []

    def paused_mid_rebuild(conn, config, **kwargs):
        should_stop = kwargs["should_stop"]
        assert not should_stop()
        autosync.pause(config)
        seen.append(should_stop())
        return SimpleNamespace(
            stopped=True,
            no_op=False,
            run_id="staging-run",
            entries_processed=7,
            entries_reused=0,
            entries_failed=0,
            claims=0,
        )

    monkeypatch.setattr(graph_service, "rebuild_graph", paused_mid_rebuild)
    result = autosync.run_once(config_path, trigger="test")

    assert seen == [True]
    assert result.status == "paused"
    assert result.graph_status == "stopped"
    assert "resumes from here" in result.detail


def test_paused_run_skips_the_sources_it_has_not_reached(tmp_path, monkeypatch):
    config_path = _project(tmp_path, broken_source=True)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    real_sync = autosync.sync_connector

    def sync_then_pause(conn, vault, connector, config):
        autosync.pause(config)
        return real_sync(conn, vault, connector, config)

    monkeypatch.setattr(autosync, "sync_connector", sync_then_pause)
    result = autosync.run_once(config_path, trigger="test")

    by_source = {value.connector: value for value in result.connectors}
    assert by_source["documents"].new == 1
    assert by_source["git"].status == "skipped"
    assert by_source["git"].detail == "kill switch engaged"


def test_dead_claim_holder_does_not_block_the_next_run(tmp_path):
    """A killed daemon leaves its RUNNING marker behind; the pid outlives nothing."""
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    claim = autosync.claim_path(cfg)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("pid=999999999 started=2026-08-03T00:00:00+00:00\n", encoding="utf-8")

    assert autosync._claim(cfg) is True
    assert "pid=" in claim.read_text(encoding="utf-8")
    autosync._release_claim(cfg)


def test_live_claim_holder_still_blocks_a_second_run(tmp_path):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    claim = autosync.claim_path(cfg)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(f"pid={os.getpid()} started=2026-08-03T00:00:00+00:00\n", encoding="utf-8")

    assert autosync._claim(cfg) is False
    assert autosync.process_alive(os.getpid()) is True


def test_daemon_hands_off_when_the_installed_code_changes(tmp_path, monkeypatch):
    """A daemon up for days runs the code it imported; it must not outlive a release."""
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    versions = iter(["before"] + ["after"] * 20)
    monkeypatch.setattr(autosync_daemon, "code_fingerprint", lambda: next(versions))
    monkeypatch.setattr(autosync_daemon, "is_schedule_due", lambda _cfg: True)
    monkeypatch.setattr(
        autosync_daemon,
        "run_once",
        lambda path, trigger: pytest.fail("a stale daemon must not run a sync"),
    )
    restarted: list[Path] = []

    def restart(path: Path) -> bool:
        restarted.append(path)
        _successor_heartbeat(cfg)
        return True

    assert (
        autosync_daemon.run_daemon(
            config_path, max_loops=20, restart=restart, sleep=lambda _seconds: None
        )
        == 0
    )
    assert restarted == [config_path.resolve()]


def _successor_heartbeat(cfg) -> None:
    autosync._atomic_json(
        autosync.daemon_heartbeat_path(cfg),
        {"pid": os.getpid() + 1, "heartbeat_at": "2026-09-26T08:00:00+00:00", "status": "idle"},
    )


def test_daemon_waits_for_an_edit_in_progress_before_handing_off(tmp_path, monkeypatch):
    """A successor that imports between two file writes dies on the half-made change."""
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    # The source keeps moving: every poll sees a different fingerprint.
    versions = iter(["before"] + [f"edit-{n}" for n in range(20)])
    monkeypatch.setattr(autosync_daemon, "code_fingerprint", lambda: next(versions))
    monkeypatch.setattr(autosync_daemon, "is_schedule_due", lambda _cfg: True)
    monkeypatch.setattr(
        autosync_daemon,
        "run_once",
        lambda path, trigger: pytest.fail("code already replaced on disk must not sync"),
    )
    restarted: list[Path] = []

    autosync_daemon.run_daemon(
        config_path,
        max_loops=15,
        restart=lambda path: bool(restarted.append(path)),
        sleep=lambda _seconds: None,
    )
    assert restarted == []
    heartbeat = json.loads(autosync.daemon_heartbeat_path(cfg).read_text(encoding="utf-8"))
    assert heartbeat["status"] == "stopped"


def test_a_successor_that_dies_at_launch_is_launched_again(tmp_path, monkeypatch):
    """Nothing else restarts autosync before the next login, so the handoff must land."""
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    versions = iter(["before"] + ["after"] * 20)
    monkeypatch.setattr(autosync_daemon, "code_fingerprint", lambda: next(versions))
    launches: list[Path] = []
    slept: list[float] = []

    def restart(path: Path) -> bool:
        launches.append(path)
        if len(launches) == 3:
            _successor_heartbeat(cfg)
        return True

    assert (
        autosync_daemon.run_daemon(config_path, restart=restart, sleep=slept.append) == 0
    )
    assert len(launches) == 3
    # Each failed launch waits longer before the next one.
    settling = autosync_daemon.POLL_SECONDS * (autosync_daemon.SETTLE_POLLS - 1)
    waits = sum(slept) - settling
    first = autosync_daemon.HANDOFF_WAIT_SECONDS
    assert waits == first + 2 * first + autosync_daemon.POLL_SECONDS


def test_a_failed_handoff_stops_retrying_once_autosync_is_disabled(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    versions = iter(["before"] + ["after"] * 20)
    monkeypatch.setattr(autosync_daemon, "code_fingerprint", lambda: next(versions))
    launches: list[Path] = []

    def restart(path: Path) -> bool:
        launches.append(path)
        if len(launches) == 2:
            autosync.disable(cfg)
        return True

    assert (
        autosync_daemon.run_daemon(config_path, restart=restart, sleep=lambda _s: None) == 1
    )
    assert len(launches) == 2


def test_stale_mcp_use_launches_only_one_detached_refresh(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    launches = []

    def fake_popen(command, **kwargs):
        launches.append((command, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(autosync.subprocess, "Popen", fake_popen)
    assert autosync.maybe_trigger_on_use(cfg, config_path) == "started"
    assert autosync.maybe_trigger_on_use(cfg, config_path) == "already_running"
    assert len(launches) == 1
    command, kwargs = launches[0]
    assert command[-1] == str(config_path.resolve())
    assert "--claimed" in command and kwargs["stdout"] is autosync.subprocess.DEVNULL


def test_fresh_state_does_not_launch_an_on_use_refresh(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    autosync.state_path(cfg).write_text(
        json.dumps({"finished_at": autosync.iso(autosync.utc_now())}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        autosync.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch")),
    )
    assert autosync.maybe_trigger_on_use(cfg, config_path) == "fresh_or_disabled"


def test_windows_install_writes_login_startup_and_launches_daemon(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    appdata = tmp_path / "Windows Profile With Space"
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(autosync.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(
        autosync.shutil,
        "which",
        lambda name: "C:/Windows/System32/wscript.exe" if name == "wscript" else None,
    )
    monkeypatch.setattr(autosync.subprocess, "Popen", fake_popen)
    installed = autosync.install_daemon(config_path, every_minutes=17)
    installed_again = autosync.install_daemon(config_path, every_minutes=17)

    startup = Path(installed)
    assert installed_again == installed
    assert startup.name.startswith("VaryzAutoSync-") and startup.suffix == ".vbs"
    assert str(appdata) in str(startup)
    startup_text = startup.read_text(encoding="utf-8")
    assert "WScript.Shell" in startup_text and autosync.REGISTRATION_MARKER in startup_text
    assert len(list(startup.parent.glob(f"{autosync.DAEMON_PREFIX}-*.vbs"))) == 1
    runner = autosync.daemon_runner_path(load_config(config_path))
    assert "varyz.autosync_daemon" in runner.read_text(encoding="utf-8")
    # A successor that dies at launch leaves its traceback in the launch log,
    # through a file of its own: a daemon holding the log as its stderr would
    # stop the next runner's own ">>log" and so the successor from starting.
    runner_text = runner.read_text(encoding="utf-8")
    assert '2>"%varyz_err%"' in runner_text and "2>>" not in runner_text
    assert "%RANDOM%" in runner_text
    assert str(config_path.resolve()) in runner.read_text(encoding="utf-8")
    assert calls[0][0] == ["C:/Windows/System32/wscript.exe", str(startup)]
    assert autosync.enabled_path(load_config(config_path)).is_file()
    marker = json.loads(autosync.enabled_path(load_config(config_path)).read_text())
    assert marker["schedule_minutes"] == 17
    login = next(check for check in autosync.doctor(config_path) if check.name == "login startup")
    assert login.ok and "will run at login" in login.detail and str(startup) in login.detail

    unrelated = startup.parent / "other-startup.vbs"
    unrelated.write_text("user file", encoding="utf-8")
    removed = autosync.uninstall_daemon(config_path)
    assert "removed" in removed and str(startup) in removed
    assert not startup.exists() and unrelated.read_text(encoding="utf-8") == "user file"
    assert "nothing installed" in autosync.uninstall_daemon(config_path)

    startup.write_text("user-owned same-name file", encoding="utf-8")
    left = autosync.uninstall_daemon(config_path)
    assert "non-Varyz" in left and startup.is_file()


def test_windows_install_names_missing_login_support(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    monkeypatch.setattr(autosync.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(autosync.shutil, "which", lambda _name: None)

    with pytest.raises(ValueError, match="APPDATA or wscript missing"):
        autosync.install_daemon(config_path, every_minutes=15)


def test_macos_launch_agent_round_trip_is_idempotent_and_owned(tmp_path, monkeypatch):
    home = tmp_path / "Home With Space"
    home.mkdir()
    config_path = _project(home)
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    monkeypatch.setattr(autosync.shutil, "which", lambda _name: None)
    spawned: list[Path] = []
    monkeypatch.setattr(autosync, "spawn_daemon", lambda path: spawned.append(path) or True)

    installed = Path(autosync.install_daemon(config_path, every_minutes=19))
    first = installed.read_text(encoding="utf-8")
    installed_again = Path(autosync.install_daemon(config_path, every_minutes=19))

    assert installed == installed_again == autosync.launch_agent_path(config_path)
    assert len(list(installed.parent.glob("com.varyz.autosync.*.plist"))) == 1
    assert installed.read_text(encoding="utf-8") == first
    plist = plistlib.loads(installed.read_bytes())
    assert plist["RunAtLoad"] is True
    assert "varyz.autosync_daemon" in plist["ProgramArguments"]
    assert str(config_path.resolve()) in plist["ProgramArguments"]
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]
    assert "Home With Space" in plist["WorkingDirectory"]
    assert plist["ProcessType"] == "Background"
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"].split(":")
    # No launchctl: it still starts now, as a plain background process.
    assert spawned == [config_path.resolve(), config_path.resolve()]
    login = next(check for check in autosync.doctor(config_path) if check.name == "login startup")
    assert login.ok and "launchd" in login.detail and "will run at login" in login.detail

    unrelated = installed.parent / "other.agent.plist"
    unrelated.write_text("not Varyz", encoding="utf-8")
    removed = autosync.uninstall_daemon(config_path)
    assert "removed" in removed and str(installed) in removed
    assert not installed.exists() and unrelated.is_file()
    assert "nothing installed" in autosync.uninstall_daemon(config_path)

    installed.write_text("user-owned same-name file", encoding="utf-8")
    assert "non-Varyz" in autosync.uninstall_daemon(config_path)
    assert installed.is_file()


def _fake_app(directory: Path, bundle_id: str, **_kwargs) -> Path:
    app = directory / "Varyz.app"
    (app / "Contents").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleVersion": autosync.mac_app.APP_VERSION,
    }))
    return app


def test_macos_agent_runs_through_the_varyz_app_so_macos_asks_for_folders(
    tmp_path, monkeypatch
):
    home = tmp_path / "Owner's Home"
    project = home / "Documents" / "varyz"
    project.mkdir(parents=True)
    config_path = _project(project)
    cfg = load_config(config_path)
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    monkeypatch.setattr(autosync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(autosync.mac_app, "build_app", _fake_app)
    monkeypatch.setattr(autosync, "spawn_daemon", lambda path: True)

    plist = plistlib.loads(Path(autosync.install_daemon(config_path, every_minutes=15))
                           .read_bytes())

    app = autosync.autosync_dir(cfg) / "Varyz.app"
    assert plist["ProgramArguments"] == ["/usr/bin/open", "-W", "-n", "-g", "-a", str(app)]
    launcher = (autosync.autosync_dir(cfg) / "varyz-autosync.sh").read_text(encoding="utf-8")
    assert "varyz.autosync_daemon" in launcher and autosync.REGISTRATION_MARKER in launcher
    login = next(check for check in autosync.doctor(config_path) if check.name == "login startup")
    assert login.ok and "will run at login" in login.detail
    assert autosync.daemon_program(config_path) == str(app)

    assert autosync.daemon_command(config_path, wait=False) == [
        "/usr/bin/open", "-n", "-g", "-a", str(app)]

    before = next(c for c in autosync.doctor(config_path) if c.name == "macOS privacy")
    assert before.ok and "macOS asks once whether Varyz may read them" in before.detail
    advice = autosync.mac_privacy_advice([project / "notes"], config_path)
    assert "choose Allow" in advice and "Files and Folders" in advice
    assert "Full Disk Access" not in advice and "varyz sync" in advice

    # A launcher that no longer runs this install's daemon is not a valid registration.
    (autosync.autosync_dir(cfg) / "varyz-autosync.sh").write_text("#!/bin/sh\n",
                                                                   encoding="utf-8")
    login = next(check for check in autosync.doctor(config_path) if check.name == "login startup")
    assert not login.ok


def test_a_hand_off_starts_the_successor_through_the_app(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config_path = _project(home)
    cfg = load_config(config_path)
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    directory = autosync.autosync_dir(cfg)
    _fake_app(directory, "com.varyz.autosync.x")
    (directory / "varyz-autosync.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    started: list[list[str]] = []
    monkeypatch.setattr(autosync.subprocess, "Popen",
                        lambda command, **_kw: started.append(command))

    assert autosync.spawn_daemon(config_path)
    assert started == [["/usr/bin/open", "-n", "-g", "-a", str(directory / "Varyz.app")]]


def _fake_launchctl(monkeypatch, *, loaded: bool = False, refuse: bool = False) -> list[list[str]]:
    calls: list[list[str]] = []
    state = {"loaded": loaded}

    def fake_run(command, **_kwargs):
        calls.append(command[1:])
        verb = command[1]
        if verb == "print":
            code = 0 if state["loaded"] else 113
        elif verb == "bootstrap":
            code = 5 if refuse else 0
            state["loaded"] = state["loaded"] or not refuse
        elif verb == "bootout":
            state["loaded"] = False
            code = 0
        else:
            code = 0
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(
        autosync.shutil, "which", lambda name: "/bin/launchctl" if name == "launchctl" else None
    )
    monkeypatch.setattr(autosync.subprocess, "run", fake_run)
    monkeypatch.setattr(autosync, "_launchd_domain", lambda: "gui/501")
    return calls


def test_macos_agent_starts_now_and_is_unloaded_on_uninstall(tmp_path, monkeypatch):
    """Before this, the agent was only written: nothing ran until the next login,
    and uninstalling left it loaded in the session."""
    home = tmp_path / "home"
    home.mkdir()
    config_path = _project(home)
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    calls = _fake_launchctl(monkeypatch)
    spawned: list[Path] = []
    monkeypatch.setattr(autosync, "spawn_daemon", lambda path: spawned.append(path) or True)
    label = autosync.launch_agent_path(config_path).stem

    agent = autosync.install_daemon(config_path, every_minutes=15)
    assert calls[-1] == ["bootstrap", "gui/501", agent]
    # A reinstall starts it only if it is not running; a sync is never killed.
    autosync.install_daemon(config_path, every_minutes=15)
    assert calls[-1] == ["kickstart", f"gui/501/{label}"]
    assert not any(call[0] == "bootout" for call in calls)
    assert spawned == []

    autosync.uninstall_daemon(config_path)
    assert calls[-1] == ["bootout", f"gui/501/{label}"]
    assert not Path(agent).exists()


def test_macos_agent_falls_back_to_a_background_process_then_refuses(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config_path = _project(home)
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    _fake_launchctl(monkeypatch, refuse=True)
    monkeypatch.setattr(autosync, "spawn_daemon", lambda _path: True)
    assert Path(autosync.install_daemon(config_path, every_minutes=15)).is_file()

    monkeypatch.setattr(autosync, "spawn_daemon", lambda _path: False)
    with pytest.raises(ValueError, match="could not start"):
        autosync.install_daemon(config_path, every_minutes=15)
    assert not autosync.launch_agent_path(config_path).exists()
    assert not autosync.is_enabled(load_config(config_path))


def test_macos_privacy_block_is_found_by_the_agent_and_shown_by_doctor(tmp_path, monkeypatch):
    """A folder macOS protects reads as unreadable to a background agent, silently.

    The walk skips unreadable folders, so without this a Mac owner's archive
    would stop growing with every check green.
    """
    home = tmp_path / "home"
    project = home / "Documents" / "varyz"
    project.mkdir(parents=True)
    config_path = _project(project)
    cfg = load_config(config_path)
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)

    protected = autosync.mac_protected_paths(cfg)
    assert cfg.data_dir in protected and project / "notes" in protected
    before = next(c for c in autosync.doctor(config_path) if c.name == "macOS privacy")
    assert before.ok and "Full Disk Access" in before.detail

    def refuse(path):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(autosync.os, "scandir", refuse)
    blocked = autosync.record_mac_privacy(cfg, config_path)
    assert blocked == protected
    after = next(c for c in autosync.doctor(config_path) if c.name == "macOS privacy")
    assert not after.ok
    assert "Full Disk Access" in after.detail and "Privacy & Security" in after.detail


def test_folders_macos_does_not_protect_need_no_access(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "code" / "varyz"
    project.mkdir(parents=True)
    config_path = _project(project)
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    assert autosync.mac_protected_paths(load_config(config_path)) == []
    check = next(c for c in autosync.doctor(config_path) if c.name == "macOS privacy")
    assert check.ok and "no archive or source folder is protected" in check.detail


def test_macos_install_names_missing_home_directory(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    missing_home = tmp_path / "missing-home"
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync.Path, "home", lambda: missing_home)

    with pytest.raises(ValueError, match="launchd.*home directory missing"):
        autosync.install_daemon(config_path, every_minutes=15)


def test_linux_systemd_user_unit_round_trip_is_idempotent_and_owned(tmp_path, monkeypatch):
    home = tmp_path / "Home With Space"
    home.mkdir()
    config_path = _project(home)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(autosync.sys, "platform", "linux")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(
        autosync.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(autosync.subprocess, "run", fake_run)

    installed = Path(autosync.install_daemon(config_path, every_minutes=23))
    first = installed.read_text(encoding="utf-8")
    installed_again = Path(autosync.install_daemon(config_path, every_minutes=23))

    assert installed == installed_again == autosync.systemd_service_path(config_path)
    assert len(list(installed.parent.glob("varyz-autosync-*.service"))) == 1
    assert installed.read_text(encoding="utf-8") == first
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(installed, encoding="utf-8")
    assert "varyz.autosync_daemon" in parser["Service"]["ExecStart"]
    assert autosync._systemd_quote(str(config_path.resolve())) in parser["Service"]["ExecStart"]
    assert "Home With Space" in parser["Service"]["WorkingDirectory"]
    assert any(command[2:4] == ["enable", "--now"] for command in calls)
    login = next(check for check in autosync.doctor(config_path) if check.name == "login startup")
    assert login.ok and "systemd" in login.detail and "will run at login" in login.detail

    unrelated = installed.parent / "other.service"
    unrelated.write_text("[Unit]\nDescription=user file\n", encoding="utf-8")
    removed = autosync.uninstall_daemon(config_path)
    assert "removed" in removed and str(installed) in removed
    assert not installed.exists() and unrelated.is_file()
    assert "nothing installed" in autosync.uninstall_daemon(config_path)

    installed.write_text("[Unit]\nDescription=user-owned same-name file\n", encoding="utf-8")
    assert "non-Varyz" in autosync.uninstall_daemon(config_path)
    assert installed.is_file()


def test_linux_xdg_fallback_round_trip_is_idempotent_and_owned(tmp_path, monkeypatch):
    home = tmp_path / "Desktop Home With Space"
    home.mkdir()
    config_path = _project(home)
    monkeypatch.setattr(autosync.sys, "platform", "linux")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(autosync.shutil, "which", lambda _name: None)

    installed = Path(autosync.install_daemon(config_path, every_minutes=29))
    first = installed.read_text(encoding="utf-8")
    installed_again = Path(autosync.install_daemon(config_path, every_minutes=29))

    assert installed == installed_again == autosync.xdg_autostart_path(config_path)
    assert len(list(installed.parent.glob("varyz-autosync-*.desktop"))) == 1
    assert installed.read_text(encoding="utf-8") == first
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(installed, encoding="utf-8")
    assert parser["Desktop Entry"]["Type"] == "Application"
    assert "varyz.autosync_daemon" in parser["Desktop Entry"]["Exec"]
    quoted_config = autosync._desktop_exec_quote(str(config_path.resolve()))
    assert quoted_config in parser["Desktop Entry"]["Exec"]
    assert "Desktop Home With Space" in parser["Desktop Entry"]["Exec"]
    login = next(check for check in autosync.doctor(config_path) if check.name == "login startup")
    assert login.ok and "XDG" in login.detail and "will run at login" in login.detail

    unrelated = installed.parent / "other.desktop"
    unrelated.write_text("[Desktop Entry]\nName=user file\n", encoding="utf-8")
    removed = autosync.uninstall_daemon(config_path)
    assert "removed" in removed and str(installed) in removed
    assert not installed.exists() and unrelated.is_file()
    assert "nothing installed" in autosync.uninstall_daemon(config_path)

    installed.write_text("[Desktop Entry]\nName=user-owned same-name file\n", encoding="utf-8")
    assert "non-Varyz" in autosync.uninstall_daemon(config_path)
    assert installed.is_file()


def test_linux_falls_back_when_systemd_user_service_cannot_be_enabled(tmp_path, monkeypatch):
    home = tmp_path / "fallback-home"
    home.mkdir()
    config_path = _project(home)

    def fake_run(command, **_kwargs):
        if command[2:4] == ["enable", "--now"]:
            return SimpleNamespace(returncode=1, stderr="user manager rejected unit", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(autosync.sys, "platform", "linux")
    monkeypatch.setattr(autosync.Path, "home", lambda: home)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(
        autosync.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(autosync.subprocess, "run", fake_run)

    installed = Path(autosync.install_daemon(config_path, every_minutes=31))

    assert installed == autosync.xdg_autostart_path(config_path)
    assert installed.is_file()
    assert not autosync.systemd_service_path(config_path).exists()


def test_linux_install_names_both_missing_login_mechanisms(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    monkeypatch.setattr(autosync.sys, "platform", "linux")
    monkeypatch.setattr(autosync.shutil, "which", lambda _name: None)
    for variable in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(variable, raising=False)

    missing = "systemd user manager missing; XDG desktop session missing"
    with pytest.raises(ValueError, match=missing):
        autosync.install_daemon(config_path, every_minutes=15)


def test_uv_managed_daemon_uses_uv_with_an_explicit_working_directory(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    environment = tmp_path / "venv"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("uv = 0.11.2\n", encoding="utf-8")
    monkeypatch.setattr(autosync.sys, "prefix", str(environment))
    monkeypatch.setattr(
        autosync.shutil,
        "which",
        lambda name: "C:/tools/uv/uv.exe" if name == "uv" else None,
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'varyz'\n", encoding="utf-8")
    command = autosync.worker_command(config_path, daemon=True)
    assert command[:4] == ["C:/tools/uv/uv.exe", "run", "--directory", str(tmp_path)]
    assert "varyz.autosync_daemon" in command


def test_uv_tool_install_daemon_runs_its_own_python(tmp_path, monkeypatch):
    """A `uv tool install` environment belongs to no project, so `uv run`
    would start a Python that cannot import Varyz."""
    config_path = _project(tmp_path)
    environment = tmp_path / "tools" / "varyzai"
    environment.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("uv = 0.11.2\n", encoding="utf-8")
    monkeypatch.setattr(autosync.sys, "prefix", str(environment))
    monkeypatch.setattr(autosync.sys, "executable", str(environment / "bin" / "python"))
    monkeypatch.setattr(autosync.shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)
    command = autosync.worker_command(config_path, daemon=True)
    assert command[0] == str(environment / "bin" / "python")
    assert "run" not in command
    assert "varyz.autosync_daemon" in command


def test_daemon_runs_due_sync_and_leaves_a_stopped_heartbeat(tmp_path, monkeypatch):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    calls = []
    monkeypatch.setattr(autosync_daemon, "is_schedule_due", lambda _cfg: True)
    monkeypatch.setattr(
        autosync_daemon,
        "run_once",
        lambda path, trigger: calls.append((path, trigger)) or SimpleNamespace(status="ok"),
    )

    assert autosync_daemon.run_daemon(config_path, max_loops=1) == 0
    assert calls == [(config_path.resolve(), "daemon")]
    heartbeat = json.loads(autosync.daemon_heartbeat_path(cfg).read_text(encoding="utf-8"))
    assert heartbeat["status"] == "stopped"


def test_daemon_singleton_exits_when_lock_is_owned(tmp_path):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    lock = autosync_daemon._acquire_lock(autosync.daemon_lock_path(cfg))
    assert lock is not None
    try:
        assert autosync_daemon.run_daemon(config_path, max_loops=1) == 0
        assert not autosync.daemon_heartbeat_path(cfg).exists()
    finally:
        autosync_daemon._release_lock(lock)


def test_doctor_checks_every_configured_source(tmp_path):
    config_path = _project(tmp_path)
    checks = autosync.doctor(config_path)
    by_name = {value.name: value for value in checks}
    assert by_name["config"].ok
    assert by_name["database"].ok
    assert by_name["connectors"].detail == "documents"
    assert by_name["source:documents"].ok


def test_a_sync_re_cuts_the_conversation_partition_and_its_index(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from varyz.db import dao
    from varyz.domain import Kind, NormalizedEntry, Sensitivity

    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())

    # Chat messages that arrived without anything having partitioned them: the
    # state an archive is in between a connector writing and anyone asking.
    start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    conn = open_db(cfg.db_path)
    with conn:
        for index, body in enumerate(
            ["is the plumber still coming on thursday", "yes, nine in the morning"]
        ):
            dao.upsert_entry(
                conn,
                NormalizedEntry(
                    kind=Kind.MESSAGE,
                    external_id=f"plumber-{index}",
                    body=body,
                    occurred_start=start + timedelta(minutes=index),
                    thread_id="plumber",
                    authored_by_user=bool(index),
                ),
                source="ai_conversations",
                sensitivity=Sensitivity.SENSITIVE,
            )
    assert conn.execute("SELECT count(*) FROM conversation_windows").fetchone()[0] == 0
    conn.close()

    autosync.run_once(config_path, trigger="test")

    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM conversation_windows").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM conversation_windows_fts").fetchone()[0] == 1
    conn.close()


def _chat(cfg, threads: int = 1) -> None:
    """Chat messages in the archive, partitioned by nothing yet."""
    from datetime import UTC, datetime, timedelta

    from varyz.db import dao
    from varyz.domain import Kind, NormalizedEntry, Sensitivity

    start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    conn = open_db(cfg.db_path)
    with conn:
        for thread in range(threads):
            for index, body in enumerate(
                [
                    "is the plumber still coming on thursday morning, or has he"
                    " pushed it to the afternoon again like he did last week",
                    "nine in the morning, he confirmed it -- he asked us to leave"
                    " the side gate unlocked so he can reach the meter",
                ]
            ):
                dao.upsert_entry(
                    conn,
                    NormalizedEntry(
                        kind=Kind.MESSAGE,
                        external_id=f"plumber-{thread}-{index}",
                        body=body,
                        occurred_start=start + timedelta(days=thread, minutes=index),
                        thread_id=f"plumber-{thread}",
                        authored_by_user=bool(index),
                    ),
                    source="ai_conversations",
                    sensitivity=Sensitivity.SENSITIVE,
                )
    conn.close()


class FakeAuthor:
    """A local model that answers instantly and counts what it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return (
            '{"label": "personal", "conclusions":'
            ' [{"text": "The plumber comes Thursday at nine.", "kind": "plan"}]}'
        )


def _read_with(monkeypatch, author) -> None:
    from varyz.pipeline import conclusions

    monkeypatch.setattr(conclusions, "reader", lambda cfg: (author, "fake"))


def test_a_sync_reads_the_conversations_nobody_has_read(tmp_path, monkeypatch):
    config_path = _project(tmp_path, background_inference=True)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    _chat(cfg)
    author = FakeAuthor()
    _read_with(monkeypatch, author)

    result = autosync.run_once(config_path, trigger="test")

    assert author.calls == 1
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT label FROM conversation_window_labels").fetchone()[0] == (
        "personal"
    )
    assert conn.execute(
        "SELECT count(*) FROM conversation_window_conclusions"
    ).fetchone()[0] == 1
    conn.close()
    assert "Conversations read: 1" in result.detail


def test_a_sync_reads_at_most_its_budget_and_leaves_the_rest(tmp_path, monkeypatch):
    config_path = _project(tmp_path, background_inference=True, max_per_sync=2)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    _chat(cfg, threads=5)
    author = FakeAuthor()
    _read_with(monkeypatch, author)

    autosync.run_once(config_path, trigger="test")

    # Five waiting, two read: the backlog is caught up over cycles rather than
    # in one that holds the card until it is done.
    assert author.calls == 2
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM conversation_window_labels").fetchone()[0] == 2
    conn.close()


def test_without_the_background_opt_in_a_sync_says_how_many_are_waiting(
    tmp_path, monkeypatch
):
    config_path = _project(tmp_path)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    _chat(cfg, threads=3)
    author = FakeAuthor()
    _read_with(monkeypatch, author)

    result = autosync.run_once(config_path, trigger="test")

    assert author.calls == 0
    assert "3 conversations unread" in result.detail


def test_a_sync_with_nothing_unread_never_reaches_for_the_model(tmp_path, monkeypatch):
    config_path = _project(tmp_path, background_inference=True)
    cfg = load_config(config_path)
    autosync.enable(cfg, config_path)
    monkeypatch.setattr(autosync, "Embedder", lambda _cfg: FakeEmbedder())
    _chat(cfg)
    author = FakeAuthor()
    _read_with(monkeypatch, author)
    autosync.run_once(config_path, trigger="test")
    assert author.calls == 1

    autosync.run_once(config_path, trigger="test")

    # The second sync had nothing to read, so it did not start a model to
    # find that out -- which is the difference between a daemon that idles
    # and one that wakes a 7 GB model every ten minutes.
    assert author.calls == 1


def test_doctor_output_keeps_bracketed_config_sections(tmp_path):
    """Rich reads `[update]` as a style tag and prints nothing for it.

    The update check's detail names the config section to change, and the
    doctor printed it as "off ( mode)" -- the one word the reader needed.
    """
    from typer.testing import CliRunner

    from varyz.cli import app

    config_path = _project(tmp_path)
    result = CliRunner().invoke(app, ["autosync", "doctor", "--config", str(config_path)])
    assert "[update] mode" in result.output, result.output

"""Health notices remain available when briefings or credentials fail."""

import contextlib
import json
from datetime import timedelta

from typer.testing import CliRunner

from varyz import doctor, hooks
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.trust import mint_agent_token, register_agent


def test_on_demand_standby_is_not_a_daemon_repair_failure(tmp_path, monkeypatch):
    from varyz.resolution import availability

    cfg = config(tmp_path)
    root = availability.directory(cfg)
    root.mkdir(parents=True)
    (root / "ON_DEMAND").touch()
    monkeypatch.setattr(hooks, "_port_open", lambda *a: False)
    ok, detail = hooks._daemon_check(cfg)
    assert ok and "on-demand standby" in detail and "unverified" in detail
    availability.set_paused(cfg, True)
    assert not hooks._daemon_check(cfg)[0]
    availability.set_paused(cfg, False)
    cfg.resolution.autostart = False
    assert not hooks._daemon_check(cfg)[0]


def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir="{tmp_path.as_posix()}/data"\n', encoding="utf-8")
    return load_config(path)


def test_credentials_checked_readonly_and_secrets_never_reported(tmp_path):
    cfg = config(tmp_path)
    with contextlib.closing(open_db(cfg.db_path)) as conn:
        credential = register_agent(conn, agent_id="agent:test", display_name="test")
        expired, _ = mint_agent_token(conn, "agent:test", valid_for=timedelta(days=-1))
    with contextlib.closing(hooks._open_readonly(cfg.db_path)) as conn:
        assert doctor.token_check(conn, credential.token)[0]
        ok, detail = doctor.token_check(conn, expired)
        assert not ok
        assert expired not in detail
        assert not doctor.token_check(conn, None)[0]
    with contextlib.closing(open_db(cfg.db_path)) as conn:
        conn.execute("UPDATE agent_tokens SET revoked_at='2026-01-01' WHERE id=?",
                     (credential.token_id,))
        conn.commit()
    with contextlib.closing(hooks._open_readonly(cfg.db_path)) as conn:
        assert not doctor.token_check(conn, credential.token)[0]


def test_notices_throttled_deduplicated_and_failure_returns_after_recovery(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    checks = [("daemon", False, "not running")]
    calls = []

    def diagnose(*args, **kwargs):
        calls.append(True)
        return checks

    monkeypatch.setattr(doctor, "diagnose", diagnose)
    assert "daemon" in doctor.session_notice(cfg, "chat", now=100)
    assert not doctor.session_notice(cfg, "chat", now=110)
    assert len(calls) == 1
    assert not doctor.session_notice(cfg, "chat", now=161)
    checks[:] = [("daemon", True, "running")]
    assert not doctor.session_notice(cfg, "chat", now=222)
    checks[:] = [("daemon", False, "not running")]
    assert doctor.session_notice(cfg, "chat", now=283)
    assert doctor.session_notice(cfg, "another chat", now=283)


def test_hook_failure_still_delivers_health_note_and_exits_zero(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    emitted = []
    monkeypatch.setattr(hooks, "_read_event", lambda: {"session_id": "chat"})
    monkeypatch.setattr(hooks, "_load_cfg", lambda _: cfg)
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    monkeypatch.setattr(doctor, "session_notice", lambda *a, **kw: "Varyz needs attention")

    def broken(*args):
        raise RuntimeError("secret-value")

    monkeypatch.setitem(hooks.HANDLERS, "user-prompt", broken)
    assert hooks.run("user-prompt", None) == 0
    assert "Varyz needs attention" in json.dumps(emitted)
    assert "secret-value" not in json.dumps(emitted)


def test_unchanged_hook_exception_is_not_reported_each_prompt(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    emitted = []
    monkeypatch.setattr(hooks, "_read_event", lambda: {"session_id": "chat"})
    monkeypatch.setattr(hooks, "_load_cfg", lambda _: cfg)
    monkeypatch.setattr(hooks, "_emit", emitted.append)
    monkeypatch.setattr(doctor, "session_notice", lambda *a, **kw: "")

    def broken(*args):
        raise RuntimeError("failed")

    monkeypatch.setitem(hooks.HANDLERS, "user-prompt", broken)
    hooks.run("user-prompt", None)
    hooks.run("user-prompt", None)
    assert len(emitted) == 1


def test_doctor_json_exit_status(tmp_path, monkeypatch):
    from varyz.cli import app

    config(tmp_path)
    monkeypatch.setattr(doctor, "diagnose", lambda *a, **kw: [("daemon", False, "down")])
    result = CliRunner().invoke(app, ["doctor", "--json", "--config",
                                      str(tmp_path / "config.toml")])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


def test_diagnose_catches_dead_reader_and_missing_session_token(tmp_path, monkeypatch):
    from varyz import autosync
    from varyz.resolution import headless_reader

    cfg = config(tmp_path)
    with contextlib.closing(open_db(cfg.db_path)):
        pass
    monkeypatch.setattr(hooks, "doctor", lambda *a, **kw: [])
    monkeypatch.setattr(headless_reader, "load_status",
                        lambda _: {"state": "running", "pid": 99999})
    monkeypatch.setattr(autosync, "process_alive", lambda _: False)
    checks = doctor.diagnose(cfg, client_configs={}, check_active=True, quick=True)
    failures = {name for name, ok, _ in checks if not ok}
    assert "current session credential" in failures
    assert "background reader" in failures
    monkeypatch.setattr(headless_reader, "load_status", lambda _: {"state": "waiting_for_usage"})
    assert dict((name, ok) for name, ok, _ in doctor.diagnose(
        cfg, client_configs={}, quick=True))["background reader"]


def test_mcp_invalid_token_returns_notice_without_disclosing_data(tmp_path, monkeypatch):
    from varyz.mcp_server import VaryzServer

    cfg = config(tmp_path)
    with contextlib.closing(open_db(cfg.db_path)):
        pass
    monkeypatch.setattr(doctor, "session_notice", lambda *a, **kw: "Varyz needs attention")
    server = VaryzServer(config_path=tmp_path / "config.toml", agent_token="invalid-secret")
    try:
        result = server.call("start_task", {"task": "check configuration"})
        assert result["error"] == "agent authentication failed"
        assert result["operational_notice"] == "Varyz needs attention"
        assert "invalid-secret" not in json.dumps(result)
        assert "how_owner_works" not in result
    finally:
        server.close()


def integrity_line(cfg, **kwargs):
    return dict((name, (ok, detail)) for name, ok, detail in doctor.diagnose(
        cfg, client_configs={}, **kwargs)).get("archive integrity")


def test_archive_scan_runs_in_background_and_doctor_shows_last_result(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    with contextlib.closing(open_db(cfg.db_path)):
        pass
    monkeypatch.setattr(hooks, "doctor", lambda *a, **kw: [])
    started = []
    monkeypatch.setattr(doctor, "_start_scan", lambda cfg: started.append(True))
    # Agents' quick checks never start the scan and say nothing before one ran.
    assert integrity_line(cfg, quick=True) is None and not started
    ok, detail = integrity_line(cfg)
    assert ok and "scanning in the background" in detail and len(started) == 1

    record = doctor.scan_integrity(cfg)
    assert record["ok"] and record["seconds"] >= 0
    assert integrity_line(cfg) == (True, "ok, scanned just now")
    assert integrity_line(cfg, quick=True) == (True, "ok, scanned just now")
    assert len(started) == 1

    later = record["checked_at"] + doctor.INTEGRITY_MAX_AGE + 3600
    ok, detail = doctor.integrity_check(cfg, start=True, now=later)
    assert ok and detail == "ok, scanned 25 h ago; scanning again in the background"
    assert len(started) == 2
    lock = doctor._integrity_lock(cfg)
    assert lock.acquire()
    try:
        assert doctor.scan_running(cfg)
        assert doctor.scan_integrity(cfg) is None
        doctor.integrity_check(cfg, start=True, now=later)
        assert len(started) == 2
    finally:
        lock.release()


def test_damaged_archive_fails_and_is_left_to_the_owner(tmp_path, monkeypatch):
    from varyz import doctor_repair

    cfg = config(tmp_path)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.db_path.write_bytes(b"not a database" * 512)
    assert doctor.scan_integrity(cfg)["ok"] is False
    ok, detail = doctor.integrity_check(cfg, start=False)
    assert not ok and "damage found just now" in detail and "back up" in detail
    assert doctor_repair.offer(cfg, ["archive integrity"], tmp_path / "config.toml") is None


def test_doctor_full_waits_for_a_fresh_scan(tmp_path, monkeypatch):
    from varyz.cli import app

    cfg = config(tmp_path)
    with contextlib.closing(open_db(cfg.db_path)):
        pass
    monkeypatch.setattr(hooks, "doctor", lambda *a, **kw: [])
    started = []
    monkeypatch.setattr(doctor, "_start_scan", lambda cfg: started.append(True))
    result = CliRunner().invoke(app, ["doctor", "--full", "--json", "--config",
                                      str(tmp_path / "config.toml")])
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["archive integrity"]["detail"] == "ok, scanned just now"
    assert not started
    result = CliRunner().invoke(app, ["doctor", "--full", "--config",
                                      str(tmp_path / "config.toml")])
    assert "Scanning the archive for damage" in result.stdout


def test_background_scan_entry_point_records_result(tmp_path):
    cfg = config(tmp_path)
    with contextlib.closing(open_db(cfg.db_path)):
        pass
    assert doctor.main(["scan", "--config", str(tmp_path / "config.toml")]) == 0
    assert doctor.last_scan(cfg)["ok"] is True

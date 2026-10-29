"""Repair is bounded, verified, leased and independent of MCP credentials."""

from types import SimpleNamespace

import pytest

from varyz import doctor, doctor_repair, hooks, install
from varyz.config import load_config


def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir="{tmp_path.as_posix()}/data"\n', encoding="utf-8")
    return load_config(path), path


def test_handoff_lease_shared_between_chats_and_expires(tmp_path):
    cfg, path = config(tmp_path)
    job = doctor_repair.offer(cfg, ["daemon"], path, now=100)
    assert "subagent_prompt" in job
    assert "--fix" in job["subagent_prompt"]
    assert doctor_repair.offer(cfg, ["codex credential"], path, now=101) is None
    assert doctor_repair.offer(cfg, ["daemon"], path, now=1301)
    assert doctor_repair.offer(cfg, [], path, now=2600) is None


def test_long_running_repair_renews_lease_and_cannot_claim_false_recovery(tmp_path, monkeypatch):
    cfg, path = config(tmp_path)
    job = doctor_repair.offer(cfg, ["daemon"], path, now=100)
    doctor_repair.update_job(cfg, job["job_id"], "running", now=1200)
    assert doctor_repair.offer(cfg, ["daemon"], path, now=1301) is None
    monkeypatch.setattr(doctor, "diagnose", lambda *a, **kw: [("daemon", False, "down")])
    with pytest.raises(ValueError, match="still reports failures"):
        doctor_repair.update_job(cfg, job["job_id"], "fixed", now=1400)
    with pytest.raises(ValueError, match="no longer current"):
        doctor_repair.update_job(cfg, "stale-job", "running", now=1400)
    monkeypatch.setattr(doctor, "diagnose", lambda *a, **kw: [("daemon", True, "up")])
    doctor_repair.update_job(cfg, job["job_id"], "fixed", now=1400)
    text = doctor_repair.render(job)
    assert doctor_repair.REPAIR_MODEL_INSTRUCTIONS in text
    assert "Leave the repair subagent running after your response" in text
    assert "Stop after one repair pass" not in text
    assert "--repair-state running" in text


def test_fix_only_runs_known_actions_and_verifies(tmp_path, monkeypatch):
    cfg, path = config(tmp_path)
    cfg.db_path.parent.mkdir(parents=True)
    cfg.db_path.touch()
    calls = []
    before = [("daemon", False, "down"), ("codex credential", False, "expired"),
              ("not paused", False, "paused"), ("archive integrity", False, "bad")]
    states = iter([before, [("archive integrity", False, "bad")]])
    monkeypatch.setattr(doctor, "diagnose", lambda *a, **kw: next(states))
    monkeypatch.setattr(hooks, "_port_open", lambda *a: False)

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 30
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(doctor_repair.subprocess, "run", run)
    result = doctor_repair.fix(cfg, path)
    assert len(calls) == 2
    assert calls[0][3:5] == ["daemon", "start"]
    assert calls[1][3:5] == ["connect", "codex"]
    assert not result["ok"]
    assert result["remaining"] == ["archive integrity"]
    assert result["restart_clients"]


def test_fix_never_takes_over_a_listening_daemon(tmp_path, monkeypatch):
    cfg, path = config(tmp_path)
    monkeypatch.setattr(doctor, "diagnose", lambda *a, **kw: [("daemon", False, "other archive")])
    monkeypatch.setattr(hooks, "_port_open", lambda *a: True)
    monkeypatch.setattr(doctor_repair.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not replace another daemon")))
    assert not doctor_repair.fix(cfg, path)["attempts"]


def test_managed_instructions_allow_repairs_preserve_owner_text_and_uninstall(tmp_path):
    for client in ("codex", "claude-code"):
        cfg = tmp_path / client / "config.toml"
        destination = install.instructions_destination(client, cfg)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("Owner instructions\n", encoding="utf-8")
        assert install._install_instructions(client, cfg, dry_run=False)[0]
        first = destination.read_text(encoding="utf-8")
        assert "repair_job" in first and "Owner instructions" in first
        repair = first.split("## Varyz doctor repair", 1)[1]
        assert doctor_repair.REPAIR_MODEL_INSTRUCTIONS in repair
        assert "gpt-6.1-sol" in repair and "Opus 5.5" in repair
        assert "cheapest" not in repair
        assert not install._install_instructions(client, cfg, dry_run=False)[0]
        install.uninstall_instructions(client, cfg)
        assert destination.read_text(encoding="utf-8") == "Owner instructions\n"


def test_reader_never_receives_repair_job(tmp_path, monkeypatch):
    cfg, path = config(tmp_path)
    monkeypatch.setenv("VARYZ_HEADLESS_READER", "reader-job")
    monkeypatch.setattr(doctor, "diagnose", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("reader must not recursively launch repairs")))
    assert not doctor.session_notice(cfg, "reader", config_path=path)


def test_unchanged_blocker_is_not_reoffered_until_recovery_or_code_change(tmp_path, monkeypatch):
    cfg, path = config(tmp_path)
    monkeypatch.setattr("varyz.autosync.code_fingerprint", lambda: "build-1")
    job = doctor_repair.offer(cfg, ["codex credential"], path, now=100)
    doctor_repair.update_job(cfg, job["job_id"], "blocked", now=101)
    assert doctor_repair.offer(cfg, ["codex credential"], path, now=10000) is None
    monkeypatch.setattr("varyz.autosync.code_fingerprint", lambda: "build-2")
    second = doctor_repair.offer(cfg, ["codex credential"], path, now=10000)
    assert second
    doctor_repair.update_job(cfg, second["job_id"], "blocked", now=10001)
    doctor_repair.observe_health(cfg, [], now=10002)
    assert doctor_repair.offer(cfg, ["codex credential"], path, now=10003)


def test_abandoned_repairs_back_off_across_chats_but_new_failure_can_run(tmp_path):
    cfg, path = config(tmp_path)
    assert doctor_repair.offer(cfg, ["daemon"], path, now=100)
    assert doctor_repair.offer(cfg, ["daemon"], path, now=1301)
    assert doctor_repair.offer(cfg, ["daemon"], path, now=2502) is None
    assert doctor_repair.offer(cfg, ["background reader"], path, now=2502)


def test_old_runtime_cannot_rewrite_canonical_connections_or_instructions(tmp_path):
    import json

    cfg, path = config(tmp_path)
    canonical = tmp_path / "installed-python.exe"
    canonical.touch()
    record = cfg.data_dir / "hooks" / "runtime.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"python": str(canonical)}), encoding="utf-8")
    client = tmp_path / "codex" / "config.toml"
    client.parent.mkdir()
    client.write_text("", encoding="utf-8")
    destination = install.instructions_destination("codex", client)
    destination.write_text("Owner rules\n", encoding="utf-8")
    assert not install.refresh_managed_instructions(path, paths={"codex": client})
    assert not install.refresh_connections(path, paths={"codex": client})
    assert install.refresh_connections_if_changed(path, paths={"codex": client}) is None
    assert destination.read_text(encoding="utf-8") == "Owner rules\n"
    install.pin_runtime(path)  # An explicit connect may intentionally choose another runtime.
    assert install.owns_runtime(path)


def test_doctor_uses_canonical_template_instead_of_another_runtime(tmp_path, monkeypatch):
    _cfg, path = config(tmp_path)
    expected = install.instructions_block("codex")
    install.pin_runtime(path)
    monkeypatch.setattr(install, "instructions_block", lambda _: "obsolete template")
    assert install.expected_instructions(path, "codex") == expected


def test_launch_refreshes_existing_instructions_without_database_or_tokens(tmp_path):
    cfg, config_path = config(tmp_path)
    client = tmp_path / "codex" / "config.toml"
    client.parent.mkdir()
    client.write_text("", encoding="utf-8")
    destination = install.instructions_destination("codex", client)
    destination.write_text(
        f"Owner text\n\n{install.INSTRUCTIONS_START}\nOld reading block\n"
        f"{install.INSTRUCTIONS_END}\n", encoding="utf-8",
    )
    assert not cfg.db_path.exists()
    results = install.refresh_managed_instructions(config_path, paths={"codex": client})
    assert results[0].changed
    refreshed = destination.read_text(encoding="utf-8")
    assert "Owner text" in refreshed
    assert "background_job" in refreshed and "repair_job" in refreshed
    assert not cfg.db_path.exists()
    assert not install.refresh_managed_instructions(
        config_path, paths={"codex": client})[0].changed


def test_repairs_are_bounded_and_name_the_owner_command(tmp_path):
    """The test Mac's repair agent had no limit and could not run its main step."""
    cfg, path = config(tmp_path)
    job = doctor_repair.offer(cfg, ["daemon"], path, now=0)
    assert f"{doctor_repair.REPAIR_MINUTES} minutes" in job["subagent_prompt"]
    assert "single-pass" not in job["subagent_prompt"]
    assert job["owner_command"].startswith("varyz doctor --fix --config ")
    assert job["owner_command"] in doctor_repair.render(job)
    offered = 1
    for now in range(1, 10 ** 6, 600):
        offered += bool(doctor_repair.offer(cfg, ["daemon"], path, now=now))
    assert offered == doctor_repair.MAX_ATTEMPTS


def test_failures_only_the_owner_can_fix_start_no_repair_agent(tmp_path):
    cfg, path = config(tmp_path)
    assert doctor_repair.offer(cfg, ["cloud sync"], path, now=0) is None
    assert doctor_repair.offer(cfg, ["agent settings file"], path, now=0) is None
    job = doctor_repair.offer(cfg, ["cloud sync", "daemon"], path, now=0)
    assert "cloud sync" not in job["subagent_prompt"]


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX file modes")
def test_archive_and_config_are_closed_to_other_accounts(tmp_path, monkeypatch):
    from varyz.filesystem import exposed, make_private

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    folder = tmp_path / ".varyz"
    folder.mkdir()
    path = folder / "config.toml"
    path.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    cfg = load_config(path)
    cfg.data_dir.mkdir()
    for item, mode in ((folder, 0o755), (cfg.data_dir, 0o755), (path, 0o644)):
        item.chmod(mode)
    assert set(exposed(cfg)) == {folder, cfg.data_dir, path}
    make_private(cfg)
    assert exposed(cfg) == []
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(cfg.data_dir.stat().st_mode & 0o777) == "0o700"

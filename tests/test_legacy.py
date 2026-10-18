"""Carrying an install made before the rename (varys -> varyz) across.

Everything here is about a machine that already ran the old version: its
archive, keys, variables and the registrations it left in other applications'
files. None of it may be lost, and none of another install's may be touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from varyz import autosync, install, legacy, secrets
from varyz.db.connection import FileLock, WriteLock
from varyz.install import (
    LEGACY_SERVER_KEY,
    SERVER_KEY,
    install_mcp,
    is_legacy_entry,
    merge_hooks,
    remove_hooks,
    server_entry,
    write_hook_scripts,
)
from varyz.setup_flow import unregister_mcp


def _old_archive(data_dir: Path, *, rows: int = 3) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "varys.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.executemany("INSERT INTO notes VALUES (?)", [(f"note {i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def _rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM notes").fetchone()[0]
    finally:
        conn.close()


# ---- archive -----------------------------------------------------------------


def test_the_old_archive_is_renamed_with_every_row(tmp_path):
    data_dir = tmp_path / "data"
    _old_archive(data_dir)

    path = legacy.archive_path(data_dir)

    assert path == data_dir / "varyz.db"
    assert _rows(path) == 3
    assert not (data_dir / "varys.db").exists()
    assert not (data_dir / "varys.lock").exists()
    assert legacy.lock_path(data_dir) == data_dir / "varyz.lock"


def test_a_new_archive_is_never_shadowed_by_an_old_one(tmp_path):
    data_dir = tmp_path / "data"
    _old_archive(data_dir)
    (data_dir / "varyz.db").write_bytes(b"")

    assert legacy.archive_path(data_dir) == data_dir / "varyz.db"
    # Both exist: the owner decides; nothing is overwritten or deleted.
    assert (data_dir / "varys.db").exists()


def test_a_fresh_install_gets_the_new_name(tmp_path):
    assert legacy.archive_path(tmp_path / "data") == tmp_path / "data" / "varyz.db"


def test_an_old_writer_holding_its_lock_keeps_the_archive_where_it_is(tmp_path):
    data_dir = tmp_path / "data"
    _old_archive(data_dir)
    old_writer = FileLock(data_dir / "varys.lock")
    assert old_writer.acquire()
    try:
        assert legacy.archive_path(data_dir) == data_dir / "varys.db"
        # Taking the old lock file keeps the two versions from writing at once.
        assert WriteLock(data_dir).path == data_dir / "varys.lock"
    finally:
        old_writer.release()
    assert (data_dir / "varys.db").exists()


def test_an_archive_another_connection_has_open_is_not_moved(tmp_path):
    data_dir = tmp_path / "data"
    path = _old_archive(data_dir)
    reader = sqlite3.connect(path)
    try:
        reader.execute("SELECT count(*) FROM notes").fetchone()
        assert legacy.archive_path(data_dir) == path
    finally:
        reader.close()
    assert _rows(path) == 3


def test_config_points_at_the_renamed_archive(tmp_path):
    from varyz.config import load_config

    config_path = tmp_path / "config.toml"
    config_path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    _old_archive(tmp_path / "data")

    db_path = load_config(config_path).db_path
    assert db_path.name == "varyz.db" and _rows(db_path) == 3


def test_a_pre_rename_backup_folder_is_recognised(tmp_path):
    (tmp_path / "varys.db").write_bytes(b"x")
    assert legacy.legacy_backup_database(tmp_path) == tmp_path / "varys.db"
    (tmp_path / "varyz.db").write_bytes(b"x")
    assert legacy.legacy_backup_database(tmp_path) is None


# ---- environment, home, keys -------------------------------------------------


def test_old_variables_are_read_under_the_new_names_without_overriding_them():
    env = {"VARYS_CONFIG": "old.toml", "VARYS_AGENT_TOKEN": "t", "VARYZ_CONFIG": "new.toml"}

    adopted = legacy.adopt_legacy_environment(env)

    assert adopted == ["VARYZ_AGENT_TOKEN"]
    assert env["VARYZ_CONFIG"] == "new.toml"
    assert env["VARYZ_AGENT_TOKEN"] == "t"


def test_oauth_bundles_move_from_the_old_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old = tmp_path / ".varys" / "oauth"
    old.mkdir(parents=True)
    (old / "claude.json").write_text("{}", encoding="utf-8")

    moved = legacy.home_path("oauth")

    assert moved == tmp_path / ".varyz" / "oauth"
    assert (moved / "claude.json").read_text(encoding="utf-8") == "{}"
    assert not (tmp_path / ".varys").exists()


class _Keyring:
    def __init__(self, *, refuse_writes: bool = False):
        self.store: dict[tuple[str, str], str] = {}
        self.refuse_writes = refuse_writes

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        if self.refuse_writes:
            raise secrets.keyring.errors.PasswordSetError("locked")
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise secrets.keyring.errors.PasswordDeleteError("missing")
        del self.store[(service, name)]


@pytest.fixture
def fake_store(monkeypatch):
    store = _Keyring()
    for method in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(secrets.keyring, method, getattr(store, method))
    return store


def test_a_key_stored_under_the_old_service_moves_on_first_read(fake_store):
    fake_store.store[("varys", "anthropic_api_key")] = "sk-old"

    assert secrets.get_secret("anthropic_api_key") == "sk-old"
    assert fake_store.store == {("varyz", "anthropic_api_key"): "sk-old"}
    assert secrets.secret_source("anthropic_api_key") == "store"


def test_a_store_that_refuses_the_copy_still_answers_with_the_old_key(fake_store):
    fake_store.store[("varys", "openai_api_key")] = "sk-old"
    fake_store.refuse_writes = True

    assert secrets.get_secret("openai_api_key") == "sk-old"
    assert ("varys", "openai_api_key") in fake_store.store


def test_deleting_a_key_removes_it_under_both_names(fake_store):
    fake_store.store[("varys", "deepseek_api_key")] = "a"
    fake_store.store[("varyz", "deepseek_api_key")] = "b"

    secrets.delete_secret("deepseek_api_key")

    assert fake_store.store == {}


# ---- MCP clients -------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    return path


def _old_entry(config: Path, *, token: str | None = None) -> dict:
    entry = {"command": "C:/venv/Scripts/varys.exe", "args": ["mcp", "--config", str(config)]}
    if token:
        entry["env"] = {"VARYS_AGENT_TOKEN": token}
    return entry


def test_only_the_old_program_for_this_archive_is_a_legacy_entry(tmp_path, config_file):
    assert is_legacy_entry(_old_entry(config_file), config_file)
    assert is_legacy_entry({"command": "python", "args": ["-m", "varys", "mcp"]}, config_file)
    # Another archive's entry may belong to an install still on the old version.
    assert not is_legacy_entry(_old_entry(tmp_path / "other" / "config.toml"), config_file)
    # A server someone else happened to call "varys" is not ours.
    assert not is_legacy_entry({"command": "npx", "args": ["varys-mcp"]}, config_file)
    assert not is_legacy_entry(None, config_file)


def test_install_replaces_the_pre_rename_entry(tmp_path, config_file):
    client = tmp_path / ".claude.json"
    client.write_text(
        json.dumps({"mcpServers": {LEGACY_SERVER_KEY: _old_entry(config_file), "x": {}}}),
        encoding="utf-8",
    )

    (dry,) = install_mcp(paths={"claude-code": client}, config_path=config_file, dry_run=True)
    assert "pre-rename" in dry.detail and LEGACY_SERVER_KEY in json.loads(
        client.read_text(encoding="utf-8")
    )["mcpServers"]

    (result,) = install_mcp(paths={"claude-code": client}, config_path=config_file)

    servers = json.loads(client.read_text(encoding="utf-8"))["mcpServers"]
    assert result.changed and "pre-rename" in result.detail
    assert set(servers) == {SERVER_KEY, "x"}
    assert servers[SERVER_KEY] == server_entry(config_file)


def test_another_archives_old_entry_survives_install(tmp_path, config_file):
    other = _old_entry(tmp_path / "other" / "config.toml")
    client = tmp_path / ".claude.json"
    client.write_text(json.dumps({"mcpServers": {LEGACY_SERVER_KEY: other}}), encoding="utf-8")

    install_mcp(paths={"claude-code": client}, config_path=config_file)

    assert json.loads(client.read_text(encoding="utf-8"))["mcpServers"][LEGACY_SERVER_KEY] == other


def test_reconnecting_reuses_the_old_entrys_agent_token(tmp_path, config_file):
    client = tmp_path / ".claude.json"
    client.write_text(
        json.dumps({"mcpServers": {LEGACY_SERVER_KEY: _old_entry(config_file, token="tok")}}),
        encoding="utf-8",
    )
    assert install._existing_agent_token("claude-code", client) == "tok"


def test_uninstall_removes_the_pre_rename_entry_too(tmp_path, config_file):
    client = tmp_path / ".claude.json"
    client.write_text(
        json.dumps({"mcpServers": {LEGACY_SERVER_KEY: _old_entry(config_file), "x": {}}}),
        encoding="utf-8",
    )

    results = unregister_mcp(paths={"claude-code": client}, archive=config_file)

    assert results[0].changed
    assert json.loads(client.read_text(encoding="utf-8"))["mcpServers"] == {"x": {}}


def test_codex_install_removes_the_old_server_and_skill(tmp_path, config_file):
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    old = _old_entry(config_file)
    codex_config.write_text(
        f"[mcp_servers.{LEGACY_SERVER_KEY}]\n"
        f"command = {json.dumps(old['command'])}\n"
        f"args = {json.dumps(old['args'])}\n",
        encoding="utf-8",
    )
    old_skill = home / ".agents" / "skills" / "varys-context" / "SKILL.md"
    old_skill.parent.mkdir(parents=True)
    old_skill.write_text("old routing", encoding="utf-8")
    calls = []

    (result,) = install_mcp(
        paths={"codex": codex_config}, config_path=config_file, codex_runner=calls.append
    )

    assert result.changed and "pre-rename" in result.detail
    assert calls[0] == ["mcp", "remove", LEGACY_SERVER_KEY]
    assert calls[1][:3] == ["mcp", "add", SERVER_KEY]
    assert not old_skill.parent.exists()
    backup = home / ".agents" / "skills.varyz-backup" / "varys-context" / "SKILL.md"
    assert backup.read_text(encoding="utf-8") == "old routing"
    # A copy left inside skills/ would be loaded by the client as a second skill.
    assert sorted(p.name for p in (home / ".agents" / "skills").iterdir()) == [
        "away", "varyz-context",
    ]
    assert (home / ".agents" / "skills" / "varyz-context" / "SKILL.md").exists()


# ---- hooks -------------------------------------------------------------------


def test_old_hook_groups_are_replaced_not_doubled():
    old = "if [ -f 'C:/a/data/hooks/varys-hook-prompt.cmd' ]; then ...; fi"
    mine = {"hooks": [{"type": "command", "command": "other-tool"}]}
    settings = {"hooks": {"UserPromptSubmit": [mine, {"hooks": [{"command": old}]}]}}

    assert merge_hooks(settings, {"UserPromptSubmit": "C:/a/data/hooks/varyz-hook-prompt.cmd"})

    groups = settings["hooks"]["UserPromptSubmit"]
    assert groups[0] == mine and len(groups) == 2
    assert "varyz-hook-" in groups[1]["hooks"][0]["command"]


def test_uninstall_removes_old_hook_groups():
    old = {"hooks": [{"command": "C:/a/data/hooks/varys-hook-stop.cmd"}]}
    settings = {"hooks": {"Stop": [old]}}
    assert remove_hooks(settings, "C:/a/data/hooks")
    assert not settings["hooks"].get("Stop")


def test_hook_launchers_written_before_the_rename_are_deleted(tmp_path, config_file):
    hooks = tmp_path / "data" / "hooks"
    hooks.mkdir(parents=True)
    suffix = ".cmd" if os.name == "nt" else ".sh"
    stale = [hooks / f"varys-hook-{event}{suffix}" for event in install.HOOK_EVENTS.values()]
    for path in stale:
        path.write_text("python -m varys.hooks", encoding="utf-8")

    scripts = write_hook_scripts(config_file)

    assert not any(path.exists() for path in stale)
    assert all(path.name.startswith("varyz-hook-") and path.exists() for path in scripts.values())


# ---- autosync login registration ---------------------------------------------


def test_a_pre_rename_startup_entry_is_replaced(tmp_path, monkeypatch, config_file):
    monkeypatch.setattr(autosync.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(
        autosync.shutil,
        "which",
        lambda name: "C:/Windows/System32/wscript.exe" if name == "wscript" else None,
    )
    monkeypatch.setattr(autosync.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=1))
    startup = autosync.startup_dir()
    startup.mkdir(parents=True)
    suffix = autosync.daemon_name(config_file).rsplit("-", 1)[-1]
    old = startup / f"VarysAutoSync-{suffix}.vbs"
    old.write_text("' Managed by Varys autosync\n", encoding="utf-8")
    foreign = startup / "VarysAutoSync-00000000.vbs"
    foreign.write_text("' Managed by Varys autosync\n", encoding="utf-8")
    owners = startup / f"VarysAutoSync-{suffix}.vbs.txt"
    owners.write_text("not ours", encoding="utf-8")

    assert autosync.legacy_registrations(config_file) == [old]
    login = autosync._login_startup_check(config_file)
    assert not login.ok and "pre-rename" in login.detail

    preview = autosync.install_daemon(config_file, every_minutes=30, dry_run=True)
    assert "remove pre-rename" in preview and old.exists()

    installed = autosync.install_daemon(config_file, every_minutes=30)

    assert "removed pre-rename" in installed
    assert not old.exists()
    # Another archive's registration is its own install's to replace.
    assert foreign.exists() and owners.exists()


def test_a_file_the_old_version_did_not_write_is_left_alone(tmp_path, monkeypatch, config_file):
    monkeypatch.setattr(autosync.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    startup = autosync.startup_dir()
    startup.mkdir(parents=True)
    suffix = autosync.daemon_name(config_file).rsplit("-", 1)[-1]
    (startup / f"VarysAutoSync-{suffix}.vbs").write_text("owner's own script", encoding="utf-8")

    assert autosync.legacy_registrations(config_file) == []

"""Archive connections hold a release lease; failed migration releases it."""

import json
from pathlib import Path

import pytest

from varyz.db import connection


def test_uv_archive_connection_blocks_release_and_releases_on_close(tmp_path, monkeypatch):
    environment = tmp_path / "tools/varyzai"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(connection.sys, "prefix", str(environment))
    lease = connection.FileLock(environment.parent / ".varyz-release.lock")
    conn = connection.connect(tmp_path / "archive.db")
    try:
        assert not lease.acquire()
    finally:
        conn.close()
    assert lease.acquire()
    try:
        with pytest.raises(connection.LockHeldError, match="being upgraded"):
            connection.connect(tmp_path / "archive.db")
    finally:
        lease.release()


def test_failed_migration_does_not_leave_release_locked(tmp_path, monkeypatch):
    environment = tmp_path / "tools/varyzai"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(connection.sys, "prefix", str(environment))

    def fail(_):
        raise ValueError("failed migration")

    monkeypatch.setattr(connection, "migrate", fail)
    with pytest.raises(ValueError):
        connection.open_db(tmp_path / "archive.db")
    lease = connection.FileLock(Path(connection.sys.prefix).parent / ".varyz-release.lock")
    assert lease.acquire()
    lease.release()


def test_interrupted_upgrade_does_not_create_an_empty_archive(tmp_path, monkeypatch):
    environment = tmp_path / "tools/varyzai"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(connection.sys, "prefix", str(environment))
    archive = tmp_path / "data/varyz.db"
    folder = archive.parent / "autosync/releases/test-upgrade"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"phase": "committing", "archive": str(archive)}))
    with pytest.raises(connection.LockHeldError, match="interrupted"):
        connection.open_db(archive)
    assert not archive.exists()

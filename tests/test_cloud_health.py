"""Doctor and `varyz cloud status` say plainly when Cloud sync is stuck."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from varyz.cloud import background
from varyz.cloud.client import CloudError


def _connected(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    config.with_name("config.cloud-sync.json").write_text(
        json.dumps({"url": "https://cloud.example", "account_id": "owner", "records": {},
                    "enabled": False}),
        encoding="utf-8",
    )
    config.with_name("config.cloud-archive.json").write_text(
        json.dumps({"enabled": True, "selected": {}, "records": {}}), encoding="utf-8"
    )
    return config


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _archive(result=None, error=None):
    class Archive:
        def __init__(self, conn, path):
            pass

        def sync(self, client, account, embedder=None):
            if error is not None:
                raise error
            return result

    return Archive


def test_no_cloud_no_check(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    assert background.health(config) is None


def test_full_storage_is_reported_every_cycle_and_in_doctor(tmp_path, monkeypatch):
    """On the test Mac, 21 runs said only "needs attention"; doctor said all was well."""
    config = _connected(tmp_path)
    monkeypatch.setattr(background, "CloudClient", _Client)
    monkeypatch.setattr(background, "load_tokens", lambda _name: {})
    monkeypatch.setattr(
        background, "ArchiveSync",
        _archive({"uploaded": 0, "deleted": 0, "blocked": 29, "storage_full": True}),
    )
    note = background.sync_if_selected(config, None)
    assert note == background.STORAGE_FULL
    ok, detail = background.health(config)
    assert not ok and "storage is full" in detail
    assert background.read_health(config)["withheld"] == 29


def test_a_safe_cloud_error_is_named_and_offline_is_not_a_failure(tmp_path, monkeypatch):
    config = _connected(tmp_path)
    monkeypatch.setattr(background, "CloudClient", _Client)
    monkeypatch.setattr(background, "load_tokens", lambda _name: {})
    monkeypatch.setattr(
        background, "ArchiveSync", _archive(error=CloudError("Cloud rejected the request."))
    )
    note = background.sync_if_selected(config, None)
    assert note == "Cloud sync did not finish: Cloud rejected the request."
    assert background.health(config) == (False, note)

    offline = CloudError("Cloud could not be reached; local preferences remain available.")
    offline.__cause__ = httpx.ConnectError("nodename nor servname provided")
    monkeypatch.setattr(background, "ArchiveSync", _archive(error=offline))
    assert background.sync_if_selected(config, None) is None
    ok, detail = background.health(config)
    assert ok and "offline" in detail


def test_doctor_has_a_cloud_line(tmp_path, monkeypatch):
    from varyz import doctor
    from varyz.config import load_config
    from varyz.db.connection import open_db

    config = _connected(tmp_path)
    background._record(config, False, background.STORAGE_FULL)
    cfg = load_config(config)
    open_db(cfg.db_path).close()
    checks = doctor.diagnose(cfg, client_configs={}, quick=True)
    assert ("cloud sync", False, background.STORAGE_FULL) in checks

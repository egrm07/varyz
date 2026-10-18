"""The completed setup opens a real loopback bridge before navigating to Cloud."""

import socket
import subprocess
import threading
from contextlib import contextmanager

import httpx
import pytest

from tests.test_setup_web import _request, machine, server  # noqa: F401
from varyz.cloud import bridge
from varyz.cloud.computer import BRIDGE_PATH, CLOUD_ORIGIN
from varyz.cloud.guided import GuidedSetup
from varyz.cloud.local_sync import save_journal
from varyz.config import load_config
from varyz.db.connection import open_db


@contextmanager
def running(config):
    with bridge.BridgeServer(config, port=0) as local:
        thread = threading.Thread(target=local.serve_forever, daemon=True)
        thread.start()
        try:
            yield local.server_address[1]
        finally:
            local.shutdown()


def test_identity_probe_does_not_sign_in_select_or_create_a_ticket(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    with running(config) as port:
        assert bridge.ready(config, port)
        assert bridge.ensure_ready(config, port=port) == bridge.CLOUD_SETUP_URL
        with httpx.Client(trust_env=False) as client:
            url = f"http://127.0.0.1:{port}{BRIDGE_PATH}/identity"
            assert client.get(url).status_code == 403
            assert client.get(url, headers={"Origin": CLOUD_ORIGIN}).status_code == 200
    assert list(tmp_path.iterdir()) == [config]


def test_ready_refuses_a_different_archive(tmp_path):
    first, second = tmp_path / "first.toml", tmp_path / "second.toml"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    with running(first) as port, pytest.raises(ValueError, match="different local archive"):
        bridge.ensure_ready(second, port=port)


def test_legacy_resolution_port_starts_a_detached_browser_bridge(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir="data"\n[resolution]\nport=8765\n', encoding="utf-8")
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
    processes = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    try:
        assert bridge.ensure_ready(config, port=port) == bridge.CLOUD_SETUP_URL
        assert len(processes) == 1
        assert bridge.ready(config, port)
        assert load_config(config).resolution.port == 8765
        assert not config.with_name("config.cloud-sync.json").exists()
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)


def test_finished_setup_checks_bridge_before_redirecting(server, monkeypatch):  # noqa: F811
    session = server.session
    session.started = session.finished = True
    checked = []
    monkeypatch.setattr(bridge, "ensure_ready",
                        lambda config: checked.append(config) or bridge.CLOUD_SETUP_URL)
    assert _request(server, "GET", "/cloud")[0] == 403
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        response = client.get(server.url.replace("/?t=", "/cloud?t="))
    assert response.status_code == 303
    assert response.headers["Location"] == bridge.CLOUD_SETUP_URL
    assert checked == [session.config_path]


def test_failed_cloud_handoff_stays_visible_and_can_retry(server, monkeypatch):  # noqa: F811
    session = server.session
    session.started = session.finished = True

    def unavailable(config):
        raise ValueError("Local Varyz could not open its Cloud connection. Please try again.")

    monkeypatch.setattr(bridge, "ensure_ready", unavailable)
    status, _, body = _request(server, "GET", "/cloud?t=" + session.token)
    assert status == 409
    assert b"Cloud could not open" in body and b"Try again" in body
    assert b'/cloud?t=' in body and not server.closed.is_set()


def test_old_local_archive_can_list_and_preview_without_report_tables(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    conn = open_db(load_config(config).db_path)
    conn.close()
    wizard = GuidedSetup(config, "fixture-account")
    save_journal(wizard.path, {"url": CLOUD_ORIGIN, "account_id": "fixture-account",
                              "enabled": False, "projects": [], "records": {}, "cursor": 0})
    choices = wizard.choices()
    assert choices["report_projects"] == []
    preview = wizard.preview([], history_projects=[], report_projects=[])
    assert preview["uploads"] == preview["history_uploads"] == preview["report_uploads"] == 0
    assert not wizard._journal()["enabled"]


def test_stale_native_daemon_is_restarted_before_cloud_opens(tmp_path, monkeypatch):
    from varyz import cli
    from varyz.resolution import client

    config = tmp_path / "config.toml"
    config.write_text('[resolution]\nport=1207\n', encoding="utf-8")
    checks = iter([False, False, True])
    restarted = []
    monkeypatch.setattr(bridge, "ready", lambda *a: next(checks))
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: object())
    monkeypatch.setattr(client, "ensure_daemon", lambda *a: object())
    monkeypatch.setattr(cli, "_restart_daemon", lambda cfg: restarted.append(cfg) or True)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: pytest.fail("already running"))
    assert bridge.ensure_ready(config) == bridge.CLOUD_SETUP_URL
    assert restarted == [config]

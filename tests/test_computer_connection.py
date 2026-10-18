"""Browser-to-computer sign-in boundaries, without real credentials or uploads."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from varyz.cloud import cli
from varyz.cloud.computer import BRIDGE_PATH, CLOUD_ORIGIN, ComputerConnection
from varyz.cloud.local_sync import read_journal, save_journal
from varyz.resolution.daemon import ResolutionDaemon

# A loaded Windows CI runner can take seconds just to start Node; a real hang still fails.
NODE_TIMEOUT = 60


@pytest.fixture
def bridge(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return ComputerConnection(config)


@pytest.fixture
def server(bridge):
    service = SimpleNamespace(
        lock=threading.RLock(), escalations=SimpleNamespace(pending=lambda: []),
        shadow_config=None,
    )
    with ResolutionDaemon(service, port=0, auth_mode="token") as daemon:
        daemon.computer_connection = bridge
        with httpx.Client(base_url="http://127.0.0.1:" + str(daemon.address[1])) as client:
            yield client, daemon


def test_detect_is_private_and_replay_is_refused(server, bridge, monkeypatch):
    client, _ = server
    finished = threading.Event()
    monkeypatch.setattr(cli, "connect_account", lambda *a, **kw: finished.wait(2))
    headers = {"Origin": CLOUD_ORIGIN, "X-Varyz-Account": "owner"}
    detected = client.get(BRIDGE_PATH, headers=headers)
    assert detected.headers["access-control-allow-origin"] == CLOUD_ORIGIN
    assert str(bridge.config) not in detected.text
    ticket = detected.json()["ticket"]
    headers["X-Varyz-Ticket"] = ticket
    started = client.post(BRIDGE_PATH, headers=headers)
    assert started.status_code == 202
    assert client.post(BRIDGE_PATH, headers=headers).status_code == 400
    assert client.get(BRIDGE_PATH, headers={"Origin": CLOUD_ORIGIN}).json() == {
        "installed": True, "busy": True,
    }
    finished.set()
    for _ in range(100):
        status = client.get(BRIDGE_PATH, headers=headers).json()["state"]
        if status == "connected":
            break
        time.sleep(.01)
    assert status == "connected"
    assert client.get("/v1/identity", headers=headers).status_code == 401


@pytest.mark.parametrize("origin", [None, "null", "https://evil.example", CLOUD_ORIGIN + ".evil"])
def test_other_origins_cannot_detect_or_start(server, origin):
    client, _ = server
    headers = {"Origin": origin} if origin else {}
    for route in ("", "/choices", "/preview", "/finish"):
        for method in ("GET", "POST", "OPTIONS"):
            response = client.request(method, BRIDGE_PATH + route, headers=headers)
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers


def test_rebinding_preflight_and_no_cors_on_daemon_api(server):
    client, _ = server
    headers = {"Origin": CLOUD_ORIGIN}
    preflight = client.options(BRIDGE_PATH, headers=headers)
    assert preflight.headers["access-control-allow-private-network"] == "true"
    assert "X-Varyz-Ticket" in preflight.headers["access-control-allow-headers"]
    assert client.get(BRIDGE_PATH, headers={**headers, "Host": "evil.example"}).status_code == 403
    assert "access-control-allow-origin" not in client.get("/v1/identity", headers=headers).headers


def test_missing_expired_and_body_requests_cannot_start(server, bridge):
    client, daemon = server
    headers = {"Origin": CLOUD_ORIGIN}
    ticket = client.get(BRIDGE_PATH, headers=headers).json()["ticket"]
    assert client.post(BRIDGE_PATH, headers=headers).status_code == 400
    headers["X-Varyz-Ticket"] = ticket
    body = client.post(BRIDGE_PATH, headers=headers, json={"url": "https://evil.example"})
    assert body.status_code == 400
    bridge.expires = 0
    assert client.post(BRIDGE_PATH, headers=headers).status_code == 400
    daemon.computer_connection = None
    assert client.get(BRIDGE_PATH, headers={"Origin": CLOUD_ORIGIN}).status_code == 503


def test_failure_does_not_disclose_provider_secrets(bridge, monkeypatch):
    def failure(*args, **kwargs):
        raise ValueError("secret-access-token")
    monkeypatch.setattr(cli, "connect_account", failure)
    bridge.ticket, bridge.expires = "ticket", time.monotonic() + 60
    bridge._connect("owner")
    assert bridge.status("ticket") == {"state": "failed", "reason": "failed"}


def test_failures_reach_the_page_only_as_fixed_reasons(bridge, monkeypatch):
    from varyz.cloud.client import CloudError
    from varyz.db.connection import LockHeldError

    cases = [
        (cli.OtherAccount("secret"), "other_account"),
        (cli.WrongSignIn("secret"), "wrong_sign_in"),
        (cli.InactiveAccount("secret"), "inactive"),
        (LockHeldError("secret"), "busy"),
        (CloudError("Authorization declined."), "declined"),
        (CloudError("Authorization timed out; no account was connected."), "timeout"),
        (CloudError("secret"), "failed"),
    ]
    seen = []
    for error, reason in cases:
        def failure(*args, error=error, **kwargs):
            seen.append(kwargs["switch_account"])
            raise error
        monkeypatch.setattr(cli, "connect_account", failure)
        bridge.ticket, bridge.expires = "ticket", time.monotonic() + 60
        bridge._connect("owner", reason == "other_account")
        assert bridge.status("ticket") == {"state": "failed", "reason": reason}
    assert seen == [True] + [False] * 6
    bridge.detect()
    assert bridge.reason == ""


def test_switch_header_is_allowed_and_reaches_the_connection(server, bridge, monkeypatch):
    client, _ = server
    calls = []
    monkeypatch.setattr(cli, "connect_account", lambda *a, **kw: calls.append(kw))
    headers = {"Origin": CLOUD_ORIGIN}
    assert "X-Varyz-Switch" in client.options(BRIDGE_PATH, headers=headers).headers[
        "access-control-allow-headers"
    ]
    headers["X-Varyz-Ticket"] = client.get(BRIDGE_PATH, headers=headers).json()["ticket"]
    headers.update({"X-Varyz-Account": "owner", "X-Varyz-Switch": "1"})
    assert client.post(BRIDGE_PATH, headers=headers).status_code == 202
    for _ in range(100):
        if calls:
            break
        time.sleep(.01)
    assert calls[0]["switch_account"] is True and calls[0]["expected_account"] == "owner"


def test_guided_http_requires_review_and_single_explicit_approval(server, bridge, monkeypatch):
    from varyz.cloud import guided

    calls = []
    finish = threading.Event()
    class Setup:
        def __init__(self, *args):
            pass
        def choices(self):
            calls.append("choices")
            return {"projects": [{"name": "alpha", "count": 1}], "general": 1}
        def preview(self, scopes, history_projects, report_projects):
            assert scopes == ["alpha"]
            assert history_projects == []
            assert report_projects == []
            calls.append("preview")
            return {"uploads": 2, "deletions": 0, "projects": scopes, "signature": "private"}
        def finish(self, preview):
            calls.append("finish")
            finish.wait(2)
            return {"state": "complete", "uploaded": 2, "downloaded": 2}
    scopes = guided.GuidedSetup.scopes
    Setup.scopes = staticmethod(scopes)
    monkeypatch.setattr(guided, "GuidedSetup", Setup)
    client, _ = server
    headers = {"Origin": CLOUD_ORIGIN}
    ticket = client.get(BRIDGE_PATH, headers=headers).json()["ticket"]
    headers["X-Varyz-Ticket"] = ticket
    assert client.get(BRIDGE_PATH + "/choices", headers=headers).status_code == 400
    bridge.state, bridge.account_id = "connected", "owner"
    assert client.get(BRIDGE_PATH + "/choices", headers=headers).status_code == 200
    assert calls == ["choices"]
    preview = client.post(BRIDGE_PATH + "/preview", headers=headers,
                          json={"projects": ["alpha"], "all_projects": False})
    assert preview.status_code == 200 and "signature" not in preview.json()
    approval = preview.json()["approval"]
    for body in ({"approval": approval}, {"approve": True, "approval": "wrong"}):
        assert client.post(BRIDGE_PATH + "/finish", headers=headers, json=body).status_code == 400
    assert calls == ["choices", "preview"]
    body = {"approval": approval, "approve": True}
    assert client.post(BRIDGE_PATH + "/finish", headers=headers, json=body).status_code == 202
    assert client.post(BRIDGE_PATH + "/finish", headers=headers, json=body).status_code == 202
    finish.set()
    for _ in range(100):
        result = client.get(BRIDGE_PATH, headers=headers).json()
        if result["state"] == "complete":
            break
        time.sleep(.01)
    assert result["state"] == "complete" and calls == ["choices", "preview", "finish"]


@pytest.fixture
def account_connection(bridge, monkeypatch):
    account = {"id": "owner", "entitlement": True}
    writes = []
    monkeypatch.setattr(cli, "browser_connect", lambda url: {"access_token": "secret"})
    def missing_tokens(_name):
        raise ValueError('Cloud is disconnected; connect it before syncing.')
    monkeypatch.setattr(cli, 'load_tokens', missing_tokens)
    monkeypatch.setattr(cli.keyring, "set_password", lambda *args: writes.append(args))
    class Client:
        def __init__(self, _url, tokens, **kwargs):
            self.tokens = tokens
        def request(self, method, path):
            assert (method, path) == ("GET", "/api/account")
            return account
        def close(self):
            pass
    monkeypatch.setattr(cli, "CloudClient", Client)
    return account, writes


def test_connection_never_selects_or_uploads(bridge, account_connection):
    _, writes = account_connection
    cli.connect_account(CLOUD_ORIGIN, bridge.config, require_active=True)
    journal = read_journal(bridge.config.with_suffix(".cloud-sync.json"))
    assert journal["enabled"] is False and journal["projects"] == []
    assert journal["records"] == {} and journal["account_id"] == "owner"
    assert "secret" not in json.dumps(journal)
    assert len(writes) == 1


def test_inactive_or_different_accounts_leave_credentials_and_selection_intact(
    bridge, account_connection,
):
    account, writes = account_connection
    path = bridge.config.with_suffix(".cloud-sync.json")
    original = {"url": CLOUD_ORIGIN, "account_id": "other", "enabled": True,
                "projects": ["chosen"], "records": {}, "cursor": 9}
    save_journal(path, original)
    with pytest.raises(cli.OtherAccount, match="different Cloud account"):
        cli.connect_account(CLOUD_ORIGIN, bridge.config, require_active=True)
    account["id"], account["entitlement"] = "other", False
    with pytest.raises(ValueError, match="inactive"):
        cli.connect_account(CLOUD_ORIGIN, bridge.config, require_active=True)
    assert read_journal(path) == original and writes == []


def test_switching_moves_old_journals_aside_and_starts_fresh(
    bridge, account_connection, monkeypatch,
):
    _, writes = account_connection
    path = bridge.config.with_suffix(".cloud-sync.json")
    archive = bridge.config.with_suffix(".cloud-archive.json")
    old = {"url": CLOUD_ORIGIN, "account_id": "other", "enabled": True,
           "projects": ["chosen"], "records": {"r": {}}, "cursor": 9}
    save_journal(path, old)
    archive.write_text(json.dumps({"account_id": "other"}), encoding="utf-8")
    revoked = []
    monkeypatch.setattr(cli, "load_tokens", lambda name: {
        "access_token": "old-access", "refresh_token": "old-refresh",
    })
    class Client:
        def __init__(self, _url, tokens, **kwargs):
            self.tokens = tokens
        def request(self, method, path, data=None):
            if path == "/oauth/revoke":
                revoked.append(data["token"])
                return {}
            return {"id": "owner", "entitlement": True}
        def close(self):
            pass
    monkeypatch.setattr(cli, "CloudClient", Client)
    cli.connect_account(CLOUD_ORIGIN, bridge.config, require_active=True, switch_account=True)
    journal = read_journal(path)
    assert journal["account_id"] == "owner" and journal["enabled"] is False
    assert journal["records"] == {} and journal["projects"] == []
    assert not archive.exists() and revoked == ["old-access", "old-refresh"]
    moved = sorted(p.name for p in (bridge.config.parent / "data" / "cloud-switch").iterdir())
    assert [name.split("-", 1)[1] for name in moved] == [
        "config.cloud-archive.json", "config.cloud-sync.json",
    ]
    assert len(writes) == 1


def test_another_browser_account_is_not_saved(bridge, account_connection):
    _, writes = account_connection
    with pytest.raises(ValueError, match="same Cloud account"):
        cli.connect_account(
            CLOUD_ORIGIN, bridge.config, require_active=True, expected_account="another",
        )
    assert not bridge.config.with_suffix(".cloud-sync.json").exists() and writes == []


def test_browser_success_and_fallback_only_run_after_click(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[1] / "src/varyz/cloud/computer_connect.js"
    harness = tmp_path / "browser.cjs"
    harness.write_text(r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
async function run(mode) {
  let clicked, calls = [];
  const button = {dataset: {cloudAccount: 'owner', csrf: 'csrf-token'},
    addEventListener: (_, cb) => clicked = cb};
  const status = {}, fallback = {open: false};
  vm.runInNewContext(source, {
    document: {getElementById: id => ({'connect-computer': button,
      'computer-status': status, 'computer-fallback': fallback})[id]},
    AbortSignal: {timeout: () => null}, Date, URLSearchParams,
    setTimeout: cb => cb(),
    fetch: async (url, options) => {
      if (url === '/computer/connect') {
        assert.equal(options.method, 'POST');
        assert.equal(options.credentials, 'same-origin');
        assert.equal(options.body.get('csrf'), 'csrf-token');
        assert.equal(calls.length, 1);
        return {ok: mode !== 'expired'};
      }
      calls.push(options);
      assert.equal(url, 'http://127.0.0.1:1207/cloud/computer');
      assert.equal(options.credentials, 'omit');
      if (mode === 'blocked') throw Error('denied');
      const value = calls.length === 1 ? {installed: true, ticket: 'capability'}
        : {state: mode === 'failed' ? 'failed' : 'connected'};
      return {ok: true, json: async () => value};
    },
  });
  assert.equal(calls.length, 0);
  await clicked();
  if (mode === 'success') {
    assert.equal(button.textContent, 'Computer connected');
    assert.equal(calls[1].method, 'POST');
    assert.equal(calls[1].headers['X-Varyz-Ticket'], 'capability');
    assert.equal(calls[1].headers['X-Varyz-Account'], 'owner');
    assert.equal(fallback.open, false);
    assert.match(status.textContent, /upload approval comes next/);
  } else {
    assert.equal(button.disabled, false);
    assert.equal(fallback.open, true);
  }
}
(async () => {for (const mode of ['success','blocked','failed','expired']) await run(mode);})();
''', encoding="utf-8")
    subprocess.run(
        [node, str(harness), str(script)], check=True, capture_output=True, timeout=NODE_TIMEOUT
    )


def test_returned_page_resumes_without_detecting_or_uploading(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[1] / "src/varyz/cloud/computer_connect.js"
    harness = tmp_path / "return.cjs"
    harness.write_text(r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
async function run(mode) {
  const ticket = 't'.repeat(43), calls = [], history = [];
  const button = {addEventListener: () => {}}, status = {}, fallback = {open: false};
  vm.runInNewContext(source, {
    document: {getElementById: id => ({'connect-computer': button,
      'computer-status': status, 'computer-fallback': fallback})[id]},
    window: {location: {pathname: '/', search: '',
      hash: '#computer=' + (mode === 'invalid' ? 'bad' : ticket)},
      history: {replaceState: (_, __, url) => history.push(url)}},
    AbortSignal: {timeout: () => null}, Date,
    fetch: async (url, options) => {
      calls.push(options);
      assert.equal(options.method, 'GET');
      assert.equal(options.headers['X-Varyz-Ticket'], ticket);
      return {ok: mode !== 'expired', json: async () =>
        mode === 'expired' ? {error: 'Connection request expired'} : {state: 'connected'}};
    },
  });
  await new Promise(setImmediate);
  assert.deepEqual(history, ['/#connect-varyz']);
  assert.equal(calls.length, mode === 'invalid' ? 0 : 1);
  if (mode === 'success') assert.equal(button.textContent, 'Computer connected');
  if (mode === 'expired') {
    assert.equal(button.disabled, false);
    assert.equal(fallback.open, true);
  }
}
(async () => {for (const mode of ['success', 'expired', 'invalid']) await run(mode);})();
''', encoding="utf-8")
    subprocess.run(
        [node, str(harness), str(script)], check=True, capture_output=True, timeout=NODE_TIMEOUT
    )


def test_other_account_offers_one_switch_instead_of_a_sign_in_loop(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[1] / "src/varyz/cloud/computer_connect.js"
    harness = tmp_path / "switch.cjs"
    harness.write_text(r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
(async () => {
  let clicked, starts = [];
  const button = {dataset: {cloudAccount: 'owner', csrf: 'csrf-token'},
    addEventListener: (_, cb) => clicked = cb};
  const status = {}, fallback = {open: false};
  vm.runInNewContext(source, {
    document: {getElementById: id => ({'connect-computer': button,
      'computer-status': status, 'computer-fallback': fallback})[id]},
    AbortSignal: {timeout: () => null}, Date, URLSearchParams,
    setTimeout: cb => cb(),
    fetch: async (url, options) => {
      if (url === '/computer/connect') return {ok: true};
      if (options.method === 'POST') starts.push(options.headers);
      const value = !options.headers['X-Varyz-Ticket'] ? {installed: true, ticket: 'capability'}
        : options.method === 'POST' ? {state: 'connecting'}
        : starts.length === 1 ? {state: 'failed', reason: 'other_account'} : {state: 'connected'};
      return {ok: true, json: async () => value};
    },
  });
  await clicked();
  assert.equal(button.textContent, 'Switch this computer to this account');
  assert.match(status.textContent, /linked to a different Varyz Cloud account/);
  assert.equal(fallback.open, false);
  assert.equal(starts[0]['X-Varyz-Switch'], undefined);
  await clicked();
  assert.equal(starts[1]['X-Varyz-Switch'], '1');
  assert.equal(button.textContent, 'Computer connected');
})().catch(error => {console.error(error); process.exit(1);});
''', encoding="utf-8")
    subprocess.run(
        [node, str(harness), str(script)], check=True, capture_output=True, timeout=NODE_TIMEOUT
    )


def test_browser_retries_local_outage_and_restores_tab_connection(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[1] / "src/varyz/cloud/computer_connect.js"
    harness = tmp_path / "recover.cjs"
    harness.write_text(r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const saved = new Map(), ticket = 't'.repeat(43);
(async () => {
  let starts = 0, detections = 0;
  async function run(reload) {
    let clicked;
    const button = {dataset: {cloudAccount: 'owner', csrf: 'csrf'},
      addEventListener: (_, cb) => clicked = cb};
    const status = {}, fallback = {};
    vm.runInNewContext(source, {
      document: {getElementById: id => ({'connect-computer': button,
        'computer-status': status, 'computer-fallback': fallback})[id]},
      sessionStorage: {getItem: k => saved.get(k), setItem: (k,v) => saved.set(k,v)},
      AbortSignal: {timeout: () => null}, Date, URLSearchParams,
      setTimeout: cb => cb(),
      fetch: async (url, options) => {
        if (url === '/computer/connect') return {ok: true};
        const capability = options.headers['X-Varyz-Ticket'];
        if (!capability) {
          detections++;
          if (detections < 3) throw new TypeError('Failed to fetch');
          return {ok: true, json: async () => ({installed: true, ticket})};
        }
        assert.equal(capability, ticket);
        if (options.method === 'POST') {
          starts++; return {ok: true, json: async () => ({state:'connecting'})};
        }
        return {ok: true, json: async () => ({state:'connected'})};
      },
    });
    if (!reload) await clicked();
    await new Promise(setImmediate);
    assert.equal(button.textContent, 'Computer connected');
  }
  await run(false);
  await run(true);
  assert.equal(detections, 3);
  assert.equal(starts, 1);
})().catch(error => {console.error(error); process.exit(1);});
''', encoding="utf-8")
    subprocess.run(
        [node, str(harness), str(script)], check=True, capture_output=True, timeout=NODE_TIMEOUT
    )

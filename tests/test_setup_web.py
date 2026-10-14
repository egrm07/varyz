"""`varyz setup`: the first run as a page, and the server that carries it.

As in test_setup.py, nothing here starts a daemon, reads the real home
directory, opens a browser or indexes anything: the daemon, the agent clients,
the doctor and the sync are replaced. What is exercised is what the page
offers, that a run approves exactly what was ticked, the verdict it reports,
and that the local server answers only the page it opened.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from varyz import cli, setup_web
from varyz.bootstrap import DetectedSources, approved_local_connectors
from varyz.card import layout
from varyz.config import load_config
from varyz.connectors import github
from varyz.install import InstallResult
from varyz.setup_web import SetupServer, SetupSession, render_page, serve

runner = CliRunner()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home with Claude Code, a repository, a documents folder and AI histories."""
    home = tmp_path / "home"
    docs = home / "Documents"
    repo = home / "projects" / "demo"
    docs.mkdir(parents=True)
    (repo / ".git").mkdir(parents=True)
    claude = home / ".claude.json"
    claude.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("VARYZ_HOME", str(home / ".varyz"))
    monkeypatch.setattr(
        "varyz.bootstrap.discover_local_sources",
        lambda: DetectedSources((docs.resolve(),), (repo.resolve(),), ("claude_code",)),
    )
    monkeypatch.setattr(cli, "_port_in_use", lambda _host, _port: False)
    # A rerun asks the daemon on the config's port whose archive it serves:
    # this one's.
    monkeypatch.setattr(cli, "_resolution_client",
                        lambda _cfg: SimpleNamespace(status=lambda: {}))
    monkeypatch.setattr(
        "varyz.resolution.client.ensure_daemon",
        lambda _client, _path: SimpleNamespace(host="127.0.0.1", port=8765),
    )
    monkeypatch.setattr(
        "varyz.install.client_paths",
        lambda: {
            "claude-code": claude,
            "codex": home / ".codex" / "config.toml",
            "cursor": home / ".cursor" / "mcp.json",
        },
    )
    connected: list[str] = []

    def connect(cid, config_path=None, **_kw):
        connected.append(cid)
        return None, [InstallResult(cid, claude, True, "connected")]

    monkeypatch.setattr("varyz.install.connect_agent", connect)
    doctor = [("daemon", True, "ok")]
    monkeypatch.setattr("varyz.hooks.doctor", lambda _cfg: doctor)
    synced: list[tuple[str, ...]] = []
    failing: list[str] = []

    def sync(_cfg, ids, on_progress=None, *, step=None, on_read=None):
        assert step == 4
        synced.append(ids)
        if on_progress is not None:
            on_progress("indexing")
        return [cid for cid in ids if cid in failing]

    monkeypatch.setattr(cli, "_sync_sources", sync)
    # Setup's last steps, as they read when each works. What each does is
    # tested with its own fakes in test_setup_steps.py.
    finishing = SimpleNamespace(
        calls=[],
        graph=("done", "12 claims from 40 entries; personal model compiled"),
        autosync=("done", "syncs every 30 minutes and at login"),
        classifier=("done", "qwen3.5:4b ready", False),
        privacy=None,
        restarted=[],
    )

    def graph(cfg_path, on_progress=None):
        finishing.calls.append("graph")
        return finishing.graph

    def autosync(cfg_path):
        finishing.calls.append("autosync")
        return finishing.autosync

    def classifier(cfg_path, on_progress=None):
        finishing.calls.append("classifier")
        return finishing.classifier

    def privacy(cfg_path, *, since, wait=10.0):
        finishing.calls.append("privacy")
        return finishing.privacy

    monkeypatch.setattr(cli, "_build_graph", graph)
    monkeypatch.setattr(cli, "_install_autosync", autosync)
    monkeypatch.setattr(cli, "_ready_classifier", classifier)
    monkeypatch.setattr(cli, "_mac_folder_access", privacy)
    monkeypatch.setattr(cli, "_restart_daemon",
                        lambda cfg_path: finishing.restarted.append(cfg_path) or True)
    # GitHub: no GitHub CLI login unless a test sets one, a keychain that is a
    # dict, and an API that knows one token.
    gh = SimpleNamespace(token=None)
    monkeypatch.setattr("varyz.connectors.github.token_from_gh", lambda: gh.token)
    keychain: dict[str, str] = {}
    monkeypatch.setattr("varyz.secrets.set_secret", keychain.__setitem__)
    github_api = SimpleNamespace(status=None, calls=[])

    def api(request: httpx.Request) -> httpx.Response:
        github_api.calls.append(request.headers["Authorization"])
        if github_api.status is not None:
            return httpx.Response(github_api.status)
        if request.headers["Authorization"] in {f"Bearer {GOOD}", f"Bearer {GH_LOGIN}"}:
            return httpx.Response(200, json={"login": "octo-owner"})
        return httpx.Response(401, json={"message": "Bad credentials"})

    # The offer to extract preferences: none unless a test sets one, so no
    # real reader or plan usage is ever looked at.
    extract = SimpleNamespace(offer=None, asked=[])

    def offer(cfg, config_file, **_kw):
        extract.asked.append(config_file)
        return extract.offer

    monkeypatch.setattr("varyz.setup_extract.offer", offer)
    real_verify = github.verify_token
    monkeypatch.setattr(
        "varyz.connectors.github.verify_token",
        lambda token, **kw: real_verify(token, transport=httpx.MockTransport(api), **kw),
    )
    return SimpleNamespace(
        home=home,
        config=home / ".varyz" / "config.toml",
        connected=connected,
        synced=synced,
        failing=failing,
        doctor=doctor,
        gh=gh,
        keychain=keychain,
        github_api=github_api,
        finishing=finishing,
        extract=extract,
    )


GOOD = "ghp_good-token"
GH_LOGIN = "gho_from-the-cli"


def _finished(session: SetupSession, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = session.snapshot()
        if state["finished"]:
            return state
        time.sleep(0.02)
    raise AssertionError("setup did not finish")


def test_plan_ticks_ai_conversations_and_offers_the_rest_unticked(machine):
    plan = SetupSession(machine.config).plan()

    sources = {source["id"]: source["checked"] for source in plan["sources"]}
    assert sources == {"ai_conversations": True, "git": False, "documents": False}
    agents = {agent["id"]: agent["installed"] for agent in plan["agents"]}
    assert agents == {"claude-code": True, "codex": False, "cursor": False}
    assert plan["exists"] is False
    assert "standards" not in plan


@pytest.mark.parametrize("share", [True, False])
def test_analytics_setup_choice_is_separate_and_persisted(machine, share):
    from varyz.analytics import status

    session = SetupSession(machine.config)
    assert session.plan()["analytics"]["sharing"] is False
    assert session.start([], [], share) is None
    _finished(session)
    assert status(load_config(machine.config))["consent"] is share
    assert SetupSession(machine.config).plan()["analytics"]["sharing"] is share


def test_a_run_approves_only_what_was_ticked(machine):
    session = SetupSession(machine.config)

    assert session.start(["claude-code"], ["ai_conversations"]) is None
    state = _finished(session)

    assert approved_local_connectors(machine.config) == ("ai_conversations",)
    assert machine.synced == [("ai_conversations",)]
    assert machine.connected == ["claude-code"]
    assert [step["state"] for step in state["steps"]] == ["done"] * 7
    assert machine.finishing.calls == ["graph", "autosync", "classifier", "privacy"]
    assert state["picked"] == {"agents": ["claude-code"], "sources": ["ai_conversations"]}
    result = state["result"]
    assert result["verdict"] == "GO"
    assert result["summary"] == "Agents are briefed from this archive."
    assert result["next"][0] == "Restart your agents so they load Varyz."


def test_setup_ends_saying_the_agents_first_use_learns_how_the_owner_works(
    machine, monkeypatch
):
    from datetime import UTC, datetime

    from varyz.db.connection import open_db

    def sync(cfg, ids, on_progress=None, *, step=None, on_read=None):
        # The owner's past turns to their agents, as indexing AI conversations leaves them.
        conn = open_db(cfg.db_path)
        when = datetime(2026, 9, 1, tzinfo=UTC).isoformat()
        conn.execute(
            """INSERT INTO entries (id, kind, source, external_id, content_hash, body,
                   occurred_start, ingested_at, thread_id, sensitivity, authored_by_user)
               VALUES ('t1', 'message', 'ai_conversations', 't1', 't1', ?, ?, ?,
                       'ai:claude_code:s1', 'personal', 1)""",
            ("From: me\nConversation: Claude Code — shop\n\n"
             "this shop is for people who restore old bikes, it should feel like a workshop",
             when, when),
        )
        conn.commit()
        conn.close()
        return []

    monkeypatch.setattr(cli, "_sync_sources", sync)
    session = SetupSession(machine.config)

    session.start(["claude-code"], ["ai_conversations"])
    reading = _finished(session)["result"]["reading"]

    assert reading == {"turns_waiting": 1, "agents": ["Claude Code"], "restart": True}
    assert b'id="reading-text"' in render_page("token")


def test_without_a_connected_agent_setup_names_no_reading(machine):
    session = SetupSession(machine.config)

    session.start([], ["ai_conversations"])

    assert _finished(session)["result"]["reading"] is None


def test_a_measured_phase_says_how_long_it_has_left(machine, monkeypatch):
    now = [0.0]
    real = cli.TimeLeft
    monkeypatch.setattr(cli, "TimeLeft", lambda: real(clock=lambda: now[0]))
    session = SetupSession(machine.config)
    session._set(4, "running")

    session._progress("Building the graph: 0 of 100 entries", 0.0)
    assert session.snapshot()["left"] == ""
    now[0] = 60.0
    session._progress("Building the graph: 25 of 100 entries", 0.25)
    assert session.snapshot()["left"] == "about 3 min left"

    # The next step's pace is its own.
    session._set(5, "running")
    assert session.snapshot()["left"] == ""
    session._progress("Downloading Ollama: 1 MB of 500 MB", 0.002)
    assert session.snapshot()["left"] == ""


def test_the_page_numbers_the_running_step_and_shows_the_time_left():
    page = render_page("tok").decode("utf-8")

    assert "Step ${running + 1} of ${state.steps.length}. " in page
    assert "`${state.activity} · ${state.left}`" in page


def test_nothing_ticked_connects_and_indexes_nothing(machine):
    session = SetupSession(machine.config)

    session.run((), ())

    state = session.snapshot()
    assert approved_local_connectors(machine.config) == ()
    assert machine.synced == [] and machine.connected == []
    assert [step["state"] for step in state["steps"]] == [
        "done", "done", "skipped", "skipped", "done", "done", "done",
    ]
    assert "No agent is connected yet" in state["result"]["summary"]


def test_a_source_that_fails_to_index_is_a_no_go(machine):
    machine.failing.append("git")
    machine.doctor.append(("hooks", False, "Claude Code hooks missing"))
    session = SetupSession(machine.config)

    session.run(("claude-code",), ("ai_conversations", "git"))

    state = session.snapshot()
    assert state["steps"][3] == {
        "name": "Data sources", "state": "failed", "detail": "could not index git",
    }
    result = state["result"]
    assert result["verdict"] == "NO-GO"
    assert result["problems"] == [
        ["hooks", "Claude Code hooks missing"], ["data sources", "could not index git (step 4)"],
    ]


def test_a_daemon_that_will_not_start_skips_the_rest(machine, monkeypatch):
    monkeypatch.setattr("varyz.resolution.client.ensure_daemon", lambda _client, _path: None)
    session = SetupSession(machine.config)

    session.run(("claude-code",), ("ai_conversations",))

    state = session.snapshot()
    assert [step["state"] for step in state["steps"]] == [
        "done", "failed", *["skipped"] * 5,
    ]
    assert state["result"]["verdict"] == "NO-GO"
    assert machine.connected == [] and machine.synced == []
    assert machine.finishing.calls == []


def test_the_last_steps_that_fail_are_a_no_go_that_names_them(machine):
    machine.finishing.graph = ("failed", "the graph could not be built: disk full")
    machine.finishing.autosync = ("failed", "registry: unknown: fax")
    machine.finishing.classifier = ("failed", "Ollama did not start", False)
    session = SetupSession(machine.config)

    session.run(("claude-code",), ())

    state = session.snapshot()
    assert [step["state"] for step in state["steps"][4:]] == ["failed"] * 3
    assert state["steps"][5]["detail"] == "registry: unknown: fax"
    result = state["result"]
    assert result["verdict"] == "NO-GO"
    assert result["problems"] == [
        ["graph", "the graph could not be built: disk full (step 5)"],
        ["autosync", "registry: unknown: fax (step 6)"],
        ["classifier", "Ollama did not start (step 7)"],
    ]
    # Autosync did not start, so nothing can say what macOS keeps from it.
    assert "privacy" not in machine.finishing.calls
    assert result["privacy"] is None


def test_a_classifier_moved_to_a_new_port_restarts_the_daemon_and_the_agents(machine):
    machine.finishing.classifier = (
        "done", "Ollama 0.11.11 is too old, so a newer one serves on port 11435", True,
    )
    session = SetupSession(machine.config)

    session.run((), ())

    state = session.snapshot()
    assert machine.finishing.restarted == [machine.config]
    assert state["steps"][6]["state"] == "done"
    assert state["result"]["restart"] is True
    assert state["result"]["next"][0] == "Restart your agents so they load Varyz."


def test_a_daemon_that_does_not_come_back_fails_the_classifier(machine, monkeypatch):
    machine.finishing.classifier = ("done", "served on port 11435", True)
    monkeypatch.setattr(cli, "_restart_daemon", lambda cfg_path: False)
    session = SetupSession(machine.config)

    session.run((), ())

    state = session.snapshot()
    assert state["steps"][6] == {
        "name": "Classifier", "state": "failed",
        "detail": "served on port 11435; the daemon did not start again",
    }
    assert state["result"]["verdict"] == "NO-GO"


def test_what_macos_keeps_from_autosync_is_in_the_result(machine):
    machine.finishing.privacy = {
        "folders": [{"source": "Documents", "path": "/Users/o/Documents"}],
        "via": "app", "program": "/Users/o/.varyz/data/autosync/Varyz.app",
    }
    session = SetupSession(machine.config)

    session.run((), ())

    assert session.snapshot()["result"]["privacy"] == machine.finishing.privacy


def test_the_plan_names_the_classifier_model(machine):
    plan = SetupSession(machine.config).plan()

    assert plan["classifier"] == "qwen3.5:4b"


def test_a_step_that_raises_stops_setup_and_says_where(machine, monkeypatch):
    def broken(_client, *, config_path):
        raise RuntimeError("the client config is unreadable")

    monkeypatch.setattr("varyz.install.connect_agent", broken)
    session = SetupSession(machine.config)

    session.run(("claude-code",), ())

    state = session.snapshot()
    assert state["finished"] is True
    assert state["steps"][2]["state"] == "failed"
    assert state["result"]["verdict"] == "NO-GO"
    assert state["result"]["problems"] == [["Agents", "the client config is unreadable"]]


def _until(session: SetupSession, holds, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = session.snapshot()
        if holds(state):
            return state
        time.sleep(0.01)
    raise AssertionError(f"never happened: {session.snapshot()}")


def _download(phase: str, on_progress, release: threading.Event) -> None:
    """A download that runs until it is paused, or until ``release`` lets it end."""
    done = 0
    while not release.is_set():
        done = min(done + 1, 999)
        on_progress(f"{phase}: {done} MB of 1,000 MB", done / 1000)
        time.sleep(0.005)


def test_a_paused_download_resumes_in_the_step_it_paused_in(machine, monkeypatch):
    release = threading.Event()
    syncs: list[tuple[str, ...]] = []

    def sync(_cfg, ids, on_progress=None, *, step=None, on_read=None):
        syncs.append(ids)
        _download("Downloading the search models", on_progress, release)
        return []

    monkeypatch.setattr(cli, "_sync_sources", sync)
    session = SetupSession(machine.config)
    assert session.pause() == "setup is not running"
    assert session.start([], ["documents"]) is None
    _until(session, lambda state: state["activity"].startswith(cli.DOWNLOADING))

    assert session.pause() is None
    state = _until(session, lambda state: state["paused"])

    step = state["steps"][3]
    assert step["state"] == "paused"
    assert step["detail"].startswith("Downloading the search models: ")
    assert step["detail"].endswith("; what has downloaded is kept")
    assert (state["finished"], state["pausing"], state["result"]) == (False, False, None)
    assert session.running is False  # so the page's server may stop when it is closed
    assert machine.finishing.calls == []  # nothing after the paused step ran
    assert session.pause() == "setup is not running"
    assert session.start([], []) == "setup is already running"

    release.set()
    assert session.resume() is None
    state = _finished(session)

    # The same sources indexed again, as ticked; the steps before it not run again.
    assert syncs == [("documents",), ("documents",)]
    assert [step["state"] for step in state["steps"]] == (
        ["done"] * 2 + ["skipped"] + ["done"] * 4  # no agent was ticked
    )
    assert machine.finishing.calls == ["graph", "autosync", "classifier", "privacy"]
    assert state["result"]["verdict"] == "GO"
    assert session.resume() == "nothing is paused"


def test_only_a_download_can_be_paused(machine):
    session = SetupSession(machine.config)
    session.started = True
    session._set(3, "running")

    session._progress("Reading git: 1,200 items", 0.4)
    assert session.pause() == "only a download can be paused"

    # A download that ends before the pause reaches it is not paused after it.
    session._progress("Downloading Ollama: 199.0 MB of 200.0 MB", 0.995)
    assert session.pause() is None
    session._progress("Unpacking Ollama")
    assert session.snapshot()["pausing"] is False
    session._progress("Downloading qwen3.5:4b: 0 bytes of 3.4 GB", 0.0)  # not raised


def test_start_refuses_what_the_page_did_not_offer_and_a_second_run(machine):
    session = SetupSession(machine.config)

    assert session.start([], ["github"]) == "not offered on this page: github"
    assert session.start(["codex"], []) == "not offered on this page: codex"
    assert session.snapshot()["started"] is False

    assert session.start([], []) is None
    assert session.start([], []) == "setup is already running"
    _finished(session)


def test_the_page_carries_the_card_palette_and_the_token():
    page = render_page("tok-123").decode("utf-8")

    assert "{{" not in page
    for colour in (layout.NIGHT, layout.INK, layout.MUTED, layout.DIM, layout.SIGNAL,
                   setup_web.FAILED):
        assert colour in page
    assert '"tok-123"' in page
    assert "Varyz Card Serif" in page
    assert "Seven steps" in page
    for index in range(len(setup_web.STEPS)):
        assert f'data-step="{index}"' in page
    assert f'data-step="{len(setup_web.STEPS)}"' not in page


@pytest.fixture
def server(machine):
    session = SetupSession(machine.config)
    server = SetupServer(session)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _request(server, method, path, *, host=None, token=None, body=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token is not None:
        headers["X-Varyz-Setup"] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        conn.close()


def test_the_page_needs_the_key_from_the_printed_link(server):
    token = server.session.token

    status, kind, body = _request(server, "GET", f"/?t={token}")
    assert status == 200 and kind.startswith("text/html")
    assert b"Set up Varyz" in body

    for path in ("/", "/?t=wrong"):
        status, _, body = _request(server, "GET", path)
        assert status == 403
        assert b"no key" in body


def test_a_rebound_host_name_is_refused(server):
    token = server.session.token

    status, _, _ = _request(server, "GET", f"/?t={token}", host="attacker.example:80")
    assert status == 403
    status, _, _ = _request(server, "POST", "/api/run", host="attacker.example",
                            token=token, body={"agents": [], "sources": []})
    assert status == 403
    assert server.session.snapshot()["started"] is False


def test_the_api_needs_the_key_in_its_header(server):
    token = server.session.token

    assert _request(server, "GET", "/api/plan")[0] == 403
    assert _request(server, "GET", f"/api/plan?t={token}")[0] == 403
    status, _, _ = _request(server, "POST", "/api/run", body={"agents": [], "sources": []})
    assert status == 403
    assert server.session.snapshot()["started"] is False

    status, _, body = _request(server, "GET", "/api/plan", token=token)
    assert status == 200
    assert json.loads(body)["sources"][0]["id"] == "ai_conversations"


def test_a_run_through_the_api(server):
    token = server.session.token

    status, _, body = _request(server, "POST", "/api/run", token=token,
                               body={"agents": [], "sources": ["documents"]})
    assert status == 202, body
    _finished(server.session)
    status, _, body = _request(server, "POST", "/api/run", token=token,
                               body={"agents": [], "sources": []})
    assert status == 409
    assert json.loads(body) == {"error": "setup is already running"}

    state = json.loads(_request(server, "GET", "/api/state", token=token)[2])
    assert state["finished"] is True
    assert approved_local_connectors(server.session.config_path) == ("documents",)

    assert _request(server, "POST", "/api/close", token=token)[0] == 200
    # The handler replies before it raises the flag, so the client can get here first.
    assert server.closed.wait(5)


def test_a_download_paused_and_resumed_through_the_api(server, machine, monkeypatch):
    token = server.session.token
    release = threading.Event()

    def classifier(cfg_path, on_progress=None):
        machine.finishing.calls.append("classifier")
        _download("Downloading qwen3.5:4b", on_progress, release)
        return machine.finishing.classifier

    monkeypatch.setattr(cli, "_ready_classifier", classifier)
    assert _request(server, "POST", "/api/pause", token=token, body={})[0] == 409
    assert _request(server, "POST", "/api/run", token=token,
                    body={"agents": [], "sources": []})[0] == 202
    _until(server.session, lambda state: state["activity"].startswith(cli.DOWNLOADING))
    assert _request(server, "POST", "/api/pause", body={})[0] == 403

    status, _, body = _request(server, "POST", "/api/pause", token=token, body={})
    assert (status, json.loads(body)) == (200, {"ok": True})
    _until(server.session, lambda state: state["paused"])

    state = json.loads(_request(server, "GET", "/api/state", token=token)[2])
    assert state["steps"][6]["state"] == "paused" and state["finished"] is False
    status, _, body = _request(server, "POST", "/api/pause", token=token, body={})
    assert (status, json.loads(body)) == (409, {"error": "setup is not running"})

    release.set()
    assert _request(server, "POST", "/api/resume", token=token, body={})[0] == 200
    state = _finished(server.session)
    assert state["result"]["verdict"] == "GO"
    # The graph and autosync were done before the pause and are not done again.
    assert machine.finishing.calls == ["graph", "autosync", "classifier", "classifier",
                                       "privacy"]
    status, _, body = _request(server, "POST", "/api/resume", token=token, body={})
    assert (status, json.loads(body)) == (409, {"error": "nothing is paused"})


def test_the_page_pauses_only_downloads_and_says_how_to_carry_on():
    page = render_page("tok").decode("utf-8")

    assert f'const DOWNLOADING = "{cli.DOWNLOADING}";' in page
    assert '<button id="pause" class="quiet" hidden>Pause download</button>' in page
    assert '<button id="resume" hidden>Resume</button>' in page
    assert '"/api/pause"' in page and '"/api/resume"' in page
    assert "run varyz setup later to carry on" in page


def test_serve_closed_while_paused_says_how_to_carry_on(machine, monkeypatch, capsys):
    class Paused(SetupSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started, self.paused = True, 4

    monkeypatch.setattr(setup_web, "SetupSession", Paused)

    assert serve(machine.config, open_browser=False, idle_limit=0) is None
    printed = capsys.readouterr().out
    assert "Setup is paused. What has downloaded is kept: run varyz setup" in printed


def test_folder_access_opens_only_for_what_the_run_found(server, monkeypatch):
    token = server.session.token
    opened: list[dict] = []
    monkeypatch.setattr(cli, "_open_folder_access", opened.append)

    status, _, _ = _request(server, "POST", "/api/folder-access", token=token, body={})
    assert status == 409 and opened == []
    assert _request(server, "POST", "/api/folder-access", body={})[0] == 403

    privacy = {"folders": [{"source": "Documents", "path": "/Users/o/Documents"}],
               "via": "full_disk_access", "program": "/opt/uv"}
    server.session.result = {"privacy": privacy}
    status, _, body = _request(server, "POST", "/api/folder-access", token=token,
                               body={"program": "/bin/sh", "via": "app"})
    assert status == 200 and json.loads(body) == {"opened": True}
    assert opened == [privacy]


def test_the_permission_panel_explains_itself_and_can_be_skipped():
    page = render_page("tok").decode("utf-8")

    assert "one more permission" in page and "Files and Folders" in page
    assert "nothing leaves" in page
    assert 'id="privacy-skip"' in page and "varyz sync" in page
    # Full Disk Access is named only as what it is: more than these folders.
    assert "opens more than these folders" in page
    assert '<h2 class="label">Full Disk Access</h2>' not in page


def test_only_the_bundled_fonts_are_served(server):
    status, kind, body = _request(server, "GET", "/fonts/VaryzCardSerif-Regular.ttf")
    assert status == 200 and kind == "font/ttf" and len(body) > 1000

    for path in ("/fonts/../setup_web.py", "/fonts/OFL-SourceSerif4.txt", "/setup_web.html"):
        assert _request(server, "GET", path)[0] in {403, 404}, path


def test_serve_without_a_run_reports_that_nothing_ran(machine):
    assert serve(machine.config, open_browser=False, idle_limit=0) is False
    assert not machine.config.exists()


def test_cli_web_refuses_flags_that_answer_for_the_page(machine):
    for extra in (["--yes"], ["--dry-run"], ["--no-sources"], ["--source", "git"]):
        result = runner.invoke(cli.app, ["setup", "--web", "--config", str(machine.config),
                                         *extra])
        assert result.exit_code == 2, (extra, result.output)
        assert "--web asks on the page" in result.output


def test_cli_web_hands_the_resolved_config_to_the_page(machine, monkeypatch):
    served: list[Path] = []
    monkeypatch.setattr(setup_web, "serve", lambda path: served.append(path) or True)

    result = runner.invoke(cli.app, ["setup", "--web", "--config", str(machine.config)])

    assert result.exit_code == 0, result.output
    assert served == [machine.config.resolve()]

    monkeypatch.setattr(setup_web, "serve", lambda path: False)
    result = runner.invoke(cli.app, ["setup", "--web", "--config", str(machine.config)])
    assert result.exit_code == 1


# -- GitHub, linked from the page ---------------------------------------------------


def test_the_page_offers_github_with_a_token_box_or_the_cli_login(machine):
    plan = SetupSession(machine.config).plan()
    assert plan["github"]["account"] is None and plan["github"]["cli"] is False
    assert "pull-request" in plan["github"]["reads"]

    machine.gh.token = GH_LOGIN
    plan = SetupSession(machine.config).plan()
    assert plan["github"]["cli"] is True
    assert GH_LOGIN not in json.dumps(plan)


def test_a_checked_token_is_kept_until_a_run_with_github_ticked_stores_it(machine):
    session = SetupSession(machine.config)

    assert session.link_github(f"  {GOOD}\n") == ("octo-owner", None)
    assert machine.keychain == {}
    offer = session.plan()["github"]
    assert (offer["account"], offer["saved"]) == ("octo-owner", False)

    session.run((), ("github",))

    state = session.snapshot()
    assert machine.keychain == {"github_token:octo-owner": GOOD}
    options = load_config(machine.config).connector_options("github")
    assert options["account"] == "octo-owner" and options["affiliation"] == "owner"
    assert GOOD not in machine.config.read_text(encoding="utf-8")
    assert machine.synced == [("github",)]
    assert state["steps"][3]["detail"] == "connected GitHub; indexed"
    assert not any("connect github" in line for line in state["result"]["next"])


def test_github_linked_but_unticked_is_not_stored(machine):
    session = SetupSession(machine.config)
    session.link_github(GOOD)

    session.run((), ("ai_conversations",))

    assert machine.keychain == {}
    assert load_config(machine.config).connector_options("github") == {}
    assert machine.synced == [("ai_conversations",)]
    assert "varyz connect github" in session.snapshot()["result"]["next"][-1]


def test_the_github_cli_login_links_in_one_click(machine):
    machine.gh.token = GH_LOGIN
    session = SetupSession(machine.config)

    assert session.link_github(None) == ("octo-owner", None)
    session.run((), ("github",))

    assert machine.keychain == {"github_token:octo-owner": GH_LOGIN}


def test_github_says_why_a_token_is_not_linked(machine):
    session = SetupSession(machine.config)

    assert session.link_github(None) == (
        None, "The GitHub CLI has no login here; paste a token instead.",
    )
    assert session.link_github("ghp_wrong") == (
        None, "GitHub did not accept this token: it is wrong, expired or revoked.",
    )
    machine.github_api.status = 503
    assert session.link_github(GOOD) == (None, "GitHub answered 503; try again in a minute.")
    assert session.github_login is None
    assert session.start([], ["github"]) == "not offered on this page: github"


def _archive_with(machine, github_section: str) -> None:
    """An archive a blank run made, with a [connectors.github] section added."""
    SetupSession(machine.config).run((), ())
    machine.synced.clear()
    with machine.config.open("a", encoding="utf-8") as config:
        config.write(f"\n[connectors.github]\n{github_section}")


def test_github_already_in_the_config_is_indexed_not_linked_again(machine):
    _archive_with(machine, 'account = "octo-owner"\n')
    machine.gh.token = GH_LOGIN
    session = SetupSession(machine.config)

    offer = session.plan()["github"]
    assert (offer["account"], offer["saved"], offer["cli"]) == ("octo-owner", True, False)
    assert session.link_github(GOOD) == (None, "GitHub is already in this archive's config")
    session.run((), ("github",))

    assert machine.keychain == {}
    assert machine.synced == [("github",)]
    assert machine.github_api.calls == []


def test_a_paused_github_is_left_to_the_owner(machine):
    _archive_with(machine, 'account = "octo-owner"\nenabled = false\n')
    session = SetupSession(machine.config)

    assert session.plan()["github"] is None
    assert session.link_github(GOOD)[1] == "GitHub is already in this archive's config"
    assert session.start([], ["github"]) == "not offered on this page: github"


def test_github_through_the_api_never_echoes_the_token(server, machine):
    token = server.session.token

    status, _, _ = _request(server, "POST", "/api/github", body={"token": GOOD})
    assert status == 403
    status, _, body = _request(server, "POST", "/api/github", token=token,
                               body={"token": "ghp_wrong"})
    assert status == 422 and b"did not accept" in body
    for bad in ({"token": 5}, ["token"]):
        assert _request(server, "POST", "/api/github", token=token, body=bad)[0] == 400

    status, _, body = _request(server, "POST", "/api/github", token=token,
                               body={"token": GOOD})
    assert status == 200 and json.loads(body) == {"login": "octo-owner"}
    for path in ("/api/plan", "/api/state"):
        assert GOOD.encode() not in _request(server, "GET", path, token=token)[2]


def test_connect_github_in_the_terminal_links_the_same_way(machine):
    machine.config.parent.mkdir(parents=True)
    machine.config.write_text("", encoding="utf-8")

    result = runner.invoke(cli.app, ["connect", "github", "--token", GOOD,
                                     "--config", str(machine.config)])

    assert result.exit_code == 0, result.output
    assert "as octo-owner" in result.output
    assert machine.keychain == {"github_token:octo-owner": GOOD}
    assert load_config(machine.config).connector_options("github")["account"] == "octo-owner"


# -- the page is what `varyz setup` opens ------------------------------------------


@pytest.fixture
def page(machine, monkeypatch):
    """`varyz setup` with a browser to open, and the page replaced by a record."""
    served: list[Path] = []
    monkeypatch.setattr(cli, "_browser_available", lambda: True)
    monkeypatch.setattr(setup_web, "serve", lambda path: served.append(path) or True)
    return served


def test_plain_setup_opens_the_page(machine, page):
    result = runner.invoke(cli.app, ["setup", "--config", str(machine.config)])

    assert result.exit_code == 0, result.output
    assert page == [machine.config.resolve()]


def test_answers_given_in_advance_or_terminal_ask_here(machine, page):
    for extra in (["--terminal"], ["--no-sources"], ["--dry-run"]):
        result = runner.invoke(cli.app, ["setup", "--config", str(machine.config), *extra],
                               input="n\n" * 6)
        assert result.exit_code == 0, (extra, result.output)
        assert "Varyz setup" in result.output
    assert page == []


def test_with_no_browser_setup_asks_in_the_terminal(machine, page, monkeypatch):
    monkeypatch.setattr(cli, "_browser_available", lambda: False)

    result = runner.invoke(cli.app, ["setup", "--config", str(machine.config), "--no-sources"])
    assert page == [] and "No browser" not in result.output

    result = runner.invoke(cli.app, ["setup", "--config", str(machine.config)], input="n\n" * 6)
    assert result.exit_code == 0, result.output
    assert "No browser to open here" in result.output
    assert page == []

    result = runner.invoke(cli.app, ["setup", "--config", str(machine.config), "--web"])
    assert result.exit_code == 0, result.output
    assert page == [machine.config.resolve()]


def test_web_and_terminal_together_are_refused(machine, page):
    result = runner.invoke(cli.app, ["setup", "--web", "--terminal", "--config",
                                     str(machine.config)])

    assert result.exit_code == 2
    assert "pick one" in result.output and page == []


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        ({"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"}, "darwin", False),
        ({}, "linux", False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
        ({}, "darwin", True),
    ],
)
def test_a_browser_is_available_only_on_this_screen(monkeypatch, env, platform, expected):
    for name in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr("webbrowser.get", lambda: object())

    assert cli._browser_available() is expected


# -- extracting preferences while it downloads ---------------------------------------

OFFER = {"state": "offered", "turns": 2807, "runner": "codex", "plan": "ChatGPT",
         "known": True, "starts_at": None, "five_hour": 12.1, "weekly": 2.0, "seconds": 566,
         "five_hour_left": 88, "weekly_left": 69, "pauses_until": None,
         "gate": {"five_hour": 30, "weekly": 25}}


def _downloading_after_reading(monkeypatch, release: threading.Event) -> None:
    """Step 4 as it runs now: the sources are read, then the search models download."""

    def sync(_cfg, ids, on_progress=None, *, step=None, on_read=None):
        if on_read is not None:
            on_read()
        _download("Downloading the search models", on_progress, release)
        return []

    monkeypatch.setattr(cli, "_sync_sources", sync)


def test_the_offer_is_asked_while_the_search_models_download(machine, monkeypatch):
    machine.extract.offer = dict(OFFER)
    release = threading.Event()
    _downloading_after_reading(monkeypatch, release)
    started: list[dict] = []

    def start(cfg, config_file, offered, **_kw):
        started.append(offered)
        return {"at": time.time(), "turns": 2807, "runner": "codex", "seconds": 566.0,
                "five_hour_used": 12.0, "weekly_used": 31.0, "merging_since": None}

    monkeypatch.setattr("varyz.setup_extract.start", start)
    monkeypatch.setattr(
        "varyz.setup_extract.progress",
        lambda cfg, begun, now: {"state": "accepted", "phase": "reading", "turns": 2807,
                                 "turns_left": 2000, "fraction": 0.29, "seconds_left": 300.0},
    )
    session = SetupSession(machine.config)
    session.start(["claude-code"], ["ai_conversations"])

    state = _until(session, lambda state: state["extract"] is not None)
    assert state["steps"][3]["state"] == "running"  # still downloading
    assert state["extract"]["state"] == "offered"
    assert machine.extract.asked == [machine.config]

    assert session.choose_extract(True) is None
    assert started == [OFFER]
    view = session.snapshot()["extract"]
    assert (view["state"], view["phase"], view["plan"]) == ("accepted", "reading", "ChatGPT")
    assert session.choose_extract(False) == "nothing to answer"

    release.set()
    _finished(session)
    # Asked once, though step 4 says the sources are read twice.
    assert machine.extract.asked == [machine.config]
    assert session.extract_accepted


def test_without_a_connected_agent_nothing_is_offered(machine, monkeypatch):
    machine.extract.offer = dict(OFFER)
    release = threading.Event()
    release.set()
    _downloading_after_reading(monkeypatch, release)
    session = SetupSession(machine.config)

    session.start([], ["ai_conversations"])
    state = _finished(session)

    assert machine.extract.asked == []
    assert state["extract"] is None


def test_the_downloads_say_what_is_left_counting_the_classifier(machine, monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "ollama"))
    session = SetupSession(machine.config)
    session.started = True
    assert session.snapshot()["downloads"] is None  # nothing downloads before step 4
    session._set(3, "running")

    session._progress("Downloading the search models: 100.0 MB of 1.3 GB", 0.08)
    downloads = session.snapshot()["downloads"]
    assert (downloads["now"], downloads["then"]) == ("the search models", "qwen3.5:4b")
    assert downloads["then_bytes"] == 3_400_000_000


def test_the_offer_answered_through_the_api(server, machine):
    token = server.session.token
    status, _, _ = _request(server, "POST", "/api/extract", token=token, body={"extract": "yes"})
    assert status == 400
    status, _, body = _request(server, "POST", "/api/extract", token=token,
                               body={"extract": True})
    assert (status, json.loads(body)) == (409, {"error": "nothing to answer"})

    server.session._extract = dict(OFFER)
    status, _, body = _request(server, "POST", "/api/extract", token=token,
                               body={"extract": False})
    assert (status, json.loads(body)) == (200, {"ok": True})
    state = json.loads(_request(server, "GET", "/api/state", token=token)[2])
    assert state["extract"]["state"] == "declined"


def test_the_page_asks_in_percent_and_minutes():
    page = render_page("tok").decode("utf-8")

    assert "Extract preferences from your existing chats?" in page
    assert '"/api/extract"' in page
    assert "5-hour limit" in page and "weekly limit" in page
    assert "Yes, extract (recommended)" in page
    assert "All downloads: " in page
    assert "—" not in page  # no em dashes in the copy


def test_serve_with_the_reading_taken_on_the_page_names_no_last_step(machine, monkeypatch,
                                                                      capsys):
    class Taken(SetupSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = self.finished = True
            self._extract_started = {"at": 0.0}
            self.result = {"verdict": "GO", "summary": "Agents are briefed.", "problems": [],
                           "next": [], "reading": {"turns_waiting": 9, "agents": ["Codex"],
                                                   "restart": False}}

    monkeypatch.setattr(setup_web, "SetupSession", Taken)

    assert serve(machine.config, open_browser=False, idle_limit=0) is True
    printed = capsys.readouterr().out
    assert "goes on in the background" in printed
    assert "Last step" not in printed

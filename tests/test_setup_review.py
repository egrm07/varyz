"""Setup opens the authenticated rules review only after learning completes."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from tests.test_setup_web import _request
from tests.test_setup_web import machine as machine
from tests.test_setup_web import server as server
from varyz import cli, review_web, setup_web


@pytest.mark.parametrize("phase", ["reading", "merging", "waiting", "failed", "agent"])
def test_review_refuses_unfinished_learning(server, monkeypatch, phase):
    monkeypatch.setattr(
        server.session,
        "snapshot",
        lambda: {
            "finished": True,
            "extract": {"state": "accepted", "phase": phase},
        },
    )
    status, _, _ = _request(server, "POST", "/api/review", token=server.session.token, body={})
    assert status == 409
    assert server._review is None


def test_review_uses_setup_identity_and_survives_closing_setup(server, monkeypatch):
    ready = {"finished": False, "extract": {"state": "accepted", "phase": "done"}}
    monkeypatch.setattr(server.session, "snapshot", lambda: ready)
    token = server.session.token
    for request in ({}, {"token": "wrong"}, {"token": token, "host": "attacker.example"}):
        assert _request(server, "POST", "/api/review", body={}, **request)[0] == 403
    assert _request(server, "POST", "/api/review", token=token, body={})[0] == 409
    ready["finished"] = True
    config = SimpleNamespace(resolution=SimpleNamespace(device_id="fixture-device"))
    loaded, identities = [], []
    client, owner = object(), object()

    def load(path):
        loaded.append(path)
        return config

    def session(actual_client, caller, device):
        identities.append((actual_client, caller, device))
        return SimpleNamespace(
            token="fixture-review-key",
            kept=0,
            dropped=0,
            items=lambda: {"items": [{"statement": "Fixture suggestion"}]},
        )

    monkeypatch.setattr(cli, "_cfg", load)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: client)
    monkeypatch.setattr(cli, "_local_owner_caller", lambda cfg, purpose: owner)
    monkeypatch.setattr(review_web, "ReviewSession", session)
    status, _, body = _request(server, "POST", "/api/review", token=token, body={})
    assert status == 200
    url = json.loads(body)["url"]
    duplicate = _request(server, "POST", "/api/review", token=token, body={})
    assert json.loads(duplicate[2])["url"] == url
    assert loaded == [server.session.config_path]
    assert identities == [(client, owner, "fixture-device")]
    with httpx.Client(trust_env=False) as http:
        response = http.get(url)
        assert response.status_code == 200 and "text/html" in response.headers["content-type"]
        origin = str(response.url).split("/?")[0]
        assert http.get(origin + "/api/items").status_code == 403
        response = http.get(origin + "/api/items", headers={"X-Varyz-Review": "fixture-review-key"})
        assert response.json()["items"][0]["statement"] == "Fixture suggestion"
        assert _request(server, "POST", "/api/close", token=token, body={})[0] == 200
        assert server.closed.is_set() and server.review_active
        assert http.get(url).status_code == 200
        assert (
            http.post(
                origin + "/api/close", headers={"X-Varyz-Review": "fixture-review-key"}
            ).status_code
            == 200
        )
        assert not server.review_active


def test_review_start_failure_is_reported_and_setup_stays_open(server, monkeypatch):
    def fail():
        raise RuntimeError("fixture daemon unavailable")

    monkeypatch.setattr(server, "open_review", fail)
    status, _, body = _request(server, "POST", "/api/review", token=server.session.token, body={})
    assert status == 503 and b"Review is unavailable" in body
    assert not server.closed.is_set()


def test_setup_process_waits_for_an_open_review_to_close(machine, monkeypatch):
    """The review must stay reachable after the setup tab's Close action."""
    snapshot = {
        "finished": True,
        "paused": False,
        "extract": {"state": "accepted", "phase": "done"},
        "result": {"verdict": "GO", "summary": "Ready", "problems": [], "next": []},
    }
    session = SimpleNamespace(
        config_path=machine.config, running=False, extract_accepted=False, snapshot=lambda: snapshot
    )
    server = setup_web.SetupServer(session)
    monkeypatch.setattr(setup_web, "SetupSession", lambda _: session)
    monkeypatch.setattr(setup_web, "SetupServer", lambda _: server)
    monkeypatch.setattr(
        cli,
        "_cfg",
        lambda _: SimpleNamespace(resolution=SimpleNamespace(device_id="fixture-device")),
    )
    monkeypatch.setattr(cli, "_resolution_client", lambda _: object())
    monkeypatch.setattr(cli, "_local_owner_caller", lambda *_: object())
    monkeypatch.setattr(
        review_web,
        "ReviewSession",
        lambda *_: SimpleNamespace(token="fixture-review-key", kept=0, dropped=0),
    )
    session.token = "fixture-setup-key"
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(setup_web.serve(machine.config, open_browser=False)),
        daemon=True,
    )
    thread.start()
    try:
        status, _, body = _request(server, "POST", "/api/review", token=session.token, body={})
        assert status == 200
        url = json.loads(body)["url"]
        assert _request(server, "POST", "/api/close", token=session.token, body={})[0] == 200
        thread.join(1.2)
        assert thread.is_alive(), "Setup must keep its open review alive"
        with httpx.Client(trust_env=False) as http:
            assert http.get(url).status_code == 200
            origin = url.split("/?")[0]
            assert (
                http.post(
                    origin + "/api/close", headers={"X-Varyz-Review": "fixture-review-key"}
                ).status_code
                == 200
            )
        thread.join(4)
        assert outcome == [True] and not thread.is_alive()
    finally:
        server.closed.set()
        if server._review is not None:
            server._review.closed.set()
        thread.join(4)


def test_review_navigation_is_authenticated_and_uses_the_real_session(server, monkeypatch):
    # Keep the actual config loader, identity constructor and review server.
    server.session.config_path.parent.mkdir(parents=True, exist_ok=True)
    server.session.config_path.write_text('[storage]\ndata_dir="data"\n', encoding="utf-8")
    monkeypatch.setattr(
        server.session,
        "snapshot",
        lambda: {
            "finished": True,
            "extract": {"state": "accepted", "phase": "done"},
        },
    )
    origin = server.url.split("/?")[0]
    with httpx.Client(trust_env=False, follow_redirects=False) as http:
        for path in ("/review", "/review?t=wrong"):
            assert http.get(origin + path).status_code == 403
        assert (
            http.get(
                origin + "/review?t=" + server.session.token, headers={"Host": "attacker.example"}
            ).status_code
            == 403
        )
        assert server._review is None
        response = http.get(origin + "/review?t=" + server.session.token)
        assert response.status_code == 303
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        review = server._review
        assert review.session.caller.agent_id == "owner:local"
        assert review.session.caller.purpose == "owner_knowledge"
        assert review.session.device_id == cli._cfg(server.session.config_path).resolution.device_id
        assert response.headers["location"] == review.url
        assert http.get(review.url).status_code == 200
        assert (
            http.get(origin + "/review?t=" + server.session.token).headers["location"] == review.url
        )


def test_review_navigation_failure_stays_visible_with_retry(server, monkeypatch):
    def fail():
        raise RuntimeError("private fixture diagnostic")

    monkeypatch.setattr(server, "open_review", fail)
    status, kind, body = _request(server, "GET", "/review?t=" + server.session.token)
    assert status == 503 and kind.startswith("text/html")
    assert b"Review could not open" in body and b"Try again" in body
    assert b"private fixture diagnostic" not in body and b"<script>" not in body
    assert b"/review?t=" + server.session.token.encode() in body
    assert not server.closed.is_set()


def test_unready_review_navigation_preserves_learning_and_escapes_errors(server, monkeypatch):
    monkeypatch.setattr(
        server.session,
        "snapshot",
        lambda: {
            "finished": True,
            "extract": {"state": "accepted", "phase": "reading"},
        },
    )
    status, _, body = _request(server, "GET", "/review?t=" + server.session.token)
    assert status == 409 and b"Learning is not ready" in body
    assert server._review is None
    rendered = setup_web.render_review_error('a"&<>', '<script>alert("unsafe")</script>')
    assert b"&lt;script&gt;" in rendered and b"<script>alert" not in rendered
    assert b"/review?t=a%22%26%3C%3E" in rendered


def test_idle_page_keepalive_requires_setup_key_and_refreshes_activity(server):
    import time

    assert _request(server, "GET", "/api/ping")[0] == 403
    server.last_request = 0
    status, _, body = _request(server, "GET", "/api/ping", token=server.session.token)
    assert status == 200 and json.loads(body) == {"ok": True}
    assert time.monotonic() - server.last_request < 1


def test_review_link_opens_real_navigation_without_blank_popup():
    page = setup_web.render_page('fixture"&<>').decode("utf-8")
    assert 'href="/review?t=fixture%22%26%3C%3E" target="_blank" rel="noopener"' in page
    assert 'window.open("about:blank"' not in page
    assert 'api("/api/ping")' in page

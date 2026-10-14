"""Actual loopback callbacks return to setup without leaking OAuth credentials."""

import base64
import hashlib
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from varyz.cloud import client


@pytest.mark.parametrize("ticket", [None, "t" * 43])
def test_callback_returns_to_cloud_and_preserves_pkce(monkeypatch, ticket):
    replies = []
    errors = []
    worker = None
    auth = {}

    def visit():
        try:
            with httpx.Client(trust_env=False, follow_redirects=False) as browser:
                invalid = browser.get(client.CALLBACK, params={"state": "wrong", "code": "bad"})
                assert invalid.status_code == 400 and "location" not in invalid.headers
                response = browser.get(client.CALLBACK, params={
                    "state": auth["state"][0], "code": "test-authorization-code",
                })
                replies.append(response)
        except Exception as error:
            errors.append(error)

    def open_browser(url):
        nonlocal worker
        auth.update(parse_qs(urlsplit(url).query))
        worker = threading.Thread(target=visit)
        worker.start()
        return True

    def exchange(url, **options):
        assert url == "https://cloud.example.test/oauth/token"
        form = options["data"]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(form["code_verifier"].encode()).digest(),
        ).decode().rstrip("=")
        assert auth["code_challenge"] == [challenge]
        assert form["code"] == "test-authorization-code"
        assert form["redirect_uri"] == client.CALLBACK
        return httpx.Response(200, json={"access_token": "test-access"})

    monkeypatch.setattr(client.webbrowser, "open", open_browser)
    monkeypatch.setattr(client.httpx, "post", exchange)
    tokens = client.browser_connect("https://cloud.example.test", timeout=5, return_ticket=ticket)
    worker.join(5)
    assert not errors and len(replies) == 1
    assert tokens["access_token"] == "test-access"
    response = replies[0]
    assert response.status_code == 200
    assert 'window.close()' in response.text
    assert 'https://cloud.example.test/' + (
        '#computer=' + ticket if ticket else '#connect-varyz'
    ) in response.text
    assert 'test-authorization-code' not in response.text and 'test-access' not in response.text
    assert "script-src 'nonce-" in response.headers['content-security-policy']
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("ticket", ["https://evil.test", "short", "x" * 43 + "&code=bad"])
def test_untrusted_return_destination_is_rejected(ticket):
    with pytest.raises(ValueError, match="connection ticket"):
        client.browser_connect("https://cloud.example.test", return_ticket=ticket)

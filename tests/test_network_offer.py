"""Consent belongs to setup; old review pages cannot offer or enable sharing."""

import threading

import httpx
import pytest

from varyz import analytics, onboarding
from varyz.config import Config
from varyz.review_web import ReviewServer, ReviewSession, render_page


class _Client:
    def call(self, *_args, **_kwargs):
        return {"items": []}


@pytest.mark.parametrize("sharing", [None, False, True])
def test_review_never_offers_consent_or_changes_existing_choice(tmp_path, sharing):
    cfg = Config(base_dir=tmp_path)
    for n in range(4):
        onboarding.note_conversation(cfg, f"chat-{n}", now=1)
    if sharing is not None:
        analytics.consent(cfg, sharing)
    before = analytics.status(cfg)
    session = ReviewSession(_Client(), None, "device", cfg)
    server = ReviewServer(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.url.split("/?")[0].rstrip("/"),
                          headers={"X-Varyz-Review": session.token}, trust_env=False) as http:
            assert http.post("/api/network/offer", json={}).json() == {"offer": None}
            assert http.post("/api/network/answer", json={"share": True}).status_code == 409
            assert http.post("/api/network/answer", json={"share": "yes"}).status_code == 400
        assert analytics.status(cfg) == before
    finally:
        server.shutdown()
        server.server_close()


def test_review_has_no_delayed_consent_dialog():
    page = render_page("preview").decode()
    assert "analytics-dialog" not in page
    assert "/api/network/offer" not in page
    assert "offerNetwork" not in page

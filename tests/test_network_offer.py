"""The Varyz network popup: offered on rules review after the trial window, never in setup."""

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from varyz import analytics, onboarding
from varyz.config import Config
from varyz.review_web import ReviewServer, ReviewSession

NOW = datetime.now(UTC)
SRC = Path(__file__).resolve().parents[1] / "src" / "varyz"


@pytest.fixture
def cfg(tmp_path):
    return Config(base_dir=tmp_path)


def _past_trial(cfg, conversations=4):
    for n in range(conversations):
        onboarding.note_conversation(cfg, f"chat-{n}", now=NOW.timestamp() - 2 * 86400)


def test_not_offered_during_the_trial_window(cfg):
    assert analytics.claim_offer(cfg, now=NOW) is False  # Nothing seen yet.
    _past_trial(cfg, conversations=3)
    assert analytics.claim_offer(cfg, now=NOW) is False  # Only 3 conversations.
    onboarding.note_conversation(cfg, "chat-new", now=NOW.timestamp())
    assert analytics.claim_offer(cfg, now=NOW) is True


def test_shown_once_then_once_more_after_30_days_and_never_again(cfg):
    _past_trial(cfg)
    assert analytics.claim_offer(cfg, now=NOW) is True
    assert analytics.claim_offer(cfg, now=NOW) is False
    analytics.consent(cfg, False)  # The x or Escape.
    assert analytics.claim_offer(cfg, now=NOW + timedelta(days=29)) is False
    assert analytics.claim_offer(cfg, now=NOW + timedelta(days=31)) is True
    analytics.consent(cfg, False)
    assert analytics.claim_offer(cfg, now=NOW + timedelta(days=400)) is False
    assert analytics.status(cfg)["consent"] is False


def test_a_terminal_no_counts_as_the_first_offer(cfg):
    _past_trial(cfg)
    analytics.consent(cfg, False)  # Terminal setup's default-no prompt.
    assert analytics.claim_offer(cfg, now=NOW) is False
    assert analytics.claim_offer(cfg, now=NOW + timedelta(days=31)) is True
    assert analytics.claim_offer(cfg, now=NOW + timedelta(days=62)) is False


def test_never_offered_to_someone_who_turned_sharing_on(cfg):
    _past_trial(cfg)
    analytics.consent(cfg, True)
    assert analytics.claim_offer(cfg, now=NOW) is False
    analytics.consent(cfg, False)  # Turned it off on purpose: not asked again.
    assert analytics.claim_offer(cfg, now=NOW + timedelta(days=90)) is False


class _Client:
    def call(self, *_args, **_kwargs):
        return {"items": []}


@pytest.fixture
def review(cfg):
    session = ReviewSession(_Client(), None, "device", cfg)
    server = ReviewServer(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=server.url.split("/?")[0].rstrip("/"),
                      headers={"X-Varyz-Review": session.token}, trust_env=False) as http:
        yield http
    server.shutdown()
    server.server_close()


def test_review_offers_once_and_records_the_answer(cfg, review):
    _past_trial(cfg)
    assert review.post("/api/network/answer", json={"share": True}).status_code == 409
    offer = review.post("/api/network/offer", json={}).json()["offer"]
    assert offer["title"] == analytics.CONSENT_TITLE
    assert offer["message"] == analytics.CONSENT_MESSAGE
    assert review.post("/api/network/offer", json={}).json() == {"offer": None}
    assert review.post("/api/network/answer", json={"share": "yes"}).status_code == 400
    assert review.post("/api/network/answer", json={"share": False}).json() == {"sharing": False}
    assert analytics.status(cfg)["consent"] is False


def test_review_opt_in_turns_sharing_on(cfg, review):
    _past_trial(cfg)
    assert review.post("/api/network/offer", json={}).json()["offer"]
    assert review.post("/api/network/answer", json={"share": True}).json() == {"sharing": True}
    assert analytics.status(cfg)["consent"] is True


def test_review_page_has_the_dialog_and_setup_does_not():
    review_page = (SRC / "review_web.html").read_text(encoding="utf-8")
    setup_page = (SRC / "setup_web.html").read_text(encoding="utf-8")
    for needle in ('<dialog id="analytics-dialog"', 'aria-label="Close without sharing"',
                   '"/api/network/offer"', 'addEventListener("cancel"', 'event.key !== "Tab"'):
        assert needle in review_page
    assert "analytics" not in setup_page
    assert chr(0x2014) not in review_page  # No em dashes in copy.

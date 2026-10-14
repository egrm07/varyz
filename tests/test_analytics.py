"""Consent, content exclusion, delivery idempotence and creator authentication."""

import json
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from varyz import analytics
from varyz.cloud import store as cloud_store
from varyz.cloud.app import Settings, create_app
from varyz.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(base_dir=tmp_path)


def source(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.db_path)
    db.executescript("""
    CREATE TABLE resolution_ledger(resolved_at TEXT,disposition TEXT,basis TEXT,
     normalized_question TEXT,purpose TEXT,conversation_id TEXT,request_json TEXT);
    CREATE TABLE resolution_outcomes(recorded_at TEXT,outcome TEXT,source TEXT);
    CREATE TABLE owner_principles(created_at TEXT,status TEXT,supersedes_id TEXT,
     provenance_json TEXT);
    CREATE TABLE owner_principle_retractions(retracted_at TEXT,reason TEXT);
    """)
    db.commit()
    return db


def answer(
    db, *, conversation="one", project="private-project", disposition="clarification", stamp=None
):
    db.execute(
        "INSERT INTO resolution_ledger VALUES(?,?,?,?,?,?,?)",
        (
            stamp or datetime.now(UTC).isoformat(),
            disposition,
            "explicit",
            "private message with alice@example.com",
            "task",
            conversation,
            json.dumps({"project": project}),
        ),
    )
    db.commit()


def receipt(request):
    payload = json.loads(request.content)
    return httpx.Response(200, json={"accepted": payload["report_id"]})


def payload():
    return {
        "schema_version": 1,
        "consent_version": analytics.CONSENT_VERSION,
        "report_id": str(uuid4()),
        "day": datetime.now(UTC).date().isoformat(),
        "version": "0.1.0",
        "os": "Windows",
        "counters": {"setup_ready": 1},
    }


def test_default_off_and_decline_send_nothing(cfg):
    calls = []
    analytics.record(cfg, "setup_started")
    assert not analytics.state_path(cfg).exists()
    analytics.consent(cfg, False)
    assert (
        analytics.sync(cfg, force=True, transport=httpx.MockTransport(calls.append))["status"]
        == "off"
    )
    assert not calls and not analytics.status(cfg)["local_counters"]


def test_preview_excludes_content_history_and_does_not_change_state(cfg):
    db = source(cfg)
    answer(db, stamp="2001-01-01T00:00:00Z")
    analytics.consent(cfg, True)
    answer(db)
    answer(db)  # A retry in the same conversation.
    answer(db, conversation="two")
    answer(db, conversation="three", project="other-project")
    analytics.record(cfg, "setup_ready")
    before = analytics.status(cfg)
    preview = analytics.sync(cfg, dry_run=True)
    assert analytics.status(cfg) == before
    counts = preview["reports"][0]["counters"]
    assert counts["answers_clarification"] == 4
    assert counts["repeated_questions"] == 1
    encoded = json.dumps(preview)
    assert all(
        secret not in encoded for secret in ("private", "alice@", "other-project", "conversation")
    )
    db.close()


def test_retries_keep_same_report_and_delivery_is_not_duplicated(cfg, monkeypatch):
    analytics.consent(cfg, True)
    analytics.record(cfg, "setup_ready")
    seen = []
    monkeypatch.setattr(analytics.time, "sleep", lambda _: None)

    def retry(request):
        seen.append(json.loads(request.content)["report_id"])
        return httpx.Response(503) if len(seen) < 3 else receipt(request)

    assert analytics.sync(cfg, force=True, transport=httpx.MockTransport(retry))["sent"] == 1
    assert len(seen) == 3 and len(set(seen)) == 1
    assert (
        analytics.sync(cfg, force=True, transport=httpx.MockTransport(retry))["status"] == "empty"
    )
    assert len(seen) == 3


def test_failed_delivery_pause_and_revocation(cfg, monkeypatch):
    analytics.consent(cfg, True)
    analytics.record(cfg, "resolution_failed")
    monkeypatch.setattr(analytics.time, "sleep", lambda _: None)
    assert (
        analytics.sync(
            cfg, force=True, transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )["status"]
        == "delivery_failed"
    )
    assert analytics.status(cfg)["pending_reports"] == 1
    assert analytics.sync(cfg)["status"] == "not_due"
    analytics.pause(cfg, True)
    assert analytics.sync(cfg, force=True)["status"] == "paused"
    analytics.consent(cfg, False)
    assert analytics.status(cfg)["pending_reports"] == 0
    assert not analytics.status(cfg)["local_counters"]
    analytics.consent(cfg, True)
    assert analytics.sync(cfg, dry_run=True)["reports"] == []


def test_preference_changes_are_not_errors(cfg):
    db = source(cfg)
    analytics.consent(cfg, True)
    now = datetime.now(UTC).isoformat()
    db.executemany(
        "INSERT INTO resolution_outcomes VALUES(?,?,?)",
        [
            (now, "overridden", "supersession"),
            (now, "corrected", "owner_feedback:wrong_scope"),
            (now, "adopted", "owner_feedback:other"),
        ],
    )
    db.execute(
        "INSERT INTO owner_principle_retractions VALUES(?,?)",
        (now, "dropped in a spot-check of trial rules"),
    )
    db.commit()
    counters = analytics.sync(cfg, dry_run=True)["reports"][0]["counters"]
    assert counters == {
        "outcome_overridden": 1,
        "outcome_corrected": 1,
        "outcome_adopted": 1,
        "trial_dropped": 1,
        "correction_wrong_scope": 1,
    }
    db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"counters": {"messages": 1}},
        {"counters": {"setup_ready": True}},
        {"counters": {"setup_ready": -1}},
        {"email": "private@example.com"},
        {"os": "secret hostname"},
        {"version": "private/path"},
        {"day": "2000-01-01"},
    ],
)
def test_schema_refuses_private_and_invalid_data(change):
    with pytest.raises(ValidationError):
        analytics.AggregateReport.model_validate(payload() | change)


@pytest.fixture
def receiver(tmp_path):
    return TestClient(
        create_app(
            Settings(
                database=str(tmp_path / "cloud.db"),
                analytics_enabled=True,
                analytics_admin_token="owner-secret",
            )
        ),
        base_url="http://127.0.0.1:8787",
    )


def test_receiver_deduplicates_and_protects_summary(receiver):
    report = payload()
    for _ in range(2):
        assert receiver.post("/api/analytics", json=report).json() == {
            "accepted": report["report_id"]
        }
    assert (
        receiver.post("/api/analytics", json=report | {"counters": {"setup_ready": 2}}).status_code
        == 409
    )
    assert receiver.get("/api/analytics/summary").status_code == 401
    summary = receiver.get(
        "/api/analytics/summary", headers={"Authorization": "Bearer owner-secret"}
    ).json()
    assert summary["reports"] == 1 and summary["counters"] == {"setup_ready": 1}
    assert "owner-secret" not in json.dumps(summary)


def test_receiver_refuses_content_and_rate_limits(receiver, monkeypatch):
    # Exercise one complete window even when Windows requests are slow or the
    # test starts just before a real minute boundary. Only the store clock moves.
    clock = {"now": int(time.time()) // 60 * 60 + 30}
    monkeypatch.setattr(cloud_store, "time", SimpleNamespace(time=lambda: clock["now"]))
    bad = receiver.post("/api/analytics", json=payload() | {"message": "private text"})
    assert bad.status_code == 422 and "private text" not in bad.text
    for _ in range(5):
        assert receiver.post("/api/analytics", json=payload()).status_code == 200
    assert receiver.post("/api/analytics", json=payload()).status_code == 429
    clock["now"] += 60
    assert receiver.post("/api/analytics", json=payload()).status_code == 200


def test_delivery_end_to_end(cfg, receiver):
    analytics.consent(cfg, True)
    analytics.record(cfg, "setup_ready")

    def send(request):
        response = receiver.post(
            request.url.path, content=request.content, headers={"content-type": "application/json"}
        )
        return httpx.Response(response.status_code, content=response.content)

    assert analytics.sync(cfg, force=True, transport=httpx.MockTransport(send))["sent"] == 1
    assert analytics.doctor(cfg)["delivery_verified"]


def test_server_receipt_disabled_by_default(tmp_path):
    client = TestClient(
        create_app(Settings(database=str(tmp_path / "off.db"))), base_url="http://127.0.0.1:8787"
    )
    assert client.post("/api/analytics", json=payload()).status_code == 503


def test_probes_do_not_contaminate_usage_summary(receiver):
    assert (
        receiver.post("/api/analytics", json=payload() | {"sample_kind": "probe"}).status_code
        == 200
    )
    summary = receiver.get(
        "/api/analytics/summary", headers={"Authorization": "Bearer owner-secret"}
    ).json()
    assert summary["probes"] == 1 and summary["reports"] == 0 and summary["counters"] == {}


def test_receiver_report_survives_restart(tmp_path):
    settings = Settings(
        database=str(tmp_path / "persist.db"),
        analytics_enabled=True,
        analytics_admin_token="owner-secret",
    )
    report = payload()
    for _ in range(2):
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8787") as client:
            assert client.post("/api/analytics", json=report).status_code == 200
            summary = client.get(
                "/api/analytics/summary", headers={"Authorization": "Bearer owner-secret"}
            ).json()
            assert summary["reports"] == 1


def test_unknown_source_outcomes_remain_unknown(cfg):
    db = source(cfg)
    analytics.consent(cfg, True)
    answer(db, disposition="answered")
    counts = analytics.sync(cfg, dry_run=True)["reports"][0]["counters"]
    assert "outcome_adopted" not in counts and "outcome_corrected" not in counts
    db.close()

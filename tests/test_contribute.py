"""Network contributions: opt-in, fail-closed scrubbing, delivery and deletion."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from varyz import analytics, contribute
from varyz.cloud.app import Settings, create_app
from varyz.config import Config


def now():
    return datetime.now(UTC).isoformat()
KEY = "sk-ant-" + "a" * 32


@pytest.fixture
def cfg(tmp_path):
    return Config(base_dir=tmp_path)


def archive(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.db_path)
    db.executescript("""
    CREATE TABLE people(id TEXT,canonical_name TEXT);
    CREATE TABLE person_aliases(person_id TEXT,alias_type TEXT,alias_value TEXT);
    CREATE TABLE owner_knowledge(id TEXT PRIMARY KEY,answer TEXT);
    CREATE TABLE resolution_ledger(id TEXT PRIMARY KEY,question TEXT,answer TEXT,basis TEXT,
     tier INTEGER,confidence REAL,caller_agent_id TEXT,withheld_json TEXT,request_json TEXT,
     resolved_at TEXT);
    CREATE TABLE resolution_outcomes(id TEXT,ledger_id TEXT,outcome TEXT,source TEXT,
     knowledge_id TEXT,recorded_at TEXT);
    CREATE TABLE resolution_escalations(id TEXT,question TEXT,caller_agent_id TEXT,
     tried_tiers_json TEXT,urgency TEXT,status TEXT,answer_knowledge_id TEXT,created_at TEXT,
     answered_at TEXT);
    CREATE TABLE owner_principles(id TEXT,statement TEXT,kind TEXT,scope TEXT,status TEXT,
     supersedes_id TEXT,caller_agent_id TEXT,created_at TEXT);
    CREATE TABLE owner_principle_retractions(id TEXT,principle_id TEXT,reason TEXT,
     retracted_at TEXT);
    INSERT INTO people VALUES('p1','Alice Moreau');
    """)
    db.commit()
    return db


def correction(db, question, *, withheld="[]", stamp=None, fix="Use pnpm, never npm"):
    ledger, knowledge = str(uuid4()), str(uuid4())
    db.execute("INSERT INTO owner_knowledge VALUES(?,?)", (knowledge, fix))
    db.execute(
        "INSERT INTO resolution_ledger VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ledger, question, "Use npm", "inferred", 3, 0.4, "claude-code", withheld,
         json.dumps({"project": "secret-launch"}), stamp or now()),
    )
    db.execute(
        "INSERT INTO resolution_outcomes VALUES(?,?,?,?,?,?)",
        (str(uuid4()), ledger, "corrected", "owner_feedback:wrong_preference", knowledge,
         stamp or now()),
    )
    db.commit()


def test_nothing_is_built_or_sent_without_opt_in(cfg):
    db = archive(cfg)
    correction(db, "Which package manager?")
    calls = []
    assert contribute.preview(cfg)["records"] == []
    analytics.consent(cfg, False)
    sent = contribute.sync(cfg, force=True, transport=httpx.MockTransport(calls.append))
    assert sent["status"] == "off" and not calls


def test_counts_only_consent_never_covers_contributions(cfg):
    db = archive(cfg)
    with analytics.connection(cfg) as state:
        state.execute("INSERT INTO state VALUES('consent',?)", (analytics.LEGACY_CONSENT,))
        state.execute("INSERT INTO state VALUES('since','2000-01-01')")
    correction(db, "Which package manager?")
    assert analytics.status(cfg)["counts"] and not analytics.status(cfg)["consent"]
    assert contribute.preview(cfg)["status"] == "off"


def test_preview_scrubs_identifiers_and_drops_what_cannot_be_cleaned(cfg):
    db = archive(cfg)
    correction(db, "Which package manager?", stamp="2001-01-01T00:00:00+00:00")
    analytics.consent(cfg, True)
    correction(
        db,
        "Should Alice Moreau get the secret-launch build at alice@example.com, "
        r"see C:\Users\egor\repo\notes.md and https://intranet.example/x?",
    )
    correction(db, f"Is {KEY} the deploy key?")
    correction(db, "What did the withheld note say?", withheld='[{"id":"x"}]')
    correction(db, "Fine question", fix=f"Use {KEY}")
    db.execute("INSERT INTO owner_principles VALUES('r1','Never ship on Fridays','rule','*',"
               "'confirmed',NULL,'codex',?)", (now(),))
    db.execute("INSERT INTO owner_principles VALUES('v1','secret-launch is a CRM','vision',"
               "'*','confirmed',NULL,'codex',?)", (now(),))
    db.commit()
    before = contribute.status(cfg)
    preview = contribute.preview(cfg)
    assert contribute.status(cfg) == before  # Preview changes nothing.
    records = preview["records"]
    assert [r["kind"] for r in records] == ["correction", "rule"]
    text = json.dumps(preview)
    for secret in ("Alice", "Moreau", "alice@", "secret-launch", "egor", "intranet", KEY,
                   "withheld note", "CRM"):
        assert secret not in text
    fixed = records[0]
    assert "[person]" in fixed["question"] and "[project]" in fixed["question"]
    assert "[email]" in fixed["question"] and "[path]" in fixed["question"]
    assert "[link]" in fixed["question"]
    assert fixed["correction"] == "Use pnpm, never npm" and fixed["agent"] == "claude"
    assert records[1] | {"record_id": ""} == records[1] | {
        "record_id": "", "answer": "Never ship on Fridays", "outcome": "confirmed",
        "rule_scope": "general", "agent": "codex"}


@pytest.fixture
def receiver(tmp_path):
    return TestClient(
        create_app(Settings(database=str(tmp_path / "cloud.db"), contributions_enabled=True,
                            analytics_admin_token="owner-secret")),
        base_url="http://127.0.0.1:8787",
    )


def relay(receiver):
    def send(request):
        response = receiver.request(
            request.method, request.url.path, content=request.content,
            headers={k: v for k, v in request.headers.items()
                     if k.lower() in {"authorization", "content-type"}},
        )
        return httpx.Response(response.status_code, content=response.content)

    return httpx.MockTransport(send)


def test_delivery_then_turning_off_deletes_from_server(cfg, receiver):
    db = archive(cfg)
    analytics.consent(cfg, True)
    correction(db, "Which package manager?")
    owner = {"Authorization": "Bearer owner-secret"}
    assert contribute.sync(cfg, force=True, transport=relay(receiver))["sent"] == 1
    assert contribute.status(cfg)["pending"] == 0
    assert receiver.get("/api/contributions/summary", headers=owner).json()["records"] == 1
    exported = receiver.get("/api/contributions/export", headers=owner).json()["records"]
    assert exported[0]["question"] == "Which package manager?"
    assert "contributor" not in json.dumps(exported)

    analytics.consent(cfg, False)
    assert contribute.status(cfg)["erase_pending"] == 1
    assert contribute.erase(cfg, transport=relay(receiver))["status"] == "erased"
    assert receiver.get("/api/contributions/summary", headers=owner).json()["records"] == 0
    assert contribute.status(cfg)["erase_pending"] == 0


def test_receiver_requires_key_refuses_extra_fields_and_guards_export(receiver):
    batch = {
        "batch_id": str(uuid4()), "version": "0.1.0",
        "records": [{"record_id": str(uuid4()), "kind": "rule", "agent": "codex",
                     "outcome": "confirmed", "day": datetime.now(UTC).date().isoformat(),
                     "answer": "Prefer small commits"}],
    }
    key = {"Authorization": "Bearer " + "k" * 43}
    assert receiver.post("/api/contributions", json=batch).status_code == 401
    bad = receiver.post("/api/contributions", headers=key,
                        json=batch | {"email": "private text"})
    assert bad.status_code == 422 and "private text" not in bad.text
    for _ in range(2):  # A retried batch stores once.
        ok = receiver.post("/api/contributions", headers=key, json=batch).json()
    assert ok == {"accepted": batch["batch_id"], "stored": 0}
    assert receiver.get("/api/contributions/export").status_code == 401
    assert receiver.delete("/api/contributions", headers=key).json() == {"deleted": 1}


def test_receipt_off_by_default_but_deletion_still_works(tmp_path):
    client = TestClient(create_app(Settings(database=str(tmp_path / "off.db"))),
                        base_url="http://127.0.0.1:8787")
    key = {"Authorization": "Bearer " + "k" * 43}
    assert client.post("/api/contributions", headers=key, json={}).status_code == 503
    assert client.delete("/api/contributions", headers=key).json() == {"deleted": 0}

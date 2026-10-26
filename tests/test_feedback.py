"""Feedback agents send to the Varyz developer, from the agent's report to the inbox."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from varyz import feedback
from varyz.config import Config

KEY = "sk-ant-" + "a" * 32
OWNER = {"Authorization": "Bearer owner-secret"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # The real doctor takes a second and looks at this machine; its result is what matters.
    monkeypatch.setattr(feedback, "_doctor", lambda cfg: (22, ["cloud sync"]))
    return Config(base_dir=tmp_path)


@pytest.fixture
def receiver(tmp_path):
    # The receiver is Varyz Cloud's; the public install tests the sending side only.
    pytest.importorskip("varyz.cloud.feedback")
    from fastapi.testclient import TestClient

    from varyz.cloud import app as cloud

    return TestClient(
        cloud.create_app(cloud.Settings(database=str(tmp_path / "cloud.db"),
                                        analytics_admin_token="owner-secret")),
        base_url="http://127.0.0.1:8787",
    )


def relay(receiver):
    def send(request):
        response = receiver.request(
            request.method, request.url.raw_path.decode(), content=request.content,
            headers={k: v for k, v in request.headers.items()
                     if k.lower() in {"authorization", "content-type"}},
        )
        return httpx.Response(response.status_code, content=response.content)

    return httpx.MockTransport(send)


def offline():
    def send(request):
        raise httpx.ConnectError("offline", request=request)

    return httpx.MockTransport(send)


def archive(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.db_path)
    db.executescript("CREATE TABLE people(id TEXT,canonical_name TEXT);"
                     "INSERT INTO people VALUES('p1','Alice Moreau');"
                     "CREATE TABLE owner_principles(scope TEXT);"
                     "INSERT INTO owner_principles VALUES('acme-billing'),('varyz');")
    db.commit()
    return db


def report(**fields):
    return {
        "feedback_id": str(uuid4()), "day": datetime.now(UTC).date().isoformat(),
        "title": "Varyz context timed out", "details": "The context tool timed out twice.",
        "version": "0.1.4", "os": "Windows",
    } | fields


def test_a_report_is_scrubbed_sent_and_the_user_is_told(cfg, receiver):
    db = archive(cfg)
    result = feedback.send(
        cfg, title="Varyz kept a CPU core busy",
        details="Alice Moreau saw it in acme-billing at C:\\Users\\alice\\work after mail to "
                "alice@example.com",
        expected="Varyz stays quiet when idle", area="speed", agent="codex",
        model="gpt-6.1-sol", archive=db, transport=relay(receiver),
    )
    assert result["status"] == "sent"
    assert result["tell_user"] == "Sent feedback to Varyz: Varyz kept a CPU core busy."
    inbox = feedback.inbox("owner-secret", transport=relay(receiver))
    stored = json.dumps(inbox)
    for private in ("Alice", "Moreau", "acme-billing", "alice@example.com", "C:\\\\Users"):
        assert private not in stored
    # The user's project named after Varyz does not hide what the report is about.
    assert inbox[0]["title"] == "Varyz kept a CPU core busy"
    assert inbox[0]["feedback_id"] == result["feedback_id"]
    assert inbox[0]["doctor_checks"] == 22 and inbox[0]["doctor_failing"] == ["cloud sync"]
    assert inbox[0]["agent"] == "codex" and inbox[0]["area"] == "speed"
    assert feedback.status(cfg)["sent"] == 1


def test_credentials_are_refused_on_this_computer_and_by_the_server(cfg, receiver):
    with pytest.raises(ValueError, match="password or key") as refused:
        feedback.send(cfg, title="Login failed", details=f"Cloud said {KEY} was wrong",
                      transport=relay(receiver))
    assert KEY not in str(refused.value)
    assert feedback.status(cfg)["waiting"] == 0
    key = {"Authorization": "Bearer " + "k" * 43}
    response = receiver.post("/api/feedback", headers=key,
                             json=report(details=f"Cloud said {KEY} was wrong"))
    assert response.status_code == 422 and KEY not in response.text


def test_a_report_that_cannot_be_sent_waits_for_the_daemon(cfg, receiver, monkeypatch):
    result = feedback.send(cfg, title="Varyz Cloud rejected sync", details="Sync said 507.",
                           transport=offline())
    assert result["status"] == "queued"
    assert result["tell_user"].startswith("Saved feedback for Varyz;")
    state = feedback.status(cfg)
    assert state["waiting"] == 1 and state["last_error"] == "delivery_failed"

    attempts = threading.Event()
    monkeypatch.setattr(feedback, "flush", lambda cfg: attempts.set())
    monkeypatch.setattr(feedback, "_last_tick", -1e9)
    feedback.tick(cfg)  # The failed delivery set a retry time an hour away.
    assert not attempts.wait(0.2)
    with feedback.connection(cfg) as db:
        feedback._put(db, "next_attempt", "0")
    monkeypatch.setattr(feedback, "_last_tick", -1e9)
    feedback.tick(cfg)
    assert attempts.wait(2)
    monkeypatch.undo()

    assert feedback.flush(cfg, transport=relay(receiver)) == {result["feedback_id"]}
    state = feedback.status(cfg)
    assert state["waiting"] == 0 and state["sent"] == 1 and state["last_error"] == ""


def test_one_problem_goes_once_a_week_and_each_day_is_capped(cfg, monkeypatch):
    first = feedback.send(cfg, title="Varyz context timed out", details="Twice in one task.",
                          transport=offline())
    again = feedback.send(cfg, title="varyz: Context timed out!", details="And again.",
                          transport=offline())
    assert again["status"] == "already_sent" and again["feedback_id"] == first["feedback_id"]
    assert again["tell_user"] == ""
    monkeypatch.setattr(feedback, "DAILY_LIMIT", 1)
    capped = feedback.send(cfg, title="Rules page was blank", details="It showed nothing.",
                           transport=offline())
    assert capped["status"] == "limit" and capped["tell_user"] == ""
    assert feedback.status(cfg)["waiting"] == 1


def test_off_drops_what_was_waiting_and_sends_nothing(cfg):
    feedback.send(cfg, title="Varyz context timed out", details="Twice in one task.",
                  transport=offline())
    feedback.set_enabled(cfg, False)
    state = feedback.status(cfg)
    assert state["on"] is False and state["waiting"] == 0

    def never(request):
        raise AssertionError("nothing may be sent while feedback is off")

    off = feedback.send(cfg, title="Rules page was blank", details="It showed nothing.",
                        transport=httpx.MockTransport(never))
    assert off["status"] == "off" and off["tell_user"] == ""
    assert feedback.flush(cfg, transport=httpx.MockTransport(never)) == set()
    feedback.set_enabled(cfg, True)
    assert feedback.status(cfg)["on"] is True


def test_the_server_keeps_one_copy_deletes_by_key_and_only_the_owner_reads(receiver):
    key = {"Authorization": "Bearer " + "k" * 43}
    body = report()
    assert receiver.post("/api/feedback", json=body).status_code == 401
    bad = receiver.post("/api/feedback", headers=key, json=body | {"email": "private text"})
    assert bad.status_code == 422 and "private text" not in bad.text
    for _ in range(2):  # A retried report stores once.
        assert receiver.post("/api/feedback", headers=key, json=body).json() == {
            "accepted": body["feedback_id"]}
    assert receiver.get("/api/feedback").status_code == 401
    assert receiver.get("/api/feedback", headers=key).status_code == 401
    inbox = receiver.get("/api/feedback", headers=OWNER).json()
    assert inbox["total"] == 1 and len(inbox["reports"][0]["reporter"]) == 12
    assert "k" * 43 not in json.dumps(inbox)
    assert receiver.delete("/api/feedback", headers=key).json() == {"deleted": 1}
    assert receiver.get("/api/feedback", headers=OWNER).json()["total"] == 0


def test_forget_deletes_what_this_computer_sent(cfg, receiver):
    feedback.send(cfg, title="Varyz context timed out", details="Twice in one task.",
                  transport=relay(receiver))
    assert feedback.forget(cfg, transport=relay(receiver)) == {"status": "deleted",
                                                               "deleted": 1}
    assert receiver.get("/api/feedback", headers=OWNER).json()["total"] == 0
    assert feedback.status(cfg)["sent"] == 0


def test_the_command_line_files_a_report_and_never_echoes_a_key(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from varyz.feedback_cli import app

    monkeypatch.setattr(feedback, "_doctor", lambda cfg: (22, []))
    monkeypatch.setattr(feedback, "flush", lambda cfg, transport=None: set())
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    runner = CliRunner()
    refused = runner.invoke(app, ["send", "Login failed", "--details", f"Cloud said {KEY}",
                                  "-c", str(config)])
    assert refused.exit_code == 1 and KEY not in refused.output
    queued = runner.invoke(app, ["send", "Varyz froze", "--details", "It froze twice.",
                                 "-c", str(config)])
    assert queued.exit_code == 0
    result = json.loads(queued.output)
    assert result["status"] == "queued" and result["report"]["agent"] == "claude"
    assert json.loads(runner.invoke(app, ["status", "-c", str(config)]).output)["waiting"] == 1

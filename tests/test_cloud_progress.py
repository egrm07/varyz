# ruff: noqa: F811
import json

from tests.test_cloud_archive import hosted  # noqa: F401
from tests.test_cloud_session_reports import call, permit, plugin_auth, report, upload


def write(client, headers, **overrides):
    args = dict(project="varyz", conversation_id="cloud-session", checkpoint_id="one",
                agent="agent:codex:hosted", task="Finish checkpoint handover",
                done=["Added Cloud checkpoints"], pending=["Run tests"])
    args.update(overrides)
    return call(client, headers, args, name="record_progress")


def decoded(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_hosted_write_immutable_retry_and_account_isolation(hosted):
    client, store, auth = hosted
    headers = plugin_auth(store)
    assert write(client, headers).get("isError")
    permit(client, auth())
    first = decoded(write(client, headers))
    assert first["recorded"] and not first["duplicate"]
    assert decoded(write(client, headers))["duplicate"]
    assert write(client, headers, done=["Different content"]).get("isError")
    result = decoded(call(client, headers, dict(project="varyz", agent="codex")))
    assert result["reports"][0]["reference"] == first["reference"]
    other = plugin_auth(store, "bob")
    assert decoded(call(client, other, dict(project="varyz")))["found"] == 0
    assert not decoded(write(client, headers, sensitivity="sensitive"))["recorded"]


def test_open_threads_claim_pickup_and_completion(hosted):
    client, store, auth = hosted
    permit(client, auth())
    headers = plugin_auth(store)
    first = decoded(write(client, headers))
    brief = decoded(call(client, headers, dict(project="varyz", task="Finish checkpoint handover",
                                               conversation_id="new"), name="start_task"))
    thread = brief["open_threads"]["threads"][0]
    assert thread["reference"] == first["reference"]
    root = thread["thread_id"]
    args = dict(project="varyz", thread_id=root, conversation_id="new")
    assert decoded(call(client, headers, args, name="claim_work"))["claimed"]
    conflict = decoded(call(client, headers, {**args, "conversation_id": "competitor"},
                            name="claim_work"))
    assert conflict["conflict"]
    assert write(client, headers, thread_id=root, conversation_id="competitor").get("isError")
    decoded(write(client, headers, thread_id=root, conversation_id="new"))
    done = decoded(write(client, headers, thread_id=root, conversation_id="new",
                         checkpoint_id="complete", status="complete", pending=[]))
    assert done["recorded"]
    brief = decoded(call(client, headers, dict(project="varyz", task="checkpoint handover"),
                         name="start_task"))
    assert brief["open_threads"]["threads"] == []
    assert call(client, headers, {**args, "conversation_id": "competitor"},
                name="claim_work").get("isError")


def test_thread_root_survives_new_snapshot_and_revocation(hosted):
    client, store, auth = hosted
    permit(client, auth())
    headers = plugin_auth(store)
    root = decoded(write(client, headers))["reference"]
    decoded(write(client, headers, checkpoint_id="two"))
    brief = decoded(call(client, headers, dict(project="varyz", task="checkpoint handover"),
                         name="start_task"))
    assert brief["open_threads"]["threads"][0]["thread_id"] == root
    args = dict(project="varyz", thread_id=root, conversation_id="new")
    assert decoded(call(client, headers, args, name="claim_work"))["claimed"]
    client.post("/api/reports/revoke", headers=auth(), json={"project": "varyz"})
    assert write(client, headers, thread_id=root, conversation_id="new").get("isError")
    assert call(client, headers, args, name="claim_work").get("isError")


def test_claim_expiry_and_closed_snapshot_does_not_reopen(hosted):
    client, store, auth = hosted
    permit(client, auth())
    headers = plugin_auth(store)
    root = decoded(write(client, headers))["reference"]
    args = dict(project="varyz", thread_id=root, conversation_id="new")
    decoded(call(client, headers, args, name="claim_work"))
    with store.connection() as db:
        db.execute("UPDATE session_work_claims SET expires='2000-01-01T00:00:00+00:00'")
    assert decoded(call(client, headers, {**args, "conversation_id": "next"},
                        name="claim_work"))["claimed"]
    decoded(write(client, headers, checkpoint_id="complete", status="complete", pending=[]))
    brief = decoded(call(client, headers, dict(project="varyz", task="checkpoint handover"),
                         name="start_task"))
    assert brief["open_threads"]["threads"] == []


def test_uploaded_reports_discoverable_without_resume_word(hosted):
    client, store, auth = hosted
    permit(client, auth())
    assert upload(client, auth(), report()).status_code == 200
    result = decoded(call(client, plugin_auth(store),
                          dict(project="varyz", task="Finish SEO metadata"), name="start_task"))
    assert result["open_threads"]["threads"][0]["task"] == "Improve SEO metadata"



def test_mined_report_cannot_replace_live_work_and_local_api_uses_same_lease(hosted):
    client, store, auth = hosted
    permit(client, auth())
    assert upload(client, auth(), report()).status_code == 200
    mined = report("mined", status="complete", pending=[], provenance="mined_report",
                   evidence={"version": "1", "response_reference": "synthetic"},
                   occurred_at="2026-10-08T12:00:00Z")
    assert upload(client, auth(), mined).status_code == 200
    headers = plugin_auth(store)
    result = decoded(call(client, headers, dict(project="varyz", task="SEO metadata"),
                          name="start_task"))
    root = result["open_threads"]["threads"][0]["thread_id"]
    args = dict(project="varyz", conversation_id="local-pickup", thread_id=root)
    claimed = client.post("/api/reports/claim", headers=auth(), json=args)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    assert decoded(call(client, headers, {**args, "conversation_id": "hosted-pickup"},
                        name="claim_work"))["conflict"]
    readonly = auth(readonly=True)
    assert client.post("/api/reports/claim", headers=readonly, json=args).status_code == 403
    payload = {**args, "checkpoint_id": "local-finish", "task": "Finish SEO metadata",
               "status": "complete", "pending": [], "agent": "agent:codex:local"}
    assert client.post("/api/reports/checkpoint", headers=auth(), json=payload).json()["recorded"]
    assert decoded(call(client, headers, dict(project="varyz", task="SEO metadata"),
                        name="start_task"))["open_threads"]["threads"] == []



def test_bulk_upload_keeps_pause_read_in_write_transaction(hosted, monkeypatch):
    from contextlib import contextmanager

    from varyz.cloud.session_reports import Change

    client, store, auth = hosted
    permit(client, auth())
    original = store.connection

    @contextmanager
    def small_cache():
        with original() as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA cache_size=1")
            yield db

    monkeypatch.setattr(store, "connection", small_cache)
    reports = client.app.state.session_reports
    changes = [Change(id="bulk" + str(i), base_version=0,
                      payload=report("bulk" + str(i), done=[str(i) + "x" * 170] * 6,
                                     pending=["p" * 170] * 6, checks=["c" * 170] * 6))
               for i in range(25)]
    assert len(reports.push("alice", changes)) == 25



def test_preferences_only_credential_cannot_read_or_write_reports(hosted):
    client, store, auth = hosted
    permit(client, auth())
    upload(client, auth(), report())
    token = store.credential("alice", "access", 3600, "preferences-client", scope="varyz")
    headers = {"Authorization": "Bearer " + token}
    brief = decoded(call(client, headers, dict(project="varyz", task="SEO metadata"),
                         endpoint="/mcp", name="start_task"))
    assert brief["open_threads"] == {"status": "access_required", "threads": []}
    args = dict(project="varyz", conversation_id="forbidden", checkpoint_id="one", task="SEO")
    assert call(client, headers, args, endpoint="/mcp", name="record_progress").get("isError")

# ruff: noqa: F811
"""Actual hosted write/read boundaries refuse detected prohibited records safely."""

import json

import pytest

from tests.test_cloud_archive import hosted, payload, push  # noqa: F401
from tests.test_cloud_session_reports import permit, report, upload
from varyz.cloud.archive import PLUGIN_SCOPE
from varyz.cloud.hosted_data import HostedDataRejected, require_hosted_safe
from varyz.cloud.payloads import canonical_payload
from varyz.cloud.sharing_service import SharingError, clean


@pytest.mark.parametrize("value", [
    "My SSN is 123-45-6789",
    {"passport_number": "N12345678"},
    {"bsn": "123456782"},
    "national ID: AB1234567",
    "I was diagnosed with diabetes",
    {"patient_name": "Alice Example", "diagnosis": "diabetes"},
    {"full_name": "Alice Example", "medical_history": "cancer"},
    "My 15-year-old daughter goes to Example School",
    "Our son is 15 years old",
    "Alice is 14 years old and lives at Example Street",
    "I am 12 years old",
    {"child_name": "Alice Example", "age": 15},
    {"email": "child@example.test", "age": "8"},
    "SSN%3A%20123-45-6789",
    "ＢＳＮ：１２３４５６７８２",
    "Card number: 4242 4242 4242 4242",
])
def test_explicit_prohibited_records(value):
    with pytest.raises(HostedDataRejected) as error:
        require_hosted_safe(value)
    # A safe reason, never a reflection of submitted private values.
    assert "123-45-6789" not in str(error.value)
    assert "Alice" not in str(error.value)


@pytest.mark.parametrize("value", [
    "Write general documentation about diabetes and medication reminders",
    "The diagnosis field must be encrypted",
    "Implement a child-protection policy; minimum age is 16",
    "My 16-year-old daughter prefers concise writing",
    "I am 21 years old",
    "Explain SSN and passport privacy requirements",
    "Use commit 2e0a4b872bc56eadfad64183d7f56c303c7e8205",
    "This database is 14 years old",
    "A fictional patient is treated for a condition in this tutorial",
    {"properties": {"patient_name": {"type": "string"}, "diagnosis": {"type": "string"}}},
    [{"full_name": "Alice Example"}, {"diagnosis": "Unrelated general schema"}],
])
def test_benign_general_discussion(value):
    require_hosted_safe(value)


@pytest.mark.parametrize("value", [
    "My religion is Buddhist",
    "My sexual orientation is bisexual",
    "I am gay.",
    "I'm a Muslim and prefer brief answers",
    "My political affiliation is Example Party",
    "My political opinions are socialist",
    "I am a member of a trade union.",
    "My trade union membership is Example Union",
    "My ethnicity is Example ethnicity",
    "My genetic profile is ACTGACTG",
    "My fingerprint template is template-example-123",
    {"full_name": "Alice Example", "religion": "Buddhist"},
    {"email": "person@example.test", "sexual_orientation": "bisexual"},
    {"full_name": "Alice Example", "political_opinion": "Example Party"},
    {"full_name": "Alice Example", "union_membership": "Example Union"},
    {"full_name": "Alice Example", "dna_sequence": "ACTGACTG"},
    {"full_name": "Alice Example", "biometric_identifier": "template-example-123"},
])
def test_explicit_special_category_personal_records(value):
    with pytest.raises(HostedDataRejected, match="special-category") as error:
        require_hosted_safe(value)
    assert "Alice" not in str(error.value)
    assert "Example Party" not in str(error.value)


@pytest.mark.parametrize("value", [
    "Write a guide to political opinions and religious freedom",
    "Discuss trade union membership and workplace rights",
    "Build a database schema for genetic profiles and biometric identifiers",
    "Explain why sexual orientation is special-category data",
    "I am conservative with the retry budget",
    "I am Christian Smith, the developer",
    "I use fingerprint authentication",
    "My religion is private",
    "My sexual orientation is prefer not to say",
    {"full_name": "Alice Example", "religion": "undisclosed"},
    {"email": "person@example.test", "political_opinion": "not disclosed"},
    {"properties": {"religion": {"type": "string"}, "full_name": {"type": "string"}}},
    [{"full_name": "Alice Example"}, {"religion": "Example schema value"}],
])
def test_special_category_topics_and_undisclosed_fields_remain_available(value):
    require_hosted_safe(value)


def test_special_category_guard_reaches_existing_preference_write_boundary():
    with pytest.raises(HostedDataRejected, match="special-category"):
        canonical_payload("rule", {"statement": "My religion is Buddhist", "scope": "varyz"})


def test_scanner_limits_are_fail_closed():
    with pytest.raises(HostedDataRejected, match="safety scan limit"):
        require_hosted_safe("a" * 1_000_001)
    value = "fine"
    for _ in range(32):
        value = [value]
    with pytest.raises(HostedDataRejected, match="nesting"):
        require_hosted_safe(value)


def test_sharing_writes_use_the_same_boundary():
    with pytest.raises(SharingError) as error:
        clean({"answer": "My SSN is 123-45-6789"})
    assert error.value.status == 422
    assert error.value.code == "prohibited_data_detected"
    assert "123-45-6789" not in str(error.value.envelope())


def rpc(client, auth, name, arguments, endpoint="/mcp/plugin"):
    response = client.post(endpoint, headers=auth, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    return response.json()["result"]


def plugin_auth(store):
    return {"Authorization": "Bearer " + store.credential(
        "alice", "access", 3600, "chatgpt", scope=PLUGIN_SCOPE,
    )}


def test_preference_archive_report_ingress(hosted):
    client, store, auth = hosted
    text = "My BSN: 123456782"
    with pytest.raises(HostedDataRejected):
        canonical_payload("rule", {"statement": text, "scope": "varyz"})
    client.post("/api/archive/permit", headers=auth(), json={"project": "varyz"})
    data = payload()
    data["text"] = text
    data["chunks"] = [{"text": text}]
    response = client.post("/api/archive/sync", headers=auth(), json={
        "records": [{"id": "one", "base_version": 0, "payload": data}],
    })
    assert response.status_code == 422
    assert "123456782" not in response.text
    permit(client, auth())
    response = upload(client, auth(), report(task=text))
    assert response.status_code == 422
    assert "123456782" not in response.text
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM archive_documents").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM cloud_session_reports").fetchone()[0] == 0


def test_mcp_prewrite_guard_and_benign_health_request(hosted):
    client, store, _ = hosted
    auth = plugin_auth(store)
    result = rpc(client, auth, "remember_owner_rule", {
        "statement": "My SSN is 123-45-6789", "scope": "varyz",
    })
    assert result["isError"]
    assert "government identifier" in str(result)
    assert "123-45-6789" not in str(result)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    result = rpc(client, auth, "start_task", {
        "request": "Write general documentation about diabetes", "project": "varyz",
    })
    assert not result.get("isError"), result


def test_legacy_preference_egress_is_explicitly_withheld(hosted):
    client, store, _ = hosted
    # Simulate pre-guard history; do not ask the current ingress to permit it.
    with store.connection() as db:
        db.execute(
            "INSERT INTO records(account,id,kind,version,payload,cursor) VALUES(?,?,?,?,?,?)",
            ("alice", "legacy", "rule", 1, json.dumps({
                "statement": "My BSN: 123456782", "scope": "varyz", "kind": "rule",
                "rationale": "", "domain": "",
            }), 1),
        )
    for name, args in (("context", {"project": "varyz"}),
                       ("start_task", {"request": "Help with my project", "project": "varyz"})):
        result = rpc(client, plugin_auth(store), name, args)
        assert result["isError"], result
        assert "government identifier" in str(result)
        assert "123456782" not in str(result)
    # Guard is non-destructive. Existing data remains available for owner export/removal.
    with store.connection() as db:
        assert "123456782" in db.execute("SELECT payload FROM records").fetchone()[0]


@pytest.mark.parametrize(("text", "private_value"), [
    ("My BSN: 123456782", "123456782"),
    ("My religion is Buddhist", "Buddhist"),
    ("My political affiliation is Example Party", "Example Party"),
])
def test_legacy_archive_report_source_pages_and_mcp_do_not_leak(hosted, text, private_value):
    client, store, auth = hosted
    client.post("/api/archive/permit", headers=auth(), json={"project": "varyz"})
    assert push(client, auth(), payload()).status_code == 200
    permit(client, auth())
    assert upload(client, auth(), report()).status_code == 200
    archive = client.app.state.session_reports.archive
    doc = payload()
    doc["text"] = text
    doc["chunks"] = [{"text": text}]
    with store.connection() as db:
        db.execute("UPDATE archive_documents SET payload=?", (archive.seal(json.dumps(doc)),))
        db.execute("UPDATE cloud_session_reports SET payload=?", (
            archive.seal(json.dumps(report(task=text))),
        ))
    for name, args in (("fetch", {"id": "one"}),
                       ("session_history", {"project": "varyz"})):
        result = rpc(client, plugin_auth(store), name, args)
        assert result["isError"], result
        assert private_value not in str(result)
    for url in ("/archive/one", "/reports/one"):
        response = client.get(url, headers=plugin_auth(store))
        assert response.status_code == 404
        assert private_value not in response.text
    # Data rights operations are preserved; no silent destruction or inaccessible export.
    assert private_value in str(archive.export("alice"))
    assert private_value in str(client.app.state.session_reports.export("alice"))

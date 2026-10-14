"""Activation proof uses real local rules and real hosted MCP, without writes."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from varyz.cloud.activation import verify_rule  # noqa: E402
from varyz.cloud.app import Settings, create_app  # noqa: E402
from varyz.cloud.client import CloudClient  # noqa: E402
from varyz.cloud.local_sync import selected_payloads  # noqa: E402
from varyz.db.connection import open_db  # noqa: E402
from varyz.resolution.models import Caller  # noqa: E402
from varyz.resolution.principles import SQLitePrincipleStore  # noqa: E402


@pytest.fixture
def activation(tmp_path):
    local = open_db(tmp_path / "local.sqlite")
    rule = SQLitePrincipleStore(local).add(
        statement="Use short replies",
        scope="demo",
        source="owner",
        device_id="test",
        caller=Caller(
            agent_id="owner:test",
            display_name="Owner",
            purpose="owner_knowledge",
            device_id="test",
            transport="local",
        ),
    )
    payload = selected_payloads(local)["rule:" + rule]
    app = create_app(Settings(database=str(tmp_path / "cloud.sqlite")))
    store = app.state.store
    with store.connection() as db:
        db.execute(
            "INSERT INTO accounts(id,email,password,status,email_verified) "
            "VALUES('account','test@example.test','unused','active',1)"
        )
    store.push("account", [{"kind": "rule", "id": rule, "base_version": 0, "payload": payload}])
    journal = {"enabled": True, "account_id": "account", "projects": ["demo"], "records": {}}
    with TestClient(app, base_url="http://127.0.0.1:8787") as http:
        client = CloudClient(
            "http://127.0.0.1:8787",
            {"access_token": store.credential("account", "access", 3600, "varyz-local")},
            http=http,
        )
        yield local, journal, client, rule, store
    local.close()


def test_actual_hosted_rule_verified_without_writes_or_phone_claim(activation):
    local, journal, client, rule, store = activation
    before = json.dumps(journal, sort_keys=True)
    with store.connection() as db:
        count = db.execute("SELECT count(*) FROM changes").fetchone()[0]
    result = verify_rule(local, journal, client, rule, "demo")
    assert result["hosted_rule_verified"] is True
    assert result["phone_verified"] is result["uploaded"] is False
    assert "Use short replies" not in json.dumps(result)
    assert json.dumps(journal, sort_keys=True) == before
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == count


@pytest.mark.parametrize("case", ["disabled", "wrong-account", "inactive", "wrong-project"])
def test_activation_boundaries(activation, case):
    local, journal, client, rule, store = activation
    project = "demo"
    if case == "disabled":
        journal["enabled"] = False
    elif case == "wrong-account":
        journal["account_id"] = "someone-else"
    elif case == "inactive":
        with store.connection() as db:
            db.execute("UPDATE accounts SET status='inactive'")
    else:
        project = "other"
    with pytest.raises(ValueError):
        verify_rule(local, journal, client, rule, project)


@pytest.mark.parametrize("payload", [None, {"statement": "Use longer replies", "scope": "demo"}])
def test_missing_or_changed_rule_is_not_success(activation, payload):
    local, journal, client, rule, store = activation
    store.push("account", [{"kind": "rule", "id": rule, "base_version": 1, "payload": payload}])
    with pytest.raises(ValueError, match="not returned"):
        verify_rule(local, journal, client, rule, "demo")


def test_cloud_rule_import_alias_is_verified(activation):
    local, journal, client, rule, store = activation
    payload = selected_payloads(local)["rule:" + rule]
    store.push(
        "account",
        [
            {"kind": "rule", "id": rule, "base_version": 1, "payload": None},
            {"kind": "rule", "id": "remote-rule", "base_version": 0, "payload": payload},
        ],
    )
    journal["records"]["rule:remote-rule"] = {"local_id": rule}
    assert verify_rule(local, journal, client, rule, "demo")["hosted_rule_verified"]

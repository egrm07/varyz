# ruff: noqa: F811
"""Promise behavior through hosted MCP routes with synthetic tenant accounts."""

import json

from tests.test_cloud_archive import hosted  # noqa: F401
from tests.test_cloud_session_reports import permit, plugin_auth, report, upload
from tests.test_hosted_plugin_capabilities import rpc


def call(client, headers, name, args):
    response = rpc(client, headers, "/mcp/plugin", "tools/call", name, args)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_hosted_continuation_uses_only_approved_current_project_reports(hosted):
    client, store, auth = hosted
    permit(client, auth())
    assert (
        upload(
            client, auth(), report(status="blocked", pending=["Domain verification"])
        ).status_code
        == 200
    )
    headers = plugin_auth(store)
    result = call(
        client,
        headers,
        "start_task",
        {
            "project": "varyz",
            "task": "Continue the last Claude session",
            "conversation_id": "new",
        },
    )
    assert result["continuation"]["status"] == "recorded"
    assert result["continuation"]["reports"][0]["pending"] == ["Domain verification"]
    assert not result["continuation"]["independently_verified"]
    other = call(
        client,
        plugin_auth(store, "bob"),
        "start_task",
        {
            "project": "varyz",
            "task": "Resume last session",
            "conversation_id": "new",
        },
    )
    assert other["continuation"]["reports"] == []
    assert (
        client.post("/api/reports/revoke", headers=auth(), json={"project": "varyz"}).status_code
        == 200
    )
    result = call(client, headers, "start_task", {"project": "varyz", "task": "Resume"})
    assert result["continuation"]["reports"] == []


def test_hosted_reuses_clear_choice_and_requires_completion_evidence(hosted):
    client, store, auth = hosted
    response = client.post(
        "/api/sync",
        headers=auth(),
        json={
            "records": [
                {
                    "kind": "answer",
                    "id": "accent",
                    "base_version": 0,
                    "payload": {
                        "question": "Which accent colour should Acorn use?",
                        "answer": "Amber",
                        "scope": "acorn",
                    },
                },
                {
                    "kind": "rule",
                    "id": "favicon",
                    "base_version": 0,
                    "payload": {
                        "statement": "Include an SVG favicon.",
                        "scope": "acorn",
                        "domain": "website",
                    },
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    headers = plugin_auth(store)
    result = call(
        client,
        headers,
        "ask",
        {
            "project": "acorn",
            "question": "Which highlight hue belongs on our product?",
        },
    )
    assert result["answer"] == "Amber"
    result = call(
        client,
        headers,
        "ask",
        {
            "project": "acorn",
            "question": "What background hue should Acorn use?",
        },
    )
    assert result.get("answer") != "Amber"
    result = call(client, headers, "finish_check", {"project": "acorn"})
    assert result["verification"]["unchecked"] == ["favicon"]
    result = call(
        client,
        headers,
        "finish_check",
        {
            "project": "acorn",
            "evidence": [
                {"rule_id": "favicon", "status": "passed", "evidence": "Inspected rendered HTML"},
            ],
        },
    )
    assert result["verification"]["ready_to_report"]
    assert not result["verification"]["independently_verified"]

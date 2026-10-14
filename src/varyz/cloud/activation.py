"""Read-only proof that a selected rule is available through hosted MCP."""

from __future__ import annotations

import json
from typing import Any

from varyz.cloud.local_sync import fingerprint, selected_payloads
from varyz.resolution.knowledge import normalize_scope


def verify_rule(conn, journal: dict[str, Any], client, rule_id: str, project: str) -> dict:
    """Verify the actual hosted projection; never select, upload or record a phone claim."""
    project = normalize_scope(project)
    if not journal.get("enabled") or not journal.get("account_id"):
        raise ValueError("Connect Cloud and explicitly select confirmed knowledge first.")
    local_key = "rule:" + rule_id
    payload = selected_payloads(conn, journal.get("projects", [])).get(local_key)
    if payload is None or payload["scope"] not in {"*", project}:
        raise ValueError("Choose a confirmed selected rule that applies to this project.")
    account = client.request("GET", "/api/account")
    if not isinstance(account, dict):
        raise ValueError("Cloud did not return a valid account status.")
    if account.get("id", account.get("account_id")) != journal["account_id"]:
        raise ValueError("Cloud account differs from the selected sync account; reconnect safely.")
    if account.get("entitlement") is not True:
        raise ValueError("Cloud access is inactive. Check your existing account status.")
    remote_id = rule_id
    for key, entry in journal["records"].items():
        if key.startswith("rule:") and entry.get("local_id") == rule_id:
            remote_id = key.split(":", 1)[1]
            break
    response = client.request(
        "POST",
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "start_task", "arguments": {"project": project}},
        },
    )
    try:
        result = response["result"]
        if result.get("isError"):
            raise ValueError
        value = json.loads(result["content"][0]["text"])
        rules = value["rules"]
        matches = [r for r in rules if r["id"] == remote_id and r["kind"] == "rule"]
        valid = len(matches) == 1 and fingerprint(matches[0]["payload"]) == fingerprint(payload)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("Hosted MCP did not return a valid rule verification response.") from exc
    if not valid:
        raise ValueError("The current rule was not returned by hosted MCP. Sync and retry.")
    return {
        "hosted_rule_verified": True,
        "rule_id": rule_id,
        "project": project,
        "phone_verified": False,
        "uploaded": False,
        "next_step": "In a supported phone tool, connect the same account and ask it to "
        "apply this project's rules to a real task. Check the actual returned rule.",
    }

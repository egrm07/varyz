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
    valid = False
    for page in range(1, 51):
        # Send only the existing project selector and page number. Local rule
        # text stays on this computer; compare the hosted response locally.
        response = client.request(
            "POST",
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": page,
                "method": "tools/call",
                "params": {"name": "start_task", "arguments": {
                    "project": project, "rules_page": page,
                }},
            },
        )
        try:
            result = response["result"]
            if result.get("isError"):
                raise ValueError
            value = json.loads(result["content"][0]["text"])
            rules = value["rules"]
            if not isinstance(rules, list):
                raise ValueError
            matches = [r for r in rules if r["id"] == remote_id
                       or remote_id in r.get("same_as", [])]
            if matches:
                valid = len(matches) == 1 and _matches_payload(matches[0], payload)
                break
            if not value.get("more_rules_in_scope"):
                break
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "Hosted MCP did not return a valid rule verification response."
            ) from exc
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


def _matches_payload(rule: dict, payload: dict) -> bool:
    if "payload" in rule:  # Older hosted versions returned storage records.
        return rule["kind"] == "rule" and fingerprint(rule["payload"]) == fingerprint(payload)
    # Current briefings expose only the advice fields. Verify those fields as
    # they are delivered to agents, including omitted defaults for general rules.
    projection = {
        "statement": rule["statement"],
        "scope": rule.get("scope", "*"),
        "rationale": rule.get("why", ""),
        "domain": rule.get("domain", ""),
        "kind": payload["kind"],
    }
    return fingerprint(projection) == fingerprint(payload)

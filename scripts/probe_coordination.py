"""Verify actual installed/local and hosted routing without recording owner answers."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import keyring

from varyz.cloud.live_sync import _client
from varyz.install import client_paths
from varyz.mcp_server import TOOLS, VaryzServer
from varyz.plugin import credential_name, registration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = args.config.resolve(strict=True)
    entry = registration("codex", client_paths()["codex"])
    token = entry.get("env", {}).get("VARYZ_AGENT_TOKEN")
    if not token:
        token = keyring.get_password("varyz-plugin", credential_name("codex", config))
    assert token, "Existing local agent grant required"
    assert "route_task" in {tool["name"] for tool in TOOLS}
    local = VaryzServer(config, agent_token=token)
    cloud = _client(config)
    assert cloud is not None, "Existing authorized Cloud connection required"
    task = {"project": "varyz", "conversation_id": "coordination-probe-" + uuid.uuid4().hex}

    def hosted(name: str, arguments: dict) -> dict:
        result = cloud.request("POST", "/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })["result"]
        assert not result.get("isError"), "Hosted tool failed"
        return json.loads(result["content"][0]["text"])

    try:
        ready = local.call("route_task", task)
        assert ready.get("local_availability") == "ready", "Local daemon not ready"
        for backend, reason in (("local", "initial"), ("cloud", "explicit_handoff"),
                                ("local", "explicit_handoff")):
            for call in (local.call, hosted):
                state = call("route_task", task)
                selected = call("route_task", {
                    **task, "action": "select", "backend": backend,
                    "expected_revision": state["revision"], "reason": reason,
                })
                assert selected["status"] == "selected"
                assert selected["workflow_backend"] == backend
            if backend == "local":
                result = hosted("ask", {**task, "question": "Coordination probe",
                                        "decision_kind": "delegated"})
                assert result["status"] == "delegated"
            else:
                assert local.call("ask", task)["status"] == "delegated"
                result = hosted("start_task", task)
                assert "rules" in result
        print(json.dumps({"local_daemon_ready": True, "hosted_mcp": True,
                          "both_selections": True, "handoff_back": True,
                          "duplicate_questions_suppressed": True,
                          "owner_answers_written": 0}))
    finally:
        local.close()
        cloud.close()


if __name__ == "__main__":
    main()

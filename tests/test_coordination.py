# ruff: noqa: F811
"""Actual MCP calls enforce one workflow owner without changing grants or history."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_cloud_archive import hosted  # noqa: F401
from varyz.coordination import guard, route
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer
from varyz.resolution.briefing import finish_check, start_task
from varyz.trust import register_agent

TASK = {"project": "demo", "conversation_id": "task-1"}


def rpc(client: Any, headers: dict[str, str], name: str, args: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def local(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("varyz.doctor.session_notice", lambda *args, **kwargs: None)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndata_dir="{(tmp_path / "data").as_posix()}"\n'
        '[update]\nmode="off"\n[resolution]\nautostart=false\n', encoding="utf-8")
    server = VaryzServer(
        config, agent_token="", resolution_client=SimpleNamespace(status=lambda: {}))
    # Install-equivalent grant; no live account or private archive is used.
    registration = register_agent(
        server.conn, agent_id="agent:codex:test", display_name="Test Codex")
    server._agent_token = registration.token
    try:
        yield server
    finally:
        server.close()


def test_local_selected_suppresses_cloud_questions_and_writes(local, hosted):
    client, store, auth = hosted
    selection = {**TASK, "action": "select", "backend": "local"}
    assert local.call("route_task", selection)["workflow_backend"] == "local"
    assert rpc(client, auth(), "route_task", selection)["workflow_backend"] == "local"
    for name, extras in (
        ("start_task", {}), ("ask", {"question": "Which colour?"}),
        ("remember_owner_rule", {"statement": "Prefer red", "scope": "*"}),
        ("finish_check", {}),
    ):
        assert rpc(client, auth(), name, {**TASK, **extras})["status"] == "delegated"
    with store.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM questions").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM records").fetchone()["n"] == 0
    # Other tenants and tasks are unaffected.
    assert "rules" in rpc(client, auth("bob"), "start_task", TASK)
    assert "rules" in rpc(client, auth(), "start_task", {**TASK, "conversation_id": "other"})


def test_cloud_selected_suppresses_local_workflow_and_hooks(local):
    assert local.call("route_task", {**TASK, "action": "select", "backend": "cloud"})[
        "status"] == "selected"
    for name in ("start_task", "ask", "finish_check", "owner_questions", "remember_owner_rule"):
        # Suppression precedes execution, so it cannot accidentally write or ask.
        assert local.call(name, TASK)["status"] == "delegated"
    for briefing in (start_task, finish_check):
        result = briefing(local.conn, "Build demo", project="demo", conversation_id="task-1")
        assert result["status"] == "delegated"
        assert not result["definition_of_done"]


def test_handoff_requires_revision_and_safe_reason(local):
    selection = {**TASK, "action": "select", "backend": "local"}
    first = local.call("route_task", selection)
    assert local.call("route_task", selection)["revision"] == first["revision"]
    assert local.call("route_task", {**selection, "backend": "cloud"})["status"] == "conflict"
    assert local.call("route_task", {
        **selection, "backend": "cloud", "expected_revision": 1,
        "reason": "transport_unavailable",
    })["revision"] == 2
    assert local.call("route_task", {
        **selection, "expected_revision": 1, "reason": "explicit_handoff",
    })["status"] == "conflict"
    with pytest.raises(ValueError, match="not a handoff reason"):
        local.call("route_task", {**selection, "reason": "withheld"})


def test_routes_survive_restart_and_do_not_block_history(tmp_path):
    path = tmp_path / "local.sqlite"
    with open_db(path) as db:
        route(db, "local", {**TASK, "action": "select", "backend": "cloud"}, "local")
    with open_db(path) as db:
        assert guard(db, "local", TASK, "local", "ask")["status"] == "delegated"
        assert guard(db, "local", TASK, "local", "recall") is None
        assert guard(db, "local", TASK, "local", "context") is None


def test_legacy_calls_and_unregistered_clients(local):
    assert local.call("route_task", TASK)["selected"] is False
    assert "how_owner_works" in local.call("start_task", {
        "task": "Explain demo", "project": "demo"})
    local._agent_token = ""
    assert local.call("route_task", {**TASK, "action": "select", "backend": "cloud"})[
        "error"] == "routing_requires_registered_client"


def test_outage_handshake_does_not_allow_access_failure_fallback(local):
    from varyz.resolution.client import ArchiveMismatchError, DaemonUnavailableError

    def unavailable():
        raise DaemonUnavailableError("connection refused")

    local._resolution_client_override = SimpleNamespace(status=unavailable)
    assert local.call("route_task", TASK)["fallback_allowed"] is True
    assert local.call("route_task", {**TASK, "action": "select", "backend": "local"})[
        "status"] == "unavailable"

    def denied():
        raise ArchiveMismatchError("different archive")

    local._resolution_client_override = SimpleNamespace(status=denied)
    assert local.call("route_task", TASK)["fallback_allowed"] is False


def test_handoff_preserves_originating_pending_question(hosted):
    client, store, auth = hosted
    pending = rpc(client, auth(), "ask", {**TASK, "question": "Which colour?"})
    rpc(client, auth(), "route_task", {**TASK, "action": "select", "backend": "local"})
    reply = rpc(client, auth(), "answer_owner_question", {
        **TASK, "question_id": pending["question_id"], "answer": "Blue", "scope": "demo",
    })
    assert reply["status"] == "recorded"
    assert store.knowledge("alice", "demo")[0]["payload"]["answer"] == "Blue"

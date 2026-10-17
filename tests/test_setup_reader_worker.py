"""Setup workers read synthetic history and queue evidence-backed candidates offline."""

from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

from tests.test_profile_reader import (
    SHOP_MODEL,
    FakeModel,
    _agent_server,
    _queue_only,
    _shop,
    daemon_client,  # noqa: F401
)
from varyz import cli
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution import headless_reader as reader
from varyz.resolution import local_reader, rule_reader
from varyz.resolution.profile_reader import profile_path
from varyz.resolution.usage import Usage, Window


def test_local_worker_waits_for_model_then_queues_real_candidates(
    tmp_path, daemon_client, monkeypatch  # noqa: F811
):
    _queue_only(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n'
        '[workflows]\ninference_mode = "local"\n'
        '[connectors.ai_conversations]\napproved = true\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    conn = open_db(cfg.db_path)
    _shop(conn)
    conn.close()
    model = FakeModel(SHOP_MODEL)
    checks = []

    def ready(cfg):
        checks.append(len(model.calls))
        return len(checks) > 1

    monkeypatch.setattr(local_reader, "model_ready", ready)
    monkeypatch.setattr(local_reader.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: daemon_client)
    monkeypatch.setattr(rule_reader, "reader_from_config", lambda *a, **kw: model)
    local_reader.run(path, wait_model=True)
    assert checks == [0, 0]
    assert local_reader.load_status(cfg)["state"] == "idle"
    assert local_reader.work_left(cfg) == (0, 0)
    candidates = daemon_client.principles("candidate")
    assert {item["provenance"]["slot"] for item in candidates} == {"rule", "vision", "workflow"}
    assert daemon_client.principles("confirmed") == ()


def test_cloud_supervisor_reads_through_the_real_mcp_tools_and_reaches_idle(
    tmp_path, daemon_client, monkeypatch  # noqa: F811
):
    from varyz.mcp_server import handle

    _queue_only(tmp_path)
    server = _agent_server(tmp_path, daemon_client)
    cfg = server.cfg
    _shop(server.conn)
    monkeypatch.setenv(reader.ENV, "synthetic-run")
    try:
        initialized = handle(server, {"id": 0, "method": "initialize"})
        assert "start_task" not in initialized["result"]["instructions"]
        listed = handle(server, {"id": 1, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == set(reader.TOOLS)
        denied = handle(server, {"id": 2, "method": "tools/call", "params": {
            "name": "remember_owner_rule", "arguments": {"rule": "Unapproved"}}})
        assert denied["error"]["code"] == -32601
        reader.update_status(cfg, lambda s: s.update(pending=["full"], setup_runner="codex"))
        model = FakeModel(SHOP_MODEL)

        def launch(command, env, cwd):
            assert profile_path(cfg, "cloud").parent == cwd
            for _ in range(10):
                task = server.call("profile_reading_task", {})
                if task.get("done"):
                    break
                reply = model.complete(task["instructions"], task["text"])
                result = server.call("submit_profile_reading", {
                    "task_id": task["task_id"], "items": json.loads(reply)["items"]})
                assert "error" not in result
            else:
                raise AssertionError("reader failed to finish synthetic history")
            return SimpleNamespace(pid=999, stdout=io.StringIO(""), poll=lambda: 0)

        now = time.time()
        found = Usage("codex", Window(0, now + 3600), Window(0, now + 604800), now, "test")
        monkeypatch.setattr(reader, "codex_models", lambda *a: ["synthetic-model"])
        status = reader.supervise(cfg, tmp_path / "config.toml",
                                  runners={"codex": reader.Runner("codex", "fake", "fake-token")},
                                  read_usage=lambda name: found, launch=launch)
        assert status["state"] == "idle" and status["pending"] == []
        assert model.calls
        assert daemon_client.principles("candidate")
        assert daemon_client.principles("confirmed") == ()
    finally:
        server.close()

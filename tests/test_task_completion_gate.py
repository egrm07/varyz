"""Completion follows the current task, including non-edit work in native Codex."""

import json
from contextlib import closing

import pytest

from varyz import hooks
from varyz.config import Config
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer
from varyz.resolution.briefing import finish_check
from varyz.resolution.task_completion import current
from varyz.session_progress import Checkpoint, pause_path, record
from varyz.trust import register_agent


def _checkpoint(conn, *, status="working", pending=None, checkpoint="one", **fields):
    return record(conn, Checkpoint(
        project="acorn", conversation_id="task-a", checkpoint_id=checkpoint,
        task="Get the application ready for launch", status=status,
        pending=["Finish remaining installer acceptance"] if pending is None else pending,
        **fields,
    ), agent_id="agent:codex:test")


def _codex(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


def _transcript(tmp_path, *events):
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    return str(path)


def _prompt():
    return _codex("message", role="user", content=[{"type": "input_text", "text": "Resume"}])


@pytest.mark.parametrize("kind", ["function_call", "custom_tool_call"])
@pytest.mark.parametrize("tool", ["apply_patch", "mcp__functions__apply_patch"])
def test_codex_edit_requires_matching_successful_finish_result(tmp_path, kind, tool):
    edit = _codex(kind, name=tool, call_id="edit", input="patch")
    check = _codex("function_call", name="mcp__varyz__finish_check", call_id="finish")
    failed = _codex("function_call_output", call_id="finish", output=json.dumps({
        "verification": {"ready_to_report": False},
    }))
    assert hooks.turn_activity(_transcript(tmp_path, _prompt(), edit, check)) == (True, False)
    assert hooks.turn_activity(
        _transcript(tmp_path, _prompt(), edit, check, failed)
    ) == (True, False)
    passed = _codex("function_call_output", call_id="finish", output=json.dumps({
        "content": [{"type": "text", "text": json.dumps({
            "verification": {"ready_to_report": True},
        })}],
    }))
    assert hooks.turn_activity(
        _transcript(tmp_path, _prompt(), edit, check, passed)
    ) == (True, True)
    assert hooks.turn_activity(
        _transcript(tmp_path, _prompt(), check, edit, passed)
    ) == (True, False)


def test_codex_duplicate_user_event_does_not_erase_activity(tmp_path):
    transcript = _transcript(tmp_path, _prompt(), _codex(
        "custom_tool_call", name="apply_patch", call_id="edit",
    ), {"type": "event_msg", "payload": {"type": "user_message", "message": "Resume"}})
    assert hooks.turn_activity(transcript) == (True, False)
    assert hooks.turn_activity(_transcript(tmp_path, _prompt())) == (False, False)


def test_failed_latest_check_cannot_be_replaced_by_older_success(tmp_path):
    events = [_prompt(), _codex("function_call", name="apply_patch", call_id="edit")]
    for identity in ("old", "new"):
        events.append(_codex("function_call", name="mcp__varyz__finish_check", call_id=identity))
    for identity, ready in (("new", False), ("old", True)):
        events.append(_codex("function_call_output", call_id=identity,
                             output=json.dumps({"verification": {"ready_to_report": ready}})))
    assert hooks.turn_activity(_transcript(tmp_path, *events)) == (True, False)


def test_empty_standards_do_not_finish_working_task(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        _checkpoint(conn)
        check = finish_check(conn, "Prepare launch", project="acorn", conversation_id="task-a",
                             cfg=cfg)
    assert check["verification"]["ready_to_report"]  # No reusable standards recorded.
    assert check["task_completion"]["continue_work"]
    assert not hooks._completion_ready(check)
    assert check["task_completion"]["pending"] == ["Finish remaining installer acceptance"]


def test_readonly_task_repompt_is_bounded_per_checkpoint_not_task_text(tmp_path, monkeypatch):
    cfg = Config(base_dir=tmp_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    state_file = hooks._state_path(cfg, "task-a")
    hooks._save_state(state_file, {"task": "Resume", "project": "acorn", "cwd": str(tmp_path),
                                   "gated": "old-task-hash"})
    event = {"session_id": "task-a", "transcript_path": _transcript(tmp_path, _prompt())}
    with closing(open_db(cfg.db_path)) as conn:
        _checkpoint(conn)
    first = hooks.stop(cfg, event)
    assert first["decision"] == "block"
    assert "Finish remaining installer acceptance" in first["reason"]
    assert "Get the application ready for launch" in first["reason"]
    assert hooks.stop(cfg, event) is None  # No unchanged-checkpoint retry storm.
    with closing(open_db(cfg.db_path)) as conn:
        _checkpoint(conn, checkpoint="two", pending=["Verify installation shortcut"])
    second = hooks.stop(cfg, event)
    assert second["decision"] == "block"
    assert "Verify installation shortcut" in second["reason"]
    assert "Finish remaining installer acceptance" not in second["reason"]
    assert hooks.stop(cfg, {**event, "stop_hook_active": True}) is None


@pytest.mark.parametrize("status,pending", [
    ("blocked", ["Owner must review publisher legal declarations"]),
    ("complete", []), ("abandoned", []),
])
def test_handoffs_and_closed_tasks_do_not_restart(tmp_path, monkeypatch, status, pending):
    cfg = Config(base_dir=tmp_path)
    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    hooks._save_state(hooks._state_path(cfg, "task-a"), {"task": "Resume", "project": "acorn"})
    with closing(open_db(cfg.db_path)) as conn:
        _checkpoint(conn, status=status, pending=pending)
        assert not current(conn, "acorn", "task-a", cfg=cfg)["continue_work"]
    assert hooks.stop(cfg, {"session_id": "task-a"}) is None


def test_exact_task_scope_and_privacy_preserve_newest_state(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        _checkpoint(conn)
        assert current(conn, "acorn", "other")["status"] == "not_recorded"
        assert current(conn, "other", "task-a")["status"] == "not_recorded"
        assert current(conn, "acorn", None)["status"] == "task_scope_required"
        _checkpoint(conn, checkpoint="private", sensitivity="sensitive")
        assert current(conn, "acorn", "task-a", cfg=cfg) == {
            "status": "withheld", "continue_work": False,
        }
        pause_path(cfg.data_dir).touch()
        assert current(conn, "acorn", "task-a", cfg=cfg)["status"] == "paused"


def test_working_without_checklist_is_not_reported_complete(tmp_path):
    cfg = Config(base_dir=tmp_path)
    with closing(open_db(cfg.db_path)) as conn:
        _checkpoint(conn, pending=[])
        state = current(conn, "acorn", "task-a", cfg=cfg)
    assert not state["continue_work"]  # No invented work or automatic retry loop.
    assert not state["ready_to_report"]
    assert "record the unfinished work" in state["how"]
    assert hooks._completion_ready({"task_completion": state,
                                    "verification": {"ready_to_report": True}}) is False


def test_error_wrappers_never_count_as_successful_finish():
    for error in ("isError", "is_error"):
        assert hooks._completion_ready({error: True, "structuredContent": {
            "verification": {"ready_to_report": True},
        }}) is False


def test_mcp_completion_uses_current_config_registered_caller_and_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir="{(tmp_path / "data").as_posix()}"\n')
    server = VaryzServer(path, agent_token="")
    try:
        _checkpoint(server.conn)
        args = {"task": "Finish launch", "project": "acorn", "conversation_id": "task-a"}
        result = server.call("finish_check", args)
        assert result["task_completion"]["status"] == "registered_agent_required"
        assert "installer acceptance" not in json.dumps(result)
        credential = register_agent(server.conn, agent_id="agent:test", display_name="Test")
        server._agent_token = credential.token
        assert server.call("finish_check", args)["task_completion"]["continue_work"]
        with server.conn:
            server.conn.execute("UPDATE agent_capabilities SET max_chars=200 WHERE agent_id=?",
                                ("agent:test",))
        result = server.call("finish_check", args)
        assert result["task_completion"]["status"] == "budget_exceeded"
        assert "installer acceptance" not in json.dumps(result)
        with server.conn:
            server.conn.execute("UPDATE agent_capabilities SET max_chars=4000 WHERE agent_id=?",
                                ("agent:test",))
        # A changed archive path is detected after server startup, not silently
        # combined with the old open database and its reports.
        path.write_text(f'[storage]\ndata_dir="{(tmp_path / "other").as_posix()}"\n')
        result = server.call("finish_check", args)
        assert result["error"] == "archive configuration changed; reconnect Varyz"
        assert "installer acceptance" not in json.dumps(result)
    finally:
        server.close()

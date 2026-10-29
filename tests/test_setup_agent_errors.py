"""Setup reports connection failures independently from successful-agent restarts."""

import pytest

from tests.test_setup_web import _finished, machine  # noqa: F401
from varyz.install import InstallResult
from varyz.setup_web import SetupSession


@pytest.mark.parametrize("reason", ["skipped - invalid JSON in Claude settings",
                                    "not installed at the expected path"])
def test_skipped_connection_names_agent_and_reason(machine, monkeypatch, reason):  # noqa: F811
    monkeypatch.setattr("varyz.install.connect_agent", lambda *a, **k:
                        (None, [InstallResult("claude-code", machine.config, False, reason)]))
    session = SetupSession(machine.config)
    assert session.start(["claude-code"], []) is None
    result = _finished(session)["result"]
    assert result["verdict"] == "NO-GO"
    assert result["problems"] == [["Claude Code", reason.removeprefix("skipped - ")]]
    assert "Claude Code:" in result["summary"]
    assert reason.removeprefix("skipped - ") in result["summary"]
    assert result["connected_agents"] == []
    assert not result["restart"]
    assert not any(line.startswith("Restart") for line in result["next"])


def test_connection_exception_is_not_replaced_with_generic_agents_error(machine, monkeypatch):  # noqa: F811
    def connect(*a, **kw):
        raise ValueError("Claude settings could not be read")

    monkeypatch.setattr("varyz.install.connect_agent", connect)
    session = SetupSession(machine.config)
    session.start(["claude-code"], [])
    result = _finished(session)["result"]
    assert result["summary"] == "Claude Code: Claude settings could not be read"
    assert result["problems"] == [["Claude Code", "Claude settings could not be read"]]


def test_restart_needed_after_success_is_not_a_setup_failure(machine, monkeypatch):  # noqa: F811
    monkeypatch.setattr("varyz.install.connect_agent", lambda *a, **k:
                        (None, [InstallResult("claude-code", machine.config, True,
                                              "MCP settings written; restart Claude Code")]))
    session = SetupSession(machine.config)
    session.start(["claude-code"], [])
    result = _finished(session)["result"]
    assert result["verdict"] == "GO"
    assert result["problems"] == []
    assert result["connected_agents"] == ["Claude Code"]
    assert result["restart"]
    assert result["next"][0] == "Restart Claude Code so it loads Varyz."
    # Feedback is on by default, so setup says so once, with the way to stop it.
    assert "varyz feedback off" in result["feedback"]


def test_skipped_connection_has_no_feedback_note(machine, monkeypatch):  # noqa: F811
    monkeypatch.setattr("varyz.install.connect_agent", lambda *a, **k:
                        (None, [InstallResult("claude-code", machine.config, False,
                                              "not installed at the expected path")]))
    session = SetupSession(machine.config)
    session.start(["claude-code"], [])
    assert _finished(session)["result"]["feedback"] is None


def test_partial_connection_identifies_failure_and_only_restarts_success(machine, monkeypatch):  # noqa: F811
    codex = machine.home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text("", encoding="utf-8")

    def connect(client, **kw):
        if client == "codex":
            return None, [InstallResult(client, codex, False,
                                        "skipped - Codex was not found on PATH; install Codex CLI")]
        return None, [InstallResult(client, machine.config, True,
                                    "configured; restart Claude Code")]

    monkeypatch.setattr("varyz.install.connect_agent", connect)
    session = SetupSession(machine.config)
    session.start(["claude-code", "codex"], [])
    result = _finished(session)["result"]
    assert result["verdict"] == "NO-GO"
    assert result["connected_agents"] == ["Claude Code"]
    assert result["problems"] == [["Codex", "Codex was not found on PATH; install Codex CLI"]]
    assert result["summary"].startswith("Codex:")
    assert result["next"][0] == "Restart Claude Code so it loads Varyz."

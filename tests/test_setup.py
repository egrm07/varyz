"""`varyz setup`, `varyz sources` and the archive check every daemon client makes.

Nothing here starts a daemon process, reads the real home directory or
downloads a model: the daemon, the agent clients, the doctor and the embedder
are replaced, and the discovery of local sources is handed a folder under
tmp_path. What is exercised is the part an owner reads -- which steps ran,
what was connected, what the archive now holds -- and that nothing is written
the owner did not approve.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeEmbedder
from varyz import cli
from varyz.bootstrap import DetectedSources
from varyz.config import find_config_file, load_config
from varyz.configfile import set_resolution_port
from varyz.db.connection import open_db
from varyz.resolution import client as client_module
from varyz.resolution.client import (
    ArchiveMismatchError,
    ResolutionClient,
    ResolutionClientError,
    ensure_daemon,
)
from varyz.sources import connected_summary, source_statuses

runner = CliRunner()


def _flat(text: str) -> str:
    """Output with Rich's wrapping undone, for phrases that may span lines."""
    return re.sub(r"\s+", " ", text)


def _config(path: Path) -> Path:
    path.write_text(
        '[storage]\ndata_dir = "data"\n\n[embedding]\nmodel = "fake-embedder-384"\n',
        encoding="utf-8",
    )
    return path


class _DownDaemon:
    """A resolution client with nothing on its port."""

    def status(self):
        raise ResolutionClientError("resolution daemon unavailable")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home with one documents folder, and no daemon, agents or model."""
    home = tmp_path / "home"
    docs = home / "Documents"
    docs.mkdir(parents=True)
    (docs / "decision.md").write_text(
        "The zephyr protocol was chosen for the launch checklist.", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # The home archive, as every command finds it -- not the real ~/.varyz.
    monkeypatch.setenv("VARYZ_HOME", str(home / ".varyz"))
    monkeypatch.setattr(
        "varyz.bootstrap.discover_local_sources",
        lambda: DetectedSources((docs.resolve(),), ()),
    )
    monkeypatch.setattr(cli, "_resolution_client", lambda _cfg: _DownDaemon())
    monkeypatch.setattr(cli, "_port_in_use", lambda _host, _port: False)
    monkeypatch.setattr(
        "varyz.resolution.client.ensure_daemon",
        lambda _client, _path: SimpleNamespace(host="127.0.0.1", port=8765),
    )
    monkeypatch.setattr("varyz.install.client_paths", lambda: {})
    monkeypatch.setattr("varyz.hooks.doctor", lambda _cfg: [("daemon", True, "ok")])
    monkeypatch.setattr(cli, "Embedder", lambda _cfg: FakeEmbedder())
    return SimpleNamespace(home=home, docs=docs)


def _entries(config: Path) -> int:
    conn = open_db(load_config(config).db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    finally:
        conn.close()


def test_dry_run_shows_what_it_would_connect_and_changes_nothing(tmp_path, machine):
    config = _config(tmp_path / "config.toml")
    before = config.read_bytes()

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Documents" in result.output
    assert machine.docs.name in result.output
    assert "Dry run: nothing connected or indexed." in result.output
    assert config.read_bytes() == before
    assert not (tmp_path / "data").exists()


def test_dry_run_without_an_archive_does_not_create_one(machine, monkeypatch):
    config = machine.home / ".varyz" / "config.toml"
    monkeypatch.chdir(machine.home)

    result = runner.invoke(cli.app, ["setup", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would create" in result.output
    assert not config.exists()


def test_a_declined_source_is_neither_connected_nor_indexed(tmp_path, machine):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--terminal"], input="n\n")

    assert result.exit_code == 0, result.output
    assert load_config(config).connector_options("documents") == {}
    assert _entries(config) == 0
    assert "Documents" in result.output and "not connected" in result.output


def test_yes_connects_indexes_and_says_what_is_connected(tmp_path, machine):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "4/7 Data sources connected Documents; indexed" in flat
    # No agent is installed on this machine, and GO says so.
    assert "GO: The archive is ready. No agent is connected yet" in flat
    assert "agents are briefed" not in flat
    assert load_config(config).connector_options("documents")["approved"] is True
    assert _entries(config) == 1
    documents = next(
        s for s in source_statuses(load_config(config), None) if s.info.label == "Documents"
    )
    assert documents.connected


def test_running_setup_again_keeps_everything_and_indexes_nothing_new(tmp_path, machine):
    config = _config(tmp_path / "config.toml")
    assert runner.invoke(cli.app, ["setup", "--config", str(config), "--yes"]).exit_code == 0
    first = config.read_bytes()

    again = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes"])

    assert again.exit_code == 0, again.output
    assert "nothing new found to connect" in again.output
    assert config.read_bytes() == first
    assert _entries(config) == 1


def test_source_limits_what_is_offered(tmp_path, machine):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(
        cli.app, ["setup", "--config", str(config), "--yes", "--source", "git"]
    )

    assert result.exit_code == 0, result.output
    assert "nothing new found to connect" in result.output
    assert load_config(config).connector_options("documents") == {}


def test_a_source_setup_cannot_connect_is_refused_before_anything_changes(
    tmp_path, machine
):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(
        cli.app, ["setup", "--config", str(config), "--source", "github"]
    )

    assert result.exit_code == 2
    assert "varyz connect github" in result.output
    assert not (tmp_path / "data").exists()


def test_an_unanswered_question_names_the_flag_that_answers_it(tmp_path, machine):
    """A scheduled task or an empty pipe answers the consent question with EOF."""
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--terminal"], input="")

    assert result.exit_code == 1, result.output
    assert "went unanswered" in result.output
    assert "--yes" in result.output
    assert "Aborted" not in result.output
    assert load_config(config).connector_options("documents") == {}


def test_no_sources_skips_the_data_step(tmp_path, machine):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--no-sources"])

    assert result.exit_code == 0, result.output
    assert "Data sources skipped: --no-sources" in _flat(result.output)
    assert load_config(config).connector_options("documents") == {}


def test_an_agent_left_without_the_mcp_server_is_a_failure_not_connected(
    tmp_path, machine, monkeypatch
):
    """The skill and hooks landed, the MCP entry did not: that read "connected"."""
    from varyz.install import InstallResult

    config = _config(tmp_path / "config.toml")
    claude = machine.home / ".claude.json"
    claude.write_text("{ half-edited", encoding="utf-8")
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"claude-code": claude})
    skipped = InstallResult("claude-code", claude, True, f"skipped - {claude} is not valid JSON")
    monkeypatch.setattr("varyz.install.connect_agent", lambda *a, **k: (None, [skipped]))

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes", "--no-sources"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "Claude Code failed (skipped" in flat
    assert "Claude Code connected" not in flat
    assert "NO-GO: Not ready: agents." in flat


def test_an_agent_whose_connect_raises_is_a_no_go_in_the_terminal_too(
    tmp_path, machine, monkeypatch
):
    """The terminal's own copy of the steps once let this through as GO."""
    config = _config(tmp_path / "config.toml")
    claude = machine.home / ".claude.json"
    claude.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"claude-code": claude})

    def refuse(*_args, **_kwargs):
        raise ValueError("no such agent here")

    monkeypatch.setattr("varyz.install.connect_agent", refuse)

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes", "--no-sources"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "3/7 Agents failed: Claude Code failed (no such agent here)" in flat
    assert "not every agent was connected (step 3)" in flat
    assert "NO-GO: Not ready: agents." in flat


def test_a_source_that_fails_to_index_is_never_printed_as_go(tmp_path, machine, monkeypatch):
    """The terminal printed GO over it, then exited 1."""
    config = _config(tmp_path / "config.toml")
    monkeypatch.setattr(cli, "_sync_sources", lambda *_a, **_k: ["documents"])

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "4/7 Data sources failed: could not index documents" in flat
    assert "NO-GO: Not ready: data sources." in flat
    assert "GO: The archive" not in flat


def test_a_setup_paused_on_the_page_exits_as_paused_not_failed(tmp_path, machine, monkeypatch):
    from varyz import setup_web

    monkeypatch.setattr(cli, "_browser_available", lambda: True)
    monkeypatch.setattr(setup_web, "serve", lambda _path: None)

    result = runner.invoke(cli.app, ["setup", "--config", str(_config(tmp_path / "c.toml"))])

    assert result.exit_code == cli.SETUP_PAUSED == 130


def test_go_says_agents_are_briefed_only_once_one_is_connected(
    tmp_path, machine, monkeypatch
):
    from varyz.install import InstallResult

    config = _config(tmp_path / "config.toml")
    claude = machine.home / ".claude.json"
    claude.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"claude-code": claude})
    added = InstallResult("claude-code", claude, True, "added the varyz MCP server")
    monkeypatch.setattr("varyz.install.connect_agent", lambda *a, **k: (None, [added]))

    result = runner.invoke(cli.app, ["setup", "--config", str(config), "--yes", "--no-sources"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Claude Code connected" in flat
    assert "GO: Agents are briefed from this archive." in flat
    assert "No agent is connected yet" not in flat


def test_a_local_source_points_to_the_setup_step_that_connects_it(tmp_path, machine):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(cli.app, ["sources", "--config", str(config)])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "varyz setup --source git" in flat
    assert "varyz setup --source documents" in flat


def test_after_setup_no_command_needs_the_config_named(tmp_path, machine, monkeypatch):
    elsewhere = tmp_path / "some-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    setup = runner.invoke(cli.app, ["setup", "--yes", "--no-sources"])
    sources = runner.invoke(cli.app, ["sources"])

    assert setup.exit_code == 0, setup.output
    assert (machine.home / ".varyz" / "config.toml").exists()
    assert "--config" not in setup.output
    assert 'varyz task "build a landing page"' in _flat(setup.output)
    assert sources.exit_code == 0, sources.output
    assert "AI conversations" in _flat(sources.output)
    assert not (elsewhere / "config.toml").exists()


def test_a_config_toml_in_the_folder_wins_over_the_home_archive(tmp_path, machine, monkeypatch):
    (machine.home / ".varyz").mkdir()
    home_config = _config(machine.home / ".varyz" / "config.toml")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    assert find_config_file() == home_config
    local = _config(project / "config.toml")
    assert find_config_file() == local
    monkeypatch.setenv("VARYZ_CONFIG", str(tmp_path / "named.toml"))
    assert find_config_file() == tmp_path / "named.toml"


def test_without_a_home_archive_nothing_is_found(tmp_path, machine, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert find_config_file() is None


def test_sources_lists_every_alpha_source_and_how_to_connect_it(tmp_path, machine):
    config = _config(tmp_path / "config.toml")

    result = runner.invoke(cli.app, ["sources", "--config", str(config)])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    for label in ("AI conversations", "Git history", "GitHub", "Documents", "Notes"):
        assert label in flat
    assert "connected" not in flat.replace("not connected", "")
    assert "varyz connect github" in flat
    assert "varyz connect ai-conversations" in flat


def test_last_sync_is_shown_on_the_owners_clock():
    stored = "2026-09-27T10:41:52+00:00"
    local = datetime(2026, 9, 27, 10, 41, 52, tzinfo=UTC).astimezone()

    assert cli._local_minute(stored) == local.strftime("%Y-%m-%d %H:%M")
    assert cli._local_minute("not a date at all") == "not a date at all"[:16]


def test_notes_count_as_connected_once_one_is_added(tmp_path, machine):
    config = _config(tmp_path / "config.toml")
    assert runner.invoke(cli.app, ["init", "--config", str(config)]).exit_code == 0
    added = runner.invoke(
        cli.app, ["add", "I moved to Berlin in 2021.", "--config", str(config)]
    )
    assert added.exit_code == 0, added.output

    result = runner.invoke(cli.app, ["sources", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert re.search(r"Notes\s*│\s*connected\s*│\s*1\b", result.output), result.output


def test_the_doctor_line_names_each_connected_source_and_its_items(tmp_path):
    config = tmp_path / "config.toml"
    docs = tmp_path / "docs"
    docs.mkdir()
    config.write_text(
        '[storage]\ndata_dir = "data"\n\n'
        f'[connectors.documents]\napproved = true\npaths = ["{docs.as_posix()}"]\n\n'
        '[connectors.git]\napproved = false\npaths = ["x"]\n',
        encoding="utf-8",
    )
    cfg = load_config(config)

    statuses = {s.info.connector_id: s for s in source_statuses(cfg, None)}

    assert statuses["documents"].state == "connected"
    assert statuses["git"].state == "not approved"
    assert statuses["github"].state == "not connected"
    assert connected_summary(list(statuses.values())) == "Documents (0 items, never synced)"
    assert "none connected" in connected_summary([statuses["github"]])


# -- one daemon per archive ---------------------------------------------------


def test_set_resolution_port_creates_replaces_and_stays_in_its_section(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")

    set_resolution_port(config, 8801)
    assert load_config(config).resolution.port == 8801

    config.write_text(
        config.read_text(encoding="utf-8") + '\n[connectors.github]\nport = 1\n',
        encoding="utf-8",
    )
    set_resolution_port(config, 8802)

    text = config.read_text(encoding="utf-8")
    assert load_config(config).resolution.port == 8802
    assert "[connectors.github]\nport = 1" in text
    assert text.count("[resolution]") == 1


def test_a_new_archive_leaves_a_port_something_else_listens_on(tmp_path, monkeypatch):
    """A daemon from before /identity answers 404 and passes the archive check,
    so a config created just now must not ask it: anything listening is foreign."""
    config = _config(tmp_path / "config.toml")
    taken = load_config(config).resolution.port
    monkeypatch.setattr(cli, "_port_in_use", lambda _host, port: port == taken)
    monkeypatch.setattr(cli, "_free_port", lambda _host, first: first + 7)

    cfg = cli._claim_own_port(config, load_config(config), fresh=True)

    assert cfg.resolution.port == taken + 8
    assert load_config(config).resolution.port == taken + 8


def test_an_existing_archive_moves_only_off_another_archives_daemon(tmp_path, monkeypatch):
    config = _config(tmp_path / "config.toml")
    port = load_config(config).resolution.port
    monkeypatch.setattr(cli, "_free_port", lambda _host, first: first)

    class Answering:
        def status(self):
            return SimpleNamespace()

    class Foreign:
        def status(self):
            raise ArchiveMismatchError("another archive")

    monkeypatch.setattr(cli, "_resolution_client", lambda _cfg: Answering())
    assert cli._claim_own_port(config, load_config(config)).resolution.port == port
    monkeypatch.setattr(cli, "_resolution_client", lambda _cfg: _DownDaemon())
    assert cli._claim_own_port(config, load_config(config)).resolution.port == port
    monkeypatch.setattr(cli, "_resolution_client", lambda _cfg: Foreign())
    assert cli._claim_own_port(config, load_config(config)).resolution.port == port + 1


def _serve(tmp_path, archive: str | None):
    """An in-process daemon that names ``archive`` as the database it serves."""
    import sqlite3

    from varyz.resolution.daemon import ResolutionDaemon, ResolutionService
    from varyz.resolution.escalation import SQLiteEscalationQueue
    from varyz.resolution.knowledge import SQLiteKnowledgeStore
    from varyz.resolution.resolver import ResolutionResolver

    db = tmp_path / "daemon.db"
    open_db(db).close()
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    knowledge = SQLiteKnowledgeStore(conn)
    queue = SQLiteEscalationQueue(conn, knowledge, device_id="device:daemon")
    service = ResolutionService(
        ResolutionResolver((knowledge,), escalation_queue=queue), queue, knowledge
    )
    daemon = ResolutionDaemon(service, host="127.0.0.1", port=0)
    daemon.archive = archive
    return daemon, conn


def test_a_client_refuses_the_daemon_of_another_archive(tmp_path):
    mine = tmp_path / "mine" / "varyz.db"
    theirs = tmp_path / "theirs" / "varyz.db"
    daemon, conn = _serve(tmp_path, str(theirs))
    try:
        with daemon:
            host, port = daemon.address
            assert ResolutionClient(host, port, archive=theirs).status().port == port
            with pytest.raises(ArchiveMismatchError, match="another Varyz archive"):
                ResolutionClient(host, port, archive=mine).status()
    finally:
        conn.close()


def test_a_daemon_that_cannot_name_its_archive_is_let_through(tmp_path, monkeypatch):
    daemon, conn = _serve(tmp_path, None)
    try:
        with daemon:
            host, port = daemon.address
            client = ResolutionClient(host, port, archive=tmp_path / "any.db")
            send = client._send

            def old_daemon(method, path, payload=None):
                if path == "/identity":
                    raise ResolutionClientError("resolution daemon returned HTTP 404")
                return send(method, path, payload)

            monkeypatch.setattr(client, "_send", old_daemon)
            assert client.status().port == port
    finally:
        conn.close()


def test_ensure_daemon_starts_nothing_on_another_archives_port(tmp_path, monkeypatch):
    class Foreign:
        def status(self):
            raise ArchiveMismatchError("another archive")

    monkeypatch.setattr(
        client_module.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("a mismatch must not spawn a daemon"),
    )

    with pytest.raises(ArchiveMismatchError):
        ensure_daemon(Foreign(), tmp_path / "config.toml")  # type: ignore[arg-type]

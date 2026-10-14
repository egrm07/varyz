"""CLI end-to-end through Typer's runner, with the embedder faked (no model download)."""

import json
import logging
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from tests.conftest import FakeEmbedder
from varyz import cli
from varyz.bootstrap import DetectedSources
from varyz.db.connection import LockHeldError, NotAnArchiveError
from varyz.llm.local_runtime import LocalModelNotInstalled
from varyz.resolution.client import ResolutionClientError

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Embedder", lambda cfg: FakeEmbedder())
    result = runner.invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_init_creates_config_and_db(project):
    assert (project / "config.toml").exists()
    assert (project / "data" / "varyz.db").exists()


def test_init_starts_the_archive_varyz_config_names(tmp_path, monkeypatch):
    """init read ./config.toml only, while every other command reads the env.

    So `VARYZ_CONFIG=elsewhere varyz init` -- the first command a second
    archive is started with -- silently opened the archive in the current
    directory instead, or wrote a new config.toml into it.
    """
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "config.toml"
    monkeypatch.setenv("VARYZ_CONFIG", str(elsewhere))

    result = runner.invoke(cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert elsewhere.exists()
    assert (elsewhere.parent / "data" / "varyz.db").exists()
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "data").exists()


def test_setup_and_uninstall_read_the_archive_varyz_config_names(
    tmp_path, monkeypatch
):
    """The same cwd-only resolution as init, on two more commands.

    bootstrap (now setup) refused a second archive with "No config found. Run:
    varyz init" while $VARYZ_CONFIG named the config it was asking for, and
    uninstall looked for that archive's autosync in the wrong directory.
    """

    class _NoDaemon:
        def status(self):
            raise ResolutionClientError("resolution daemon unavailable")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n', encoding="utf-8"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setenv("VARYZ_CONFIG", str(elsewhere / "config.toml"))

    # A dry run still reads the machine: keep it off the real daemon, agents
    # and home folders.
    monkeypatch.setattr("varyz.bootstrap.discover_local_sources", DetectedSources)
    monkeypatch.setattr("varyz.install.client_paths", lambda: {})
    monkeypatch.setattr(cli, "_resolution_client", lambda _cfg: _NoDaemon())
    found = runner.invoke(cli.app, ["setup", "--dry-run"])
    assert found.exit_code == 0, found.output
    assert "No config found" not in found.output
    assert str(elsewhere) in _flat(found.output).replace(" ", "")

    gone = runner.invoke(cli.app, ["uninstall", "--dry-run"])
    assert gone.exit_code == 0, gone.output
    assert str(empty) not in gone.output


def test_running_init_again_does_not_read_like_a_fresh_archive(project):
    """"Varyz initialized." over entries that were already there.

    Under it sat the advice for someone with none -- capture your first
    thought -- which is what a run that had just emptied the archive would say.
    """
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0

    again = runner.invoke(cli.app, ["init"])

    assert again.exit_code == 0, again.output
    assert "Already initialized, 1 entry kept" in _flat(again.output)
    assert "your first thought" not in again.output


def test_trust_summary_is_noninteractive_and_local(project):
    result = runner.invoke(cli.app, ["trust", "--summary"])
    assert result.exit_code == 0, result.output
    assert "Varyz trust center" in result.output
    assert "Local trust overview" in result.output
    assert "Archive" in result.output
    assert "Excluded" in result.output


def test_add_search_roundtrip(project):
    result = runner.invoke(cli.app, ["add", "ran into Lena at the gym", "--when", "2026-07-01"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ["search", "Lena"])
    assert result.exit_code == 0, result.output
    assert "2026-07-01" in result.output


def test_searching_an_empty_archive_does_not_fetch_an_embedding_model(project, monkeypatch):
    """The semantic half of a hybrid search needs something to compare against.

    With no vectors indexed there is nothing, and building the embedder anyway
    downloaded 220 MB of model before printing "No matches." -- on the first
    command someone runs after installing.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError("built an embedder for an index holding no vectors")

    monkeypatch.setattr(cli, "Embedder", refuse)

    result = runner.invoke(cli.app, ["search", "anything"])

    assert result.exit_code == 0, result.output
    assert "nothing to search yet" in result.output


def test_asking_an_empty_archive_says_it_is_empty_and_sends_nothing(project, monkeypatch):
    """An empty archive answered "no shareable entries matching that question".

    That blames the question, and it arrived only after a 220 MB model download
    and an egress receipt for a call that had nothing to send.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError("built an embedder for an archive holding no entries")

    monkeypatch.setattr(cli, "Embedder", refuse)

    result = runner.invoke(cli.app, ["ask", "anything?"])

    assert result.exit_code == 0, result.output
    assert "nothing to answer from yet" in result.output


def test_ask_help_makes_local_gpu_use_an_explicit_per_call_choice():
    command = typer.main.get_command(cli.app).commands["ask"]
    option = next(
        parameter for parameter in command.params if "--local-distill" in parameter.opts
    )

    assert option.default is False
    assert "Explicitly start the managed local model" in option.help


def test_ordinary_ask_never_constructs_the_managed_model(project, monkeypatch):
    assert runner.invoke(cli.app, ["add", "I live in Berlin."]).exit_code == 0

    def refuse(*_args, **_kwargs):
        pytest.fail("ordinary ask constructed the managed local provider")

    seen = []

    def fake_ask(*_args, **kwargs):
        seen.append(kwargs.get("local_provider"))
        return SimpleNamespace(
            approval_required=False,
            sent=1,
            chars_sent=16,
            considered=1,
            distilled=False,
            depth=SimpleNamespace(level=1, label="lookup"),
            excluded_sensitive=0,
            receipt_id=None,
            stream=iter(["Berlin"]),
            hits=[],
        )

    monkeypatch.setattr("varyz.llm.local.build_local_provider", refuse)
    monkeypatch.setattr("varyz.retrieval.rag.ask", fake_ask)

    result = runner.invoke(cli.app, ["ask", "where do I live?"])

    assert result.exit_code == 0, result.output
    assert seen == [None]
    assert "Berlin" in result.output


def test_local_distill_uses_the_conservative_interactive_shape(project, monkeypatch):
    assert runner.invoke(cli.app, ["add", "I live in Berlin."]).exit_code == 0
    provider = object()
    builds = []

    def fake_build(_cfg, **kwargs):
        builds.append(kwargs)
        return provider

    def fake_ask(*_args, **kwargs):
        assert kwargs["local_provider"] is provider
        return SimpleNamespace(
            approval_required=False,
            sent=1,
            chars_sent=16,
            considered=1,
            distilled=True,
            depth=SimpleNamespace(level=1, label="lookup"),
            excluded_sensitive=0,
            receipt_id=None,
            stream=iter(["Berlin"]),
            hits=[],
        )

    monkeypatch.setattr("varyz.llm.local.build_local_provider", fake_build)
    monkeypatch.setattr("varyz.retrieval.rag.ask", fake_ask)

    result = runner.invoke(
        cli.app, ["ask", "where do I live?", "--local-distill"]
    )

    assert result.exit_code == 0, result.output
    assert builds == [{"conservative_resources": True}]


def test_the_word_you_searched_for_survives_being_printed(project):
    """The one word the row exists for must appear in the row.

    SQLite marks what matched, and Rich reads a bracketed lowercase word as a
    style tag: `varyz search alpha` printed every row with `alpha` deleted from
    it. Capitalised names hid this, so the search test above never saw it.
    """
    assert runner.invoke(cli.app, ["add", "the alpha launch checklist"]).exit_code == 0
    result = runner.invoke(cli.app, ["search", "alpha"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "[alpha]" not in result.output
    assert "" not in result.output and "" not in result.output


def test_the_id_you_are_told_to_copy_survives_a_narrow_terminal(project, monkeypatch):
    """`varyz forget` takes the id `search` prints, so `search` must print all of it.

    Rich shrinks every column to fit the terminal, and at the 80 columns it
    assumes by default the id lost its tail to an ellipsis -- in tables whose
    only reason to carry an id is that another command takes one: `varyz todo
    done`, `varyz privacy verify` and `varyz graph merge` are each handed an id
    printed by a table that was cutting it short.
    """
    monkeypatch.setenv("COLUMNS", "80")
    long_enough = "the alpha launch checklist, " + "one more thing to do before it ships, " * 4
    assert runner.invoke(cli.app, ["add", long_enough]).exit_code == 0
    assert runner.invoke(cli.app, ["add", long_enough + " call mom", "--todo"]).exit_code == 0
    conn = sqlite3.connect(project / "data" / "varyz.db")
    entry_id = conn.execute("SELECT id FROM entries").fetchone()[0]
    todo_id = conn.execute("SELECT id FROM todos").fetchone()[0]
    conn.close()

    found = runner.invoke(cli.app, ["search", "alpha"])
    assert found.exit_code == 0, found.output
    assert entry_id in found.output

    listed = runner.invoke(cli.app, ["todo", "list"])
    assert listed.exit_code == 0, listed.output
    assert todo_id in listed.output

    receipt_id = "01M27RMM0P3YXXM6ZHHE9W4633"
    conn = sqlite3.connect(project / "data" / "varyz.db")
    conn.execute(
        """INSERT INTO context_requests
           (id, actor, purpose, destination, intent_hash, requested_types_json,
            requested_scope_json, max_items, max_chars, interaction, user_present,
            reversible, policy_version, created_at)
           VALUES ('req-narrow','mcp','answer_question','cloud:claude','h','[]','{}',
                   1,1,'cli',1,1,'1','2026-09-10T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO egress_receipts
           (id, request_id, payload_hash, payload_item_count, payload_char_count,
            considered_ids_json, decision_summary_json, policy_version,
            transformation_versions_json, approval_required, receipt_version, created_at)
           VALUES (?,'req-narrow','a-hash',1,357,'[]','{}','1','{}',0,'1',
                   '2026-09-10T00:00:00+00:00')""",
        (receipt_id,),
    )
    conn.commit()
    conn.close()

    receipts = runner.invoke(cli.app, ["privacy", "receipts"])
    assert receipts.exit_code == 0, receipts.output
    assert receipt_id in receipts.output


def test_add_todo_and_complete(project):
    assert runner.invoke(cli.app, ["add", "call mom about the cabin", "--todo"]).exit_code == 0
    listed = runner.invoke(cli.app, ["todo", "list"])
    assert "call mom" in listed.output

    conn = sqlite3.connect(project / "data" / "varyz.db")
    todo_id = conn.execute("SELECT id FROM todos").fetchone()[0]
    conn.close()
    assert runner.invoke(cli.app, ["todo", "done", todo_id]).exit_code == 0
    assert "call mom" not in runner.invoke(cli.app, ["todo", "list"]).output


def test_completing_a_todo_twice_is_not_the_same_as_mistyping_its_id(project):
    """A repeat succeeds, a typo does not.

    Both printed "No open todo with that id." and exited 0, so a script that
    closed the wrong id was told it had worked, and a person who mistyped one
    got the same line as somebody re-running a command that already ran.
    """
    assert runner.invoke(cli.app, ["add", "call mom about the cabin", "--todo"]).exit_code == 0
    conn = sqlite3.connect(project / "data" / "varyz.db")
    todo_id = conn.execute("SELECT id FROM todos").fetchone()[0]
    conn.close()

    assert runner.invoke(cli.app, ["todo", "done", todo_id]).exit_code == 0
    again = runner.invoke(cli.app, ["todo", "done", todo_id])
    assert again.exit_code == 0
    assert "Already done" in again.output

    missing = runner.invoke(cli.app, ["todo", "done", "01NOSUCHTODO"])
    assert missing.exit_code == 1
    assert "No todo with that id" in missing.output


def test_sync_documents_and_backup(project, journal_dir):
    (project / "config.toml").write_text(
        f"""[storage]
data_dir = "data"

[connectors.documents]
approved = true
paths = [{str(journal_dir)!r}]
""",
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "3 new" in result.output

    result = runner.invoke(cli.app, ["sync"])
    assert "0 new" in result.output and "3 unchanged" in result.output

    result = runner.invoke(cli.app, ["backup", str(project / "bak")])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(project / "bak" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
    conn.close()
    assert (project / "bak" / "vault").exists()


def test_a_backup_carries_the_config_and_says_how_to_put_it_back(project):
    """A directory holding varyz.db and vault/ is not yet a restorable archive.

    config.toml holds the data directory, the sensitivity defaults and the
    connector approvals; there is no `varyz restore`, so the command has to
    name the copy-back itself or the backup is only possessed, not usable.
    """
    result = runner.invoke(cli.app, ["backup", str(project / "bak")])

    assert result.exit_code == 0, result.output
    assert (project / "bak" / "config.toml").exists()
    assert "restore" in result.output
    assert "varyz.db" in result.output


def test_a_backup_does_not_point_at_the_archive_it_is_backing_up(project):
    """`varyz backup data` is the obvious thing to type, and it was accepted.

    The SQLite backup ran with the live database as both source and
    destination, the vault was copied over itself, and the command ended in a
    raw shutil error list -- exit 1, after having already written.
    """
    result = runner.invoke(cli.app, ["backup", str(project / "data")])

    assert result.exit_code == 2, result.output
    assert "inside the archive" in _flat(result.output)

    nested = runner.invoke(cli.app, ["backup", str(project / "data" / "vault" / "bak")])
    assert nested.exit_code == 2, nested.output


def test_a_backup_does_not_point_at_the_directory_the_archive_lives_in(project):
    """`varyz backup .` was the same mistake one level up, and it crashed.

    The refusal above only looks downwards, so a target that *contains* the
    archive passed it: the database was copied out beside config.toml, the
    vault beside that, and then config.toml was copied onto itself -- ending in
    a raw WinError 32 about a file held open by another process, with the
    backup half written into the directory it was backing up. `$DEST` unset
    reaches the command as exactly this path.
    """
    for target in (".", "", str(project)):
        result = runner.invoke(cli.app, ["backup", target])

        assert result.exit_code == 2, (target, result.output)
        assert "already holds the archive's config.toml" in _flat(result.output), target
        assert "Traceback" not in result.output, target
        assert not (project / "varyz.db").exists(), target


def test_an_empty_query_is_not_answered_with_the_archive(project):
    """A shell variable that expanded to nothing reached both commands.

    `search ""` ranked every entry as "similar meaning", which is a ranking of
    nothing against nothing, and `ask ""` got as far as naming which providers
    were connected -- reading as a question understood and a key missing.
    """
    runner.invoke(cli.app, ["add", "an entry so the archive is not empty"])

    for command in (["search", ""], ["search", "   "], ["ask", ""]):
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 2, (command, result.output)
        assert "is empty" in _flat(result.output), command


def test_a_backup_target_that_is_a_file_is_not_a_traceback(project):
    """mkdir over an existing file raised FileExistsError from inside pathlib.

    The traceback named a stdlib line rather than the argument that was wrong,
    and a path with a suffix -- backup varyz.db, backup archive.zip -- is an
    ordinary thing to type.
    """
    occupied = project / "already-a-file.txt"
    occupied.write_text("not a directory", encoding="utf-8")

    result = runner.invoke(cli.app, ["backup", str(occupied)])

    assert result.exit_code == 2, result.output
    assert "is a file, not a directory" in _flat(result.output)
    assert "Traceback" not in result.output
    assert occupied.read_text(encoding="utf-8") == "not a directory"


def test_an_install_that_cannot_fit_is_a_sentence_not_a_traceback(project, monkeypatch):
    """`varyz local-model install` is among the first commands anyone runs.

    Every failure inside the installer -- a full disk, a half-extracted runtime
    directory, a checksum that does not match -- reached the terminal as a
    traceback naming a line of local_runtime.py.
    """
    import varyz.llm.local_runtime as local_runtime

    monkeypatch.setattr(
        local_runtime.shutil, "disk_usage", lambda path: SimpleNamespace(total=1, used=1, free=0)
    )

    result = runner.invoke(cli.app, ["local-model", "install", "--yes"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "not enough free space" in _flat(result.output)


def test_a_backup_that_would_fill_the_disk_is_refused_before_it_writes(project, monkeypatch):
    """The archive is the size of the disk it lives on, and backup never looked.

    A 74 GB database copied onto 30 GB of free space fills the disk to the last
    byte, leaves a truncated varyz.db behind, and does it on the machine the
    original is on. Nothing in the package ever called disk_usage.
    """
    assert runner.invoke(cli.app, ["add", "a thought worth keeping"]).exit_code == 0
    monkeypatch.setattr(
        cli.shutil, "disk_usage", lambda path: SimpleNamespace(total=1, used=1, free=4096)
    )

    result = runner.invoke(cli.app, ["backup", str(project / "bak")])

    assert result.exit_code == 2, result.output
    assert "Not enough free space" in _flat(result.output)
    assert "another disk" in _flat(result.output)
    assert not (project / "bak").exists()


def test_a_second_backup_replaces_the_first_and_says_so(project):
    """Two backups into one directory left a mixture of both.

    The database was replaced and the vault was merged file by file, so a
    blob `varyz forget` had deleted in between stayed in the backup, beside a
    database that no longer mentions it. Nothing said the directory already
    held a backup either.
    """
    assert runner.invoke(cli.app, ["add", "the first thought"]).exit_code == 0
    first = runner.invoke(cli.app, ["backup", str(project / "bak")])
    assert first.exit_code == 0, first.output
    assert "replaced" not in first.output
    stale = project / "bak" / "vault" / "ff" / "ff"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "gone.json").write_text("{}", encoding="utf-8")

    second = runner.invoke(cli.app, ["backup", str(project / "bak")])

    assert second.exit_code == 0, second.output
    assert "replaced" in _flat(second.output)
    assert not (stale / "gone.json").exists()


def test_an_empty_capture_is_refused_rather_than_filed(project):
    """A shell variable that expanded to nothing became an archive entry.

    `varyz add ""` answered "Captured 01M2..." and filed a blank note: dated,
    embedded, counted, sent to the graph, and removable only with
    `varyz forget`. `varyz todo add ""` did the same for commitments.
    """
    for argv in (["add", ""], ["add", "   "], ["todo", "add", ""]):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 2, (argv, result.output)
        assert "Captured" not in result.output
        assert "TODO added" not in result.output

    conn = sqlite3.connect(project / "data" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0
    conn.close()


def test_a_date_varyz_cannot_read_is_refused_not_traced_back(project):
    """`varyz add "..." --when yesterday` printed a Python traceback.

    The option advertises three shapes and an ISO datetime; anything else --
    a word, a European date, a typo -- reached datetime.fromisoformat inside
    the connector and raised out of the capture. Every other date option in
    the CLI already refuses the value by name and says what it accepts.
    """
    for bad in ("yesterday", "14/07/2019", "2019-13-40"):
        result = runner.invoke(cli.app, ["add", "a thought", "--when", bad])
        assert result.exit_code == 2, (bad, result.output)
        assert "Traceback" not in result.output
        assert "Captured" not in result.output
        assert bad in _flat(result.output)

    good = runner.invoke(cli.app, ["add", "a thought", "--when", "2019-07"])
    assert good.exit_code == 0, good.output

    conn = sqlite3.connect(project / "data" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    conn.close()


def test_a_count_of_nothing_is_refused_rather_than_answered(project):
    """`-n 0` -- a shell variable that expanded to nothing -- read as "none".

    SQLite takes the limit literally: 0 returns no rows and a negative one
    returns every row. So a mistyped count answered `varyz search` with
    "No matches" over an archive that matched, and `varyz privacy receipts`
    with "No egress receipts yet" over a log that had them -- which is the
    one place a false nothing costs the most.
    """
    for argv in (["search", "anything", "-n", "0"], ["privacy", "receipts", "-n", "0"]):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 2, result.output
        assert "No matches" not in result.output
        assert "No egress receipts" not in result.output
        assert "x>=1" in result.output


def test_the_web_inbox_is_not_an_alpha_command(project):
    """docs/SHIPPING.md: the alpha ships the CLI and MCP, not the web UI."""
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 2, result.output
    assert "No such command" in result.output
    assert "serve" not in runner.invoke(cli.app, ["--help"]).output.split()


def test_search_on_an_empty_archive_says_it_is_empty(project):
    """"No matches." on a brand-new archive blames the query for the emptiness.

    A person who runs `varyz search` before connecting anything is told their
    words matched nothing, when the truthful answer is that there is nothing
    there yet and the next step is a source.
    """
    result = runner.invoke(cli.app, ["search", "anything"])
    assert result.exit_code == 0, result.output
    assert "No matches" not in result.output
    assert "archive is empty" in result.output
    assert "varyz add" in result.output


def test_a_first_rebuild_counts_its_one_seeded_alias_as_one(project):
    """Every first rebuild reported "1 aliases" for the alias it seeds itself."""
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["graph", "rebuild"])
    assert result.exit_code == 0, result.output
    assert "1 alias," in result.output
    assert "1 aliases" not in result.output


def test_ask_names_the_account_that_is_already_connected(project, monkeypatch):
    """One connected provider and the default left alone is a dead end.

    `ask` answered "No credentials for 'claude'. Run: varyz connect claude"
    and stopped -- on a machine holding a working OpenAI key, and without
    saying that one line of config.toml would have used it.
    """
    from varyz.llm import providers

    monkeypatch.setattr(
        providers,
        "provider_overview",
        lambda cfg: [
            {"name": "claude", "connected": False, "active": True},
            {"name": "openai", "connected": True, "active": False},
        ],
    )
    assert runner.invoke(cli.app, ["add", "I live in Berlin."]).exit_code == 0

    result = runner.invoke(cli.app, ["ask", "where do I live"])

    assert result.exit_code == 1
    flat = _flat(result.output)
    assert "No credentials for 'claude'" in flat
    assert 'provider = "openai"' in flat
    assert "[llm]" in flat


def test_commands_fail_cleanly_without_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["search", "anything"])
    assert result.exit_code == 1
    assert "varyz setup" in result.output


def test_prune_does_not_advise_a_vacuum_that_would_reclaim_nothing(project):
    """A few free pages rounded to "0 MB is free inside the database".

    Under it sat "run with --vacuum to give it back to the disk": a
    full-database rewrite recommended for nothing.
    """
    result = runner.invoke(cli.app, ["graph", "prune"])

    assert result.exit_code == 0, result.output
    assert "0 MB is free" not in result.output


def test_a_correction_to_a_graph_that_does_not_exist_says_so(project):
    """Past tense is reserved for what happened.

    An override is replayed onto every rebuild, so recording one before the
    graph exists is legitimate. Reporting it as "Entity label changed." is not:
    on an archive with no graph nothing changed, and a mistyped entity id read
    exactly like success.
    """
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0

    labelled = runner.invoke(cli.app, ["graph", "label", "person-nobody", "Nope"])
    assert labelled.exit_code == 0, labelled.output
    assert "No graph yet" in labelled.output
    assert "next varyz graph rebuild" in labelled.output
    # The note said nothing had changed, under a headline saying it had.
    assert "Entity label changed" not in labelled.output
    assert "Label override recorded" in labelled.output

    for argv in (
        ["graph", "merge", "person-a", "person-b"],
        ["graph", "split", "person-a"],
        ["graph", "claim", "deadbeef", "accept"],
        ["graph", "speaker", "deadbeef", "person-a"],
        ["graph", "sensitivity", "claim", "deadbeef", "sensitive"],
    ):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 0, result.output
        assert "No graph yet" in result.output, argv
        for past_tense in ("Merged.", "Claim time corrected", "Sensitivity changed"):
            assert past_tense not in result.output, argv


def test_a_time_correction_with_nothing_to_correct_is_refused(project):
    """The replay writes the payload straight into the row.

    Bare `varyz graph time <fingerprint>` therefore erased the claim's dates
    and marked the erasure as the owner's own -- under "Time correction
    recorded", the same words a correction that landed prints.
    """
    empty = runner.invoke(cli.app, ["graph", "time", "deadbeef"])
    assert empty.exit_code == 2, empty.output
    assert "Nothing to correct" in _flat(empty.output)

    conn = sqlite3.connect(project / "data" / "varyz.db")
    filed = conn.execute("SELECT COUNT(*) FROM graph_overrides").fetchone()[0]
    conn.close()
    assert filed == 0, "an override was appended for a correction that was refused"

    # Erasing an interval stays possible, by asking for it.
    deliberate = runner.invoke(cli.app, ["graph", "time", "deadbeef", "--precision", "unknown"])
    assert deliberate.exit_code == 0, deliberate.output


def test_a_split_needs_a_merge_to_reverse(project):
    """"Split recorded." for an entity that was never merged.

    A split sets merged_into_id back to NULL, so against an unmerged entity
    its replay is a no-op forever -- and the confirmation read exactly like a
    merge undone.
    """
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0
    assert runner.invoke(cli.app, ["graph", "rebuild"]).exit_code == 0
    conn = sqlite3.connect(project / "data" / "varyz.db")
    ids = [row[0] for row in conn.execute("SELECT id FROM entities ORDER BY id")]
    conn.close()
    assert len(ids) >= 2, ids

    never_merged = runner.invoke(cli.app, ["graph", "split", ids[0]])
    assert never_merged.exit_code == 1, never_merged.output
    assert "not merged into anything" in never_merged.output
    assert "Split recorded" not in never_merged.output

    # An id the graph does not hold keeps its own note: the override is
    # replayed onto every rebuild, so recording one early is legitimate.
    unknown = runner.invoke(cli.app, ["graph", "split", "person-nobody"])
    assert unknown.exit_code == 0, unknown.output
    assert "No entity person-nobody" in unknown.output

    assert runner.invoke(cli.app, ["graph", "merge", ids[1], ids[0]]).exit_code == 0
    undone = runner.invoke(cli.app, ["graph", "split", ids[1]])
    assert undone.exit_code == 0, undone.output
    assert "Split recorded" in undone.output


def test_a_merge_names_the_surviving_entity_the_graph_does_not_have(project):
    """The mistyped-id note checked one of the two ids a merge takes.

    A merge is replayed as `merged_into_id = <surviving>`, so a typo in that
    second id merges nothing: it files the entity away under an id nothing
    resolves to, and the entity leaves the graph at the next rebuild without
    arriving anywhere. That was confirmed with "Merged."
    """
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0
    assert runner.invoke(cli.app, ["graph", "rebuild"]).exit_code == 0
    conn = sqlite3.connect(project / "data" / "varyz.db")
    ids = [row[0] for row in conn.execute("SELECT id FROM entities ORDER BY id")]
    conn.close()
    assert ids

    stranger = runner.invoke(cli.app, ["graph", "merge", ids[0], "person-nobody"])
    assert stranger.exit_code == 0, stranger.output
    flat = _flat(stranger.output)
    assert "No entity person-nobody in the current graph" in flat, stranger.output
    assert "Merge recorded" in flat
    assert "Merged." not in flat

    # Merging an entity into itself is dropped by the replay, so the override
    # is one that can never do anything -- reported as a merge that happened.
    itself = runner.invoke(cli.app, ["graph", "merge", ids[0], ids[0]])
    assert itself.exit_code == 2, itself.output
    assert "into itself" in _flat(itself.output)


def test_a_config_file_that_cannot_be_read_is_reported_as_a_config_file(project, tmp_path):
    """The three ways config.toml goes wrong all name the file and the reason.

    config.toml is the one file Varyz asks its owner to edit, and each of its
    failure modes answered with a Python traceback through varyz internals:
    a TOMLDecodeError, a FileNotFoundError, a pydantic ValidationError. None
    of them said which line to go and change.
    """
    unparsable = tmp_path / "bad.toml"
    unparsable.write_text("this is not toml [[[", encoding="utf-8")
    result = runner.invoke(cli.app, ["search", "hi", "--config", str(unparsable)])
    assert result.exit_code == 2
    assert "not valid TOML" in _flat(result.output)
    assert "Traceback" not in result.output
    # The parser brackets its position, which Rich would have read as markup.
    assert "line 1" in _flat(result.output)

    missing = runner.invoke(cli.app, ["search", "hi", "--config", str(tmp_path / "nope.toml")])
    assert missing.exit_code == 2
    assert "No config file at" in missing.output

    wrong_type = tmp_path / "wrong.toml"
    wrong_type.write_text("[storage]\ndata_dir = 42\n", encoding="utf-8")
    typed = runner.invoke(cli.app, ["search", "hi", "--config", str(wrong_type)])
    assert typed.exit_code == 2
    assert "storage.data_dir" in " ".join(typed.output.split())


def test_a_graph_that_was_never_built_is_not_reported_as_healthy(project):
    """A clean bill of health needs something to have been examined.

    diagnose finds no issues in an archive with no graph, which on a fresh
    install is every archive, and the command said the graph was healthy.
    """
    result = runner.invoke(cli.app, ["graph", "doctor"])
    assert result.exit_code == 0, result.output
    assert "No graph has been built yet" in result.output
    assert "healthy" not in result.output.lower()


def test_review_and_prune_also_say_when_there_is_no_graph_to_speak_of(project):
    """The same clean bill of health, one command over.

    With no graph there are no identity candidates and no generations, so
    `review` answered "nothing needs review" and `prune` answered "every
    generation is live" -- two settled-sounding reports about a graph that has
    never existed. `doctor` already names that state; these now agree with it.
    """
    review = runner.invoke(cli.app, ["graph", "review"])
    assert review.exit_code == 0, review.output
    assert "No graph has been built yet" in review.output
    assert "need review" not in review.output

    prune = runner.invoke(cli.app, ["graph", "prune"])
    assert prune.exit_code == 0, prune.output
    assert "nothing to prune" in prune.output.lower()
    assert "within the window" not in prune.output


def test_an_override_is_not_recorded_against_an_id_that_is_empty(project):
    """`varyz graph merge "$A" "$B"` with an unset variable filed an override.

    An override replays onto every rebuild by exact id, and one recorded for an
    id the graph does not hold yet is deliberate. An empty id is the one no
    rebuild can ever produce, and it was accepted, written, and reported as
    recorded under the note that says a rebuild would apply it.
    """
    for argv in (
        ["graph", "merge", "", "somewhere"],
        ["graph", "merge", "someone", ""],
        ["graph", "split", ""],
        ["graph", "claim", "", "accept"],
        ["graph", "label", "", "A name"],
        ["graph", "speaker", "", "someone"],
        ["graph", "time", "", "--valid-from", "2026-01-01"],
        ["graph", "sensitivity", "entity", "", "public"],
    ):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 2, f"{argv}: {result.output}"
        assert "needs" in result.output, f"{argv}: {result.output}"

    with sqlite3.connect(project / "data" / "varyz.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_overrides").fetchone()[0] == 0


def test_model_interpret_answers_an_empty_id_without_a_traceback(project):
    """The checks behind this command are an agent's, and raise ValueError.

    An agent reads that as a tool error; here it reached a person at a terminal
    as a rendered Python traceback.
    """
    result = runner.invoke(cli.app, ["model", "interpret", ""])

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "question_id is required" in result.output


def test_revoking_nothing_says_so_rather_than_that_no_such_grant_exists(project):
    """The two are different, and revoking is a command run in a hurry."""
    result = runner.invoke(cli.app, ["privacy", "revoke", ""])

    assert result.exit_code == 2, result.output
    assert "the grant id is empty" in result.output


def test_graph_prune_reports_before_it_deletes(project):
    """Two commands with one name did opposite things.

    `varyz model prune` reports and waits for `--apply`; `varyz graph prune`
    deleted on sight -- including the generations a rollback returns to. The
    safe habit learned from one was destructive with the other.
    """
    conn = sqlite3.connect(project / "data" / "varyz.db")
    for run_id, state, started in (
        ("prune-live", "active", "2026-08-05T00:00:00+00:00"),
        ("prune-old", "superseded", "2026-08-01T00:00:00+00:00"),
        ("prune-older", "superseded", "2026-07-01T00:00:00+00:00"),
        ("prune-oldest", "superseded", "2026-06-01T00:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO graph_runs (id,generation_key,extractor_name,extractor_version,"
            "predicate_version,identity_version,input_content_hash,state,entries_total,"
            "entries_processed,started_at,activated_at) "
            "VALUES (?,?,'deterministic','1','1','1','h',?,0,0,?,?)",
            (run_id, run_id, state, started, started),
        )
    conn.commit()
    conn.close()

    reported = runner.invoke(cli.app, ["graph", "prune"])

    assert reported.exit_code == 0, reported.output
    flat = _flat(reported.output)
    assert "would delete" in flat
    assert "Run with --apply to delete them." in flat
    conn = sqlite3.connect(project / "data" / "varyz.db")
    kept = conn.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    conn.close()
    assert kept == 4, "a report deleted a generation"

    applied = runner.invoke(cli.app, ["graph", "prune", "--apply"])

    assert applied.exit_code == 0, applied.output
    assert "deleted" in _flat(applied.output)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    left = conn.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    conn.close()
    assert left < 4

    # And with nothing left to prune, one sentence -- not that sentence
    # followed by "would delete 0 rows (0 policy labels, 0 finished jobs)".
    empty = runner.invoke(cli.app, ["graph", "prune"])
    assert empty.exit_code == 0, empty.output
    assert "Nothing to prune" in empty.output
    assert "would delete" not in empty.output


def test_doctor_says_when_the_live_graph_is_behind_the_archive(project):
    """A graph built before the last entries arrived is not simply healthy.

    Every check in diagnose reads the graph and none reads the archive, so a
    graph that has never seen last week passes all of them -- and the person
    asking why `ask` does not know about last week is told nothing is wrong.
    """
    conn = sqlite3.connect(project / "data" / "varyz.db")
    conn.execute(
        "INSERT INTO graph_runs (id,generation_key,extractor_name,extractor_version,"
        "predicate_version,identity_version,input_content_hash,state,entries_total,"
        "entries_processed,started_at,activated_at) "
        "VALUES ('behind-1','k','deterministic','1','1','1','h','active',1,1,"
        "'2026-08-01T00:00:00+00:00','2026-08-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    for text in ("first thought", "second thought", "third thought"):
        assert runner.invoke(cli.app, ["add", text]).exit_code == 0

    result = runner.invoke(cli.app, ["graph", "doctor"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "2 entries are newer than the live graph." in flat
    assert "varyz graph rebuild" in flat
    assert "Graph is healthy" not in flat


def test_graph_human_correction_commands(project):
    # The subject here is label/correct/forget, not extraction, and the default
    # extractor wants a local model. Asking for the deterministic one keeps the
    # test offline rather than leaving it to whether a local model is installed.
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    added = runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."])
    assert added.exit_code == 0, added.output
    rebuilt = runner.invoke(cli.app, ["graph", "rebuild"])
    assert rebuilt.exit_code == 0, rebuilt.output

    conn = sqlite3.connect(project / "data" / "varyz.db")
    entity_id = conn.execute(
        "SELECT id FROM entities WHERE status='active' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    claim_fingerprint = conn.execute(
        """SELECT c.fingerprint FROM claims c
           JOIN graph_runs g ON g.id=c.graph_generation_id
           WHERE g.state='active' LIMIT 1"""
    ).fetchone()[0]
    evidence_fingerprint = conn.execute(
        """SELECT e.fingerprint FROM claim_evidence e
           JOIN graph_runs g ON g.id=e.graph_generation_id
           WHERE g.state='active' LIMIT 1"""
    ).fetchone()[0]
    conn.close()

    label = runner.invoke(cli.app, ["graph", "label", entity_id, "Berlin home"])
    assert label.exit_code == 0, label.output
    corrected_time = runner.invoke(
        cli.app,
        [
            "graph",
            "time",
            claim_fingerprint,
            "--valid-from",
            "March 2021",
            "--valid-to",
            "April 2021",
            "--precision",
            "month",
        ],
    )
    assert corrected_time.exit_code == 0, corrected_time.output
    speaker = runner.invoke(
        cli.app,
        ["graph", "speaker", evidence_fingerprint, entity_id],
    )
    assert speaker.exit_code == 0, speaker.output
    sensitivity = runner.invoke(
        cli.app,
        ["graph", "sensitivity", "claim", claim_fingerprint, "sensitive"],
    )
    assert sensitivity.exit_code == 0, sensitivity.output
    conn = sqlite3.connect(project / "data" / "varyz.db")
    assert (
        conn.execute("SELECT canonical_label FROM entities WHERE id=?", (entity_id,)).fetchone()[0]
        == "Berlin home"
    )
    claim = conn.execute(
        """SELECT c.valid_from, c.valid_to, c.temporal_precision, c.sensitivity
           FROM claims c JOIN graph_runs g ON g.id=c.graph_generation_id
           WHERE g.state='active' AND c.fingerprint=?""",
        (claim_fingerprint,),
    ).fetchone()
    assert claim[0].startswith("2021-03-01")
    assert claim[1].startswith("2021-04-30")
    assert claim[2:] == ("month", "sensitive")
    assert (
        conn.execute(
            """SELECT e.speaker_entity_id FROM claim_evidence e
           JOIN graph_runs g ON g.id=e.graph_generation_id
           WHERE g.state='active' AND e.fingerprint=?""",
            (evidence_fingerprint,),
        ).fetchone()[0]
        == entity_id
    )
    assert conn.execute("SELECT COUNT(*) FROM graph_overrides").fetchone()[0] == 4
    conn.close()


def test_an_assertion_with_no_graph_to_put_it_in_names_the_command(project):
    """The refusal was correct and led nowhere.

    "Build the graph before adding an assertion" is exactly right and does
    not say how, on a command whose whole point is that the owner is
    correcting Varyz by hand -- so the reader who most wants to be told the
    next command is the one who was not.
    """
    result = runner.invoke(cli.app, ["graph", "assert", "person-anyone", "worked_on", "varyz"])

    assert result.exit_code == 1, result.output
    assert "varyz graph rebuild" in _flat(result.output)


def test_an_assertion_typed_wrong_is_answered_in_words(project):
    """`graph assert` writes a claim out of nothing, so all of it is typed.

    Every wrong field came back as a traceback through varyz internals: an
    unknown entity as `IntegrityError: FOREIGN KEY constraint failed`, an
    unregistered predicate as a ValueError that did not say which predicates
    exist, an unparsable --valid-from likewise. `--sensitivity nope` did not
    even do that: the INSERT OR IGNORE that makes replay idempotent swallowed
    the CHECK, and the command said "User assertion recorded."
    """
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0
    assert runner.invoke(cli.app, ["graph", "rebuild"]).exit_code == 0
    conn = sqlite3.connect(project / "data" / "varyz.db")
    entity_id = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE namespace='system' LIMIT 1"
    ).fetchone()[0]
    conn.close()

    unknown_entity = runner.invoke(
        cli.app, ["graph", "assert", "person-nobody", "worked_on", "varyz"]
    )
    assert unknown_entity.exit_code == 2
    assert "No entity person-nobody" in unknown_entity.output

    unknown_predicate = runner.invoke(
        cli.app, ["graph", "assert", entity_id, "works_on", "varyz"]
    )
    assert unknown_predicate.exit_code == 2
    assert "worked_on" in _flat(unknown_predicate.output)

    bad_time = runner.invoke(
        cli.app,
        ["graph", "assert", entity_id, "worked_on", "varyz", "--valid-from", "someday"],
    )
    assert bad_time.exit_code == 2

    bad_sensitivity = runner.invoke(
        cli.app,
        ["graph", "assert", entity_id, "worked_on", "varyz", "--sensitivity", "nope"],
    )
    assert bad_sensitivity.exit_code == 2
    assert "recorded" not in bad_sensitivity.output

    # A shell variable that expanded to nothing filed "<entity> worked_on" with
    # no object -- a claim carrying the owner's own provenance, saying nothing.
    empty_object = runner.invoke(cli.app, ["graph", "assert", entity_id, "worked_on", "  "])
    assert empty_object.exit_code == 2, empty_object.output
    assert "needs an object" in _flat(empty_object.output)

    conn = sqlite3.connect(project / "data" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM graph_overrides").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE origin='user'").fetchone()[0] == 0
    conn.close()

    good = runner.invoke(
        cli.app,
        ["graph", "assert", entity_id, "worked_on", "varyz", "--valid-from", "March 2021"],
    )
    assert good.exit_code == 0, good.output
    assert "User assertion recorded" in good.output


def test_a_freshly_built_graph_passes_the_privacy_doctor(project):
    """Building a graph is not a privacy fault, and it reported itself as one.

    Each generation seeds one `system:self` alias with no source entry.
    Aliases are labelled by looking them up by source entry, so that one was
    never labelled -- and `privacy doctor` answered every archive with a
    graph in it "Active aliases, claims, or evidence quotes lack a policy
    label", error severity, exit 1.
    """
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0
    assert runner.invoke(cli.app, ["graph", "rebuild"]).exit_code == 0

    result = runner.invoke(cli.app, ["privacy", "doctor"])

    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_a_correction_to_a_fingerprint_the_graph_does_not_have_says_so(project):
    """The mistyped-id check stopped at the commands that take an entity id.

    `graph merge person-typo person-b` said the entity is not in the graph.
    `graph claim deadbeef accept` -- the same typo one table over -- answered
    "Claim accept recorded" and filed an override that will never match a row.
    """
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0
    assert runner.invoke(cli.app, ["graph", "rebuild"]).exit_code == 0

    for argv, noun in (
        (["graph", "claim", "deadbeef", "accept"], "claim"),
        (["graph", "time", "deadbeef", "--valid-from", "March 2021"], "claim"),
        (["graph", "speaker", "deadbeef", "person-nobody"], "entity"),
        (["graph", "sensitivity", "claim", "deadbeef", "sensitive"], "claim"),
        (["graph", "sensitivity", "evidence", "deadbeef", "sensitive"], "evidence quote"),
    ):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        assert f"No {noun}" in flat, (argv, result.output)
        assert "a rebuild would apply it" in flat, argv

    # An id this graph does not contain is a typo far more often than it is a
    # correction staged for a rebuild, and it used to report the same success.
    stranger = runner.invoke(cli.app, ["graph", "label", "person-nobody", "Nope"])
    assert stranger.exit_code == 0, stranger.output
    assert "No entity person-nobody in the current graph" in " ".join(stranger.output.split())


def _prepare_personal_model_compile(project) -> None:
    (project / "config.toml").write_text(
        '[storage]\ndata_dir = "data"\n\n[graph]\nextractor = "deterministic"\n',
        encoding="utf-8",
    )
    added = runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."])
    assert added.exit_code == 0, added.output
    rebuilt = runner.invoke(cli.app, ["graph", "rebuild"])
    assert rebuilt.exit_code == 0, rebuilt.output


def test_model_compile_dry_run_reports_reachable_semantic_mode(project, monkeypatch):
    _prepare_personal_model_compile(project)
    composition = SimpleNamespace(model_name="offline-semantic-fixture", compiler=object())
    monkeypatch.setattr(
        "varyz.personal_model.semantic_runtime.build_local_semantic_compiler",
        lambda conn, cfg: composition,
    )

    result = runner.invoke(cli.app, ["model", "compile"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "offline-semantic-fixture" in flat
    assert "--run will compile in semantic mode" in flat
    conn = sqlite3.connect(project / "data" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM personal_model_generations").fetchone()[0] == 0
    conn.close()


def test_model_compile_dry_run_rejects_a_stale_graph(project, monkeypatch):
    _prepare_personal_model_compile(project)
    runner.invoke(cli.app, ["add", "This arrived after the graph was built."])
    monkeypatch.setattr(
        "varyz.personal_model.semantic_runtime.build_local_semantic_compiler",
        lambda conn, cfg: SimpleNamespace(model_name="unused", compiler=object()),
    )

    result = runner.invoke(cli.app, ["model", "compile"])

    assert result.exit_code == 1
    assert "preflight failed" in _flat(result.output)
    assert "rebuild the graph" in _flat(result.output)


def test_model_compile_does_not_offer_a_run_it_is_already_doing(project, monkeypatch):
    """The status lines are printed before the compile, on a dry run and a run alike.

    A --run therefore announced itself as something a --run would do.
    """

    class _Refuses:
        def compile(self, request):
            raise RuntimeError("nothing to compile in this fixture")

    _prepare_personal_model_compile(project)
    composition = SimpleNamespace(model_name="offline-semantic-fixture", compiler=_Refuses())
    monkeypatch.setattr(
        "varyz.personal_model.semantic_runtime.build_local_semantic_compiler",
        lambda conn, cfg: composition,
    )

    result = runner.invoke(cli.app, ["model", "compile", "--run"])

    flat = _flat(result.output)
    assert "Compiling in semantic mode" in flat
    assert "--run will" not in flat


def test_model_compile_literal_only_never_constructs_a_local_provider(project, monkeypatch):
    _prepare_personal_model_compile(project)
    provider_calls = []

    def forbidden_provider(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("literal-only compile constructed a local provider")

    monkeypatch.setattr("varyz.llm.local.build_local_provider", forbidden_provider)

    result = runner.invoke(cli.app, ["model", "compile", "--literal-only"])

    assert result.exit_code == 0, result.output
    assert provider_calls == []
    assert "no semantic layer" in _flat(result.output)


def test_model_compile_help_surfaces_both_observation_budget_controls() -> None:
    command = typer.main.get_command(cli.app).commands["model"].commands["compile"]
    option_names = {name for parameter in command.params for name in parameter.opts}

    assert "--max-annotation-calls" in option_names
    assert "--observation-deadline" in option_names
    assert "--frontier" in option_names
    assert "--frontier-estimated-spend-per-call" in option_names
    assert "--frontier-spend-cap" in option_names


def test_model_compile_frontier_dry_run_is_explicit_and_writes_nothing(project, monkeypatch):
    _prepare_personal_model_compile(project)
    composition = SimpleNamespace(
        local_model_name="local-fixture",
        frontier_model_name="frontier-fixture",
        destination="cloud:fixture",
        compiler=object(),
    )
    calls = []

    def build(conn, cfg, **kwargs):
        calls.append(kwargs)
        return composition

    monkeypatch.setattr(
        "varyz.personal_model.cloud_runtime.build_hybrid_semantic_compiler",
        build,
    )

    result = runner.invoke(
        cli.app,
        [
            "model",
            "compile",
            "--frontier",
            "--frontier-estimated-spend-per-call",
            "0.10",
            "--frontier-spend-cap",
            "2.50",
        ],
    )

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "frontier-fixture" in flat
    assert "local-fixture" in flat
    assert "explicitly authorizes" in flat
    assert calls == [{"estimated_spend_usd_per_call": 0.1, "spend_cap_usd": 2.5}]
    conn = sqlite3.connect(project / "data" / "varyz.db")
    assert conn.execute("SELECT COUNT(*) FROM personal_model_generations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM egress_receipts").fetchone()[0] == 0
    conn.close()


def test_model_compile_refuses_frontier_literal_only_combination(project):
    result = runner.invoke(
        cli.app,
        ["model", "compile", "--frontier", "--literal-only"],
    )

    assert result.exit_code == 2
    assert "--frontier requires --semantic" in _flat(result.output)


def test_model_compile_accepts_an_offset_aware_wall_clock_deadline(project) -> None:
    _prepare_personal_model_compile(project)

    result = runner.invoke(
        cli.app,
        [
            "model",
            "compile",
            "--literal-only",
            "--max-annotation-calls",
            "12000",
            "--observation-deadline",
            "2026-08-14T07:00:00+02:00",
        ],
    )

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "12,000 new annotation calls" in flat
    assert "2026-08-14T07:00:00+02:00" in flat


def test_uninstall_does_not_unregister_an_archive_it_was_not_run_in(project, monkeypatch):
    """The command is typed in a directory; it used to act on the machine.

    Run anywhere -- a second archive, a directory holding no archive at all --
    it removed every varyz registration it could find, including the one that
    starts the archive the reader uses every day and had not named.
    """
    import json

    elsewhere = project / "elsewhere" / "config.toml"
    client = project / "mcp.json"
    entry = {"command": "varyz", "args": ["mcp", "--config", str(elsewhere)]}
    client.write_text(json.dumps({"mcpServers": {"varyz": entry}}), encoding="utf-8")
    monkeypatch.setattr("varyz.install.client_paths", lambda: {"cursor": client})

    result = runner.invoke(cli.app, ["uninstall"])

    assert result.exit_code == 0, result.output
    assert "another archive" in _flat(result.output)
    assert "varyz" in json.loads(client.read_text(encoding="utf-8"))["mcpServers"]


def test_uninstall_does_not_offer_to_remove_an_autosync_never_installed(project, monkeypatch):
    """The dry run named three files by absolute path -- startup shortcut,
    runner, enable marker -- on an install that had never enabled autosync and
    where none of the three exists. Read on the way out, that is being told you
    had a login registration you never made.
    """
    monkeypatch.setattr("varyz.setup_flow.unregister_mcp", lambda **_kwargs: [])

    result = runner.invoke(cli.app, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Would remove autosync" not in result.output
    assert "Autosync is not installed here" in _flat(result.output)


def test_autosync_uninstall_does_not_headline_a_removal_that_did_not_happen(project):
    """uninstall_daemon answers "nothing installed" for an archive that never
    had it; the line printed it after the words "login startup removed".
    """
    result = runner.invoke(cli.app, ["autosync", "uninstall"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "nothing installed" in flat
    assert "login startup removed" not in flat

def test_interpret_answers_an_uncompiled_archive_instead_of_raising(project):
    """`varyz model interpret` reaches into the MCP server's stored-interpretation
    path, which takes an egress gate because the agent boundary needs one. The
    CLI passed no gate at all, so every invocation of a documented command died
    on a TypeError traceback before it read anything.

    It then answered in the other wrong direction: the failure came back as a
    JSON object with an "error" key and exit 0, so a script that checks the
    exit code read a request nothing could satisfy as a success.
    """
    result = runner.invoke(cli.app, ["model", "interpret", "any-question-id"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "question not offered" in result.output
    assert "varyz model compile --run" in _flat(result.output)

def test_model_status_reports_partial_progress_and_remaining_cost(project) -> None:
    _prepare_personal_model_compile(project)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at)
           VALUES (?,?,?,?,?,'staged',?)""",
        (
            "partial-status-fixture",
            graph_id,
            "fixture-compiler",
            "fixture-policy",
            "fixture-input",
            "2026-08-13T12:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO personal_model_observation_progress
           (generation_id,state,episodes_annotated,episodes_total,episodes_remaining,
            entries_reached,annotation_calls,cache_hits,max_annotation_calls,deadline,
            budget_exhausted_by,annotated_episode_ids_json,reached_entry_ids_json,updated_at)
           VALUES (?,'partial',9,15,6,11,4,5,4,NULL,'call_budget','[]','[]',?)""",
        ("partial-status-fixture", "2026-08-13T12:10:00+00:00"),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "partial-status" in flat and "partial" in flat
    assert "9/15 annotated" in flat
    assert "6 remaining" in flat and "11 entries reached" in flat
    assert "measured 4.5 calls/min" in flat and "6 annotation calls" in flat


def _seed_message(conn, entry_id: str, sensitivity: str) -> None:
    """One chat message, at the sensitivity the test is about."""
    conn.execute(
        "INSERT INTO entries (id,source,external_id,kind,body,occurred_start,"
        "time_precision,ingested_at,sensitivity,content_hash) "
        "VALUES (?,'ai_conversations',?,'message','hi','2026-08-01T10:00:00+00:00','day',"
        "'2026-08-01T10:00:00+00:00',?,?)",
        (entry_id, entry_id, sensitivity, entry_id),
    )


def test_enrich_says_when_the_archive_has_no_chats_at_all(project) -> None:
    """An empty archive was told its chats were "already curated".

    There were no chats. The sentence is the same one a finished run prints,
    so nothing distinguished a source that was never connected from work that
    is genuinely done.
    """
    result = runner.invoke(cli.app, ["enrich"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "No chat messages in the archive yet." in flat
    assert "connect whatsapp" not in flat


def test_enrich_says_sensitive_chats_stayed_local_rather_than_claiming_they_are_done(
    project,
) -> None:
    """Held-back data must be reported as held back, not as finished work."""
    conn = sqlite3.connect(project / "data" / "varyz.db")
    _seed_message(conn, "enrich-sensitive-1", "sensitive")
    _seed_message(conn, "enrich-sensitive-2", "sensitive")
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["enrich"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "all 2 chat messages are sensitive" in flat
    assert "stay on this machine" in flat


def test_model_status_names_what_the_kept_generations_cost(project) -> None:
    """Every compile keeps the one before it, and the disk that costs was unsaid.

    A real archive reached 67 GB across ten generations with no line anywhere
    naming the size or the command that gives part of it back. The split has to
    match `varyz model prune`: a failure goes with the plain command, a staging
    behind a newer one only with --include-staged.
    """
    _prepare_personal_model_compile(project)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    for generation_id, state, records, created, failed_at in (
        # Generation ids sort the way ULIDs do, oldest first, because that is how
        # both `model status` and `model prune` pick the staging attempt to keep.
        ("footprint-1-failed", "failed", 900, "2026-08-13T09:00:00+00:00", "2026-08-13T09:30Z"),
        ("footprint-2-staged", "staged", 700, "2026-08-13T10:00:00+00:00", None),
        ("footprint-3-staged", "staged", 500, "2026-08-13T11:00:00+00:00", None),
    ):
        conn.execute(
            """INSERT INTO personal_model_generations
               (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
                state,created_at,record_count,failed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                generation_id,
                graph_id,
                "fixture-compiler",
                "fixture-policy",
                "fixture-input",
                state,
                created,
                records,
                failed_at,
            ),
        )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "archive file:" in flat
    assert "3 generation(s) holding 2,100 records" in flat
    assert "1 failed (900 records)" in flat
    assert "1 superseded staging attempt(s) (700 records)" in flat


def test_model_status_names_the_stages_a_compile_never_reached(project) -> None:
    """A stage that never ran writes no checkpoint row; silence reads as success."""
    _prepare_personal_model_compile(project)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at)
           VALUES (?,?,?,?,?,'staged',?)""",
        (
            "unreached-status-fixture",
            graph_id,
            "fixture-compiler",
            "fixture-policy",
            "fixture-input",
            "2026-08-13T12:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO personal_model_compiler_checkpoints
           (generation_id,stage,state,input_hash,detail_json,completed_at)
           VALUES (?,'pin_inputs','complete',?,'{}',?)""",
        ("unreached-status-fixture", "fixture-input", "2026-08-13T12:01:00+00:00"),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "pin_inputs: complete" in flat
    for stage in ("synthesis", "validate", "activate"):
        assert f"{stage}: not reached" in flat


def test_model_status_says_when_synthesis_read_everything_and_concluded_nothing(
    project,
) -> None:
    """Not stale and not failed: every window answered and nothing survived."""
    _prepare_personal_model_compile(project)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at)
           VALUES (?,?,?,?,?,'staged',?)""",
        (
            "hollow-status-fixture",
            graph_id,
            "fixture-compiler",
            "fixture-policy",
            "fixture-input",
            "2026-08-13T12:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO personal_model_compiler_checkpoints
           (generation_id,stage,state,input_hash,detail_json,completed_at)
           VALUES (?,'synthesis','complete',?,?,?)""",
        (
            "hollow-status-fixture",
            "fixture-input",
            '{"candidates": 4, "interpretations": 0}',
            "2026-08-13T12:01:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "synthesis produced no interpretations" in flat


def test_model_status_will_not_call_a_generation_activated_on_the_checkpoint_alone(
    project,
) -> None:
    """The activate checkpoint is written before the pointer swap, of necessity.

    A generation goes immutable the moment it activates, so the row cannot be
    written afterwards -- which means a run killed in between leaves
    ``activate: complete`` standing over a generation that never went live.
    Read alone it says the model is up.  It is not.
    """

    _prepare_personal_model_compile(project)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at)
           VALUES (?,?,?,?,?,'staged',?)""",
        (
            "activation-killed-fixture",
            graph_id,
            "fixture-compiler",
            "fixture-policy",
            "fixture-input",
            "2026-09-03T08:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO personal_model_compiler_checkpoints
           (generation_id,stage,state,input_hash,detail_json,completed_at)
           VALUES (?,'activate','complete',?,?,?)""",
        (
            "activation-killed-fixture",
            "fixture-input",
            '{"requested": true, "validated": true}',
            "2026-09-03T08:31:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "the switch never happened" in flat
    assert "staged" in flat


def test_a_finished_compile_reports_in_words_and_keeps_json_behind_a_flag(project) -> None:
    """The longest command Varyz runs ended by printing its result model.

    Two hundred lines of JSON -- twelve readiness gates with numerators,
    thresholds and failure sentences -- scrolled past after a compile that can
    take hours, and the two things the person waited for (did it finish, is it
    what my questions run against now) were somewhere inside it.
    """

    _prepare_personal_model_compile(project)

    compiled = runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"])

    assert compiled.exit_code == 0, compiled.output
    assert '"generation_id"' not in compiled.output
    assert "Compiled and activated" in compiled.output
    assert "readiness:" in compiled.output
    assert "varyz model status" in compiled.output

    as_json = runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert '"generation_id"' in as_json.output


def test_model_status_does_not_print_its_checkpoints_as_json(project) -> None:
    """The readiness checkpoint stores twelve gates and printed all of them.

    Each gate carries a measured value, a threshold and a verdict, and the row
    was written to the terminal in its storage format: seven hundred characters
    of braces, on a command people run to see whether a compile worked.
    """

    _prepare_personal_model_compile(project)
    assert (
        runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"]).exit_code == 0
    )

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    assert '"verdict"' not in result.output
    assert '"threshold"' not in result.output
    # A zero is an answer -- "people=0" is what identity resolution found --
    # so the flattening must not drop it along with the braces.
    assert "people=0" in result.output


def test_verifying_a_receipt_says_which_of_the_two_things_went_wrong(project):
    """"Receipt does not verify." covered a typo and a tampered payload alike.

    One is a mistyped id; the other says something disclosed was not what the
    receipt recorded, which is the reason the receipt is kept at all.
    """
    conn = sqlite3.connect(project / "data" / "varyz.db")
    conn.execute(
        """INSERT INTO context_requests
           (id, actor, purpose, destination, intent_hash, requested_types_json,
            requested_scope_json, max_items, max_chars, interaction, user_present,
            reversible, policy_version, created_at)
           VALUES ('req-1','test','test','local','h','[]','{}',1,1,'cli',1,1,'1',
                   '2026-09-10T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO egress_receipts
           (id, request_id, payload_hash, payload_item_count, payload_char_count,
            considered_ids_json, decision_summary_json, policy_version,
            transformation_versions_json, approval_required, receipt_version, created_at)
           VALUES ('receipt-1','req-1','the-real-hash',1,1,'[]','{}','1','{}',0,'1',
                   '2026-09-10T00:00:00+00:00')"""
    )
    conn.commit()
    conn.close()

    missing = runner.invoke(cli.app, ["privacy", "verify", "no-such-receipt", "the-real-hash"])
    assert missing.exit_code == 1, missing.output
    assert "No receipt with id" in missing.output

    mismatch = runner.invoke(cli.app, ["privacy", "verify", "receipt-1", "a-different-hash"])
    assert mismatch.exit_code == 1, mismatch.output
    assert "payload hash is not this one" in mismatch.output

    good = runner.invoke(cli.app, ["privacy", "verify", "receipt-1", "the-real-hash"])
    assert good.exit_code == 0, good.output
    assert "verifies" in good.output


def test_a_grant_that_could_never_be_matched_is_not_recorded_as_consent(project):
    """A grant is matched on its exact destination and purpose.

    One naming neither, or naming a destination with a typo in it, authorizes
    nothing -- and was still written down and reported as created.
    """
    import sqlite3

    empty = runner.invoke(cli.app, ["privacy", "grant", "", "export"])
    assert empty.exit_code == 2, empty.output
    assert "needs a destination" in empty.output

    typo = runner.invoke(cli.app, ["privacy", "grant", "cloud:claud", "export"])
    assert typo.exit_code == 2, typo.output
    assert "Did you mean cloud:claude?" in _flat(typo.output)

    no_purpose = runner.invoke(cli.app, ["privacy", "grant", "cloud:claude", ""])
    assert no_purpose.exit_code == 2, no_purpose.output
    assert "needs a purpose" in no_purpose.output

    real = runner.invoke(cli.app, ["privacy", "grant", "cloud:claude", "export"])
    assert real.exit_code == 0, real.output

    conn = sqlite3.connect(project / "data" / "varyz.db")
    stored = conn.execute("SELECT destination, purpose FROM consent_grants").fetchall()
    conn.close()
    assert stored == [("cloud:claude", "export")]


def test_a_grant_narrowed_to_nothing_is_refused_in_words(project):
    """The narrow scopes were never held to the destination's standard.

    A resource type Varyz labels nothing with authorizes nothing, and was
    written down and reported as created. The two shapes the stored model
    refuses outright -- an id with no type beside it, a content window that
    ends before it starts -- reached the owner as a pydantic traceback over a
    typo in one flag, on the surface that exists to be trusted about what
    leaves this machine.
    """
    import sqlite3

    typo = runner.invoke(
        cli.app, ["privacy", "grant", "cloud:claude", "export", "--resource-type", "entrie"]
    )
    assert typo.exit_code == 2, typo.output
    assert "Did you mean entry?" in _flat(typo.output)

    unpaired = runner.invoke(
        cli.app, ["privacy", "grant", "cloud:claude", "export", "--resource-id", "abc"]
    )
    assert unpaired.exit_code == 2, unpaired.output
    assert "ValidationError" not in unpaired.output
    assert "--resource-type" in _flat(unpaired.output)

    backwards = runner.invoke(
        cli.app,
        [
            "privacy",
            "grant",
            "cloud:claude",
            "export",
            "--scope-from",
            "2026-05-01",
            "--scope-to",
            "2026-01-01",
        ],
    )
    assert backwards.exit_code == 2, backwards.output
    assert "ValidationError" not in backwards.output
    assert "no content at all" in _flat(backwards.output)

    conn = sqlite3.connect(project / "data" / "varyz.db")
    stored = conn.execute("SELECT COUNT(*) FROM consent_grants").fetchone()[0]
    conn.close()
    assert stored == 0


def test_no_command_offers_an_option_it_will_not_explain():
    """`--help` is the whole manual, and nineteen flags were blank rows in it.

    `varyz privacy grant` announced itself as budget-scoped and then showed
    --max-items and --max-chars with nothing beside them, which is the one
    command where a reader guessing at a number decides what leaves the
    machine.
    """
    unexplained = []

    def walk(command, path):
        subcommands = getattr(command, "commands", {})
        if subcommands:
            for name, sub in sorted(subcommands.items()):
                walk(sub, [*path, name])
            return
        for param in command.params:
            if getattr(param, "hidden", False) or param.opts[:1] == ["--help"]:
                continue
            if not (getattr(param, "help", "") or "").strip():
                unexplained.append(f"varyz {' '.join(path)} {param.opts[0]}")

    walk(typer.main.get_command(cli.app), [])
    assert unexplained == []


def test_nothing_tells_a_reader_to_run_a_command_that_only_previews():
    """`varyz model compile` previews and exits; building needs `--run`.

    Three places told somebody stuck to run it: the Mirror page when the graph
    has moved on, the web Mirror when no model is active, and the compiler's
    own advice after a run died between the last checkpoint and the pointer
    swap. Each of them is read by somebody who cannot see what they were
    promised, and each sent them to a command that prints a plan and changes
    nothing, so the next refresh says exactly the same thing.
    """
    import ast

    previewing = {}

    def walk(command, path):
        subcommands = getattr(command, "commands", {})
        if subcommands:
            for name, sub in sorted(subcommands.items()):
                walk(sub, [*path, name])
            return
        for param in command.params:
            # A `--dry-run/--run` pair that defaults to the preview: the bare
            # command is a report, and advice that omits the flag is a loop.
            if param.name == "dry_run" and param.default is True and param.secondary_opts:
                previewing[" ".join(path)] = param.secondary_opts[0]

    walk(typer.main.get_command(cli.app), [])
    assert previewing, "no command previews by default; this test has stopped watching anything"

    loops = []
    for path in sorted((Path(cli.__file__).parent).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for name, activator in previewing.items():
                if f"varyz {name}" in node.value and activator not in node.value:
                    loops.append(f"{path.name}:{node.lineno} names `varyz {name}`")
    assert loops == []


def test_a_key_cannot_be_filed_under_a_name_nothing_reads(project, monkeypatch):
    """Windows Credential Manager takes any string; varyz reads six names.

    A key typed under "anthropic", or under the environment variable's own
    name, was stored and kept, and `varyz secrets show` then called it present
    -- which is how a connected machine reports itself.
    """
    from varyz import secrets as secret_store

    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(secret_store, "set_secret", lambda *a: stored.append(a))

    for typo in ("anthropic", "ANTHROPIC_API_KEY"):
        wrong = runner.invoke(cli.app, ["secrets", "set", typo])
        assert wrong.exit_code == 2, wrong.output
        assert "never reads a secret called" in wrong.output
        assert "anthropic_api_key" in wrong.output
    assert stored == []

    unrecognizable = runner.invoke(cli.app, ["secrets", "show", "nonesuch"])
    assert unrecognizable.exit_code == 2, unrecognizable.output
    assert "Names varyz reads" in _flat(unrecognizable.output)

    # A connector login carries an account after the colon and is a real name.
    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    real = runner.invoke(cli.app, ["secrets", "show", "github_token:someone"])
    assert real.exit_code == 1, real.output
    assert "not set" in real.output


def test_forgetting_an_unread_name_still_runs_but_does_not_imply_a_home(project, monkeypatch):
    """Deleting has to keep working, so a value an older version wrote can go."""
    from varyz import secrets as secret_store

    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    monkeypatch.setattr(secret_store.keyring, "delete_password", lambda *_a: None)

    result = runner.invoke(cli.app, ["secrets", "forget", "anthropic"])
    assert result.exit_code == 0, result.output
    assert "never reads a secret called" in result.output


def test_forgetting_a_secret_with_no_name_is_refused(project, monkeypatch):
    """`varyz secrets forget "$NAME"` with NAME unset reported a removal.

    Deleting under a name varyz never reads has to keep working -- an older
    version may have written one -- but the empty string is not that. It is
    what a shell hands this command when the variable is not set, and the
    answer was a sentence about a secret called '' and an exit code of 0: a
    credential reported gone that was never named, in a script that would
    then carry on as though the cleanup had happened.
    """
    from varyz import secrets as secret_store

    deleted = []
    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    monkeypatch.setattr(secret_store.keyring, "delete_password", lambda *a: deleted.append(a))

    for name in ("", "   "):
        result = runner.invoke(cli.app, ["secrets", "forget", name])
        assert result.exit_code == 2, (name, result.output)
        assert "name is empty" in _flat(result.output), name

    assert deleted == []


def test_a_stored_secret_can_be_seen_and_removed_without_being_printed(project, monkeypatch):
    """`varyz secrets set` could write a key and nothing could take it back.

    A value typed into the wrong name, or a key since revoked at the provider,
    stayed in Windows Credential Manager with no command to remove it and no
    way to ask whether it was there.
    """
    from varyz import secrets as secret_store

    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        secret_store.keyring, "get_password", lambda service, name: vault.get((service, name))
    )
    monkeypatch.setattr(
        secret_store.keyring,
        "set_password",
        lambda service, name, value: vault.__setitem__((service, name), value),
    )
    monkeypatch.setattr(
        secret_store.keyring,
        "delete_password",
        lambda service, name: vault.pop((service, name), None),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    secret_store.set_secret("anthropic_api_key", "sk-secret-value")

    shown = runner.invoke(cli.app, ["secrets", "show", "anthropic_api_key"])
    assert shown.exit_code == 0, shown.output
    assert "credential store" in shown.output
    assert "sk-secret-value" not in shown.output

    forgotten = runner.invoke(cli.app, ["secrets", "forget", "anthropic_api_key"])
    assert forgotten.exit_code == 0, forgotten.output
    assert "sk-secret-value" not in forgotten.output
    assert secret_store.get_secret("anthropic_api_key") is None

    missing = runner.invoke(cli.app, ["secrets", "show", "anthropic_api_key"])
    assert missing.exit_code == 1, missing.output
    assert "not set" in missing.output


def test_an_interpretation_id_that_was_never_offered_is_an_error(project) -> None:
    """A mistyped question ID came back as a JSON error object and exit 0.

    A shell that checks the exit code saw a success, and the person who typed
    it saw their failure in its storage format.
    """

    _prepare_personal_model_compile(project)
    assert (
        runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"]).exit_code == 0
    )

    result = runner.invoke(cli.app, ["model", "interpret", "no-such-question"])

    assert result.exit_code == 1, result.output
    assert '"error"' not in result.output
    assert "question not offered" in result.output


def test_a_lookup_answers_in_words_unless_json_is_asked_for(project) -> None:
    """`varyz model lookup "What does Anna like?"` printed the typed result model.

    Forty lines whose whole content, on an archive with nothing compiled about
    Anna, was `"not_known": true`. Agents read the typed form over MCP and
    --query-json asks for it; a person at a terminal asked a question.
    """

    _prepare_personal_model_compile(project)
    assert (
        runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"]).exit_code == 0
    )

    spoken = runner.invoke(cli.app, ["model", "lookup", "What does Anna like?"])
    assert spoken.exit_code == 0, spoken.output
    assert '"disposition"' not in spoken.output
    assert "Nothing stored answers that." in spoken.output

    typed = runner.invoke(cli.app, ["model", "lookup", "What does Anna like?", "--json"])
    assert typed.exit_code == 0, typed.output
    assert '"disposition"' in typed.output


def test_model_status_says_whose_checkpoints_it_is_showing(project) -> None:
    """The checkpoint block is the newest generation's, which is often not the live one.

    After a re-run that fails, the header reads "active generation: X" and the
    block below it ends "activate: failed" -- for Y.  Nothing said they were
    different generations, so a working model was reported as a broken one.
    """

    _prepare_personal_model_compile(project)
    compiled = runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"])
    assert compiled.exit_code == 0, compiled.output
    conn = sqlite3.connect(project / "data" / "varyz.db")
    active = conn.execute(
        "SELECT generation_id FROM personal_model_active_generation"
    ).fetchone()[0]
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at,failure_reason,failed_at)
           VALUES (?,?,?,?,?,'failed',?,?,?)""",
        (
            "zz-newer-failed-rerun",
            graph_id,
            "fixture-compiler",
            "fixture-policy",
            "fixture-input",
            "2099-01-01T00:00:00+00:00",
            "OperationalError: too many SQL variables",
            "2099-01-01T00:10:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO personal_model_compiler_checkpoints
           (generation_id,stage,state,input_hash,detail_json,completed_at)
           VALUES (?,'activate','failed',?,?,?)""",
        (
            "zz-newer-failed-rerun",
            "fixture-input",
            '{"error_type": "OperationalError"}',
            "2099-01-01T00:10:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "zz-newer-failed-rerun" in flat
    assert "not the live one" in flat
    assert active in flat


def test_model_status_says_a_generation_compiled_without_the_semantic_model(project) -> None:
    """Activated and literal-only are both true at once, and only one is on screen."""
    _prepare_personal_model_compile(project)
    compiled = runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"])
    assert compiled.exit_code == 0, compiled.output

    result = runner.invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "stale: " in flat
    assert "no observations or interpretations of its own" in flat


def test_unavailable_semantic_compile_preserves_active_generation(project, monkeypatch):
    from varyz.personal_model.semantic_runtime import LocalModelUnavailable

    _prepare_personal_model_compile(project)
    literal = runner.invoke(cli.app, ["model", "compile", "--run", "--literal-only"])
    assert literal.exit_code == 0, literal.output
    db_path = project / "data" / "varyz.db"
    conn = sqlite3.connect(db_path)
    before_count = conn.execute("SELECT COUNT(*) FROM personal_model_generations").fetchone()[0]
    active_id = conn.execute(
        "SELECT id FROM personal_model_generations WHERE state='current'"
    ).fetchone()[0]
    conn.close()

    def unavailable(conn, cfg):
        raise LocalModelUnavailable(
            "managed local semantic model is unavailable for: observation, candidate, proposal"
        )

    monkeypatch.setattr(
        "varyz.personal_model.semantic_runtime.build_local_semantic_compiler",
        unavailable,
    )
    result = runner.invoke(cli.app, ["model", "compile", "--run"])

    assert result.exit_code == 1
    flat = _flat(result.output)
    assert "observation, candidate, proposal" in flat
    assert "Start the configured local model" in flat
    assert "--literal-only" in flat
    conn = sqlite3.connect(db_path)
    after_count = conn.execute("SELECT COUNT(*) FROM personal_model_generations").fetchone()[0]
    assert after_count == before_count
    assert (
        conn.execute("SELECT id FROM personal_model_generations WHERE state='current'").fetchone()[
            0
        ]
        == active_id
    )
    conn.close()


# --- misconfiguration is reported, never silently succeeded ------------------
#
# Every failure below used to reach the user as either a raw traceback or as
# "0 new, 0 updated, 0 unchanged, 0 errors" — a line indistinguishable from a
# successful sync of an empty folder. These are the first commands anyone runs,
# so getting them wrong is getting the install wrong.


def _write_config(project, body: str) -> None:
    (project / "config.toml").write_text(
        f'[storage]\ndata_dir = "data"\n\n{body}', encoding="utf-8"
    )


def test_init_writes_a_connector_template_it_then_tells_you_to_edit(project):
    config = (project / "config.toml").read_text(encoding="utf-8")
    assert "[connectors.documents]" in config
    assert "paths =" in config


def test_sync_without_any_connector_names_the_section_to_add(project):
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    # Rich reads square brackets as style markup, so this line once printed as
    # "Add paths under  in config.toml" — the one word that mattered, missing.
    assert "[connectors.*]" in result.output


def test_sync_of_an_unknown_connector_lists_the_real_ones(project):
    result = runner.invoke(cli.app, ["sync", "documnets"])
    assert result.exit_code == 2
    assert "documents" in result.output


def test_sync_of_a_known_but_unconfigured_connector_explains_itself(project):
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 2
    assert "[connectors.documents]" in result.output
    assert "Traceback" not in result.output


def test_a_key_prompt_that_cannot_be_typed_names_the_variable(project, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    stored = []
    monkeypatch.setattr("varyz.secrets.set_secret", lambda *a: stored.append(a))

    result = runner.invoke(cli.app, ["connect", "deepseek"])

    # The sibling connect commands all answer a non-interactive terminal with
    # the way to pass the value. This one echoed a hidden prompt at a stream
    # that could not fill it and then died with a bare "Aborted."
    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output
    assert "Aborted" not in result.output
    assert stored == []


def test_a_key_already_in_the_environment_is_not_asked_for_again(project, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "a key the environment already carries")
    stored = []
    monkeypatch.setattr("varyz.secrets.set_secret", lambda *a: stored.append(a))

    result = runner.invoke(cli.app, ["connect", "deepseek"])

    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY" in result.output
    # Nothing is copied into the credential store: get_secret already falls
    # back to this variable, and writing it there would outlive the export.
    assert stored == []
    config = (project / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "deepseek"' in config


def test_every_provider_key_falls_back_to_its_own_standard_variable():
    from varyz.secrets import ENV_FALLBACKS

    assert ENV_FALLBACKS == {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
    }


def test_a_connector_error_keeps_the_section_name_rich_would_have_eaten(project):
    """The message names the TOML section, and Rich reads that as a style tag.

    `[connectors.documents]` went through Console.print unescaped and came out
    as "Add paths = [...] under  in config.toml" -- the answer deleted from the
    sentence written to give it. Every exception the CLI prints is escaped now,
    because the ones worth printing are the ones that quote config and paths.
    """
    config = project / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[connectors.documents]\napproved = true\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["sync", "documents"])

    assert result.exit_code == 1
    assert "[connectors.documents]" in result.output
    assert "Traceback" not in result.output


def test_a_mistyped_ai_conversation_path_is_refused_not_approved(project):
    """An approval written for a directory that is not there is not an approval.

    Telegram and Instagram both refuse a missing export. This one wrote the
    typo into config.toml, printed "approved", and left the discovery to
    come up empty at the next sync.
    """
    before = (project / "config.toml").read_text(encoding="utf-8")
    result = runner.invoke(
        cli.app,
        ["connect", "ai-conversations", "--path", str(project / "not-here")],
    )
    assert result.exit_code == 1, result.output
    assert "No directory at" in result.output
    assert (project / "config.toml").read_text(encoding="utf-8") == before


def test_a_since_that_is_not_a_date_is_refused_rather_than_ignored(project):
    """A cut-off that cannot be parsed read back as None and never applied.

    The whole history was imported instead, which is the opposite of what
    was asked for and was said nowhere.
    """
    bad = runner.invoke(
        cli.app, ["connect", "ai-conversations", "--since", "last tuesday"]
    )
    assert bad.exit_code == 1, bad.output
    assert "is not a date" in bad.output
    assert "YYYY-MM-DD" in bad.output

    good = runner.invoke(
        cli.app,
        ["connect", "ai-conversations", "--path", str(project), "--since", "2024-01-01"],
    )
    assert good.exit_code == 0, good.output
    assert 'since = "2024-01-01"' in (project / "config.toml").read_text(encoding="utf-8")


def test_autosync_dry_run_discovers_all_configured_sources(project, journal_dir):
    _write_config(
        project,
        f'[connectors.documents]\napproved = true\npaths = ["{journal_dir.as_posix()}"]\n',
    )
    result = runner.invoke(cli.app, ["autosync", "run", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry_run" in result.output and "documents: planned" in result.output
    assert not (project / "data" / "autosync").exists()


def test_storing_a_secret_without_a_terminal_stops_instead_of_waiting(project):
    """A hidden prompt with no console to type at must fail, not hang.

    getpass reads the Windows console directly, so redirecting stdin does not
    answer the prompt: run from a script, `varyz secrets set` waited forever.
    """
    result = runner.invoke(cli.app, ["secrets", "set", "anthropic_api_key"])
    assert result.exit_code == 1, result.output
    assert "needs an interactive terminal" in result.output


def test_autosync_pause_and_resume_manage_the_kill_switch(project):
    paused = runner.invoke(cli.app, ["autosync", "pause"])
    assert paused.exit_code == 0, paused.output
    sentinel = project / "data" / "autosync" / "PAUSED"
    assert sentinel.is_file()
    resumed = runner.invoke(cli.app, ["autosync", "resume"])
    assert resumed.exit_code == 0, resumed.output
    assert not sentinel.exists()


def test_resuming_a_kill_switch_that_was_never_engaged_says_so(project):
    """"Autosync resumed." was printed whether or not anything had been paused.

    It was also printed with no daemon to resume into, so someone who paused,
    uninstalled, and then resumed was told refreshes were back on.
    """
    never_paused = runner.invoke(cli.app, ["autosync", "resume"])
    assert never_paused.exit_code == 0, never_paused.output
    assert "was not engaged" in never_paused.output
    assert "Autosync resumed" not in never_paused.output
    assert "not enabled" in never_paused.output

    assert runner.invoke(cli.app, ["autosync", "pause"]).exit_code == 0
    resumed = runner.invoke(cli.app, ["autosync", "resume"])
    assert resumed.exit_code == 0, resumed.output
    assert "Autosync resumed" in resumed.output


def test_status_describes_the_kill_switch_and_not_its_opposite(project):
    """A safety control has to report its own state, not the machine's.

    The line said "kill switch: active" while nothing was paused, which on a
    fresh install reads as something stopping your syncs.
    """
    idle = runner.invoke(cli.app, ["autosync", "status"])
    assert idle.exit_code == 0, idle.output
    assert "kill switch: not engaged" in idle.output
    assert "(not written yet)" in _flat(idle.output)

    assert runner.invoke(cli.app, ["autosync", "pause"]).exit_code == 0
    engaged = runner.invoke(cli.app, ["autosync", "status"])
    assert engaged.exit_code == 0, engaged.output
    assert "kill switch: ENGAGED" in engaged.output


def test_autosync_status_distinguishes_warnings_from_failures(project):
    state = project / "data" / "autosync" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "finished_at": "2026-08-13T18:48:49+00:00",
                "status": "ok",
                "new": 0,
                "errors": 0,
                "warnings": ["Graph prune failed: synthetic"],
                "graph_status": "updated",
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["autosync", "status"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "last run:" in output
    assert "ok" in output
    assert "0 errors" in output
    assert "1 warning" in output
    assert "graph updated" in output


def test_autosync_status_says_what_failed_a_run(project):
    """A failure no connector caused left the line reading "0 errors".

    The author's last autosync reads "failed", "24 new", "0 errors", "0
    warnings". The cause -- a graph prune that hit a foreign key -- sat in the
    state file the whole time and was printed only into the digest file.
    """
    state = project / "data" / "autosync" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "finished_at": "2026-08-13T18:48:11+00:00",
                "status": "failed",
                "new": 24,
                "errors": 0,
                "warnings": [],
                "graph_status": "updated",
                "detail": "Graph prune failed: IntegrityError: FOREIGN KEY constraint failed",
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["autosync", "status"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "failed" in output
    assert "why: Graph prune failed: IntegrityError: FOREIGN KEY constraint failed" in output


def test_sync_of_an_unapproved_connector_is_an_answer_not_a_traceback(project):
    _write_config(project, "[connectors.documents]\npaths = []\n")
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 1
    assert "approval" in result.output
    assert "Traceback" not in result.output


def test_a_misspelled_paths_key_fails_instead_of_reporting_a_clean_sync(project):
    # `folders` for `paths`: approved, well-formed TOML, and nothing to walk.
    _write_config(project, '[connectors.documents]\napproved = true\nfolders = ["."]\n')
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 1
    assert "no paths configured" in result.output
    assert "0 new" not in result.output


def test_paths_that_do_not_exist_here_fail_instead_of_reporting_a_clean_sync(project):
    _write_config(project, '[connectors.documents]\napproved = true\npaths = ["Z:/nowhere"]\n')
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 1
    assert "none of the configured paths exist" in result.output


def test_one_live_path_among_dead_ones_still_syncs(project, journal_dir):
    # A config shared between two machines is a real thing; only a total miss
    # means the configuration doesn't apply here.
    _write_config(
        project,
        f'[connectors.documents]\napproved = true\npaths = ["Z:/nowhere", {str(journal_dir)!r}]\n',
    )
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 0, result.output
    assert "0 new" not in result.output


def test_a_path_that_is_not_here_is_named_rather_than_passed_over(project, journal_dir):
    """The counts describe what was walked, not what was configured.

    A config shared between two machines is why a missing path is tolerated;
    a mistyped one is why it cannot be silent. The summary read as an
    unqualified success while it had skipped a folder entirely.
    """
    _write_config(
        project,
        f'[connectors.documents]\napproved = true\npaths = ["Z:/nowhere", {str(journal_dir)!r}]\n',
    )
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 0, result.output
    printed = " ".join(result.output.split())
    assert "not on this machine" in printed
    assert "Z:/nowhere" in printed


def test_a_wildcard_root_is_named_as_a_wildcard_not_as_a_missing_disk(project, journal_dir):
    """Roots are literal paths, so a pattern matches nothing and is "missing".

    Saying it is not on this machine to someone who has that folder open reads
    as a broken disk rather than as the one-line config fix it is.
    """
    _write_config(
        project,
        f'[connectors.documents]\napproved = true\n'
        f'paths = [{str(journal_dir / "*.md")!r}, {str(journal_dir)!r}]\n',
    )
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 0, result.output
    printed = " ".join(result.output.split())
    assert "wildcard" in printed
    assert "not on this machine" not in printed


def test_a_wildcard_as_the_only_root_says_so_instead_of_stopping_flat(project, journal_dir):
    _write_config(
        project,
        f'[connectors.documents]\napproved = true\npaths = [{str(journal_dir / "*.md")!r}]\n',
    )
    result = runner.invoke(cli.app, ["sync", "documents"])
    assert result.exit_code == 1
    assert "wildcard" in " ".join(result.output.split())


def test_a_folder_with_no_repositories_is_not_reported_as_a_git_sync(project):
    _write_config(project, f"[connectors.git]\napproved = true\npaths = [{str(project)!r}]\n")
    result = runner.invoke(cli.app, ["sync", "git"])
    assert result.exit_code == 1
    assert "no git repositories found" in result.output


def test_one_broken_connector_does_not_stop_the_others(project, journal_dir):
    """Sources are independent. Stopping the whole sync at the first bad
    section means one mistyped git path leaves the documents unindexed, which
    looks like varyz not working rather than like a typo.
    """
    _write_config(
        project,
        "[connectors.documents]\napproved = true\n"
        f"paths = [{str(journal_dir)!r}]\n\n"
        f"[connectors.git]\napproved = true\npaths = [{str(project)!r}]\n",
    )
    result = runner.invoke(cli.app, ["sync"])
    # Still a failure — the user has something to fix and the exit code says so.
    assert result.exit_code == 1
    assert "no git repositories found" in result.output
    assert "Did not sync: git" in result.output
    # But the documents went in.
    assert "0 new" not in result.output
    search = runner.invoke(cli.app, ["search", "Lena"])
    assert search.exit_code == 0


def test_a_held_write_lock_escapes_the_command_unhandled(project, monkeypatch):
    """Every write command takes the cross-process lock, and a scheduled sync
    holds it for minutes. No command catches it — which is the point of
    catching it once in main(), tested below.
    """
    from varyz.db.connection import LockHeldError

    def held(self):
        raise LockHeldError("Another varyz process is writing (sync already running?)")

    monkeypatch.setattr(cli.WriteLock, "__enter__", held)
    result = runner.invoke(cli.app, ["add", "a thought"])
    assert isinstance(result.exception, LockHeldError)


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (LockHeldError("sync already running?"), 1),
        (KeyboardInterrupt(), 130),
        (
            LocalModelNotInstalled(
                "Local semantic extraction requires the managed model; "
                "run: varyz local-model install"
            ),
            1,
        ),
        (NotAnArchiveError("C:/somewhere/varyz.db is not a varyz archive"), 1),
    ],
    ids=["write lock held", "ctrl-c", "managed model not downloaded", "path is not an archive"],
)
def test_main_turns_an_ordinary_condition_into_a_sentence(monkeypatch, raised, code):
    """Unhandled, all three are a Python traceback — which on a tool somebody
    installed five minutes ago reads as a broken install."""

    def boom(*args, **kwargs):
        raise raised

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == code


def test_a_stopped_daemon_is_a_sentence_naming_the_fix(project, monkeypatch, capsys):
    """`varyz knowledge list` with the daemon stopped printed a urllib traceback."""
    import socket

    from varyz.resolution.client import ResolutionClient

    with socket.socket() as probe:  # a loopback port nothing listens on
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setattr(
        cli, "_resolution_client", lambda cfg: ResolutionClient("127.0.0.1", closed_port)
    )
    monkeypatch.setattr(cli.sys, "argv", ["varyz", "knowledge", "list"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    output = capsys.readouterr().out
    assert exit_info.value.code == 1
    assert "Traceback" not in output
    assert "varyz daemon start" in output


def test_a_missing_optional_download_still_names_the_command_that_fixes_it(
    monkeypatch, capsys
):
    """`varyz graph rebuild` is the second thing the docs tell a new user to
    run, and the managed model is a separate multi-gigabyte download. Before
    it existed the command answered with forty lines of Rich traceback wrapped
    around one accurate sentence."""

    def boom(*args, **kwargs):
        raise LocalModelNotInstalled(
            "Local semantic extraction requires the managed model; "
            "run: varyz local-model install"
        )

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit):
        cli.main()

    printed = _flat(capsys.readouterr().out)
    assert "run: varyz local-model install" in printed
    assert "Traceback" not in printed


def _flat(output: str) -> str:
    """Rich wraps at the terminal width; assertions are about words, not columns.

    Typer also colours its errors wherever GITHUB_ACTIONS is set, so CI output
    carries escape codes a terminal-less run does not.
    """
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_a_search_result_is_a_line_of_prose_not_a_page_of_layout():
    """A match cell reproduced the document's own line breaks, blank ones too.

    An entry keeps the layout of the file it came from, so a two-hundred
    character excerpt printed as fourteen table rows, most of them empty, and
    four results filled the screen with whitespace. A cell is one flowed line;
    the paragraphing belongs to the entry, not to the table.
    """
    from varyz.retrieval.search import MATCH_CLOSE, MATCH_OPEN

    laid_out = f"title\n\n# Heading\n\nTalked to {MATCH_OPEN}Mira{MATCH_CLOSE} about it.\n\n"
    cell = cli._match_cell(laid_out)

    assert "\n" not in cell.plain
    assert cell.plain == "title # Heading Talked to Mira about it."
    assert any(span.style == "bold" for span in cell.spans)

    flooding = cli._match_cell("x " * 900)
    assert len(flooding.plain) <= cli.MATCH_CELL_CHARS + 1
    assert flooding.plain.endswith("\u2026")


def test_progress_logs_are_off_unless_the_caller_asks(monkeypatch) -> None:
    """A command somebody is watching should not narrate itself.

    The default keeps Python's last-resort handler, which prints warnings and
    drops everything under them.
    """

    monkeypatch.delenv("VARYZ_LOG_LEVEL", raising=False)
    package = logging.getLogger("varyz")
    before = list(package.handlers)

    cli._configure_logging()

    assert package.handlers == before


def test_a_supervised_compile_can_ask_for_its_progress_lines(monkeypatch) -> None:
    """The long stage of a compile writes nothing for tens of minutes.

    Its ``log.info`` calls are the only thing that separates a run doing work
    from a run that has hung, and the default level drops exactly those.
    """

    monkeypatch.setenv("VARYZ_LOG_LEVEL", "info")
    package = logging.getLogger("varyz")
    before_handlers = list(package.handlers)
    before_level = package.level
    before_propagate = package.propagate
    try:
        cli._configure_logging()
        assert package.level == logging.INFO
        assert len(package.handlers) == 1
        # Ours, so an INFO level here cannot turn on urllib3 or httpx as well.
        assert package.propagate is False
    finally:
        package.handlers = before_handlers
        package.setLevel(before_level)
        package.propagate = before_propagate


def test_a_level_that_is_not_a_level_does_not_end_the_command(monkeypatch) -> None:
    monkeypatch.setenv("VARYZ_LOG_LEVEL", "loud")
    package = logging.getLogger("varyz")
    before_handlers = list(package.handlers)
    before_level = package.level
    before_propagate = package.propagate
    try:
        cli._configure_logging()
        assert package.level == logging.WARNING
    finally:
        package.handlers = before_handlers
        package.setLevel(before_level)
        package.propagate = before_propagate


def _one_entry_id(project):
    conn = sqlite3.connect(project / "data" / "varyz.db")
    try:
        return conn.execute("SELECT id FROM entries LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _exclusions(project):
    conn = sqlite3.connect(project / "data" / "varyz.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM entry_exclusions").fetchone()[0]
    finally:
        conn.close()


def test_forget_leaves_a_tombstone_so_the_next_sync_does_not_undo_it(project):
    """The connector still holds the upstream item after the local row is gone.

    Without a tombstone the very next `varyz sync` re-imported the mail someone
    had just asked varyz to forget, and the command that promised "permanently"
    said nothing about it.
    """
    assert runner.invoke(cli.app, ["add", "a note to be forgotten"]).exit_code == 0
    entry_id = _one_entry_id(project)

    result = runner.invoke(cli.app, ["forget", entry_id, "-y"])

    assert result.exit_code == 0, result.output
    assert "blocked from re-import" in result.output
    assert _exclusions(project) == 1


def test_forget_can_still_be_asked_for_the_one_time_kind(project):
    assert runner.invoke(cli.app, ["add", "a note that may return"]).exit_code == 0
    entry_id = _one_entry_id(project)

    result = runner.invoke(cli.app, ["forget", entry_id, "-y", "--allow-reimport"])

    assert result.exit_code == 0, result.output
    assert "may bring it back" in result.output
    assert _exclusions(project) == 0


def test_forget_says_the_id_is_missing_rather_than_that_nothing_matched_it(project):
    """`varyz forget "$ID"` with an unset variable is a shell slip, not a miss.

    It printed "No entry with id ." -- a sentence about an id that was never
    given -- and exited 1, which reads as "that entry is gone already".
    """
    assert runner.invoke(cli.app, ["add", "a note nobody asked to delete"]).exit_code == 0

    empty = runner.invoke(cli.app, ["forget", "", "-y"])
    assert empty.exit_code == 2, empty.output
    assert "the entry id is empty" in empty.output

    missing = runner.invoke(cli.app, ["forget", "01ZZZZZZZZZZZZZZZZZZZZZZZZ", "-y"])
    assert missing.exit_code == 1, missing.output
    assert "No entry with id 01ZZZZZZZZZZZZZZZZZZZZZZZZ." in " ".join(missing.output.split())

    markup = runner.invoke(cli.app, ["forget", "[red]not an id[/red]", "-y"])
    assert markup.exit_code == 1, markup.output
    assert "[red]not an id[/red]" in " ".join(markup.output.split())

    with sqlite3.connect(project / "data" / "varyz.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1


def test_status_says_which_provider_key_is_only_an_environment_variable(project, monkeypatch):
    """Both kinds of key work; only one of them survives closing the terminal."""
    import varyz.secrets as secret_store

    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    monkeypatch.setenv("OPENAI_API_KEY", "a key the environment already carries")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = runner.invoke(cli.app, ["connect", "status"])

    assert result.exit_code == 0, result.output
    assert "OPENAI_API_KEY" in result.output


def test_status_does_not_call_a_provider_that_cannot_answer_the_one_in_use(project, monkeypatch):
    """The dot marks the provider varyz would call, not one that would answer.

    A machine carrying only OPENAI_API_KEY read as set up: a connected row, a
    dot on another row, and nothing saying those were different lines.
    """
    import varyz.secrets as secret_store

    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    monkeypatch.setenv("OPENAI_API_KEY", "a key the environment already carries")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = runner.invoke(cli.app, ["connect", "status"])

    assert result.exit_code == 0, result.output
    assert "The active provider" in result.output
    assert "Already connected: openai" in result.output


def test_status_stays_quiet_when_the_provider_in_use_is_the_connected_one(project, monkeypatch):
    """The warning is about a mismatch, so a machine set up right never sees it."""
    import varyz.secrets as secret_store

    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a key the environment already carries")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = runner.invoke(cli.app, ["connect", "status"])

    assert result.exit_code == 0, result.output
    assert "The active provider" not in result.output


def test_ask_does_not_collect_approval_for_a_call_it_cannot_make(project, monkeypatch):
    """A payload preview and a yes/no are consent for an imminent cloud call.

    With no credentials there is none, and `ask` before `connect` is the order a
    first run takes.
    """
    import varyz.retrieval.rag as rag

    import varyz.secrets as secret_store

    # An empty archive answers before it reaches any of this, so the case
    # under test needs something to have been asked about.
    assert runner.invoke(cli.app, ["add", "I moved to Berlin in 2021."]).exit_code == 0
    monkeypatch.setattr(secret_store.keyring, "get_password", lambda *_a: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        rag,
        "ask",
        lambda *_a, **_k: SimpleNamespace(
            approval_required=True,
            preflight=None,
            payload_preview="every excerpt this would send",
            payload_hash="0" * 64,
            answer="",
        ),
    )

    result = runner.invoke(cli.app, ["ask", "anything at all"])

    assert result.exit_code == 1, result.output
    assert "varyz connect claude" in result.output
    assert "payload preview" not in result.output


def test_verifying_a_receipt_needs_both_a_receipt_and_a_hash(project):
    """This command answers whether a disclosure matched its record.

    Answering that about nothing is the one thing it must not do, and
    `varyz privacy verify "$R" "$H"` unset got "No receipt with id ."
    """
    both = runner.invoke(cli.app, ["privacy", "verify", "", ""])
    assert both.exit_code == 2, both.output
    assert "a receipt id and a payload hash" in _flat(both.output)

    hash_only = runner.invoke(cli.app, ["privacy", "verify", "r", ""])
    assert hash_only.exit_code == 2, hash_only.output
    assert "without a payload hash" in _flat(hash_only.output)

    unknown = runner.invoke(cli.app, ["privacy", "verify", "no-such-receipt", "0" * 64])
    assert unknown.exit_code == 1, unknown.output
    assert "No receipt with id no-such-receipt" in _flat(unknown.output)


def test_closing_a_todo_needs_an_id(project):
    """"No todo with that id" reads as a lookup that ran; none did."""
    assert runner.invoke(cli.app, ["todo", "add", "call the bank"]).exit_code == 0

    empty = runner.invoke(cli.app, ["todo", "done", ""])
    assert empty.exit_code == 2, empty.output
    assert "the todo id is empty" in _flat(empty.output)

    unknown = runner.invoke(cli.app, ["todo", "done", "not-an-id"])
    assert unknown.exit_code == 1, unknown.output

    with sqlite3.connect(project / "data" / "varyz.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM todos WHERE status='open'").fetchone()[0] == 1


def test_a_file_that_is_not_an_archive_is_named_rather_than_raised(tmp_path):
    """A half-copied file, a sync conflict, a path pointed at the wrong thing.

    sqlite answers "file is not a database" and every command -- read ones
    included -- ended in a traceback that named sqlite rather than the file.
    """
    from varyz.db import connection

    not_an_archive = tmp_path / "varyz.db"
    not_an_archive.write_text("this was never a database", encoding="utf-8")

    with pytest.raises(NotAnArchiveError) as raised:
        connection.connect(not_an_archive)

    said = str(raised.value)
    assert "varyz.db" in said
    assert "varyz backup" in said

    # A real archive still opens, so the guard is about this file and not
    # about every DatabaseError sqlite can raise.
    real = tmp_path / "real.db"
    connection.connect(real).close()


def test_prune_names_the_activated_history_it_will_not_touch(project) -> None:
    """Three superseded generations held more rows than prune could discard.

    Nothing said so, so the database grew by gigabytes while this command
    reported there was nothing to do. They are kept on purpose -- a later
    compile reuses their records -- and that is the sentence that was missing.
    """
    _prepare_personal_model_compile(project)
    conn = sqlite3.connect(project / "data" / "varyz.db")
    graph_id = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()[0]
    conn.execute(
        """INSERT INTO personal_model_generations
           (id,graph_generation_id,compiler_version,policy_version,input_content_hash,
            state,created_at,record_count)
           VALUES (?,?,?,?,?,'staged','2026-08-13T09:00:00+00:00',2)""",
        ("kept-history", graph_id, "fixture-compiler", "fixture-policy", "fixture-input"),
    )
    # Records are immutable once their generation is activated, so they go in
    # while it is still staged -- the order a real compile writes them in.
    for record_id in ("one", "two"):
        conn.execute(
            """INSERT INTO personal_model_records
               (generation_id,record_id,record_kind,record_json,canonical_json_hash,
                dependency_hash,readiness,sensitivity,created_at)
               VALUES ('kept-history',?,'fact','{}','hash','deps','staged','public',
                       '2026-08-13T09:00:00+00:00')""",
            (record_id,),
        )
    conn.execute(
        "UPDATE personal_model_generations SET state='superseded', activated_at=?"
        " WHERE id='kept-history'",
        ("2026-08-13T09:30:00+00:00",),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(cli.app, ["model", "prune"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "1 earlier activated generation(s) hold 2 records" in flat
    assert "reuses their records" in flat


def _lookup_result(**overrides):
    fields = {
        "disposition": "answered",
        "clarification": None,
        "ambiguity_choices": (),
        "answers": (),
        "withheld_records": (),
        "sensitive_records_withheld": False,
        "uncompiled_evidence_candidates": (),
        "generation_id": "a-generation",
    }
    return SimpleNamespace(**{**fields, **overrides})


def test_a_lookup_does_not_invent_a_count_for_what_it_withheld(capsys) -> None:
    """The direct query path carries the fact that something was held back, not
    how much. `len(withheld) or 1` turned "some" into "1", so a live archive
    said one record was withheld without having counted anything."""
    cli._report_lookup(_lookup_result(sensitive_records_withheld=True))
    uncounted = _flat(capsys.readouterr().out)
    assert "Sensitive material was withheld from this answer" in uncounted
    assert "1 matching record(s)" not in uncounted

    # Where markers exist the count is real and stays.
    cli._report_lookup(
        _lookup_result(
            sensitive_records_withheld=True,
            withheld_records=(object(), object(), object()),
        )
    )
    assert "3 matching record(s) were withheld" in _flat(capsys.readouterr().out)
def test_a_prompt_nothing_can_answer_names_the_flag_that_answers_it(monkeypatch, capsys):
    """Every confirmation goes through this, the one-time cloud-egress approval
    included: an unanswered question is a no, and the reply says what to pass
    next time instead of printing click's bare "Aborted."."""

    def _abort(*_args, **_kwargs):
        raise typer.Abort()

    monkeypatch.setattr(cli.typer, "confirm", _abort)

    with pytest.raises(typer.Exit) as raised:
        cli._confirm("Approve this exact payload for one cloud call?", instead="Nothing was sent.")

    assert raised.value.exit_code == 1
    printed = _flat(capsys.readouterr().out)
    assert "went unanswered" in printed
    assert "Nothing was sent." in printed
    assert "Aborted" not in printed
def test_a_stored_instant_is_shown_as_the_day_it_was_here() -> None:
    """Every timestamp is stored in UTC, and every table that showed one sliced
    the first ten characters off the string: a note captured at 00:25 in Berlin
    was filed under the day before, and the search for today did not list it."""
    from varyz.domain import local_day, local_stamp

    # One instant, two spellings. Slicing gave two different days.
    assert local_day("2026-09-10T22:25:52+00:00") == local_day("2026-09-11T00:25:52+02:00")
    assert local_stamp("2026-09-10T22:25:52+00:00") == local_stamp("2026-09-11T00:25:52+02:00")
    assert local_day(None) == "undated"
    assert local_day(None, empty="not dated") == "not dated"
    assert local_stamp(None) == ""
    # Nothing displayable is worse than something odd: a value that is not a
    # timestamp is shown as it was stored rather than raising in a table.
    assert local_day("sometime in May") == "sometime i"


def test_dismissing_a_project_with_nothing_pending_is_done_not_an_error(monkeypatch):
    """`varyz dismiss --project scratch` said "name ... a --project" and exited 2."""
    pending = [{"id": "e1", "project": "lumen"}]
    closed: list[str] = []
    client = SimpleNamespace(pending=lambda: pending, dismiss=lambda i: closed.append(i) or True)
    monkeypatch.setattr(cli, "_cfg", lambda config: SimpleNamespace())
    monkeypatch.setattr(cli, "_resolution_client", lambda cfg: client)

    quiet = runner.invoke(cli.app, ["dismiss", "--project", "scratch"])
    assert quiet.exit_code == 0 and "No pending questions from scratch." in quiet.output
    done = runner.invoke(cli.app, ["dismiss", "--project", "lumen"])
    assert done.exit_code == 0 and closed == ["e1"]
    assert runner.invoke(cli.app, ["dismiss"]).exit_code == 2

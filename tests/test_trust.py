"""Interactive trust-center workflows over the real local domain services."""

from __future__ import annotations

import typer
from rich.console import Console

from varyz.connectors.manual import ManualConnector
from varyz.deletion import forget_entry
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.trust import TrustCenter


def _center(tmp_path, conn, cfg, answers, *, confirmations=(True,)):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[storage]\ndata_dir = "{cfg.data_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    prompts = iter(answers)
    confirms = iter(confirmations)
    console = Console(record=True, width=100, force_terminal=False)
    center = TrustCenter(
        config_path,
        conn,
        console,
        prompt=lambda _label: next(prompts),
        confirm=lambda _label: next(confirms),
    )
    return center, console


def test_guided_delete_previews_impact_and_blocks_reimport(tmp_path, conn, vault, cfg):
    connector = ManualConnector()
    item = ManualConnector.make_item("private launch discussion")
    entry_id, _ = ingest_item(
        conn,
        vault,
        connector,
        SensitivityGate(cfg.sensitivity),
        item,
    )
    center, console = _center(tmp_path, conn, cfg, ["launch", "1", "1"])

    center._delete_entry()

    assert conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM entry_exclusions").fetchone()[0] == 1
    output = console.export_text()
    assert "upstream file/message/event will not be touched" in output
    assert "blocked from re-import" in output


def test_exclusion_can_be_removed_from_the_guided_interface(
    tmp_path, conn, vault, cfg
):
    connector = ManualConnector()
    item = ManualConnector.make_item("temporary exclusion")
    entry_id, _ = ingest_item(
        conn,
        vault,
        connector,
        SensitivityGate(cfg.sensitivity),
        item,
    )
    forget_entry(conn, vault, entry_id, prevent_reimport=True)
    center, console = _center(tmp_path, conn, cfg, ["1"])

    center._review_exclusions(list(conn.execute("SELECT * FROM entry_exclusions")))

    assert conn.execute("SELECT COUNT(*) FROM entry_exclusions").fetchone()[0] == 0
    assert "future sync may restore" in console.export_text()


def test_the_overview_counts_one_thing_as_one_and_names_what_needs_attention(
    tmp_path, conn, vault, cfg
):
    """The trust center is the inspection surface, and it summarised badly.

    One entry read "1 entries · 1 chunks · 1 receipts · 1 unrevoked grants",
    and the closing line was "Needs attention: 1 privacy checks, 0 failed
    graph runs" -- a count of checks rather than the check, listing the
    category that was fine beside the one that was not.
    """
    connector = ManualConnector()
    ingest_item(
        conn,
        vault,
        connector,
        SensitivityGate(cfg.sensitivity),
        ManualConnector.make_item("one single thought"),
    )
    conn.execute(
        """INSERT INTO consent_grants
           (id, actor, destination, purpose, valid_from, valid_until, created_at)
           VALUES ('grant-expired','local_user','export:file','export',
                   '2020-01-01T00:00:00+00:00','2020-01-01T00:30:00+00:00',
                   '2020-01-01T00:00:00+00:00')"""
    )
    conn.commit()
    center, console = _center(tmp_path, conn, cfg, [])

    center.overview()

    output = " ".join(console.export_text().split())
    assert "1 entry ·" in output
    assert "entries ·" not in output
    assert "1 unrevoked grant" in output
    assert "unrevoked grants" not in output
    assert "privacy checks" not in output
    assert "failed graph runs" not in output
    assert "Expired grants remain stored" in output


def test_a_redacted_url_survives_the_console_that_prints_it():
    """The redaction marker is bracketed, and Rich eats brackets.

    `_safe_error` replaces a URL with "[redacted-url]" so a bearer token in an
    exception never reaches a durable log. Printed through Rich markup without
    escaping, the marker itself was read as a style tag and dropped, leaving
    "cannot reach  now" -- a sentence that looks truncated and no longer says
    that anything was removed or why.
    """
    from rich.markup import escape

    from varyz.autosync import _safe_error

    exc = ConnectionError("cannot reach https://cal.example/secret.ics now")
    console = Console(record=True, width=200, force_terminal=False)

    console.print(f"[red]Sync failed: {escape(_safe_error(exc))}[/red]")

    output = console.export_text()
    assert "[redacted-url]" in output
    assert "cal.example" not in output


def test_source_scope_never_echoes_calendar_bearer_url():
    options = {
        "account": "private-calendar",
        "url": "https://calendar.example/private/bearer-token.ics",
    }
    rendered = TrustCenter._safe_source_scope("calendar", options)
    assert rendered == "private-calendar · secret URL in credential manager"
    assert "bearer-token" not in rendered
def test_the_menu_answers_an_unanswerable_terminal_with_the_flag_that_needs_none(
    tmp_path, conn, cfg
):
    """The centre opens with a menu, and a pipe or a scheduled task answers EOF.
    click turned that into a bare "Aborted." while --summary prints the same
    overview and asks nothing."""

    def _eof(_label):
        raise typer.Abort()

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[storage]{chr(10)}data_dir = "{cfg.data_dir.as_posix()}"{chr(10)}',
        encoding="utf-8",
    )
    console = Console(record=True, width=100, force_terminal=False)
    TrustCenter(config_path, conn, console, prompt=_eof, confirm=_eof).run()

    output = console.export_text()
    assert "No settings changed." in output
    assert "varyz trust --summary" in output


def test_a_build_without_the_graph_builder_offers_no_graph_review(
    tmp_path, conn, cfg, monkeypatch
):
    """The alpha builds no graph, so the centre neither offers nor counts one."""
    monkeypatch.setattr("varyz.trust.builder_is_shipped", lambda: False)
    center, console = _center(tmp_path, conn, cfg, ["1", "0"])

    center.run()

    output = " ".join(console.export_text().split())
    assert "Knowledge graph review" not in output
    assert "3 Privacy, receipts, and consent" in output
    assert "4 Delete or restore exclusions" in output
    assert "Graph not built" not in output
    assert "No recorded privacy failures." in output

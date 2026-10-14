"""Inspect and control the local rule experiment without changing live rules."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from varyz.config import ConfigError, load_config
from varyz.resolution import shadow

app = typer.Typer(help="Local A/B shadow comparisons; current rules still govern agents.")
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Local Varyz config")]


class Control(StrEnum):
    enable = "enable"
    pause = "pause"
    resume = "resume"
    disable = "disable"


class Outcome(StrEnum):
    better = "better"
    worse = "worse"
    equivalent = "equivalent"
    unclear = "unclear"


class Window(StrEnum):
    development = "development"
    held_out = "held_out"
    replication = "replication"


def _print(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


@app.command("status")
def status(config: ConfigOpt = None) -> None:
    """Show collection counts, bounds and review outcomes."""
    _print(shadow.status(load_config(config)))


@app.command("control")
def control(
    action: Annotated[Control, typer.Argument(help="Collection action; history is retained")],
    config: ConfigOpt = None,
) -> None:
    """Enable, pause, resume or disable collection; retain existing history."""
    try:
        cfg = load_config(config)
        shadow.control(cfg, action.value)
        _print(shadow.status(cfg))
    except (ValueError, OSError, ConfigError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("doctor")
def doctor(config: ConfigOpt = None) -> None:
    """Check journal integrity without initializing or writing it."""
    result = shadow.doctor(load_config(config))
    _print(result)
    if result["integrity"] not in {"ok", "not_initialized"} or result.get("foreign_key_errors"):
        raise typer.Exit(1)


@app.command("comparisons")
def comparisons(
    limit: Annotated[int, typer.Option(min=1, max=100, help="Recent comparisons to show")] = 20,
    config: ConfigOpt = None,
) -> None:
    """Show ID-level packet differences and exact-answer controls; no private text."""
    _print(shadow.comparisons(load_config(config), limit))


@app.command("review")
def review(
    seq: Annotated[int, typer.Argument(help="Comparison sequence from comparisons")],
    outcome: Annotated[Outcome, typer.Argument(help="Human paired judgment")],
    window: Annotated[Window, typer.Argument(help="Evaluation window for this judgment")]
    = Window.development,
    config: ConfigOpt = None,
) -> None:
    """Append a human judgment; no automatic promotion or activation."""
    try:
        shadow.review(load_config(config), seq, outcome.value, window.value)
    except (sqlite3.Error, ConfigError) as exc:
        typer.echo(type(exc).__name__, err=True)
        raise typer.Exit(1) from exc
    _print({"comparison": seq, "outcome": outcome.value, "window": window.value})


@app.command("probe")
def probe(
    task: Annotated[str, typer.Option(help="Example task to compile locally")]
    = "Implement a Python tool for Varyz",
    project: Annotated[str, typer.Option(help="Working project scope")] = "varyz",
    dry_run: Annotated[bool, typer.Option(help="Preview; --no-dry-run records an excluded probe")]
    = True,
    config: ConfigOpt = None,
) -> None:
    """Compile locally; --no-dry-run records a probe excluded from prospective counts."""
    from varyz.resolution.briefing import start_task
    from varyz.resolution.shadow_policy import compile_packet, project_records

    cfg = load_config(config)
    with contextlib.closing(sqlite3.connect(cfg.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        baseline = start_task(conn, task, project=project)
        if dry_run:
            records = project_records(conn, max_records=cfg.rule_shadow.max_source_records)
            proposal = compile_packet(records, task, project,
                                      {x["domain"] for x in baseline["kinds_of_work"]})
            _print({"dry_run": True, "proposal": proposal})
        else:
            recorded = shadow.observe_briefing(conn, cfg, task, project, "shadow-probe", baseline,
                                                origin="probe")
            _print({"recorded": recorded, "prospective": False, "status": shadow.status(cfg)})

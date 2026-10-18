"""Owner controls for compact agent reports; all commands stay local."""

from __future__ import annotations

import contextlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from varyz import session_progress as progress
from varyz.config import load_config
from varyz.db.connection import open_db

app = typer.Typer(help="Inspect, pause and erase concise agent session progress (local only).")
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Archive configuration file")
]
ProjectArg = Annotated[str, typer.Argument(help="Exact project name")]
SessionArg = Annotated[str, typer.Argument(help="Recorded conversation ID to inspect or erase")]


class ReportOrigin(StrEnum):
    agent_report = "agent_report"
    mined_report = "mined_report"


@app.command("latest")
def latest(
    project: ProjectArg,
    topic: Annotated[str | None, typer.Option(help="Literal topic words to match together")] = None,
    agent: Annotated[
        str | None, typer.Option(help="Client name or exact registered agent ID")
    ] = None,
    origin: Annotated[ReportOrigin | None, typer.Option(help="Live or mined reports only")] = None,
    config: ConfigOpt = None,
) -> None:
    cfg = load_config(config)
    with contextlib.closing(open_db(cfg.db_path)) as conn:
        value = progress.history(
            conn, progress.HistoryQuery(project=project, topic=topic, agent=agent,
                                        origin=origin.value if origin else None), cfg=cfg
        )
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command("history")
def history(
    project: ProjectArg,
    conversation_id: SessionArg,
    before_seq: Annotated[
        int | None,
        typer.Option(help="Continue before next_before_seq from the previous page", min=1),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    cfg = load_config(config)
    with contextlib.closing(open_db(cfg.db_path)) as conn:
        value = progress.history(
            conn,
            progress.HistoryQuery(
                project=project,
                conversation_id=conversation_id,
                latest=False,
                before_seq=before_seq,
            ),
            cfg=cfg,
        )
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command("pause")
def pause(config: ConfigOpt = None) -> None:
    cfg = load_config(config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    progress.pause_path(cfg.data_dir).touch()
    typer.echo("Session recording paused; existing reports remain readable.")


@app.command("resume")
def resume(config: ConfigOpt = None) -> None:
    progress.pause_path(load_config(config).data_dir).unlink(missing_ok=True)
    typer.echo("Session recording resumed.")


class MiningMethod(StrEnum):
    extractive = "extractive"
    local = "local"


@app.command("mine")
def mine(
    project: ProjectArg,
    apply: Annotated[
        bool, typer.Option(help="Store reports; otherwise preview without writing")
    ] = False,
    adapter: Annotated[
        str | None, typer.Option(help="Approved adapter ID, e.g. claude_code")
    ] = None,
    conversation_id: Annotated[
        str | None, typer.Option(help="Mine only this source session ID")
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum new reports this run", min=1, max=1000)] = 25,
    method: Annotated[
        MiningMethod,
        typer.Option(help="Extract source statements, or let installed local AI select them"),
    ] = MiningMethod.extractive,
    config: ConfigOpt = None,
) -> None:
    from varyz.db.connection import FileLock
    from varyz.session_miner import mine as run_miner

    cfg = load_config(config)
    lock = FileLock(cfg.data_dir / "session-mining.lock")
    if not lock.acquire():
        typer.echo("Another session miner is running.")
        raise typer.Exit(1)
    try:
        with contextlib.closing(open_db(cfg.db_path)) as conn:
            result = run_miner(
                conn,
                cfg,
                project=project,
                apply=apply,
                adapter_id=adapter,
                conversation_id=conversation_id,
                limit=limit,
                method=method.value,
            )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if result["errors"]:
            raise typer.Exit(1)
    except ValueError as error:
        # Only deliberate preflight messages reach here; data/inference errors
        # are counted by the miner without exposing transcript contents.
        from varyz.privacy.secrets_scan import redact

        typer.echo(redact(str(error))[0])
        raise typer.Exit(1) from None
    finally:
        lock.release()


@app.command("forget")
def forget(project: ProjectArg, conversation_id: SessionArg, config: ConfigOpt = None) -> None:
    cfg = load_config(config)
    with contextlib.closing(open_db(cfg.db_path)) as conn:
        count = progress.forget(conn, project, conversation_id)
    typer.echo(f"Erased {count} checkpoints and their search text.")


@app.command("doctor")
def doctor(config: ConfigOpt = None) -> None:
    cfg = load_config(config)
    with contextlib.closing(open_db(cfg.db_path)) as conn:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.execute(
            "INSERT INTO session_progress_fts(session_progress_fts) VALUES('integrity-check')"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM session_progress").fetchone()[0]
        mined = conn.execute(
            "SELECT COUNT(*) FROM session_progress WHERE origin='mined_report'"
        ).fetchone()[0]
        receipts = conn.execute("SELECT COUNT(*) FROM session_mining_files").fetchone()[0]
        mismatches = conn.execute(
            "SELECT (SELECT COUNT(*) FROM session_progress p LEFT JOIN session_progress_fts f "
            "ON p.seq=f.rowid WHERE f.rowid IS NULL) + "
            "(SELECT COUNT(*) FROM session_progress_fts f LEFT JOIN session_progress p "
            "ON p.seq=f.rowid WHERE p.seq IS NULL)"
        ).fetchone()[0]
    ok = integrity == "ok" and mismatches == 0
    typer.echo(
        json.dumps(
            {
                "ok": ok,
                "checkpoints": count,
                "mined_reports": mined,
                "scan_receipts": receipts,
                "index_mismatches": mismatches,
                "paused": progress.pause_path(cfg.data_dir).exists(),
                "storage": "local SQLite; bounded local backfill, no uploads",
            }
        )
    )
    if not ok:
        raise typer.Exit(1)

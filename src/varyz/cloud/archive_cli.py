"""Local archive approval controls; the ChatGPT connection remains read-only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import keyring
import typer

from varyz.cloud.archive_sync import (
    ArchiveSync,
    allow_project,
    approve,
    entry_project,
    read_archive_journal,
)
from varyz.cloud.cli import ConfigOpt, _connected, _fail, _paths
from varyz.cloud.local_sync import read_journal
from varyz.config import load_config
from varyz.db.connection import open_db

app = typer.Typer(
    help="Explicit approved evidence sync. Default off; never selects your whole archive."
)
Entries = Annotated[list[str], typer.Option("--entry", help="Exact local entry ID; repeat")]


def journal_path(config: Path) -> Path:
    return config.with_name(config.stem + ".cloud-archive.json")


@app.command("projects")
def projects(config: ConfigOpt = None) -> None:
    """List project categories and approved scopes offline, including General."""
    try:
        resolved, _ = _paths(config)
        journal = read_archive_journal(journal_path(resolved))
        conn = open_db(load_config(resolved).db_path)
        try:
            counts: dict[str, int] = {"*": 0, **{name: 0 for name in journal["projects"]}}
            for row in conn.execute(
                "SELECT * FROM entries WHERE source IN "
                "('ai_conversations','documents','git','github')"
            ):
                project = entry_project(row, journal["projects"]) or "*"
                counts[project] = counts.get(project, 0) + 1
            result = [
                {
                    "project": "General" if project == "*" else project,
                    "records": count,
                    "allowed": project in journal["projects"],
                }
                for project, count in sorted(counts.items(), key=lambda r: (r[0] != "*", r[0]))
            ]
            typer.echo(json.dumps({"projects": result, "sends_text": False}))
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("allow")
def allow(
    project: Annotated[
        str | None, typer.Option("--project", help="Project name; defaults to current folder")
    ] = None,
    root: Annotated[
        Path | None, typer.Option("--root", help="Exact project folder; defaults to current folder")
    ] = None,
    repository: Annotated[
        str | None, typer.Option("--repository", help="Optional exact GitHub owner/repository")
    ] = None,
    approve_sharing: Annotated[
        bool,
        typer.Option(
            "--approve-sharing",
            help="Approve this project's eligible history and future records for Cloud and AI",
        ),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Allow a project once. Existing and future eligible project records can sync automatically."""
    try:
        if not approve_sharing:
            raise ValueError(
                "Approval required: this project's eligible history and future records "
                "will be readable by Cloud and connected AI"
            )
        resolved, _ = _paths(config)
        folder = (root or Path.cwd()).resolve()
        if not folder.is_dir():
            raise ValueError("Choose an existing project folder")
        allow_project(journal_path(resolved), project or folder.name, folder, repository)
        typer.echo(
            "Project allowed. Eligible existing and future records can sync; "
            "sensitive, unclassified and unrelated records stay local. Nothing sent now."
        )
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("select")
def select(
    entry: Entries,
    project: Annotated[str, typer.Option("--project", help="Project these records belong to")],
    approve_sharing: Annotated[
        bool,
        typer.Option(
            "--approve-sharing",
            help="Approve these exact records for Cloud and connected AI retrieval",
        ),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Approve exact content versions locally. This sends nothing."""
    try:
        if not approve_sharing:
            raise ValueError(
                "Approval required: selected text will be readable by Cloud and connected AI"
            )
        resolved, _ = _paths(config)
        conn = open_db(load_config(resolved).db_path)
        try:
            typer.echo(json.dumps(approve(conn, journal_path(resolved), entry, project)))
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("sync")
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Offline counts; no content sent")
    ] = False,
    semantic: Annotated[
        bool,
        typer.Option(
            "--semantic", help="Create vectors locally using the configured supported model"
        ),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Send approved records, checkpoint each acknowledgement, and remove revoked records."""
    try:
        resolved, connection_path = _paths(config)
        cfg = load_config(resolved)
        conn = open_db(cfg.db_path)
        try:
            worker = ArchiveSync(conn, journal_path(resolved))
            if dry_run:
                result = worker.preview()
            else:
                connection = read_journal(connection_path)
                with _connected(config) as (_, _, client):
                    embedder = None
                    if semantic:
                        from varyz.pipeline.embed import Embedder

                        embedder = Embedder(cfg)
                    else:
                        journal = read_archive_journal(journal_path(resolved))
                        if journal.get("semantic") and journal.get("model_cache"):
                            from varyz.cloud.archive import CachedEmbedder

                            embedder = CachedEmbedder(journal["model_cache"])
                    result = worker.sync(client, connection["account_id"], embedder=embedder)
            typer.echo(json.dumps(result, sort_keys=True))
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("control")
def control(
    action: Annotated[str, typer.Argument(help="pause, resume or revoke")],
    entry: Annotated[
        list[str] | None, typer.Option("--entry", help="Exact local entry ID to revoke; repeat")
    ] = None,
    project: Annotated[str | None, typer.Option("--project", help="Revoke a whole project")] = None,
    config: ConfigOpt = None,
) -> None:
    """Pause uploads and Cloud retrieval, resume, or revoke a project and sync its deletions."""
    try:
        if action == "revoke" and not entry and not project:
            raise ValueError("Name the project or exact entries to revoke")
        resolved, _ = _paths(config)
        conn = open_db(load_config(resolved).db_path)
        try:
            worker = ArchiveSync(conn, journal_path(resolved))
            result = worker.control(action, entry, project)
            with _connected(config) as (_, connection_path, client):
                if action in {"pause", "resume"}:
                    result["remote"] = client.request(
                        "POST", "/api/archive/control", json={"paused": action == "pause"}
                    )
                elif result["enabled"]:
                    connection = read_journal(connection_path)
                    result["remote"] = worker.sync(client, connection["account_id"])
            typer.echo(json.dumps(result))
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("doctor")
def doctor(config: ConfigOpt = None) -> None:
    """Offline selection/privacy preflight; no server calls or model download."""
    try:
        resolved, _ = _paths(config)
        conn = open_db(load_config(resolved).db_path)
        try:
            preview = ArchiveSync(conn, journal_path(resolved)).preview()
            preview["go"] = preview["enabled"] and not preview["blocked_ids"]
            typer.echo(json.dumps(preview))
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("status")
def status(config: ConfigOpt = None) -> None:
    """Report local consent and remote counts, without printing private passages."""
    try:
        resolved, _ = _paths(config)
        journal = read_archive_journal(journal_path(resolved))
        result: dict = {
            "enabled": journal["enabled"],
            "approved": len(journal["selected"]),
            "projects": sorted(journal["projects"]),
        }
        with _connected(config) as (_, _, client):
            result["cloud"] = client.request("GET", "/api/archive/status")
        typer.echo(json.dumps(result))
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)

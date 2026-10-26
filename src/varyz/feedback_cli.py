"""Feedback to the Varyz developer: send, status, on/off, forget, and the developer's inbox."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from varyz import feedback
from varyz.config import load_config

app = typer.Typer(help="Feedback agents send to the Varyz developer when Varyz goes wrong.")
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Archive configuration file")
]


def show(value: object) -> None:
    typer.echo(json.dumps(value, indent=2))


@app.command("send")
def send(
    title: Annotated[str, typer.Argument(help="The problem in a few plain words")],
    details: str = typer.Option(..., "--details", help="What happened, with any error text"),
    expected: str = typer.Option("", "--expected", help="What should have happened"),
    area: str = typer.Option("other", "--area", help=", ".join(feedback.AREAS)),
    noticed_by: str = typer.Option("agent", "--noticed-by", help="agent or user"),
    model: str = typer.Option("", "--model", help="The sending agent's model id"),
    config: ConfigOpt = None,
) -> None:
    """For agents whose Varyz tools are failing; others use send_feedback."""
    import contextlib
    import sqlite3

    cfg = load_config(config)
    archive = None
    if cfg.db_path.exists():
        with contextlib.suppress(sqlite3.Error):
            archive = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True,
                                      timeout=2)
    try:
        result = feedback.send(cfg, title=title, details=details, expected=expected, area=area,
                               noticed_by=noticed_by, agent=feedback.client(), model=model,
                               archive=archive)
    except ValueError as error:
        typer.echo(str(error))
        raise typer.Exit(1) from None
    finally:
        if archive is not None:
            archive.close()
    show(result)


@app.command("status")
def status(config: ConfigOpt = None) -> None:
    """What this computer sent and what is still waiting."""
    show(feedback.status(load_config(config)))


@app.command("on")
def on(config: ConfigOpt = None) -> None:
    """Send feedback from this computer again."""
    feedback.set_enabled(load_config(config), True)
    show(feedback.status(load_config(config)))


@app.command("off")
def off(config: ConfigOpt = None) -> None:
    """Stop sending feedback from this computer; drops reports still waiting."""
    feedback.set_enabled(load_config(config), False)
    show(feedback.status(load_config(config)))


@app.command("forget")
def forget(config: ConfigOpt = None) -> None:
    """Delete the feedback this computer sent from the server."""
    result = feedback.forget(load_config(config))
    show(result)
    if result["status"] == "failed":
        raise typer.Exit(1)


@app.command("inbox")
def inbox(
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Newest reports to show"),
) -> None:
    """The developer's inbox, using the owner credential in the OS credential store."""
    import httpx
    import keyring

    token = keyring.get_password("varyz-cloud", "VARYZ_ANALYTICS_ADMIN_TOKEN")
    if not token:
        typer.echo("Owner credentials are not configured on this computer.")
        raise typer.Exit(1)
    try:
        show(feedback.inbox(token, limit=limit))
    except (httpx.HTTPError, ValueError, KeyError):
        typer.echo("Feedback could not be retrieved; check server status and owner credentials.")
        raise typer.Exit(1) from None

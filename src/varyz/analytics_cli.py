"""Local analytics consent, preview and owner reporting controls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from varyz import analytics, contribute
from varyz.config import load_config

app = typer.Typer(
    help="Optional aggregate analytics: consent, preview, delivery and owner reports."
)
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Archive configuration file")
]


def show(value: object) -> None:
    typer.echo(json.dumps(value, indent=2))


@app.command("status")
def status(config: ConfigOpt = None) -> None:
    show(analytics.status(load_config(config)))


@app.command("consent")
def consent(
    choice: Annotated[str, typer.Argument(help="on to request sharing consent, off to revoke it")],
    config: ConfigOpt = None,
) -> None:
    if choice not in {"on", "off"}:
        raise typer.BadParameter("choose on or off")
    _set_sharing(choice == "on", config)
    show(analytics.status(load_config(config)))


def _set_sharing(enabled: bool, config: Path | None) -> None:
    cfg = load_config(config)
    if enabled:
        typer.echo(f"{analytics.CONSENT_TITLE}\n\n{analytics.CONSENT_COPY}")
        if not typer.confirm("Share anonymized data?", default=False):
            raise typer.Exit(0)
    analytics.consent(cfg, enabled)
    if not enabled:
        contribute.erase(cfg)


data_app = typer.Typer(
    help="One switch for everything Varyz sends: usage counts and network contributions."
)


@data_app.command("status")
def data_status(config: ConfigOpt = None) -> None:
    show(contribute.status(load_config(config)))


@data_app.command("preview")
def data_preview(
    limit: int = typer.Option(
        20, "--limit", min=1, max=200, help="Maximum number of anonymized examples to preview"
    ),
    config: ConfigOpt = None,
) -> None:
    """Show the anonymized examples the next upload would hold. Sends nothing."""
    show(contribute.preview(load_config(config), limit))


@data_app.command("signals")
def data_signals(config: ConfigOpt = None) -> None:
    """What this computer's signals show: rules holding per model, false "done"
    claims, needless questions, guessed answers, the memory test. Sends nothing."""
    import contextlib
    import sqlite3

    from varyz.resolution.rule_signals import summary

    cfg = load_config(config)
    if not cfg.db_path.exists():
        show({})
        return
    archive = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    with contextlib.closing(archive):
        show({"memory_test_on": cfg.signals.memory_test, **summary(archive)})


@data_app.command("on")
def data_on(config: ConfigOpt = None) -> None:
    _set_sharing(True, config)
    show(contribute.status(load_config(config)))


@data_app.command("off")
def data_off(config: ConfigOpt = None) -> None:
    """Stop sharing and delete what this computer shared from the server."""
    _set_sharing(False, config)
    show(contribute.status(load_config(config)))


@app.command("pause")
def pause(config: ConfigOpt = None) -> None:
    analytics.pause(load_config(config), True)
    show(analytics.status(load_config(config)))


@app.command("resume")
def resume(config: ConfigOpt = None) -> None:
    analytics.pause(load_config(config), False)
    show(analytics.status(load_config(config)))


@app.command("sync")
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview aggregate counts; send nothing"),
    config: ConfigOpt = None,
) -> None:
    result = analytics.sync(load_config(config), dry_run=dry_run, force=True)
    show(result)
    if result["status"] == "delivery_failed":
        raise typer.Exit(1)


@app.command("doctor")
def doctor(config: ConfigOpt = None) -> None:
    result = analytics.doctor(load_config(config))
    show(result)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("report")
def report() -> None:
    """Fetch the creator's summary using his OS credential-store token."""
    import httpx
    import keyring

    token = keyring.get_password("varyz-cloud", "VARYZ_ANALYTICS_ADMIN_TOKEN")
    if not token:
        typer.echo("Analytics owner credentials are not configured on this computer.")
        raise typer.Exit(1)
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = client.get(
                analytics.ORIGIN + "/api/analytics/summary",
                headers={"Authorization": "Bearer " + token},
            )
            response.raise_for_status()
            show(response.json())
    except (httpx.HTTPError, ValueError):
        typer.echo(
            "Analytics summary could not be retrieved; check server status and owner credentials."
        )
        raise typer.Exit(1) from None


@app.command("feedback")
def feedback(
    ledger_id: Annotated[str, typer.Argument(help="Local resolution ledger ID for feedback")],
    outcome: Annotated[str, typer.Argument(help="adopted, corrected or overridden")],
    reason: str = typer.Option(
        "other", "--reason",
        help="wrong_preference, stale_rule, wrong_scope, agent_ignored or other",
    ),
    config: ConfigOpt = None,
) -> None:
    """Record an explicit outcome against a local resolution ledger ID."""
    from varyz.db.connection import open_db
    from varyz.resolution.outcomes import OUTCOMES, SQLiteOutcomeLedger

    if outcome not in OUTCOMES:
        raise typer.BadParameter("choose adopted, corrected or overridden")
    if "correction_" + reason not in analytics.METRICS:
        raise typer.BadParameter(
            "choose wrong_preference, stale_rule, wrong_scope, agent_ignored, other"
        )
    cfg = load_config(config)
    conn = open_db(cfg.db_path)
    try:
        identifier = SQLiteOutcomeLedger(conn, device_id=cfg.resolution.device_id).record(
            ledger_id,
            outcome,
            source="owner_feedback:" + reason,
        )
        show({"recorded": identifier, "outcome": outcome})
    except KeyError:
        typer.echo("That resolution ledger ID was not found in this archive.")
        raise typer.Exit(1) from None
    finally:
        conn.close()

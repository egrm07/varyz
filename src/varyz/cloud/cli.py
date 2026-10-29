"""Cloud controls imported by the CLI, without importing the web server."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import keyring
import typer

from varyz.cloud.client import CloudClient, browser_connect, cloud_url, credential_name, load_tokens
from varyz.cloud.local_sync import (
    LocalSync,
    _sync_lock,
    read_journal,
    save_journal,
    select_confirmed,
)
from varyz.config import find_config_file, load_config
from varyz.db.connection import open_db

app = typer.Typer(
    help="Optional paid cloud: connect, select, preview, sync and disconnect. No trial."
)
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Local Varyz config path")]


@app.command("exclude-projects")
def exclude_projects(
    project: Annotated[list[str], typer.Option("--project", help="Exact unwanted project; repeat")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Persist exclusions; keep local evidence")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Preview exclusions, or stop future uploads and queue Cloud removal on the next sync."""
    from varyz.cloud.project_cleanup import remove_projects

    try:
        resolved, _ = _paths(config)
        typer.echo(json.dumps(remove_projects(resolved, project, apply=apply)))
    except (ValueError, OSError) as error:
        _fail(error)


@app.command("projects")
def project_audit(config: ConfigOpt = None) -> None:
    """Explain project origins and flag uncertain ownership offline."""
    from varyz.cloud.guided import GuidedSetup

    try:
        resolved, path = _paths(config)
        account = read_journal(path).get("account_id")
        choices = GuidedSetup(resolved, account).choices()
        typer.echo(json.dumps({key: choices[key] for key in (
            "sync_projects", "sync_selected", "excluded_projects",
        )}))
    except (ValueError, OSError) as error:
        _fail(error)


def _paths(config: Path | None) -> tuple[Path, Path]:
    found = find_config_file(config)
    if found is None:
        raise ValueError("Set up local Varyz before connecting cloud.")
    resolved = Path(found).resolve()
    # Distinct configs in one directory must not share an account or sync cursor.
    return resolved, resolved.with_name(resolved.stem + ".cloud-sync.json")


@app.command("promotions")
def promotions_control(
    action: Annotated[str, typer.Argument(help="status, off, or on (reset dismissal)")] = "status",
    config: ConfigOpt = None,
) -> None:
    """Control local-agent Cloud notices; never enable unreviewed hosts or billing."""
    from varyz import promotions

    found = find_config_file(config)
    if found is None:
        raise typer.BadParameter("Set up local Varyz first.")
    cfg = load_config(found)
    conn = open_db(cfg.db_path)
    try:
        state = promotions.control(conn, action)
        typer.echo(json.dumps({
            "disabled": bool(state["disabled"]),
            "offered": bool(state["offer_id"]),
            "snoozed_until": state["snoozed_until"],
            "interested": bool(state["interested"]),
            "enabled": cfg.promotions.enabled,
            "cloud_ready": cfg.promotions.cloud_ready,
            "reviewed_agents": len(cfg.promotions.reviewed_agent_ids),
        }))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    finally:
        conn.close()


@contextlib.contextmanager
def _connected(config: Path | None):
    resolved, path = _paths(config)
    journal = read_journal(path)
    if not journal.get("url"):
        raise ValueError("Cloud is disconnected; connect it before syncing.")
    name = credential_name(resolved, journal["url"])
    client = CloudClient(journal["url"], load_tokens(name), credential=name)
    try:
        yield resolved, path, client
    finally:
        client.close()


def _fail(exc: Exception) -> None:
    # Never echo keyring/provider internals; credentials can occur in exception reprs.
    if isinstance(exc, (keyring.errors.KeyringError, OSError)):
        typer.echo(
            "Cloud operation could not complete. Check the OS keyring and local file access."
        )
    else:
        typer.echo(str(exc))
    raise typer.Exit(1) from exc


class OtherAccount(ValueError):
    """This computer's sync journals belong to a different Cloud account."""


class WrongSignIn(ValueError):
    """The browser signed in to a different account than the page that asked."""


class InactiveAccount(ValueError):
    """The signed-in account has no active subscription."""


def _switch_account(resolved: Path, path: Path, origin: str) -> None:
    """Unlink this computer from its previous account. Local preferences stay; that
    account keeps its Cloud data. Both sync journals are moved aside, never deleted."""
    from varyz.config import load_config
    from varyz.db.connection import LockHeldError

    archive = resolved.with_name(resolved.stem + ".cloud-archive.json")
    reports = resolved.with_name(resolved.stem + ".cloud-reports.json")
    deadline = time.monotonic() + 120
    with contextlib.ExitStack() as held:
        for lock in (
            path.with_suffix(".lock"), archive.with_suffix(".lock"), reports.with_suffix(".lock")
        ):
            while True:
                try:
                    held.enter_context(_sync_lock(lock))
                    break
                except LockHeldError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(2)
        backups = load_config(resolved).data_dir / "cloud-switch"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        for journal in (path, archive, reports):
            if journal.exists():
                os.replace(journal, backups / f"{stamp}-{journal.name}")
        name = credential_name(resolved, origin)
        # Best effort: the old account can also revoke its sessions from its account page.
        with contextlib.suppress(Exception):
            old = load_tokens(name)
            client = CloudClient(origin, old)
            try:
                for kind in ("access_token", "refresh_token"):
                    if old.get(kind):
                        client.request(
                            "POST", "/oauth/revoke",
                            data={"token": old[kind], "client_id": "varyz-local"},
                        )
            finally:
                client.close()


def connect_account(
    url: str, config: Path | None = None, *, require_active: bool = False,
    expected_account: str | None = None,
    return_ticket: str | None = None,
    switch_account: bool = False,
    resume_only: bool = False,
    open_browser: Callable[[str], bool] | None = None,
) -> None:
    """Shared browser sign-in for CLI and computer connection; never select data.

    ``switch_account`` is the owner's explicit choice to move this computer from the
    account its journals belong to onto the signed-in one."""
    resolved, path = _paths(config)
    origin = cloud_url(url)
    journal = read_journal(path)
    if journal.get("url") and journal["url"] != origin:
        raise ValueError("Disconnect the existing cloud before changing servers.")
    name = credential_name(resolved, origin)
    # Reuse persisted credentials first. Refresh rotates and saves tokens in keyring.
    # Only a user-initiated connection may open OAuth; background recovery cannot.
    tokens = None
    account = None
    if not switch_account and journal.get("account_id"):
        if expected_account and journal["account_id"] != expected_account:
            raise OtherAccount("This computer is linked to a different Cloud account.")
        try:
            saved = CloudClient(origin, load_tokens(name), credential=name)
            try:
                account = saved.request("GET", "/api/account")
                tokens = saved.tokens
            finally:
                saved.close()
        except ValueError as error:
            # Network failures and inactive access are not reasons to launch sign-in.
            if "disconnected" not in str(error) and "expired or was revoked" not in str(error):
                raise
    if tokens is None:
        if resume_only:
            raise ValueError("Sign in to connect this computer.")
        options: dict[str, Any] = {}
        if return_ticket:
            options["return_ticket"] = return_ticket
        if open_browser:
            options["open_browser"] = open_browser
        tokens = browser_connect(origin, **options)
    client = CloudClient(origin, tokens, credential=name)
    try:
        if account is None:
            account = client.request("GET", "/api/account")
    finally:
        client.close()
    if not isinstance(account, dict):
        raise ValueError("Cloud did not return an account identity.")
    account_id = account.get("id") or account.get("account_id")
    if not account_id:
        raise ValueError("Cloud did not return an account identity.")
    if require_active and account.get("entitlement") is not True:
        raise InactiveAccount("Cloud access is inactive. Check your existing account status.")
    if expected_account is not None and account_id != expected_account:
        raise WrongSignIn("Sign in to the same Cloud account as the account page.")
    # OAuth may take minutes; retain journal changes committed during sign-in.
    journal = read_journal(path)
    if journal.get("url") and journal["url"] != origin:
        raise ValueError("Disconnect the existing cloud before changing servers.")
    if journal.get("account_id") and journal["account_id"] != account_id:
        if not switch_account:
            raise OtherAccount(
                "This computer is linked to a different Cloud account; switch it first."
            )
        _switch_account(resolved, path, origin)
        journal = read_journal(path)
    keyring.set_password("varyz", name, json.dumps(client.tokens))
    journal.update(url=origin, account_id=account_id)
    save_journal(path, journal)


@app.command("connect")
def connect(
    url: Annotated[str, typer.Option("--url", help="Approved HTTPS cloud origin")],
    config: ConfigOpt = None,
) -> None:
    """Sign in using the browser. Connecting does not enable uploads."""
    try:
        connect_account(url, config)
        typer.echo("Cloud connected. Uploads stay off until you select confirmed knowledge.")
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("select")
def select(
    all_confirmed: Annotated[
        bool, typer.Option("--all-confirmed", help="Allow confirmed records to sync")
    ] = False,
    project: Annotated[
        list[str] | None,
        typer.Option("--project", help="Selected project plus global knowledge; repeat for more"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Explicitly allow confirmed rules, own answers and stated workflows to sync."""
    try:
        _resolved, path = _paths(config)
        if not all_confirmed:
            raise ValueError(
                "Choose --all-confirmed explicitly; --project can restrict project scopes."
            )
        select_confirmed(path, project)
        typer.echo(
            "Selected confirmed rules, answers and stated workflows. "
            "Future confirmed records in these scopes are included. Nothing uploaded yet."
        )
        if project:
            typer.echo("Selected projects plus your global rules are included.")
    except (ValueError, OSError) as exc:
        _fail(exc)


@app.command("sync")
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show counts offline; send no text")
    ] = False,
    accept_remote: Annotated[
        bool, typer.Option("--accept-remote", help="Accept cloud revisions on conflicts")
    ] = False,
    keep_local: Annotated[
        bool, typer.Option("--keep-local", help="Publish local revisions on conflicts")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Sync the selected projection; dry-run is offline and reports counts only."""
    try:
        if accept_remote and keep_local:
            raise ValueError("Choose either --accept-remote or --keep-local.")
        resolved, path = _paths(config)
        conn = open_db(load_config(resolved).db_path)
        try:
            synchronizer = LocalSync(conn, path)
            if dry_run:
                typer.echo(json.dumps(synchronizer.preview(), sort_keys=True))
                return
            with _connected(config) as (_resolved, _path, client):
                policy = "remote" if accept_remote else "local" if keep_local else "stop"
                result = synchronizer.sync(client, conflict_policy=policy)
            typer.echo(
                f"Cloud synced: {result['uploaded']} changes sent, "
                f"{result['downloaded']} changes received."
            )
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("status")
def status(config: ConfigOpt = None) -> None:
    """Show selection locally and the server's current paid entitlement."""
    try:
        _resolved, path = _paths(config)
        journal = read_journal(path)
        if not journal.get("url"):
            typer.echo("Cloud disconnected. Local Varyz remains available.")
            return
        typer.echo("Uploads selected." if journal.get("enabled") else "Uploads off.")
        from varyz.cloud.background import health, read_health

        # What is wrong comes first, and does not need the network.
        state = health(_resolved)
        if state is not None:
            typer.echo(("Background sync: " if state[0] else "Needs attention: ") + state[1])
        withheld = read_health(_resolved).get("withheld")
        if withheld:
            typer.echo(f"Withheld as private, never uploaded: {withheld}.")
        with _connected(config) as (_resolved, _path, client):
            account = client.request("GET", "/api/account")
        # Print status only: never full account or server-returned private fields.
        typer.echo(
            "Paid entitlement: " + str(account.get("entitlement", account.get("paid", "unknown")))
        )
        from varyz.cloud.access import account_access
        from varyz.cloud.local_access import observe

        access = account.get("cloud_access") or account_access(account)
        observe(_resolved, access)
        if access.get("active") is False:
            typer.echo(access["message"])
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("verify")
def verify(
    rule_id: Annotated[str, typer.Option("--rule-id", help="Confirmed local rule to verify")],
    project: Annotated[str, typer.Option("--project", help="Project to check through hosted MCP")],
    config: ConfigOpt = None,
) -> None:
    """Check paid account and hosted rule retrieval without uploading or changing selection."""
    from varyz.cloud.activation import verify_rule

    try:
        resolved, path = _paths(config)
        journal = read_journal(path)
        conn = open_db(load_config(resolved).db_path)
        try:
            with _connected(config) as (_resolved, _path, client):
                result = verify_rule(conn, journal, client, rule_id, project)
            typer.echo(json.dumps(result, sort_keys=True))
        finally:
            conn.close()
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("disconnect")
def disconnect(config: ConfigOpt = None) -> None:
    """Disable sync and revoke local credentials; keep preferences and cloud history."""
    try:
        resolved, path = _paths(config)
        journal = read_journal(path)
        url = journal.get("url")
        # The local kill switch must work even when cloud is down or a token has expired.
        journal["enabled"] = False
        save_journal(path, journal)
        from varyz.cloud.session_report_sync import journal_path
        from varyz.cloud.session_report_sync import read_journal as read_reports

        report_path = journal_path(resolved)
        if report_path.exists():
            report_journal = read_reports(report_path)
            report_journal["enabled"] = False
            save_journal(report_path, report_journal)
        pending_revoke = False
        if url:
            name = credential_name(resolved, url)
            try:
                tokens = load_tokens(name)
                client = CloudClient(url, tokens)
                try:
                    client.request(
                        "POST",
                        "/oauth/revoke",
                        data={
                            "token": tokens["access_token"],
                            "client_id": "varyz-local",
                        },
                    )
                    if tokens.get("refresh_token"):
                        client.request(
                            "POST",
                            "/oauth/revoke",
                            data={
                                "token": tokens["refresh_token"],
                                "client_id": "varyz-local",
                            },
                        )
                finally:
                    client.close()
            except ValueError:
                pending_revoke = True
            finally:
                with contextlib.suppress(keyring.errors.PasswordDeleteError):
                    keyring.delete_password("varyz", name)
            journal.pop("url", None)
            # Keep account binding and version journal to resume safely with this account.
            save_journal(path, journal)
        typer.echo("Cloud disconnected. Local preferences remain available; cloud data was kept.")
        if pending_revoke:
            typer.echo(
                "Remote token revocation could not be confirmed. "
                "Revoke sessions in your cloud account."
            )
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


@app.command("export")
def export(
    output: Annotated[Path, typer.Option("--output", help="New private JSON export file")],
    config: ConfigOpt = None,
) -> None:
    """Export account preferences even after a subscription ends."""
    try:
        if output.exists():
            raise ValueError("Export destination already exists; choose a new file.")
        with _connected(config) as (_resolved, _path, client):
            result = client.request("GET", "/api/export")
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        typer.echo(
            "Cloud preferences exported. This file contains your private selected knowledge."
        )
    except (ValueError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)


share_app = typer.Typer(
    help="Shared projects: add or remove people by their Varyz username. Needs Cloud."
)
app.add_typer(share_app, name="share")
ProjectOpt = Annotated[
    str | None, typer.Option("--project", "-p", help="Project name; default this folder's")
]


def _collaborate(config: Path | None, arguments: dict[str, Any]) -> None:
    from varyz.cloud import collaboration
    from varyz.cloud.client import CloudError
    from varyz.resolution.advice import default_project

    if arguments["action"] != "status" and not arguments.get("share_id"):
        arguments["project"] = arguments["project"] or default_project()
    try:
        value = collaboration.call(config, {k: v for k, v in arguments.items() if v is not None})
    except (ValueError, CloudError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)
    typer.echo(json.dumps(value, indent=2))


@share_app.command("add")
def share_add(
    username: Annotated[str, typer.Argument(help="Their Varyz username")],
    level: Annotated[
        str, typer.Option("--level", "-l", help="consult, contribute or decide")
    ] = "consult",
    project: ProjectOpt = None,
    share_id: Annotated[
        str | None, typer.Option("--share-id", help="Existing shared project ID")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Invite someone to a project at a level, or change a member's level.

    Shares only the project's confirmed preferences, never General ones.
    """
    if level not in ("consult", "contribute", "decide"):
        _fail(ValueError("Choose --level consult, contribute or decide"))
    _collaborate(
        config,
        {
            "action": "add",
            "username": username,
            "level": level,
            "project": project,
            "share_id": share_id,
        },
    )


@share_app.command("remove")
def share_remove(
    username: Annotated[str, typer.Argument(help="Their Varyz username")],
    project: ProjectOpt = None,
    share_id: Annotated[
        str | None, typer.Option("--share-id", help="Existing shared project ID")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """End someone's access to a project, or cancel their pending invitation."""
    _collaborate(
        config,
        {"action": "remove", "username": username, "project": project, "share_id": share_id},
    )


@share_app.command("status")
def share_status(
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Show only this project's sharing")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Members, pending invitations, reports and review items for shared projects."""
    _collaborate(config, {"action": "status", "project": project})


# Public Varyz: follow creators by username; publish your own standards on the web.


def _follow(config: Path | None, arguments: dict[str, Any]) -> dict[str, Any]:
    from varyz.cloud import collaboration
    from varyz.cloud.client import CloudError

    try:
        return collaboration.follow(config, {k: v for k, v in arguments.items() if v})
    except (ValueError, CloudError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)
        raise  # _fail exits; this keeps the type checker certain of it


@app.command("follow")
def follow(
    username: Annotated[str, typer.Argument(help="The creator's Varyz username")],
    topic: Annotated[
        str | None,
        typer.Option("--for", help="Use them for this kind of work, e.g. 'code review'"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Follow a creator's public Varyz. Free; your own rules still win."""
    value = _follow(
        config, {"action": "pick" if topic else "follow", "username": username, "topic": topic}
    )
    typer.echo("You follow " + value["creator"] + ". " + value["next"])
    typer.echo("Their page: " + value["profile"])


@app.command("unfollow")
def unfollow(
    username: Annotated[str, typer.Argument(help="The creator's Varyz username")],
    config: ConfigOpt = None,
) -> None:
    """Stop following a creator. Their standards stop reaching your tasks at once."""
    value = _follow(config, {"action": "unfollow", "username": username})
    typer.echo("You no longer follow " + value["creator"] + ".")


@app.command("following")
def following(config: ConfigOpt = None) -> None:
    """Who you follow, their topics, and whose standards each shared topic uses."""
    value = _follow(config, {"action": "list"})
    if not value["following"]:
        typer.echo("You follow nobody yet. Follow someone with `varyz cloud follow USERNAME`.")
        return
    for creator in value["following"]:
        topics = ", ".join(creator["topics"]) or "nothing published yet"
        paused = " (paused)" if creator.get("paused") else ""
        typer.echo(f"{creator['creator']}{paused}: {topics}")
    for choice in value["choices"]:
        typer.echo(
            f"{choice['topic']}: using {choice['using']} ({choice['why']}). Change it with "
            f"`varyz cloud follow USERNAME --for \"{choice['topic'].lower()}\"`."
        )


@app.command("public")
def public(
    topic: Annotated[
        str | None,
        typer.Argument(help="A kind of work to publish standards for, e.g. 'code review'"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Your public Varyz: what is published, or the page to publish a topic.

    You choose the standards and review the exact text on that page. Needs the Cloud plan.
    """
    from varyz.cloud import collaboration
    from varyz.cloud.client import CloudError

    arguments: dict[str, Any] = {"action": "publish", "topic": topic} if topic else {
        "action": "public"
    }
    try:
        value = collaboration.call(config, arguments)
    except (ValueError, CloudError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)
    if topic:
        typer.echo("Choose and review your " + topic + " standards here: " + value["review_url"])
        return
    if not value["published"]:
        typer.echo('Nothing is public yet. Publish a topic: varyz cloud public "code review"')
        return
    topics = ", ".join(f"{t['topic']} ({t['standards']})" for t in value["topics"])
    uses = value["uses_this_week"]
    typer.echo(f"{value['url']}{'' if value['serving'] else ' (paused)'}")
    typer.echo(f"Topics: {topics}")
    typer.echo(
        f"{value['followers']} followers. This week: {uses['tasks']} tasks, "
        f"{uses['answers']} answers."
    )


notify_app = typer.Typer(
    help="Shared Varyz notifications: email and desktop, for invitations and questions."
)
app.add_typer(notify_app, name="notifications")
Switch = Annotated[str, typer.Argument(help="on or off")]


def _switch(value: str) -> bool:
    if value not in ("on", "off"):
        _fail(ValueError("Say on or off"))
    return value == "on"


@notify_app.command("status")
def notifications_status(config: ConfigOpt = None) -> None:
    """Whether desktop notifications and notice emails are on."""
    from varyz import desktop_notify

    value = desktop_notify.status(config)
    typer.echo("Desktop: " + ("on" if value["desktop"] else "off"))
    if not value["connected"]:
        typer.echo("Email: connect Varyz Cloud to get emails (`varyz cloud connect`).")
        return
    typer.echo("Email: " + ("on" if value["email"] else "off"))


@notify_app.command("email")
def notifications_email(value: Switch, config: ConfigOpt = None) -> None:
    """Turn notice emails on or off (never includes shared content)."""
    from varyz import desktop_notify
    from varyz.cloud.client import CloudError

    try:
        desktop_notify.set_email(config, _switch(value))
    except (ValueError, CloudError, OSError, keyring.errors.KeyringError) as exc:
        _fail(exc)
    typer.echo("Email notifications " + value + ".")


@notify_app.command("desktop")
def notifications_desktop(value: Switch, config: ConfigOpt = None) -> None:
    """Turn desktop notifications on this computer on or off."""
    from varyz import desktop_notify

    desktop_notify.set_enabled(config, _switch(value))
    typer.echo("Desktop notifications " + value + ".")


@notify_app.command("test")
def notifications_test(config: ConfigOpt = None) -> None:
    """Show one sample desktop notification and send one sample email."""
    from varyz import desktop_notify

    value = desktop_notify.test(config)
    typer.echo(
        "Desktop: "
        + {"shown": "sent a sample; look for it now", "unsupported": "not supported here"}.get(
            value["desktop"], str(value["desktop"])
        )
    )
    email = value["email"]
    typer.echo(
        "Email: "
        + {
            None: "connect Varyz Cloud first",
            "sent": "sent a sample to your account email",
            "not_configured": "Cloud email is not set up yet",
            "unverified": "verify your account email first",
            "limit": "three tests a day; try tomorrow",
            "failed": "could not send; try later",
        }.get(email, str(email))
    )

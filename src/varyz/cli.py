"""Varyz CLI — the primary capture and admin surface."""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import math
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console, RenderableType
from rich.markup import escape
from rich.table import Column, Table

from varyz import LEGAL_NOTICE, __version__
from varyz import connectors as connector_registry
from varyz.analytics_cli import app as analytics_app
from varyz.analytics_cli import data_app
from varyz.cloud.archive_cli import app as archive_cloud_app
from varyz.cloud.cli import app as cloud_app
from varyz.config import Config, ConfigError, find_config_file, load_config
from varyz.connectors.base import get_connector
from varyz.connectors.manual import ManualConnector, parse_when
from varyz.db import dao
from varyz.db.connection import LockHeldError, WriteLock, connect, open_db
from varyz.diskspace import bytes_label, directory_bytes, free_bytes
from varyz.domain import IngestStats, local_day, local_stamp, utc_now
from varyz.pipeline.embed import Embedder, embed_pending
from varyz.pipeline.ingest import ingest_item, sync_connector
from varyz.pipeline.model_download import fetch_search_models
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.resolution.shadow_cli import app as shadow_app
from varyz.session_progress_cli import app as session_progress_app
from varyz.vault import Vault

if TYPE_CHECKING:
    from varyz.resolution.profile_reader import ProfileItem, ProfileState
    from varyz.resolution.rule_miner import MiningStats
    from varyz.update import UpdateResult

# Windows' legacy console is cp1252 and crashes on em-dashes/bullets/arrows.
# Force UTF-8 so CLI output can use the same glyphs as everywhere else.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")

app = typer.Typer(help="Varyz: local owner context for agents, with sources or abstention.")
# A column holding an id another command takes, printed whole. Rich shrinks
# every column to fit, and at the 80 columns it assumes by default the id lost
# its last characters to an ellipsis -- in tables whose only reason to carry an
# id is that `varyz forget`, `varyz todo done`, `varyz privacy verify` and
# `varyz graph merge` each take one. The text beside it can lose the width
# instead; it is already an excerpt.
ID_COLUMN = {"style": "dim", "no_wrap": True, "min_width": 26}


todo_app = typer.Typer(help="Commitments and tasks.")
secrets_app = typer.Typer(help="Secrets in the OS credential store.")
connect_app = typer.Typer(help="Wire agents and supported local data sources to Varyz.")
graph_app = typer.Typer(help="Build, inspect, and correct the temporal truth graph.")
privacy_app = typer.Typer(help="Inspect context policy, consent, and egress receipts.")
autosync_app = typer.Typer(help="Keep every configured source fresh in the background.")
local_model_app = typer.Typer(help="Install and inspect private local inference.")
model_app = typer.Typer(help="Compile, inspect, query, and correct your personal model.")
conversations_app = typer.Typer(help="Read chat threads as conversations, not messages.")
agent_app = typer.Typer(help="Register, inspect, grant, and revoke agent callers.")
daemon_app = typer.Typer(help="Run and inspect the local owner-resolution daemon.")
knowledge_app = typer.Typer(help="Inspect and retract what you have told Varyz.")
hooks_app = typer.Typer(
    help="The hooks that brief agents and check their work: doctor, pause, resume."
)
rules_app = typer.Typer(
    help="How you build things: the rules and vision agents are advised with."
)
workflows_app = typer.Typer(
    help="The order you guide agents through work in: website, product, ..."
)
update_app = typer.Typer(
    help="Keep this checkout on the latest Varyz: status, check, apply, pause, resume."
)
news_app = typer.Typer(
    help="News from varyzai.com/news, checked hourly and passed on by your agent: on, off, check.",
    invoke_without_command=True,
)
app.add_typer(todo_app, name="todo")
app.add_typer(secrets_app, name="secrets")
app.add_typer(connect_app, name="connect")
app.add_typer(graph_app, name="graph")
app.add_typer(privacy_app, name="privacy")
app.add_typer(autosync_app, name="autosync")
app.add_typer(local_model_app, name="local-model")
app.add_typer(model_app, name="model")
app.add_typer(conversations_app, name="conversations")
app.add_typer(session_progress_app, name="sessions")
app.add_typer(agent_app, name="agent")
app.add_typer(daemon_app, name="daemon")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(rules_app, name="rules")
rules_app.add_typer(shadow_app, name="shadow")
app.add_typer(workflows_app, name="workflows")
app.add_typer(hooks_app, name="hooks")
app.add_typer(update_app, name="update")
app.add_typer(news_app, name="news")

# The cloud controls are the client side only; the hosted service is not part of this package.
app.add_typer(cloud_app, name="cloud")
cloud_app.add_typer(archive_cloud_app, name="archive")
app.add_typer(analytics_app, name="analytics")
app.add_typer(data_app, name="data")

plugin_app = typer.Typer(help="Local plugin adapters: serve, hook and migrate an existing grant.")
app.add_typer(plugin_app, name="plugin")


@plugin_app.command("serve")
def plugin_serve(
    client: Annotated[str, typer.Option("--client", help="Plugin host: claude-code or codex")],
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Local Varyz config")
    ] = None,
) -> None:
    from varyz.plugin import serve

    try:
        serve(client, config)
    except (ValueError, OSError) as exc:
        # stderr only: stdout belongs to the MCP protocol.
        print(str(exc), file=sys.stderr)
        raise typer.Exit(1) from exc


@plugin_app.command("hook")
def plugin_hook(
    client: Annotated[str, typer.Option("--client", help="Plugin host: claude-code or codex")],
    event: Annotated[str, typer.Option("--event", help="Canonical Varyz hook event")],
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Local Varyz config")
    ] = None,
) -> None:
    from varyz.plugin import hook

    raise typer.Exit(hook(client, event, config))


@plugin_app.command("migrate")
def plugin_migrate(
    client: Annotated[str, typer.Option("--client", help="Plugin host: claude-code or codex")],
    config: Annotated[Path, typer.Option("--config", "-c", help="Archive whose grant to migrate")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without moving credentials")
    ] = False,
) -> None:
    import keyring

    from varyz.plugin import migrate

    try:
        console.print(migrate(client, config, dry_run=dry_run), markup=False)
    except keyring.errors.KeyringError as exc:
        console.print("Could not preserve the plugin credential in the OS keyring.", markup=False)
        raise typer.Exit(1) from exc
    except (ValueError, OSError) as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from exc

# legacy_windows=False makes rich emit VT/UTF-8 through our reconfigured stdout
# instead of the cp1252 win32 console API (which crashes on non-latin glyphs).
console = Console(legacy_windows=False)

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Path to config.toml")]

# The legal notice lives in varyz/__init__.py: a library embedder never runs
# `--version` and still has to be able to display it.


def _version_callback(value: bool) -> None:
    if not value:
        return
    console.print(f"varyz {__version__}")
    console.print(LEGAL_NOTICE, highlight=False)
    raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and the legal notices, then exit.",
        ),
    ] = False,
) -> None:
    """Varyz: a self-filling, local-first memory of your life."""


# Written by `varyz init` when there is no config.example.toml beside it — that
# is, by every installed copy, as opposed to a checkout of this repository. It
# carries the connector template so an owner who would rather write paths by
# hand than let `varyz setup` find them has the sections to edit.
DEFAULT_CONFIG = """\
[storage]
data_dir = "data"

# Sensitivity levels are built in, not written here: your turns to coding
# agents are quotable to the agents you connect, and an entry naming a
# therapist, a diagnosis, a medication, a salary or an account number is held
# back wherever it turns up. Naming a
# source below changes that one source; the rest keep what they ship with.
# [sensitivity.source_defaults]
# documents = "sensitive"

# Start here: `varyz connect ai-conversations` reads the agent histories
# already on this machine -- your own turns, never tool output.
# [connectors.ai_conversations]
# approved = true
# include_assistant = false

# Point varyz at the folders you want it to read, then run `varyz sync`.
# `approved = true` is your consent to index them; without it the connector
# refuses to walk anything.
# [connectors.documents]
# approved = true
# paths = ["~/Documents"]

# [connectors.git]
# approved = true
# paths = ["~/projects/some-repo"]
"""


def _cfg(config: Path | None) -> Config:
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        # The message quotes a TOML parser, which brackets its positions, and
        # Rich would read those as style tags and print the message with the
        # position missing.
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    return cfg


def _db(cfg: Config) -> sqlite3.Connection:
    if not cfg.db_path.exists():
        console.print("[red]No varyz database found. Run: varyz setup[/red]")
        raise typer.Exit(1)
    return open_db(cfg.db_path)


def _resolution_client(cfg: Config):
    """The sole CLI route into live resolution state."""
    from varyz.resolution.client import ResolutionClient

    return ResolutionClient(
        cfg.resolution.bind_host,
        cfg.resolution.port,
        auth_mode=cfg.resolution.auth_mode,
        archive=cfg.db_path,
        config_path=cfg.source_file,
    )


def _local_owner_caller(cfg: Config, purpose: str):
    from varyz.resolution.models import Caller

    return Caller(
        agent_id="owner:local",
        display_name="Local owner",
        purpose=purpose,
        device_id=cfg.resolution.device_id,
        transport="stdio",
    )


def _create_archive(cfg_path: Path) -> tuple[Config, bool, int]:
    """Create config.toml if missing, then the data directory and database.

    Returns the loaded config, whether the config file was created, and how
    many entries the archive already held.
    """
    created = not cfg_path.exists()
    if created:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        example = Path.cwd() / "config.example.toml"
        if example.exists():
            shutil.copy(example, cfg_path)
        else:
            cfg_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        console.print(f"Created {cfg_path}")
    cfg = _cfg(cfg_path)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    try:
        entries = dao.count_entries(conn)
    finally:
        conn.close()
    return _claim_own_port(cfg_path, cfg, fresh=created), created, entries


def _free_port(host: str, first: int) -> int:
    """The first port from ``first`` nothing listens on, else one the OS picks."""
    import socket

    for port in range(first, first + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def _claim_own_port(cfg_path: Path, cfg: Config, *, fresh: bool = False) -> Config:
    """Move this config off a port another archive's daemon already serves.

    Every config starts on the default port. A second archive on the same machine --
    a checkout's config.toml beside ~/.varyz, say -- then had its rules,
    questions and answers read from and written to the first one, and every
    command reported success. The daemon names its archive now, so the
    collision is detectable, and init is where it is cheapest to fix.

    ``fresh``: the config was created just now, so nothing listening on its
    port can be its own daemon. That has to be decided without asking the
    listener: a daemon from before it could name its archive answers the
    identity check with a 404, and is let through by it.
    """
    from varyz.configfile import set_resolution_port
    from varyz.resolution.client import ArchiveMismatchError, ResolutionClientError

    if fresh:
        if not _port_in_use(cfg.resolution.bind_host, cfg.resolution.port):
            return cfg
    else:
        try:
            _resolution_client(cfg).status()
        except ArchiveMismatchError:
            pass
        except ResolutionClientError:
            # Nothing answering: the port is this config's to use.
            return cfg
        else:
            # This archive's own daemon.
            return cfg
    old = cfg.resolution.port
    port = _free_port(cfg.resolution.bind_host, old + 1)
    set_resolution_port(cfg_path, port)
    console.print(
        f"Port {old} is taken{'' if fresh else ' by another Varyz archive'}, so this "
        f"archive's daemon uses port {port} (written to {cfg_path})."
    )
    return load_config(cfg_path)


@app.command()
def init(config: ConfigOpt = None) -> None:
    """Create config.toml (if missing), the data directory, and the database."""
    from varyz.config import find_config_file
    from varyz.trust import count_noun

    # Every other command resolves its config through find_config_file, which
    # reads --config, then $VARYZ_CONFIG, then ./config.toml. init read only
    # the last of those, so the first command a second archive is started with
    # quietly opened the first one instead -- or wrote a new config.toml into
    # whatever directory the shell happened to be in.
    cfg_path = config or find_config_file() or Path.cwd() / "config.toml"
    cfg, _, entries = _create_archive(cfg_path)
    if entries:
        # "Varyz initialized." over an archive that already held entries, under
        # the advice given to someone who has none. init is idempotent by
        # design, and this read like a run that had just emptied it.
        console.print(
            f"Already initialized, {count_noun(entries, 'entry', 'entries')} kept. "
            f"Database: [bold]{cfg.db_path}[/bold]"
        )
        console.print("[dim]The schema is up to date; nothing was created or removed.[/dim]")
        return
    console.print(f"Varyz initialized. Database: [bold]{cfg.db_path}[/bold]")
    # One next step, the same one the README and the installers give: setup
    # finishes what init starts, and says which data sources it connected.
    console.print(f"Next: [bold]varyz setup{_config_flag(cfg_path)}[/bold]")


def _local_minute(stamp: str) -> str:
    """A stored UTC timestamp as the owner's wall clock, to the minute."""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:16].replace("T", " ")
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def _quote_path(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


# The agents `varyz setup` connects, in the order it offers them.
SETUP_AGENTS = (("claude-code", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor"))
# The local data sources setup can find and connect by itself. GitHub needs a
# token the owner types, and notes need nothing, so both are pointed to.
SETUP_SOURCES = ("ai_conversations", "git", "documents")


def _home_config() -> Path:
    """Where the installers put the archive when no config is named."""
    from varyz.config import home_config_file

    return home_config_file()


def _config_flag(cfg_path: Path) -> str:
    """` --config <path>` for a next-step hint, or nothing for the home archive.

    Every command finds the home archive by itself, so naming it only made the
    hint longer; any other archive has to be named or the command opens the
    home one instead.
    """
    from varyz.config import home_config_file

    if cfg_path.resolve() == home_config_file().resolve():
        return ""
    return f" --config {_quote_path(cfg_path)}"


def _step(number: int, name: str, detail: str) -> None:
    console.print(f"[bold]{number}/7 {name}[/bold]  {detail}", highlight=False)


def _step_state(state: str, detail: str) -> str:
    """A step helper's (state, detail) as `_step` prints it."""
    detail = escape(detail)
    if state == "failed":
        return f"[red]failed[/red]: {detail}"
    return f"skipped: {detail}" if state == "skipped" else detail


def _readonly_entry_count(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    except sqlite3.Error:
        return None


def _offered_sources(detected, wanted: tuple[str, ...]) -> list[tuple[str, str, list[str]]]:
    """(connector id, label, what exactly would be read) for each offer."""
    from varyz.connectors.ai_conversations import ADAPTERS_BY_ID
    from varyz.sources import ALPHA_SOURCES

    labels = {info.connector_id: info.label for info in ALPHA_SOURCES}
    offers: list[tuple[str, str, list[str]]] = []
    if "ai_conversations" in wanted and detected.ai_adapters:
        agents = [ADAPTERS_BY_ID[a].label for a in detected.ai_adapters if a in ADAPTERS_BY_ID]
        offers.append((
            "ai_conversations",
            labels["ai_conversations"],
            [
                f"histories of: {', '.join(agents)}",
                "your turns only - never tool output, commands or file contents",
            ],
        ))
    if "git" in wanted and detected.git_paths:
        offers.append(("git", labels["git"], [str(path) for path in detected.git_paths]))
    if "documents" in wanted and detected.document_paths:
        offers.append(
            ("documents", labels["documents"], [str(path) for path in detected.document_paths])
        )
    return offers


# A long step reports (line, fraction): what it is doing, and how much of it is
# done, or None while nothing says how far along it is. The terminal and the
# setup page draw the same bar from it.
OnProgress = Callable[[str, float | None], None]

# Every download setup makes reports a line that starts so, and only those can
# be paused: each keeps what has arrived and carries on from it next time.
DOWNLOADING = "Downloading "


def is_download(line: str) -> bool:
    return line.startswith(DOWNLOADING)


class DownloadPaused(Exception):
    """A download stopped by the owner, its partial file kept for the next run.

    Raised from the progress callback, which every download calls between two
    chunks: the file on disk is then whole up to the last chunk written.
    """


def _bytes_progress(on_progress: OnProgress | None) -> Callable[[str, int, int], None]:
    """A download's (phase, bytes done, bytes in all) as a bar's line and fraction."""
    shown: dict[str, int] = {}

    def progress(phase: str, done: int, total: int) -> None:
        if on_progress is None:
            return
        if not total:
            on_progress(phase, None)
            return
        percent = done * 100 // total
        if shown.get(phase) != percent:
            shown[phase] = percent
            on_progress(f"{phase}: {bytes_label(done)} of {bytes_label(total)}", done / total)

    return progress


# Said before the first vectors: loading the models is the wait. When they
# could not be fetched ahead, a first run downloads them here, with no progress
# of its own to show.
LOADING_SEARCH_MODELS = "Loading the local search models"
FETCHING_SEARCH_MODELS = "Loading the local search models (the first run downloads about 1.3 GB)"


def _fetch_search_models(cfg: Config, progress: OnProgress) -> str:
    """Download the search models so a stop can resume; the line to show next."""
    if fetch_search_models(cfg, _bytes_progress(progress)):
        return LOADING_SEARCH_MODELS
    return FETCHING_SEARCH_MODELS


class TimeLeft:
    """How long a measured stretch of work still takes, from its own pace so far.

    A stretch is one phase of a step ("Embedding locally", "Downloading
    Ollama"): the text before the colon in its progress line. A new phase, a
    fraction that goes back, or none at all starts the measuring again, so
    one phase's pace is never used for the next. Nothing is said until the
    phase has run a few seconds and moved a little: before that the pace
    is a guess.
    """

    SETTLE_SECONDS = 5.0
    SETTLE_FRACTION = 0.02

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._phase: str | None = None
        self._start: tuple[float, float] | None = None
        self._last = 0.0

    def __call__(self, line: str, fraction: float | None) -> str:
        phase = line.split(":", 1)[0]
        now = self._clock()
        if (
            fraction is None or self._start is None or phase != self._phase
            or fraction < self._last
        ):
            self._phase = phase
            self._start = None if fraction is None else (now, fraction)
            self._last = fraction or 0.0
            return ""
        self._last = fraction
        started, first = self._start
        elapsed, done = now - started, fraction - first
        if fraction >= 1.0 or elapsed < self.SETTLE_SECONDS or done < self.SETTLE_FRACTION:
            return ""
        return time_left_label(elapsed * (1.0 - fraction) / done)


def time_left_label(seconds: float) -> str:
    if seconds < 60:
        return "under a minute left"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min left" if minutes else f"about {hours} h left"


@contextlib.contextmanager
def _progress_bar(description: str, *, step: int | None = None) -> Iterator[OnProgress]:
    """A bar in the terminal while a long step runs, updated with (line, fraction).

    With no fraction it pulses, so a wait with no known end still moves. It
    is gone when the step ends, which then prints its own line. A setup step
    is numbered as `_step` numbers it, and a measured phase says how long it
    has left. Ctrl+C during a download pauses it (`DownloadPaused`): what has
    arrived is kept for the next run.
    """
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

    prefix = f"{step}/7 " if step is not None else ""
    time_left = TimeLeft()
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as bar:
        task = [bar.add_task(escape(prefix + description), total=None)]
        measured = [False]
        last = [description]

        def update(line: str, fraction: float | None = None) -> None:
            last[0] = line
            left = time_left(line, fraction)
            shown = escape(prefix + line + (f" · {left}" if left else ""))
            if fraction is None:
                if measured[0]:
                    # Rich cannot take a total back, so an unknown end is a new task.
                    bar.remove_task(task[0])
                    task[0] = bar.add_task(shown, total=None)
                    measured[0] = False
                bar.update(task[0], description=shown)
                return
            measured[0] = True
            bar.update(
                task[0], description=shown, total=1.0,
                completed=min(max(fraction, 0.0), 1.0),
            )

        try:
            yield update
        except KeyboardInterrupt:
            if not is_download(last[0]):
                raise
            raise DownloadPaused(last[0]) from None


def _embedding_progress(progress: OnProgress) -> Callable[[int, int], None]:
    def embedded(done: int, total: int) -> None:
        progress(f"Embedding locally: {done:,} of {total:,} chunks",
                 done / total if total else 1.0)

    return embedded


def _sync_sources(
    cfg: Config,
    connector_ids: tuple[str, ...],
    on_progress: OnProgress | None = None,
    *,
    step: int | None = None,
    on_read: Callable[[], None] | None = None,
) -> list[str]:
    """Index these sources now; returns the ones that failed.

    No graph rebuild: the briefing and recall need entries and vectors, and a
    first run that also waits on the graph extractor is a first run that
    looks hung. Setup builds the graph in its next step.

    ``on_progress`` hears what the terminal's bar shows, for a caller that is
    not the terminal (the browser setup page). ``on_read`` is called once the
    sources are read and before the search models download: what they hold
    is in the archive from then on (the page offers to extract the owner's
    preferences from their chats while the download runs).
    """
    conn = _db(cfg)
    vault = Vault(cfg.vault_dir)
    failed: list[str] = []
    try:
        with WriteLock(cfg.data_dir), _progress_bar("Indexing", step=step) as bar:

            def progress(line: str, fraction: float | None = None) -> None:
                bar(line, fraction)
                if on_progress is not None:
                    on_progress(line, fraction)

            for number, cid in enumerate(connector_ids, start=1):
                # How many items a source holds is known only once it is read,
                # and how far through it the reading is only if the connector
                # says (`read_fraction`).
                reading = f"Reading {cid}"
                if len(connector_ids) > 1:
                    reading += f" ({number} of {len(connector_ids)} sources)"
                progress(reading)
                connector = get_connector(cid)
                try:
                    stats = sync_connector(
                        conn,
                        vault,
                        connector,
                        cfg,
                        on_progress=lambda s, reading=reading, connector=connector: progress(
                            f"{reading}: {s.total:,} items",
                            getattr(connector, "read_fraction", None),
                        ),
                    )
                except (ValueError, FileNotFoundError, PermissionError) as exc:
                    console.print(f"[yellow]  {cid}: {escape(str(exc))}[/yellow]")
                    failed.append(cid)
                    continue
                _print_stats(stats)
            if on_read is not None:
                on_read()
            # The sources are read first, so the archive holds them during the
            # download. A stop in it is a pause; the resume reads the sources
            # again, which only picks up what is new.
            loading = _fetch_search_models(cfg, progress)
            progress(loading)
            embedded = embed_pending(
                conn, cfg, Embedder(cfg), on_progress=_embedding_progress(progress)
            )
            if embedded:
                console.print(f"Embedded {embedded} chunks locally.")
    finally:
        conn.close()
    return failed


# Setup's last steps. Each returns (state, detail), state being done, failed or
# skipped, and reports progress as (line, fraction), so the page and the
# terminal show the same bar.


def _build_graph(cfg_path: Path, on_progress: OnProgress | None = None) -> tuple[str, str]:
    """The graph and the personal model compiled from it, which `lookup` and `interpret` read."""
    from varyz.configfile import set_graph_extractor
    from varyz.graph.service import rebuild_graph
    from varyz.llm.local_runtime import installer_is_shipped
    from varyz.personal_model.compiler import build_personal_model_compiler
    from varyz.trust import count_noun

    cfg = _cfg(cfg_path)
    notes: list[str] = []
    if cfg.graph.extractor.casefold().strip() != "deterministic":
        if installer_is_shipped():
            # Read by the managed model, for hours on a full archive: the
            # owner starts that, not setup.
            return "skipped", (
                f"the {cfg.graph.extractor} graph is built by `varyz graph rebuild"
                f"{_config_flag(cfg_path)}`, then `varyz model compile --run`"
            )
        # The example config names the semantic extractor, which this build
        # refuses; the deterministic one is the only graph it can build.
        set_graph_extractor(cfg_path, "deterministic")
        cfg = _cfg(cfg_path)
        notes.append('[graph] extractor set to "deterministic", the one this build runs')
    shown = [-1]

    def progress(done: int, total: int) -> None:
        percent = done * 100 // total if total else 100
        if on_progress is not None and percent != shown[0]:
            shown[0] = percent
            on_progress(f"Building the graph: {done:,} of {total:,} entries",
                        done / total if total else 1.0)

    conn = _db(cfg)
    try:
        try:
            graph = rebuild_graph(conn, cfg, on_progress=progress)
        except Exception as exc:
            return "failed", f"the graph could not be built: {exc}"
        if graph.entries_total == 0:
            return "skipped", (
                "the archive is empty; connect a data source or add a note, "
                "then run setup again to build the personal model"
            )
        if on_progress is not None:
            on_progress("Compiling the personal model", None)
        try:
            compiled = build_personal_model_compiler(conn, cfg).compile()
        except (LookupError, RuntimeError, ValueError) as exc:
            return "failed", f"the personal model could not compile: {exc}"
    finally:
        conn.close()
    if compiled.failed or not compiled.activated:
        reason = compiled.error or f"compile {compiled.status}, not activated"
        return "failed", f"the personal model did not compile: {reason}"
    notes.insert(
        0,
        f"{count_noun(graph.claims, 'claim')} from "
        f"{count_noun(graph.entries_total, 'entry', 'entries')}; personal model compiled",
    )
    return "done", "; ".join(notes)


def _install_autosync(cfg_path: Path) -> tuple[str, str]:
    """Autosync at login, on the same preflight `varyz autosync install` refuses on."""
    from varyz.autosync import doctor, install_daemon

    cfg = _cfg(cfg_path)
    # A folder macOS keeps from the background agent does not stop the rest
    # syncing, and setup shows how to allow it once the agent has run again.
    failed = [
        check for check in doctor(cfg_path) if not check.ok and check.name != "macOS privacy"
    ]
    if [check.name for check in failed] == ["connectors"]:
        return "skipped", "no data source to keep current; connect one, then run setup again"
    if failed:
        return "failed", "; ".join(f"{check.name}: {check.detail}" for check in failed)
    try:
        install_daemon(cfg_path, every_minutes=cfg.autosync.schedule_minutes)
    except ValueError as exc:
        return "failed", str(exc)
    detail = (
        f"syncs every {cfg.autosync.schedule_minutes} minutes and at login; "
        f"pause with `varyz autosync pause{_config_flag(cfg_path)}`"
    )
    return "done", detail + _mac_ask_ahead(cfg_path)


def _mac_ask_ahead(cfg_path: Path) -> str:
    """Say before macOS does that it will ask about a folder, and what to answer."""
    if sys.platform != "darwin":
        return ""
    from varyz import mac_app
    from varyz.autosync import autosync_dir, mac_folder_name, mac_protected_paths

    cfg = _cfg(cfg_path)
    if mac_app.ready(autosync_dir(cfg)) is None:
        return ""
    names = sorted({mac_folder_name(path) for path in mac_protected_paths(cfg)})
    if not names:
        return ""
    return (
        f". macOS may ask whether Varyz can access your {' and '.join(names)}: choose "
        "Allow (it opens only that; nothing leaves this Mac)"
    )


def _ready_classifier(
    cfg_path: Path, on_progress: OnProgress | None = None
) -> tuple[str, str, bool]:
    """Ollama and the classifier's model: (state, detail, whether its URL moved)."""
    import httpx

    from varyz.resolution.ollama_setup import OllamaUnavailable, ensure_classifier

    cfg = _cfg(cfg_path)
    if cfg.workflows.inference_mode == "conversations":
        return "skipped", "AI conversation mode; Ollama and Qwen are not required", False
    fallback = "questions go to the embedding classifier instead, which is less exact"
    try:
        result = ensure_classifier(cfg, cfg_path, _bytes_progress(on_progress))
    except OllamaUnavailable as exc:
        return "failed", f"{exc}; {fallback}", False
    except (OSError, RuntimeError, httpx.HTTPError) as exc:
        return "failed", f"Ollama could not be set up: {exc}; {fallback}", False
    return "done", result.detail, result.moved_to is not None


def _restart_daemon(cfg_path: Path) -> bool:
    """Stop this archive's daemon and start it again, so it reads the config anew."""
    from varyz.resolution.client import ResolutionClientError, ensure_daemon

    client = _resolution_client(_cfg(cfg_path))
    with contextlib.suppress(ResolutionClientError):
        client.stop()
        # As `varyz daemon stop`: the acknowledgement comes before it exits.
        for _ in range(50):
            try:
                client.status()
            except ResolutionClientError:
                break
            time.sleep(0.1)
    return ensure_daemon(client, cfg_path) is not None


FULL_DISK_ACCESS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
FILES_AND_FOLDERS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
)


def _mac_folder_access(cfg_path: Path, *, since: float, wait: float = 10.0) -> dict | None:
    """The folders macOS kept from autosync on this run, or None when none were.

    Only the background agent's own attempt says whether it is blocked, and
    its first run checks within seconds of starting, so that is waited for.
    Without an answer nothing is claimed: macOS shows its own question then.
    """
    if sys.platform != "darwin":
        return None
    from varyz.autosync import mac_privacy_path, mac_protected_paths, mac_protected_sources

    cfg = _cfg(cfg_path)
    if not mac_protected_paths(cfg):
        return None
    record = mac_privacy_path(cfg)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            if record.stat().st_mtime >= since:
                break
        time.sleep(0.5)
    blocked: list[str] = []
    program = ""
    with contextlib.suppress(OSError, ValueError):
        if record.stat().st_mtime >= since:
            import json

            answer = json.loads(record.read_text(encoding="utf-8"))
            blocked = list(answer.get("blocked", []))
            program = str(answer.get("program", ""))
    if not blocked:
        return None
    sources = mac_protected_sources(cfg)
    return {
        "folders": [
            {"source": sources.get(path, Path(path).name), "path": path} for path in blocked
        ],
        # The Varyz app has a folder-only switch; a bare Python has only Full Disk Access.
        "via": "app" if program.endswith(".app") else "full_disk_access",
        "program": program,
    }


def _open_folder_access(privacy: dict) -> None:
    """Open the list in System Settings where macOS keeps this permission."""
    import subprocess

    if privacy["via"] == "app":
        subprocess.run(["open", FILES_AND_FOLDERS_URL], timeout=10, check=False)  # noqa: S607
        return
    subprocess.run(["open", FULL_DISK_ACCESS_URL], timeout=10, check=False)  # noqa: S607
    # The program to add, shown in Finder to be dragged into that list.
    subprocess.run(["open", "-R", privacy["program"]], timeout=10, check=False)  # noqa: S607


def _print_folder_access(privacy: dict) -> None:
    """What to allow in System Settings, which no program can do for itself."""
    folders = privacy["folders"]
    console.print("[yellow]macOS has not let Varyz read these folders yet, so background "
                  "syncing skips them:[/yellow]")
    for folder in folders:
        console.print(f"  {escape(folder['source'])}  {escape(folder['path'])}", highlight=False)
    if privacy["via"] == "app":
        console.print(
            "If macOS asked, choose Allow. Otherwise switch them on under Varyz in System "
            "Settings > Privacy & Security > Files and Folders. That opens these folders "
            "only. To open that list:",
            highlight=False,
        )
        console.print(f'  open "{FILES_AND_FOLDERS_URL}"', highlight=False)
    else:
        console.print(
            "macOS has no folder-only switch for a program like this; the one it offers is "
            f"Full Disk Access, which opens more than these folders. To allow it, add "
            f"{escape(privacy['program'])} to System Settings > Privacy & Security > "
            "Full Disk Access and switch it on:",
            highlight=False,
        )
        console.print(f'  open "{FULL_DISK_ACCESS_URL}"', highlight=False)
    console.print(
        "Or skip it and run `varyz sync` yourself when you want them updated. "
        "Nothing leaves this Mac either way.\n",
        highlight=False,
    )


def _print_sources(cfg: Config) -> list:
    """The table `varyz sources` and the end of `varyz setup` share."""
    from varyz.sources import source_statuses

    conn = open_db(cfg.db_path) if cfg.db_path.exists() else None
    try:
        statuses = source_statuses(cfg, conn)
    finally:
        if conn is not None:
            conn.close()
    table = Table(
        "data source", "state", Column("items", justify="right"), "last sync", "scope",
        title="Data sources",
    )
    styles = {"connected": "green", "paused": "yellow", "not approved": "yellow"}
    for status in statuses:
        style = styles.get(status.state, "dim")
        last = "-"
        if status.last_sync:
            last = _local_minute(str(status.last_sync))
            if status.last_status and status.last_status != "ok":
                last += f" ({status.last_status})"
        table.add_row(
            status.info.label,
            f"[{style}]{status.state}[/{style}]",
            f"{status.items:,}",
            last,
            escape(status.scope),
        )
    console.print(table)
    for status in statuses:
        if status.connected:
            continue
        console.print(
            f"  [dim]{status.info.label} ({status.info.reads}):[/dim] "
            f"{escape(status.info.connect)}",
            highlight=False,
        )
    return statuses


def _browser_available() -> bool:
    """Whether `varyz setup` can open its page on this screen.

    Over SSH the browser would open on the far machine, if anywhere; on Linux
    with no display the only browsers are text ones, which cannot run the page.
    """
    import webbrowser

    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


@app.command()
def setup(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Connect the detected agents and the listed data sources"
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show every step; change nothing")
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="Offer only these data sources (repeatable): ai_conversations, git, documents",
        ),
    ] = None,
    no_sources: Annotated[
        bool, typer.Option("--no-sources", help="Connect no data sources now")
    ] = False,
    web: Annotated[
        bool,
        typer.Option(
            "--web", help="Open the setup page even where no browser is found (the default)"
        ),
    ] = False,
    terminal: Annotated[
        bool,
        typer.Option("--terminal", help="Ask in this terminal instead of opening a page"),
    ] = False,
    ai_mode: Annotated[
        str | None,
        typer.Option("--ai-mode", help="local (more privacy) or conversations (lighter)"),
    ] = None,
    cloud_conversations: Annotated[
        bool,
        typer.Option("--cloud-conversations",
                     help="Allow eligible AI conversation excerpts to your connected AI"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """First run, start to finish: archive, daemon, agents, data sources,
    graph, autosync and the local classifier. Varyz is fully working after it.

    Opens as a page in your browser, where agents and sources are ticked. It
    asks here instead with --terminal, with any flag that answers a question
    in advance, or where no browser can open (over SSH, say).

    Safe to run again: every step keeps what is already there, and the end
    says exactly which data sources are connected and how much each holds.
    """
    from varyz.config import find_config_file
    from varyz.setup_run import TerminalSetup, print_result

    if web and terminal:
        console.print("[red]--web and --terminal ask in different places;[/red] pick one.")
        raise typer.Exit(2)
    # The page asks every question itself, so an answer given in advance here
    # would be one the owner never saw there.
    given = [
        flag for flag, value in (
            ("--yes", yes), ("--dry-run", dry_run), ("--source", source),
            ("--no-sources", no_sources),
            ("--ai-mode", ai_mode), ("--cloud-conversations", cloud_conversations),
        ) if value
    ]
    if web and given:
        console.print(
            f"[red]--web asks on the page;[/red] drop {', '.join(given)}, "
            "or run setup here without --web."
        )
        raise typer.Exit(2)
    if not (web or terminal or given) and not _browser_available():
        console.print("[dim]No browser to open here, so setup asks in this terminal. "
                      "`varyz setup --web` serves the page anyway.[/dim]\n")
        terminal = True
    if not (terminal or given):
        from varyz.setup_web import serve

        cfg_path = (find_config_file(config) or _home_config()).expanduser().resolve()
        outcome = serve(cfg_path)
        if outcome is None:
            # Paused on the page: not a failure, and the installers say how
            # to carry on rather than "finished with problems".
            raise typer.Exit(SETUP_PAUSED)
        if not outcome:
            raise typer.Exit(1)
        return

    wanted = tuple(source) if source else SETUP_SOURCES
    unknown = [name for name in wanted if name not in SETUP_SOURCES]
    if unknown:
        console.print(
            f"[red]setup cannot connect {', '.join(unknown)}.[/red] "
            f"It connects {', '.join(SETUP_SOURCES)}; "
            "for GitHub run `varyz connect github`."
        )
        raise typer.Exit(2)
    cfg_path = (find_config_file(config) or _home_config()).expanduser().resolve()
    label = "Varyz setup (dry run: nothing is changed)" if dry_run else "Varyz setup"
    console.print(f"[bold]{label}[/bold] · {escape(str(cfg_path))}\n", highlight=False)
    # What is offered is decided before anything changes, as on the page; the
    # questions come first, so the steps then run with no one at the keyboard.
    run = TerminalSetup(cfg_path, wanted=wanted)
    if ai_mode is not None and ai_mode not in {"local", "conversations"}:
        console.print("[red]--ai-mode must be local or conversations.[/red]")
        raise typer.Exit(2)
    if ai_mode is not None and ai_mode not in run.ai_plan["choices"]:
        console.print("[red]This machine cannot comfortably run Qwen; use conversations.[/red]")
        raise typer.Exit(2)
    if cloud_conversations and ai_mode == "local":
        console.print("[red]--cloud-conversations requires conversation mode.[/red]")
        raise typer.Exit(2)
    if ai_mode is not None:
        run.inference_mode = ai_mode
    if cloud_conversations:
        run.inference_mode = "conversations"
    if dry_run:
        _setup_dry_run(run)
        return

    console.print("AI setup: " + ("Both modes fit this machine." if run.ai_plan["comfortable"]
                                 else "AI conversation mode selected for this machine."))
    for reason in run.ai_plan["reasons"]:
        console.print(f"  {reason}", markup=False)
    if run.ai_plan["comfortable"] and ai_mode is None and not yes and not _confirm(
        "More privacy: read locally? Choose no for Lighter on your device.",
        instead="Use --ai-mode local or --ai-mode conversations.",
        default=run.inference_mode == "local",
    ):
        run.inference_mode = "conversations"
    if run.inference_mode == "conversations":
        console.print(
            "Lighter on your device: no Ollama/Qwen download. Only eligible AI conversation "
            "excerpts may go to your connected AI. Documents and repositories stay local.",
            markup=False,
        )
        if cloud_conversations:
            run.cloud_conversations = True
        elif not yes:
            run.cloud_conversations = _confirm(
                "Allow eligible AI conversation excerpts to your connected cloud AI?",
                instead="Use --cloud-conversations to allow this explicitly.",
                default=run.cloud_conversations,
            )
    else:
        run.cloud_conversations = False

    present = [(cid, name) for cid, name, installed in run.agents if installed]
    agents: list[str] = []
    if present and (yes or _confirm(
        f"Connect Varyz to {', '.join(name for _, name in present)}? "
        "(adds the MCP server, briefing hooks and skill)",
        instead="Pass --yes to connect every agent found.",
        default=True,
    )):
        agents = [cid for cid, _ in present]
    sources: list[str] = []
    if no_sources:
        run.skip_sources = "--no-sources"
    elif not run.offers:
        console.print("Data sources: nothing new found to connect")
    else:
        console.print("Data sources found on this machine (read locally, never uploaded):")
        for cid, name, lines in run.offers:
            console.print(f"  [bold]{name}[/bold]")
            for line in lines:
                console.print(f"    {escape(line)}", highlight=False)
            if yes or _confirm(
                f"  Connect {name}?",
                instead="Pass --yes to connect every source listed, or "
                "--source to choose which.",
            ):
                sources.append(cid)
    console.print()

    from varyz.analytics import CONSENT_COPY, CONSENT_TITLE

    if not yes:
        console.print(f"{CONSENT_TITLE}\n\n{CONSENT_COPY}", markup=False)
        try:
            run.analytics_choice = typer.confirm("Share anonymized data?", default=False)
        except (typer.Abort, EOFError):
            console.print("No analytics choice given; existing sharing settings are kept.")
    result = run.execute(tuple(agents), tuple(sources))

    # What the owner has now, and what is still missing.
    console.print()
    _print_sources(_cfg(cfg_path))
    if result["privacy"] is not None:
        _print_folder_access(result["privacy"])
    print_result(result)
    if result["verdict"] != "GO":
        raise typer.Exit(1)


# The exit code of a setup paused in a download, on the page or with Ctrl+C
# in the terminal (`main`): not a failure, and the next `varyz setup` carries
# on from what has downloaded. The installers tell it from a failure.
SETUP_PAUSED = 130


def _setup_dry_run(run) -> None:
    """Every step as it would run, with nothing created, connected or indexed."""
    from varyz.install import connect_agent
    from varyz.resolution.client import ResolutionClientError

    cfg_path = run.config_path
    comfort = "local model fits" if run.ai_plan["comfortable"] else "lighter mode selected"
    console.print(f"AI mode: {run.inference_mode}; {comfort}")
    if not cfg_path.exists():
        _step(1, "Archive", f"would create {cfg_path} and its database")
        console.print(
            "Dry run stops here: the remaining steps read the archive this creates. "
            "They start the daemon, connect your agents and "
            "offer your local data sources, asking before each, then build the graph, "
            "turn on autosync and ready the local classifier."
        )
        return
    cfg = _cfg(cfg_path)
    entries = _readonly_entry_count(cfg.db_path)
    _step(1, "Archive",
          "would create the database" if entries is None else f"ready, {entries:,} entries")
    try:
        _resolution_client(cfg).status()
        _step(2, "Daemon", f"running on {cfg.resolution.bind_host}:{cfg.resolution.port}")
    except ResolutionClientError as exc:
        _step(2, "Daemon", f"would start ({escape(str(exc))})")
    present = [(cid, name) for cid, name, installed in run.agents if installed]
    absent = [name for _, name, installed in run.agents if not installed]
    if not present:
        _step(3, "Agents", f"none found ({', '.join(absent)} not installed)")
    else:
        outcome = []
        for cid, name in present:
            try:
                _, results = connect_agent(cid, config_path=cfg_path, dry_run=True)
            except ValueError as exc:
                outcome.append(f"{name} [red]failed[/red] ({escape(str(exc))})")
                continue
            changed = any(result.changed for result in results)
            outcome.append(f"{name} {'would connect' if changed else 'connected'}")
        if absent:
            outcome.append(f"{', '.join(absent)} not installed")
        _step(3, "Agents", " · ".join(outcome))
    if not run.offers:
        _step(4, "Data sources", "nothing new found to connect")
    else:
        _step(4, "Data sources", "would offer (read locally, never uploaded):")
        for _, name, lines in run.offers:
            console.print(f"  [bold]{name}[/bold]")
            for line in lines:
                console.print(f"    {escape(line)}", highlight=False)
    console.print("  Dry run: nothing connected or indexed.")
    _step(5, "Graph", "would build the graph and compile the personal model")
    _step(6, "Autosync", "would sync in the background, at login and on a schedule")
    if run.inference_mode == "local":
        _step(7, "Classifier", f"would ready {cfg.workflows.classifier_model} in Ollama, "
              "installing Ollama if it is missing")
    else:
        _step(7, "Classifier", "would use AI conversation mode; no Ollama/Qwen download; "
              "cloud reading requires explicit consent")
    console.print()
    _print_sources(cfg)


@app.command("sources")
def sources_command(config: ConfigOpt = None) -> None:
    """Which data sources are connected, how much each holds, and how to add the rest."""
    _print_sources(_cfg(config))


def local_model_install(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Approve the displayed downloads"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show exact downloads; write nothing"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Install Varyz-owned llama.cpp and its pinned GGUF model."""
    from varyz.llm.local_install import install, managed_downloads
    from varyz.llm.local_runtime import ManagedLlamaRuntime

    cfg = _cfg(config)
    if cfg.local_model.backend == "ollama":
        console.print(
            "[yellow]The configured backend is external Ollama; Varyz will not "
            "install or modify it.[/yellow]"
        )
        return
    runtime = ManagedLlamaRuntime(cfg)
    if runtime.available():
        console.print("[green]Local model is already installed and ready.[/green]")
        return

    console.print("Managed local inference plan:")
    for item in managed_downloads(cfg):
        console.print(f"  {item.label}  {item.size_bytes:,} bytes")
        console.print(f"    from  {item.source}")
        console.print(f"    to    {item.destination}")
        if item.sha256:
            console.print(f"    sha256 {item.sha256}")
    console.print("Every download is pinned and SHA-256 verified.")
    needed = runtime.bytes_still_needed()
    free = free_bytes(cfg.data_dir)
    if free is not None:
        console.print(
            f"Needs about {bytes_label(needed)}; {bytes_label(free)} is free on "
            f"{cfg.data_dir.anchor or cfg.data_dir}."
        )
    if dry_run:
        console.print("Dry run: no network requests or file writes were made.")
        return
    if not yes and not _confirm(
        "Download and install these local artifacts?",
        instead="Pass --yes to approve exactly the downloads printed above.",
    ):
        console.print("Nothing downloaded or installed.")
        return

    reported: dict[str, int] = {}

    def progress(phase: str, downloaded: int, total: int) -> None:
        percent = min(100, int(downloaded * 100 / max(total, 1)))
        bucket = percent // 10
        if reported.get(phase) == bucket:
            return
        reported[phase] = bucket
        console.print(f"  {phase}: {percent}%")

    try:
        status = install(runtime, on_progress=progress)
    except RuntimeError as exc:
        # A full disk, a half-extracted runtime directory, a checksum that does
        # not match: every one of these was a traceback naming a line of
        # local_runtime.py, on the command a first-time user runs first.
        console.print(f"[red]Local model not installed: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Local model ready.[/green] {escape(str(status.detail))}")


# Held back from the alpha wheel (docs/SHIPPING.md): the alpha never downloads
# the managed model. The command registers only where its installer module is
# present, which is the source tree.
def _register_local_model_install() -> None:
    from varyz.llm.local_runtime import installer_is_shipped

    if installer_is_shipped():
        local_model_app.command("install")(local_model_install)


_register_local_model_install()


@local_model_app.command("status")
def local_model_status(config: ConfigOpt = None) -> None:
    """Show the selected backend, artifacts, and on-demand readiness."""
    from varyz.llm.local import build_local_provider

    cfg = _cfg(config)
    provider = build_local_provider(cfg)
    console.print(f"backend: {cfg.local_model.backend}")
    console.print(f"model: {provider.model}")
    console.print(f"status: {provider.status()}")
    details = provider.runtime_details()
    if details:
        for key, value in details.items():
            console.print(f"{key}: {value}")


@app.command()
def add(
    text: Annotated[str, typer.Argument(help="The thought/story/note to capture")],
    when: Annotated[
        str | None,
        typer.Option(help="When it happened: 2019, 2019-07, 2019-07-14, or ISO datetime"),
    ] = None,
    todo: Annotated[bool, typer.Option("--todo", help="Also file it as an open TODO")] = False,
    config: ConfigOpt = None,
) -> None:
    """Capture a thought in seconds. It is dated, filed, and embedded."""
    # `varyz add ""` -- a shell that expanded a variable to nothing -- filed an
    # empty entry, embedded it, and answered "Captured 01M2...". It then
    # counted in the archive, went to the graph, and could only be removed by
    # `varyz forget`.
    if not text.strip():
        console.print("[red]Nothing to capture: the text is empty.[/red]")
        raise typer.Exit(2)
    # A date varyz cannot read used to reach datetime.fromisoformat inside the
    # connector and raise there, so "--when yesterday" -- or a date written the
    # European way round -- answered with a traceback instead of saying which
    # spellings it takes.
    if when is not None:
        try:
            parse_when(when, utc_now())
        except ValueError:
            console.print(
                f"[red]--when {escape(when)} is not a date varyz can read.[/red] "
                "Write it as 2019, 2019-07, 2019-07-14, or a full ISO timestamp."
            )
            raise typer.Exit(2) from None
    cfg = _cfg(config)
    conn = _db(cfg)
    connector = ManualConnector()
    item = ManualConnector.make_item(text, when=when)
    gate = SensitivityGate(cfg.sensitivity)
    with WriteLock(cfg.data_dir):
        entry_id, _ = ingest_item(conn, Vault(cfg.vault_dir), connector, gate, item)
        if todo:
            with conn:
                dao.create_todo(conn, text, entry_id=entry_id, source_quote=text)
        embed_pending(conn, cfg, Embedder(cfg))
    console.print(f"Captured [dim]{entry_id}[/dim]" + (" (+ TODO)" if todo else ""))


@app.command()
def sync(
    connector: Annotated[
        str | None, typer.Argument(help="Connector to sync; omit for all configured")
    ] = None,
    no_embed: Annotated[bool, typer.Option("--no-embed", help="Skip the embedding pass")] = False,
    config: ConfigOpt = None,
) -> None:
    """Pull new items from configured sources into your archive."""
    cfg = _cfg(config)
    conn = _db(cfg)
    known = connector_registry.all_connector_ids()
    if connector is not None and connector not in known:
        console.print(f"Unknown connector '{connector}'. Available: {', '.join(known)}.")
        raise typer.Exit(2)
    configured = [c for c in known if cfg.connector_options(c)]
    ids = [connector] if connector else configured
    if not ids:
        # Rich reads square brackets as style tags, so a config section name has
        # to be escaped or it silently vanishes from the very message meant to
        # tell the user which section to write.
        console.print(r"No connectors configured. Add paths under \[connectors.*] in config.toml.")
        raise typer.Exit(0)
    if connector is not None and connector not in configured:
        console.print(
            rf"'{connector}' is not configured. Add a \[connectors.{connector}] section "
            "to config.toml first."
        )
        raise typer.Exit(2)
    vault = Vault(cfg.vault_dir)
    failed: list[str] = []
    with WriteLock(cfg.data_dir), _progress_bar("Syncing") as bar:
        for cid in ids:
            bar(f"Reading {cid}")
            try:
                stats = sync_connector(
                    conn,
                    vault,
                    get_connector(cid),
                    cfg,
                    on_progress=lambda s, cid=cid: bar(f"Reading {cid}: {s.total:,} items"),
                )
            except (ValueError, FileNotFoundError, PermissionError) as exc:
                # A connector refusing its own configuration is an ordinary
                # answer to a user mistake. A stack trace on the first command
                # anyone runs reads as a broken install.
                #
                # And it stops at this connector, not at the sync. The sources
                # are independent: someone who mistyped one git path still
                # wants their documents indexed, and stopping everything makes
                # a small mistake look like a broken tool.
                console.print(f"[yellow]{cid}: {escape(str(exc))}[/yellow]")
                failed.append(cid)
                continue
            _print_stats(stats)
        if not no_embed:
            bar(_fetch_search_models(cfg, bar))
            n = embed_pending(conn, cfg, Embedder(cfg), on_progress=_embedding_progress(bar))
            if n:
                console.print(f"Embedded {n} chunks locally.")
    if failed:
        console.print(f"[red]Did not sync: {', '.join(failed)}.[/red]")
        raise typer.Exit(1)


@app.command()
def embed(config: ConfigOpt = None) -> None:
    """Run the local embedding pass over anything not yet embedded."""
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir), _progress_bar(LOADING_SEARCH_MODELS) as bar:
        bar(_fetch_search_models(cfg, bar))
        n = embed_pending(conn, cfg, Embedder(cfg), on_progress=_embedding_progress(bar))
    console.print(f"Embedded {n} chunks.")


def _explain_nothing_to_enrich(conn: sqlite3.Connection) -> None:
    """Why there is no chat-day to send, which is three different situations.

    "all chats are already curated" was printed for all of them: for an archive
    holding no chat message at all, and for one whose chats are every one of
    them sensitive. The second is the case where varyz is deliberately keeping
    data on this machine, and it was reported as the case where varyz had
    nothing left to do.
    """
    total = conn.execute("SELECT COUNT(*) FROM entries WHERE kind='message'").fetchone()[0]
    if not total:
        console.print("No chat messages in the archive yet.")
        return
    held = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE kind='message' "
        "AND sensitivity NOT IN ('public','personal')"
    ).fetchone()[0]
    if held == total:
        console.print(
            f"Nothing to send: all {total:,} chat messages are sensitive. They stay on "
            "this machine and keep their local significance score."
        )
        return
    if held:
        console.print(
            f"Nothing pending - the {total - held:,} cloud-eligible chat messages are "
            f"curated, and {held:,} sensitive ones stayed local."
        )
        return
    console.print("Nothing pending - all chats are already curated.")


def enrich(
    estimate: Annotated[
        bool, typer.Option("--estimate", help="Only show volume and cost, don't run")
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(min=1, help="Process at most this many chat-days"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Let Claude curate which chat messages matter for the timeline.

    Sends cloud-eligible chat-days to the cheap enrichment model; sensitive
    chats keep their local heuristic score. Search and ask are unaffected."""
    from varyz.llm.router import LLMRouter, NoApiKeyError
    from varyz.pipeline.enrich import (
        estimate_tokens,
        pending_chat_days,
        refine_significance,
    )

    cfg = _cfg(config)
    conn = _db(cfg)
    days = pending_chat_days(conn, limit)
    if not days:
        _explain_nothing_to_enrich(conn)
        raise typer.Exit(0)
    tokens = estimate_tokens(days)
    enrich_model = cfg.llm.models_for().enrich_model
    console.print(
        f"{len(days)} chat-days pending · ~{tokens:,} input tokens "
        f"on {cfg.llm.provider}:{enrich_model}"
    )
    if estimate:
        raise typer.Exit(0)
    try:
        provider = LLMRouter(cfg).enrich_provider()
    except NoApiKeyError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    with WriteLock(cfg.data_dir):
        processed, promoted = refine_significance(
            conn,
            provider,
            destination=f"cloud:{cfg.llm.provider}",
            policy_version=cfg.privacy.policy_version,
            privacy_mode=cfg.privacy.mode,
            limit=limit,
            on_progress=lambda i, n, p: (
                console.print(f"  {i}/{n} chat-days · {p} promoted") if i % 25 == 0 else None
            ),
        )
    console.print(
        f"[green]Done.[/green] {processed} chat-days curated, "
        f"{promoted} messages promoted to the timeline."
    )


# Held back from the alpha wheel (docs/SHIPPING.md): it sends chat-days to a
# cloud model. It registers only where its pipeline module is present, which is
# the source tree.
def _enrich_is_shipped() -> bool:
    # find_spec, not an import: the module stays unloaded until the command runs.
    try:
        return importlib.util.find_spec("varyz.pipeline.enrich") is not None
    except ModuleNotFoundError as exc:
        # Only the module itself being absent means "held back".
        if exc.name != "varyz.pipeline.enrich":
            raise
        return False


if _enrich_is_shipped():
    app.command("enrich")(enrich)


@app.command()
def mcp(config: ConfigOpt = None) -> None:
    """Run the MCP server on stdio. Agents call this; people rarely do."""
    from varyz.config import find_config_file
    from varyz.mcp_server import serve as serve_mcp

    # Nothing may be printed here. stdout is the protocol channel, and a
    # welcome banner on it is a parse error at the other end.
    serve_mcp(config_path=find_config_file(config))


@app.command("resolve")
def resolve_owner(
    question: Annotated[str, typer.Argument(help="Owner-specific question to resolve")],
    no_escalation: Annotated[
        bool, typer.Option("--no-escalation", help="Return NOT_KNOWN instead of asking the owner")
    ] = False,
    conversation_id: Annotated[
        str | None, typer.Option(help="Calling conversation identifier")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Resolve an owner-specific question through the local daemon."""
    from varyz.resolution.models import ResolutionRequest

    cfg = _cfg(config)
    purpose = "owner_context"
    request = ResolutionRequest(
        question=question,
        caller=_local_owner_caller(cfg, purpose),
        purpose=purpose,
        conversation_id=conversation_id,
        allow_escalation=not no_escalation,
    )
    result = _resolution_client(cfg).resolve(request)
    if result.answer is not None:
        console.print(result.answer)
        basis = result.basis.value if result.basis else "no basis"
        console.print(
            f"[dim]{result.disposition.value} · {basis} "
            f"· tier {result.tier} · confidence {result.confidence:.2f}[/dim]"
        )
        return
    if result.escalation_id:
        console.print(f"ESCALATED {result.escalation_id}")
    else:
        console.print("NOT_KNOWN")


@app.command("know")
def know(
    question: Annotated[str, typer.Argument(help="Question this knowledge answers")],
    answer: Annotated[str, typer.Argument(help="Owner's explicit answer")],
    supersedes_id: Annotated[
        str | None, typer.Option(help="Earlier knowledge entry this corrects")
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project", "-p",
            help="Project for this answer (default: the current working directory)",
        ),
    ] = None,
    general: Annotated[
        bool, typer.Option("--general", help="Explicitly apply this answer to all projects")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Teach Varyz an explicit owner answer through the daemon."""
    from varyz.resolution.advice import default_project
    from varyz.resolution.scope import suggest_scope
    from varyz.scopes import normalize_scope

    if general and project is not None:
        raise typer.BadParameter("Choose --general or --project, not both.")
    asked_in = project or default_project()
    scope = (
        "*" if general else normalize_scope(project) if project is not None
        else suggest_scope(question, answer, asked_in).scope
    )
    cfg = _cfg(config)
    purpose = "owner_knowledge"
    knowledge_id = _resolution_client(cfg).remember(
        question=question,
        answer=answer,
        caller=_local_owner_caller(cfg, purpose),
        device_id=cfg.resolution.device_id,
        supersedes_id=supersedes_id,
        scope=scope,
    )
    where = "general preference" if scope == "*" else f"for {scope} only"
    console.print(f"[green]Known[/green] ({where}). {knowledge_id}")


@app.command("answer")
def answer_escalation(
    escalation_id: Annotated[str, typer.Argument(help="Pending question id")],
    answer: Annotated[str, typer.Argument(help="Owner's answer")],
    scope: Annotated[
        str | None,
        typer.Option(
            help="general (every project) | project (the one it was asked in) | a "
            "project name. Default: varyz guesses, and a one-off choice stays with "
            "its project",
        ),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Answer a pending owner question through the daemon."""
    from varyz.resolution.scope import parse_scope_choice

    cfg = _cfg(config)
    client = _resolution_client(cfg)
    stored_scope = None
    if scope:
        asked_in = next(
            (item.get("project") for item in client.pending() if item.get("id") == escalation_id),
            None,
        )
        try:
            stored_scope = parse_scope_choice(scope, asked_in)
        except ValueError:
            console.print(
                "[red]--scope project needs a question that was asked in a project; "
                "name the project instead.[/red]"
            )
            raise typer.Exit(2) from None
    result = client.answer(
        escalation_id,
        answer,
        device_id=cfg.resolution.device_id,
        scope=stored_scope,
    )
    console.print(result.answer or "Answered.")
    console.print(f"[dim]{result.basis.value if result.basis else result.disposition.value}[/dim]")


@app.command("questions")
def pending_questions(config: ConfigOpt = None) -> None:
    """List unanswered owner questions from the resolution daemon."""
    items = _resolution_client(_cfg(config)).pending()
    if not items:
        console.print("No pending questions.")
        return
    table = Table("id", "project", "caller", "question", "created")
    for item in items:
        caller = item.get("caller")
        table.add_row(
            str(item.get("id", "")),
            str(item.get("project") or "-"),
            str(caller.get("agent_id", "") if isinstance(caller, dict) else caller or ""),
            str(item.get("question", "")),
            str(item.get("created_at", "")),
        )
    console.print(table)


@app.command("away")
def away_mode(
    action: Annotated[str, typer.Argument(help="on, off, status or complete")] = "on",
    scope: Annotated[str, typer.Option(help="task (default), project or all")] = "task",
    project: Annotated[str | None, typer.Option(help="Defaults to this folder's name")] = None,
    task: Annotated[str | None, typer.Option("--task", help="Stable conversation/task id")] = None,
    minutes: Annotated[
        int | None, typer.Option(min=1, max=10080, help="Expire this setting after N minutes")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Defer questions while agents keep working; this never changes their permissions."""
    import json

    from varyz.resolution.away import review, set_mode, status

    if action not in {"on", "off", "status", "complete"}:
        raise typer.BadParameter("use on, off, status or complete")
    if scope not in {"task", "project", "all"}:
        raise typer.BadParameter("scope must be task, project or all")
    if action == "complete" and scope != "task":
        raise typer.BadParameter("complete only ends task scope")
    task = task or os.environ.get("CODEX_THREAD_ID")
    if scope == "task" and not task:
        raise typer.BadParameter(
            "pass --task <conversation-id>, or explicitly use --scope project/all"
        )
    project = project or Path.cwd().name
    with contextlib.closing(open_db(_cfg(config).db_path)) as conn:
        mode = status(conn, project, task) if action == "status" else set_mode(
            conn, active=action == "on", scope=scope, project=project, conversation_id=task,
            instruction=f"varyz away {action} --scope {scope}", duration_minutes=minutes,
        )
        payload = {"away_mode": mode, "review": review(conn, project, task, scope=scope)}
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("dismiss")
def dismiss_questions(
    escalation_ids: Annotated[
        list[str] | None, typer.Argument(help="Pending question ids to close")
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Close every pending question from this project"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Close pending owner questions without answering; varyz learns nothing from them."""
    client = _resolution_client(_cfg(config))
    ids = list(escalation_ids or [])
    if project:
        ids += [str(item["id"]) for item in client.pending() if item.get("project") == project]
        if not ids:
            # Asked for and already done: nothing from this project is waiting.
            console.print(f"No pending questions from {project}.")
            return
    if not ids:
        console.print("Nothing to dismiss: name question ids or a --project.")
        raise typer.Exit(2)
    closed = sum(client.dismiss(escalation_id) for escalation_id in dict.fromkeys(ids))
    console.print(f"Dismissed {closed} of {len(dict.fromkeys(ids))} question(s).")


@app.command("wrapped")
def wrapped(
    handle: Annotated[
        str,
        typer.Option(
            "--handle",
            help="Your @handle for the top-right corner, e.g. egor or @egor (default: none)",
        ),
    ] = "",
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", "-x", help="Leave this rule off the card (id or id prefix)"),
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help="Use only these rules (id or id prefix; repeatable)"),
    ] = None,
    include_project: Annotated[
        list[str] | None,
        typer.Option(
            "--include-project",
            help="Allow this project-scoped rule on the card, one id at a time",
        ),
    ] = None,
    size: Annotated[
        str, typer.Option("--size", help="square (1080x1080), story (1080x1920) or both")
    ] = "both",
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Folder for the files (default: <data dir>/wrapped)"),
    ] = None,
    no_stats: Annotated[
        bool, typer.Option("--no-stats", help="Leave the rule counts off the footer")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show exactly what the card would say; write nothing")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Write without asking after the preview")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """My Way: a shareable card (SVG + PNG) of how you want things done, from your own rules.

    Only your general rules are used unless you name a project rule with
    --include-project. You see every line and rule id before anything is written.
    Nothing leaves this machine: the output is local files.
    """
    from varyz.card import (
        EmptyCardError,
        HandleError,
        SelectionError,
        load_owner_rules,
        normalize_handle,
        output_paths,
        plan_card,
        png_available,
        preview_lines,
        write_card,
    )
    from varyz.card.layout import Size

    choices: dict[str, tuple[Size, ...]] = {
        "square": ("square",),
        "story": ("story",),
        "both": ("square", "story"),
    }
    if size not in choices:
        console.print("[red]--size must be square, story or both.[/red]")
        raise typer.Exit(2)
    try:
        handle = normalize_handle(handle)
    except HandleError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    cfg = _cfg(config)
    if not cfg.db_path.exists():
        console.print("[red]No varyz database found. Run: varyz setup[/red]")
        raise typer.Exit(1)
    # Read-only: the card never writes to the database.
    owner = load_owner_rules(cfg.db_path)
    try:
        plan = plan_card(
            owner,
            handle=handle,
            include=include_project or [],
            exclude=exclude or [],
            only=only or [],
            sizes=choices[size],
            stats=not no_stats,
        )
    except SelectionError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    except EmptyCardError:
        console.print(
            "No rules left for the card (answers that need their question to make "
            "sense are never printed). Add one with `varyz rules add`, or allow a "
            "project rule with --include-project <id>.",
            markup=False,
        )
        raise typer.Exit(1) from None
    for line in preview_lines(plan):
        console.print(line, markup=False, highlight=False)
    target = out if out is not None else cfg.data_dir / "wrapped"
    png = png_available()
    paths = output_paths(plan, target, png=png)
    if dry_run:
        console.print("Dry run: nothing written. Would write:", markup=False)
        for path in paths:
            console.print(f"  {path}", markup=False, highlight=False)
        return
    if not yes and not typer.confirm(f"Write {len(paths)} file(s) to {target}?", default=False):
        console.print("Nothing written.")
        return
    for path in write_card(plan, target, png=png):
        console.print(f"wrote {path}", markup=False, highlight=False)
    if not png:
        console.print(
            "[yellow]Pillow is not installed, so only the SVG files were written.[/yellow]"
        )


@app.command("doctor")
def operational_doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable checks")] = False,
    fix: Annotated[
        bool, typer.Option("--fix", help="Try bounded local repairs, then verify")
    ] = False,
    repair_job: Annotated[str | None, typer.Option(help="Current repair job ID")] = None,
    repair_state: Annotated[str | None, typer.Option(help="running, fixed or blocked")] = None,
    config: ConfigOpt = None,
) -> None:
    """Check Varyz's archive, daemon, credentials, hooks, sync and background reader."""
    from varyz.doctor import diagnose

    cfg = _cfg(config)
    if repair_job is not None or repair_state is not None:
        from varyz.doctor_repair import update_job

        if not repair_job or not repair_state:
            raise typer.BadParameter("--repair-job and --repair-state must be used together")
        try:
            update_job(cfg, repair_job, repair_state)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        console.print("Repair state recorded.")
        return
    repair = None
    if fix:
        from varyz.doctor_repair import fix as repair_local
        from varyz.install import resolve_config_path

        repair = repair_local(cfg, resolve_config_path(config))
    checks = diagnose(cfg)
    healthy = all(ok for _, ok, _ in checks)
    if as_json:
        import json

        console.print_json(json.dumps({"ok": healthy, "repair": repair, "checks": [
            {"name": name, "ok": ok, "detail": detail} for name, ok, detail in checks
        ]}))
    else:
        if repair:
            for attempt in repair["attempts"]:
                outcome = "completed" if attempt["ok"] else "failed"
                console.print(f"Repair {escape(attempt['action'])}: {outcome}")
            if repair["restart_clients"]:
                console.print("Restart reconnected AI clients to load their credentials.")
        for name, ok, detail in checks:
            mark = "[green]ok[/green]" if ok else "[red]FAIL[/red]"
            console.print(f"{mark} {escape(name)}: {escape(detail)}", highlight=False)
        console.print("Varyz operational checks passed." if healthy
                      else "Varyz needs attention; see failed checks above.")
    if not healthy:
        raise typer.Exit(1)


@hooks_app.command("doctor")
def hooks_doctor(config: ConfigOpt = None) -> None:
    """Go/no-go for the owner workflow: daemon, database, rules, hooks, pause."""
    from varyz.hooks import doctor

    checks = doctor(_cfg(config))
    for name, ok, detail in checks:
        mark = "[green]ok[/green]  " if ok else "[red]FAIL[/red]"
        console.print(f"{mark} {name}: {escape(str(detail))}", markup=True, highlight=False)
    failed = [name for name, ok, _ in checks if not ok]
    console.print("GO" if not failed else f"NO-GO: {', '.join(failed)}")
    if failed:
        raise typer.Exit(1)


@hooks_app.command("pause")
def hooks_pause(config: ConfigOpt = None) -> None:
    """Kill switch: every varyz hook does nothing until `varyz hooks resume`."""
    from varyz.hooks import pause_path

    path = pause_path(_cfg(config))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("paused\n", encoding="utf-8")
    console.print("Varyz hooks paused. Agents are not briefed or checked until resumed.")


@hooks_app.command("resume")
def hooks_resume(config: ConfigOpt = None) -> None:
    """Turn the varyz hooks back on after `varyz hooks pause`."""
    from varyz.hooks import pause_path

    pause_path(_cfg(config)).unlink(missing_ok=True)
    console.print("Varyz hooks running.")


@knowledge_app.command("list")
def knowledge_list(config: ConfigOpt = None) -> None:
    """List the owner answers resolution may use: current, not superseded or retracted."""
    items = _resolution_client(_cfg(config)).knowledge()
    if not items:
        console.print("Varyz knows nothing explicit yet. Teach it with `varyz know`.")
        return
    table = Table("id", "question", "answer", "basis", "source", "stated")
    for item in items:
        table.add_row(
            str(item["id"]),
            str(item["question"]),
            str(item["answer"]),
            str(item["basis"]),
            str(item.get("source", "")),
            str(item["stated_at"])[:10],
        )
    console.print(table)


@knowledge_app.command("retract")
def knowledge_retract(
    knowledge_id: Annotated[str, typer.Argument(help="Knowledge entry to withdraw")],
    reason: Annotated[str, typer.Option(help="Why it no longer holds")] = "",
    config: ConfigOpt = None,
) -> None:
    """Stop agents receiving an answer; the original stays in the audit history."""
    cfg = _cfg(config)
    retraction_id = _resolution_client(cfg).retract(
        knowledge_id,
        caller=_local_owner_caller(cfg, "owner_knowledge"),
        device_id=cfg.resolution.device_id,
        reason=reason,
    )
    console.print(f"[green]Retracted.[/green] {retraction_id}")


@knowledge_app.command("capture")
def knowledge_capture(
    apply: Annotated[
        bool, typer.Option("--apply", help="Record the picks now instead of listing them")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Record the options you picked in Claude Code questions as project decisions.

    Autosync does this on every cycle. By itself this is a dry run over every
    transcript; with --apply it records from where autosync last stopped.
    """
    from varyz.autosync import capture_decisions

    cfg = _cfg(config)
    conn = _db(cfg)
    try:
        stats = capture_decisions(cfg, conn, apply=apply)
    finally:
        conn.close()
    table = Table("measure", "count")
    for name, value in stats.as_dict().items():
        table.add_row(name.replace("_", " "), str(value))
    console.print(table)
    for pick in stats.recorded_picks:
        console.print(
            f"[bold]{escape(pick.project or '')}[/bold] {escape(pick.question)}\n"
            f"  → {escape(pick.answer)}  [dim]{escape(str(pick.answered_at or ''))[:10]}[/dim]"
        )
    if not apply:
        console.print("[dim]Dry run. Add --apply to record these as decisions.[/dim]")


def _rules_call(operation):
    """Run one rules call against the daemon, explaining the two usual failures."""
    from varyz.resolution.client import ResolutionClientError

    try:
        return operation()
    except ResolutionClientError as exc:
        if "HTTP 404" in str(exc):
            console.print(
                "[red]The running daemon predates rules.[/red] Restart it: "
                "`varyz daemon stop` then `varyz daemon start`."
            )
        elif "HTTP 400" in str(exc):
            console.print(f"[red]Refused:[/red] {escape(str(exc))}")
        elif "unavailable" in str(exc):
            console.print(f"[red]{escape(str(exc))}[/red] Start it with `varyz daemon start`.")
        else:
            console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc


def _scope_label(scope: str) -> str:
    return "all projects" if scope == "*" else scope


def _rule_origin(item: dict) -> str:
    provenance = item.get("provenance") or {}
    if "file" in provenance:
        return f"{Path(str(provenance['file'])).name}:{provenance.get('line', '')}"
    if provenance.get("occurred"):
        return f"your words, {str(provenance['occurred'])[:10]}"
    return str(item.get("source", ""))


@rules_app.command("list")
def rules_list(
    candidates: Annotated[
        bool, typer.Option("--candidates", help="Show suggestions awaiting your review")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """List the rules agents are advised with (or the suggestions awaiting review)."""
    cfg = _cfg(config)
    status = "candidate" if candidates else "confirmed"
    items = _rules_call(lambda: _resolution_client(cfg).principles(status))
    if not items:
        console.print(
            "No suggestions awaiting review." if candidates else
            "No rules yet. Add one with `varyz rules add`, or mine your own words "
            "with `varyz rules mine`."
        )
        return
    table = Table(Column("id", **ID_COLUMN), "kind", "scope", "rule", "why", "from")
    for item in items:
        table.add_row(
            str(item["id"]),
            str(item["kind"]),
            _scope_label(str(item["scope"])),
            str(item["statement"]),
            str(item.get("rationale") or ""),
            _rule_origin(item),
        )
    console.print(table)


@rules_app.command("add")
def rules_add(
    statement: Annotated[str, typer.Argument(help="The rule, in your words")],
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Only this project (default: all)")
    ] = None,
    why: Annotated[str, typer.Option(help="Why you want it this way")] = "",
    vision: Annotated[
        bool, typer.Option("--vision", help="What the project is for (needs --project)")
    ] = False,
    domain: Annotated[
        str,
        typer.Option(
            "--domain", "-d",
            help="A kind of work (website, python-tool, travel, cooking, ...), or your own "
            "(gardening): the rule joins its definition of done and reaches agents when a "
            "task is that work. See: varyz rules checklist",
        ),
    ] = "",
    config: ConfigOpt = None,
) -> None:
    """Tell agents how you want something done, every time."""
    cfg = _cfg(config)
    principle_id = _rules_call(lambda: _resolution_client(cfg).add_principle(
        statement=statement,
        caller=_local_owner_caller(cfg, "owner_knowledge"),
        device_id=cfg.resolution.device_id,
        source="manual",
        kind="vision" if vision else "rule",
        scope=project or "*",
        rationale=why,
        domain=domain,
    ))
    console.print(f"[green]Rule kept.[/green] {principle_id}")


@rules_app.command("retract")
def rules_retract(
    principle_id: Annotated[str, typer.Argument(help="Rule or suggestion to withdraw")],
    reason: Annotated[str, typer.Option(help="Why it no longer holds")] = "",
    config: ConfigOpt = None,
) -> None:
    """Stop agents receiving a rule; the original stays in the audit history."""
    cfg = _cfg(config)
    retraction_id = _rules_call(lambda: _resolution_client(cfg).retract_principle(
        principle_id,
        caller=_local_owner_caller(cfg, "owner_knowledge"),
        device_id=cfg.resolution.device_id,
        reason=reason,
    ))
    console.print(f"[green]Withdrawn.[/green] {retraction_id}")


@rules_app.command("review")
def rules_review(
    limit: Annotated[int, typer.Option(help="In the terminal, stop after this many")] = 20,
    terminal: Annotated[
        bool, typer.Option("--terminal", help="Ask in the terminal instead of a page")
    ] = False,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the page's link without opening it")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Keep, reword or drop what varyz learned from you; spot-check rules on trial."""
    from varyz.review_web import (
        ReviewSession,
        drop_candidate,
        is_trial,
        keep_candidate,
        serve,
        slot_of,
    )

    cfg = _cfg(config)
    client = _resolution_client(cfg)
    caller = _local_owner_caller(cfg, "owner_knowledge")
    device_id = cfg.resolution.device_id
    session = ReviewSession(client, caller, device_id, cfg)
    items = sorted(
        _rules_call(session.pending),
        key=lambda item: (not is_trial(item),
                          -int((item.get("provenance") or {}).get("score", 0))),
    )
    if terminal and not items:
        console.print("Nothing to review. Find suggestions with `varyz rules mine --apply`.")
        return
    if not terminal:
        kept, rejected = serve(session, open_browser=not no_browser)
        console.print(f"[green]{kept} kept[/green], {rejected} dropped.")
        return
    kept = rejected = 0
    for number, item in enumerate(items[:limit], 1):
        provenance = item.get("provenance") or {}
        console.print(
            f"\n[bold]{number}/{min(limit, len(items))}[/bold]  "
            + ("[yellow]in effect on trial[/yellow] " if is_trial(item) else "")
            + f"[dim]{escape(_rule_origin(item))} · {_scope_label(str(item['scope']))} · "
            f"{slot_of(item)}[/dim]"
        )
        console.print(f"  {escape(str(item['statement']))}")
        quotes = provenance.get("quotes") or (
            [{"quote": provenance["quote"]}] if provenance.get("quote") else []
        )
        for quote in quotes[:3]:
            console.print(f"  [dim]you wrote: \u201c{escape(str(quote.get('quote')))}\u201d[/dim]")
        choice = typer.prompt(
            "  [a]ccept  [e]dit  [s]cope  [r]eject  [k]skip  [q]uit", default="k"
        ).strip().casefold()[:1]
        if choice == "q":
            break
        if choice == "r":
            _rules_call(lambda item=item: drop_candidate(client, caller, device_id, item))
            rejected += 1
            continue
        if choice not in {"a", "e", "s"}:
            continue
        statement = None
        answer = None
        scope = None
        if choice == "e" and isinstance(provenance.get("decision"), dict):
            answer = typer.prompt("  Your answer",
                                  default=str(provenance["decision"].get("answer", "")))
        elif choice == "e" and "workflow" not in provenance:
            statement = typer.prompt("  Your wording", default=str(item["statement"]))
        if choice in {"e", "s"}:
            scope = typer.prompt(
                "  Applies to ('all' or a project name)",
                default="all" if item["scope"] == "*" else str(item["scope"]),
            )
        _rules_call(lambda item=item, statement=statement, scope=scope, answer=answer:
                    keep_candidate(client, caller, device_id, item, statement=statement,
                                   scope=scope, answer=answer))
        kept += 1
    console.print(f"\n[green]{kept} kept[/green], {rejected} rejected.")


_PROVIDER_NAMES = {"claude": "Claude", "openai": "ChatGPT", "deepseek": "DeepSeek"}


def _read_profile(
    cfg: Config, conn: sqlite3.Connection, stats: MiningStats,
    known: Callable[[str], list[str]],
) -> tuple[list[ProfileItem], ProfileState, str] | None:
    """What a model finds in the owner's turns: (items, the state to save, source).

    The cloud model reads them first; the local one when the owner keeps them
    on this machine (``--no-cloud``) or when the cloud cannot.
    ``None`` when no model can read them now.
    """
    from varyz.resolution.profile_reader import (
        CLOUD_REPLY_TOKENS,
        LOCAL_REPLY_TOKENS,
        SCHEMA,
        SOURCES,
        ProfileState,
        profile_path,
        read_with_model,
    )
    from varyz.resolution.rule_reader import (
        ReaderUnavailable,
        cloud_reader_from_config,
        local_fit,
        reader_from_config,
        reading_order,
    )

    cloud_name = _PROVIDER_NAMES.get(cfg.llm.provider, cfg.llm.provider)
    fit = local_fit(cfg)
    if fit[0] != "fits" and not cfg.workflows.rule_reader_cloud:
        console.print(f"[yellow]This machine is short for the local model: {escape(fit[1])}."
                      "[/yellow]")
        console.print(f"[dim]`varyz rules mine --cloud` lets {cloud_name} read "
                      "your turns instead, through your sign-in.[/dim]")
    order = reading_order(cfg, fit)
    for place, reader in enumerate(order, 1):
        label = "the local model" if reader == "local" else cloud_name
        before = stats.as_dict()
        try:
            if reader == "local":
                model = reader_from_config(cfg, schema=SCHEMA, max_tokens=LOCAL_REPLY_TOKENS)
            else:
                model, _name = cloud_reader_from_config(cfg, max_tokens=CLOUD_REPLY_TOKENS)
        except ReaderUnavailable as exc:
            console.print(f"[yellow]{label} cannot read your turns ({escape(str(exc))}).[/yellow]")
            continue
        state = ProfileState(profile_path(cfg, reader))
        title = f"Reading your turns with {label}"
        with _progress_bar(title) as progress:
            def step(kind: str, calls: int, title: str = title) -> None:
                what = "reading" if kind == "read" else "joining what it found"
                progress(f"{title}: {what}, {calls + 1} so far", None)

            try:
                items = read_with_model(
                    state, conn, model, tier=reader, reader=reader,
                    known=(lambda _scope: []) if reader == "cloud"
                    and cfg.workflows.inference_mode == "conversations" else known,
                    max_calls=None if reader == "local"
                    else cfg.workflows.rule_reader_cloud_max_calls,
                    on_progress=step, stats=stats,
                )
            except ReaderUnavailable as exc:
                console.print(
                    f"[yellow]{label} stopped: {escape(str(exc))}. "
                    "Run it again to carry on; what was read is kept.[/yellow]"
                )
                if place < len(order):
                    stats.__dict__.update(before)
                    continue
                return None
        if stats.deferred:
            console.print(
                f"[dim]{stats.deferred:,} turns wait for the next run: at most "
                f"{cfg.workflows.rule_reader_cloud_max_calls} calls a run "
                "([workflows] rule_reader_cloud_max_calls).[/dim]"
            )
        return items, state, SOURCES[reader]
    return None


@rules_app.command("mine")
def rules_mine(
    apply: Annotated[
        bool, typer.Option("--apply", help="Queue the suggestions for `varyz rules review`")
    ] = False,
    from_file: Annotated[
        list[Path] | None,
        typer.Option("--from-file", help="Instruction file or folder (CLAUDE.md, AGENTS.md)"),
    ] = None,
    turns: Annotated[
        bool, typer.Option("--turns/--no-turns", help="Read your turns to coding agents")
    ] = True,
    scope: Annotated[
        str, typer.Option(help="Scope for rules found in files: 'all' or a project")
    ] = "all",
    cap: Annotated[int, typer.Option(help="At most this many suggestions")] = 200,
    lexical: Annotated[
        bool, typer.Option("--lexical", help="Match words instead of reading turns with the model")
    ] = False,
    cloud: Annotated[
        bool | None,
        typer.Option(
            "--cloud/--no-cloud",
            help="Read your turns with the cloud model you signed in to (the default), "
            "or --no-cloud: only on this machine. Remembered",
        ),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Find rules in your own words. Prints counts only; nothing is advice until reviewed."""
    from varyz.configfile import set_rule_reader_cloud
    from varyz.resolution.rule_miner import MiningStats, mine_files, mine_turns, select

    cfg = _cfg(config)
    if cloud is not None and cloud != cfg.workflows.rule_reader_cloud:
        set_rule_reader_cloud(_config_path(config), cloud)
        cfg = _cfg(config)
        console.print(
            f"{_PROVIDER_NAMES.get(cfg.llm.provider, cfg.llm.provider)} reads your turns to "
            "AI agents for rules again, with credentials removed; sensitive turns never go. "
            "`--no-cloud` keeps them on this machine."
            if cloud else "Your turns are read on this machine only, by the local model. "
            "`--cloud` goes back to the cloud model."
        )
    stats = MiningStats()
    found = []
    profile: tuple[list[ProfileItem], ProfileState, str] | None = None
    client = _resolution_client(cfg) if apply else None
    if turns:
        conn = _db(cfg)
        try:
            if lexical or cfg.workflows.rule_miner == "lexical":
                found.extend(mine_turns(conn, stats))
            else:
                from varyz.resolution.profile_reader import known_from, replace_outdated

                profile_known: list[Callable[[str], list[str]]] = []

                def known(scope: str) -> list[str]:
                    # Looked up at the first merge, once the turns could be read: the
                    # candidates an older reading queued are replaced by this one's.
                    if not profile_known:
                        ledger: tuple[Any, ...] = ()
                        answers: tuple[Any, ...] = ()
                        if client is not None:
                            _rules_call(lambda: replace_outdated(
                                client, _local_owner_caller(cfg, "owner_knowledge"),
                                cfg.resolution.device_id))
                            ledger = _rules_call(lambda: (*client.principles("confirmed"),
                                                          *client.principles("candidate")))
                            answers = _rules_call(client.knowledge)
                        profile_known.append(known_from(ledger, answers))
                    return profile_known[0](scope)

                profile = _read_profile(cfg, conn, stats, known)
                if profile is None:
                    # Matching words would queue the owner's sentences as typed, which
                    # read as no rule at all; only `--lexical` asks for that.
                    console.print("[yellow]No model can read your turns now, so nothing "
                                  "was queued. Ask your AI agent to read them for Varyz: it "
                                  "hands the reading to a subagent on its cheapest model."
                                  "[/yellow]")
        finally:
            conn.close()
    if from_file:
        found.extend(mine_files(from_file, stats, scope=scope))
    chosen = select(found, stats, cap=cap)
    counts = stats.as_dict()
    queued = seen = on_trial = 0
    if client is not None:
        caller = _local_owner_caller(cfg, "owner_knowledge")

        def add(**fields: Any) -> str | None:
            nonlocal on_trial
            created = _rules_call(lambda: client.add_principle(
                caller=caller, device_id=cfg.resolution.device_id, **fields
            ))
            if client.last_trial_id:
                on_trial += 1
            return created

        for candidate in chosen:
            created = add(
                statement=candidate.statement,
                source=candidate.source,
                scope=candidate.scope,
                provenance={**candidate.provenance, "score": candidate.score},
                status="candidate",
            )
            if created is None:
                seen += 1
            else:
                queued += 1
        if profile is not None:
            from varyz.resolution.profile_reader import queue_items

            items, state, source = profile
            new, known = queue_items(items, add, source)
            queued += new
            seen += known
            # Only once they are queued: a failure leaves them to be joined again.
            state.save()
    table = Table("measure", "count")
    for key, value in counts.items():
        table.add_row(key.replace("_", " "), str(value))
    if profile is not None:
        for slot, label in (("vision", "vision"), ("never", "never"), ("rule", "rules"),
                            ("done", "definition of done"), ("workflow", "workflows"),
                            ("decision", "decisions")):
            table.add_row(f"found: {label}",
                          str(sum(1 for item in profile[0] if item.slot == slot)))
    if apply:
        table.add_row("in effect on trial", str(on_trial))
        table.add_row("queued for review", str(queued - on_trial))
        table.add_row("already seen", str(seen))
    console.print(table)
    if not apply:
        console.print("[dim]Dry run. Add --apply to queue them, then `varyz rules review`.[/dim]")
    elif queued:
        if on_trial:
            console.print(f"{on_trial} had strong evidence and are in effect on trial "
                          "(`varyz rules trials`).")
        console.print("Review the rest, and spot-check the trial rules, with "
                      "`varyz rules review`.")


@rules_app.command("read")
def rules_read(
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="full: every turn not read yet; live: the last day's"),
    ] = None,
    status: Annotated[bool, typer.Option("--status", help="Show the reader's state")] = False,
    stop: Annotated[
        bool, typer.Option("--stop", help="Stop the reader and drop what waits to be read")
    ] = False,
    now: Annotated[
        bool, typer.Option("--now", help="Read now, even if you asked to read later")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Read your turns to AI agents now, on your Claude Code or Codex sign-in, within its limits.

    The hooks start this in the background by themselves; it stops before the
    plan's limits run low and starts again once they reset.
    """
    from varyz.resolution import headless_reader

    cfg = _cfg(config)
    if kind is not None and kind not in headless_reader.KINDS:
        raise typer.BadParameter("--kind is full or live")
    if stop:
        headless_reader.update_status(cfg, lambda found: found.update(
            {"stop_run": "*", "pending": []}))
        console.print("The reader stops after the task in hand; nothing waits to be read.")
        return
    if now:
        from varyz.resolution.profile_reader import defer_reading

        defer_reading(cfg, 0.0)
    if not status:
        if kind is not None and not headless_reader.enabled(cfg, kind):
            console.print("[yellow]Reading is off: `[workflows] rule_reader_agent`, "
                          "`live_reading` or `reader_runner = \"agent\"`.[/yellow]")
            raise typer.Exit(1)
        headless_reader.supervise(cfg, _config_path(config).resolve(), kind=kind)
    from varyz.resolution import usage

    console.print(f"Reader: {escape(headless_reader.summary(cfg))}")
    stored = headless_reader.load_status(cfg).get("usage") or {}
    for name in usage.PROVIDERS:
        found = usage.read(name) if status else None
        if found is not None:
            headless_reader.update_status(cfg, lambda saved, name=name, found=found: saved
                                          .setdefault("usage", {}).update({name: found.as_dict()}))
        elif name in stored:
            found = usage.Usage.from_dict(stored[name])
        else:
            continue
        console.print(f"  {name}: {escape(usage.describe(found))}")


@rules_app.command("trials")
def rules_trials(
    off: Annotated[
        bool, typer.Option("--off", help="Stop putting new rules into effect on trial")
    ] = False,
    on: Annotated[bool, typer.Option("--on", help="Resume trial rules")] = False,
    undo_since: Annotated[
        str | None,
        typer.Option("--undo-since", help="Withdraw every rule still on trial since this "
                     "date (YYYY-MM-DD) or time"),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Withdraw without asking")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="The raw status")] = False,
    config: ConfigOpt = None,
) -> None:
    """Rules in effect on trial: how their trials ended, and the spot-check's measure."""
    import json

    from varyz.resolution.trials import off_path

    cfg = _cfg(config)
    sentinel = off_path(cfg.db_path)
    if off and on:
        raise typer.BadParameter("--off or --on, not both")
    if off:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("stopped by `varyz rules trials --off`\n", encoding="utf-8")
        console.print("No new rule goes into effect on trial; the ones on trial stay. "
                      "`--on` resumes.")
    elif on:
        sentinel.unlink(missing_ok=True)
        console.print("Rules with strong evidence go into effect on trial again.")
    client = _resolution_client(cfg)
    if undo_since is not None:
        since = _since_iso(undo_since)
        status = _rules_call(client.trial_rules)
        doomed = [rule for rule in status["rules"] if str(rule["since"]) >= since[:10]]
        if not doomed:
            console.print(f"No rule on trial since {since[:10]}.")
            return
        if not yes and not typer.confirm(
            f"Withdraw {len(doomed)} rule(s) on trial since {since[:10]}?", default=False
        ):
            return
        undone = _rules_call(lambda: client.undo_trials(
            since, caller=_local_owner_caller(cfg, "owner_knowledge"),
            device_id=cfg.resolution.device_id,
        ))
        console.print(f"[green]{undone} withdrawn.[/green] They will not be suggested again.")
        return
    status = _rules_call(client.trial_rules)
    if as_json:
        console.print_json(json.dumps(status, ensure_ascii=False, default=str))
        return
    settings = status["settings"]
    table = Table("trial rules", "count")
    for key, label in (("on_trial", "in effect on trial"), ("promoted", "held for good"),
                       ("kept", "kept in a spot-check"), ("dropped", "dropped in a spot-check"),
                       ("corrected", "corrected by you"), ("withdrawn", "withdrawn"),
                       ("undone", "undone")):
        table.add_row(label, str(status[key]))
    share = status["kept_share"]
    table.add_row("spot-check kept", "-" if share is None
                  else f"{share:.0%} of {status['spot_checked']}")
    console.print(table)
    console.print(
        f"[dim]A trial rule holds for good after {settings['promote_tasks']} tasks over "
        f"{settings['promote_days']} days with no correction. New trials pause when the "
        f"spot-check keeps under {settings['min_kept']:.0%}.[/dim]"
    )
    if status["paused"]:
        console.print(f"[yellow]Paused: {escape(str(status['paused']))}[/yellow]")
    for rule in status["rules"]:
        console.print(f"- {escape(str(rule['statement']))} [dim]({rule['since']}, "
                      f"{rule['tasks']} task(s), {escape(', '.join(rule['signals']))})[/dim]")


@rules_app.command("activate")
def rules_activate(
    apply: Annotated[
        bool, typer.Option("--apply", help="Put them into effect; without it, only count")
    ] = False,
    show: Annotated[int, typer.Option(help="Print this many of each lane")] = 5,
    config: ConfigOpt = None,
) -> None:
    """Sort the rules waiting for review: strong evidence goes into effect on trial."""
    cfg = _cfg(config)
    client = _resolution_client(cfg)
    result = _rules_call(lambda: client.scan_trials(
        apply=apply, device_id=cfg.resolution.device_id,
    ))
    table = Table("lane", "count")
    table.add_row("strong evidence: on trial", str(len(result["trial"])))
    table.add_row("waits for your review", str(len(result["review"])))
    console.print(table)
    for item in result["trial"][:show]:
        console.print(f"[green]trial[/green] {escape(str(item['statement']))} "
                      f"[dim]({escape(', '.join(item['signals']))})[/dim]")
    for item in result["review"][:show]:
        console.print(f"[dim]review[/dim] {escape(str(item['statement']))} "
                      f"[dim]({escape('; '.join(item['held_back']))})[/dim]")
    if result["paused"]:
        console.print(f"[yellow]Trial rules are paused: {escape(str(result['paused']))}[/yellow]")
    elif apply:
        console.print(f"[green]{result['started']} in effect on trial.[/green]")
    else:
        console.print("[dim]Dry run. Add --apply to put them into effect on trial.[/dim]")


def _since_iso(value: str) -> str:
    from datetime import UTC, datetime

    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise typer.BadParameter(f"not a date: {value} (use YYYY-MM-DD)") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


@rules_app.command("stats")
def rules_stats(config: ConfigOpt = None) -> None:
    """How many rules you keep, and how often varyz's suggestions were right."""
    from varyz.resolution.principles import SQLitePrincipleStore

    conn = _db(_cfg(config))
    try:
        stats = SQLitePrincipleStore(conn).stats()
    finally:
        conn.close()
    table = Table("measure", "value")
    for key in ("confirmed", "candidates", "accepted", "rejected", "acceptance_rate"):
        value = stats[key]
        table.add_row(key.replace("_", " "), "-" if value is None else str(value))
    for family, counts in sorted(stats["by_source"].items()):
        table.add_row(
            f"from {family}", f"{counts.get('confirmed', 0)} kept, "
            f"{counts.get('candidate', 0)} pending"
        )
    console.print(table)


@app.command("task")
def task_briefing(
    task: Annotated[str, typer.Argument(help="The task, as you would give it to an agent")],
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Default: this folder's name")
    ] = None,
    finish: Annotated[
        bool, typer.Option("--finish", help="Show the finish check instead of the briefing")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="The raw tool result")] = False,
    config: ConfigOpt = None,
) -> None:
    """Preview what an agent is handed for a task (start_task), or checked on (finish_check)."""
    import json

    from varyz.resolution.advice import default_project
    from varyz.resolution.briefing import finish_check, render_markdown, start_task
    from varyz.resolution.task_size import sizer_from_config

    cwd = None if project else Path.cwd()
    cfg = _cfg(config)
    conn = _db(cfg)
    try:
        call = finish_check if finish else start_task
        result = call(
            conn, task, project=project or default_project(), cwd=cwd,
            sizer=sizer_from_config(cfg),
        )
    finally:
        conn.close()
    if as_json:
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))
    elif finish:
        console.print(f"[dim]project: {escape(str(result['project']))}[/dim]")
        for item in result["definition_of_done"]:
            console.print(f"- \\[ ] [dim]{item['domain']}[/dim] {escape(item['item'])}")
        for item in result["open_owner_questions"]:
            console.print(f"[yellow]open[/yellow] {escape(item['question'])}")
        console.print(f"[dim]{escape(result['how'])}[/dim]")
    else:
        console.print(render_markdown(result, include_protocol=False), markup=False)


@rules_app.command("checklist")
def rules_checklist(
    domain: Annotated[
        str | None, typer.Argument(help="One kind of work; omit to list every kind")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Include this project's own items")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """The definition of done agents are handed for each kind of work.

    Your own kinds of work ('gardening') are listed after the built-in ones once
    a rule or workflow is filed under them.
    """
    from varyz.resolution.domains import BY_NAME, DOMAINS, domain_title
    from varyz.resolution.principles import SQLitePrincipleStore, normalize_domain
    from varyz.resolution.workflows import owner_kinds

    try:
        wanted = normalize_domain(domain) if domain else ""
    except ValueError:
        console.print(
            f"[red]Not a kind of work: {escape(domain or '')}.[/red] Use a short name such "
            "as travel, cooking or gardening."
        )
        raise typer.Exit(2) from None
    conn = _db(_cfg(config))
    try:
        own = [name for name in owner_kinds(conn) if name not in BY_NAME]
        names = [wanted] if wanted else [item.name for item in DOMAINS] + own
        checklist = SQLitePrincipleStore(conn).checklist(names, project=project)
    finally:
        conn.close()
    for name in names:
        items = checklist.get(name, ())
        mine = "" if name in BY_NAME else ", yours"
        console.print(
            f"[bold]{escape(domain_title(name))}[/bold] [dim]({escape(name)}{mine}, "
            f"{len(items)})[/dim]"
        )
        for item in items:
            where = "" if item.scope == "*" else f" [dim]({escape(item.scope)} only)[/dim]"
            console.print(f"  - {escape(item.statement)}{where}")
    if wanted and wanted not in BY_NAME and wanted not in own:
        console.print(
            f"[dim]No rule is filed under {escape(wanted)} yet. Add one with: "
            f'varyz rules add "..." --domain {escape(wanted)}[/dim]'
        )


@rules_app.command("advise")
def rules_advise(
    plan: Annotated[str, typer.Argument(help="A task or plan, as an agent would send it")],
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Default: this folder's name")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Preview what an agent is told before it builds this."""
    from varyz.resolution.advice import advise, default_project

    conn = _db(_cfg(config))
    try:
        result = advise(conn, plan, project=project or default_project())
    finally:
        conn.close()
    console.print(f"[dim]project: {escape(str(result['project']))}[/dim]")
    for heading, key in (("Vision", "project_vision"), ("Rules", "owner_rules")):
        for item in result[key]:
            console.print(
                f"[bold]{heading[:-1] if heading == 'Rules' else heading}[/bold] "
                f"[dim]({escape(str(item['scope']))})[/dim] {escape(str(item['statement']))}"
            )
    for item in result["owner_decisions"]:
        console.print(
            f"[bold]Decision[/bold] {escape(str(item['question']))} → "
            f"{escape(str(item['answer']))}"
        )
    if result["more_rules_in_scope"]:
        console.print(f"[dim]{result['more_rules_in_scope']} more rule(s) in scope.[/dim]")
    if not result["known"]:
        console.print(result["how_to_use"])


def _workflow_steps(item: dict) -> str:
    return " → ".join(
        str(step["step"]) + (" (optional)" if step.get("optional") else "")
        for step in item.get("steps", [])
    )


def _workflow_origin(item: dict) -> str:
    if item.get("basis") != "learned":
        return "your words"
    provenance = item.get("provenance") or {}
    units = provenance.get("units")
    return f"learned, {units} {provenance.get('unit', 'sessions')}" if units else "learned"


@workflows_app.command("list")
def workflows_list(
    steps: Annotated[
        bool, typer.Option("--steps", help="Also show how you guide each step")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """List the workflows agents are handed with a task."""
    cfg = _cfg(config)
    items = _rules_call(lambda: _resolution_client(cfg).workflows())
    if not items:
        console.print(
            "No workflows yet. Add one with `varyz workflows add website colour logo "
            "\"page content\"`, or learn them from your sessions with `varyz workflows learn`."
        )
        return
    table = Table(Column("id", **ID_COLUMN), "name", "scope", "kind of work", "order", "from")
    for item in items:
        table.add_row(
            str(item["id"]),
            str(item["name"]),
            _scope_label(str(item["scope"])),
            str(item.get("domain") or ""),
            _workflow_steps(item),
            _workflow_origin(item),
        )
    console.print(table)
    if steps:
        for item in items:
            notes = [step for step in item.get("steps", []) if step.get("how")]
            if notes or item.get("rationale"):
                console.print(f"\n[bold]{escape(str(item['name']))}[/bold]"
                              f" ({_scope_label(str(item['scope']))})")
                if item.get("rationale"):
                    console.print(f"  why: {escape(str(item['rationale']))}")
                for step in notes:
                    console.print(
                        f"  {escape(str(step['step']))}: {escape(str(step['how']))}"
                    )


@workflows_app.command("add")
def workflows_add(
    name: Annotated[str, typer.Argument(help="What the workflow is for: website, product, ...")],
    steps: Annotated[
        list[str],
        typer.Argument(
            help="The steps in your order. 'logo: three options side by side' adds how "
            "you guide that step."
        ),
    ],
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Only this project (default: all)")
    ] = None,
    domain: Annotated[
        str,
        typer.Option(
            "--domain", "-d",
            help="The kind of work it is for (website, docs, travel, ...), or your own "
            "(gardening); omit for one that spans kinds of work, like a product",
        ),
    ] = "",
    why: Annotated[str, typer.Option(help="Why you take the steps in this order")] = "",
    config: ConfigOpt = None,
) -> None:
    """Tell agents the order you guide a kind of work in. Replaces one of the same name."""
    from varyz.resolution.workflows import parse_steps

    cfg = _cfg(config)
    try:
        parsed = parse_steps(steps)
    except ValueError as exc:
        console.print(f"[red]Refused:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    workflow_id = _rules_call(lambda: _resolution_client(cfg).add_workflow(
        name=name,
        steps=[step.to_dict() for step in parsed],
        caller=_local_owner_caller(cfg, "owner_knowledge"),
        device_id=cfg.resolution.device_id,
        source="manual",
        scope=project or "*",
        domain=domain,
        rationale=why,
    ))
    order = " → ".join(step.step for step in parsed)
    console.print(f"[green]Workflow kept.[/green] {workflow_id}  {escape(order)}")


@workflows_app.command("retract")
def workflows_retract(
    workflow_id: Annotated[str, typer.Argument(help="Workflow to withdraw")],
    reason: Annotated[str, typer.Option(help="Why it no longer holds")] = "",
    config: ConfigOpt = None,
) -> None:
    """Stop agents receiving a workflow. A withdrawn learned order is never learned back."""
    cfg = _cfg(config)
    retraction_id = _rules_call(lambda: _resolution_client(cfg).retract_workflow(
        workflow_id,
        caller=_local_owner_caller(cfg, "owner_knowledge"),
        device_id=cfg.resolution.device_id,
        reason=reason,
    ))
    console.print(f"[green]Withdrawn.[/green] {retraction_id}")


@workflows_app.command("review")
def workflows_review(
    workflow_id: Annotated[
        str | None, typer.Argument(help="Workflow to decide about (omit to list)")
    ] = None,
    step: Annotated[str | None, typer.Argument(help="The step you keep skipping")] = None,
    decision: Annotated[
        str | None, typer.Argument(help="keep, optional (never raised as open) or drop")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """How often agents raised each step, what you answered, and steps up for review."""
    from varyz.resolution.workflow_nudges import DECISIONS, SQLiteNudgeStore
    from varyz.resolution.workflows import REVIEW_AFTER_SKIPS, REVIEW_WINDOW

    cfg = _cfg(config)
    if workflow_id is not None:
        if step is None or decision not in DECISIONS:
            console.print(
                "[red]Refused:[/red] give the workflow id, the step, and keep, optional "
                "or drop: `varyz workflows review <id> logo optional`"
            )
            raise typer.Exit(1)
        result = _rules_call(lambda: _resolution_client(cfg).review_workflow(
            workflow_id,
            step=step,
            decision=str(decision),
            caller=_local_owner_caller(cfg, "owner_knowledge"),
            device_id=cfg.resolution.device_id,
            source="manual",
        ))
        if result.get("already_answered"):
            console.print(
                f"Already answered: {escape(str(result['decision']))}. Nothing changed."
            )
        else:
            console.print(
                f"[green]Kept your answer.[/green] {escape(str(result['decision']))}; "
                f"agents now follow workflow {result['workflow_id']}."
            )
        return
    conn = _db(cfg)
    try:
        rows = SQLiteNudgeStore(conn).counts()
    finally:
        conn.close()
    if not rows:
        console.print(
            "No step has been raised yet. Agents raise one when a task starts new work "
            "at a later step while an earlier one is open, and record your answer."
        )
        return
    table = Table(
        Column("workflow id", **ID_COLUMN), "workflow", "scope", "step",
        "took first", "skipped", "this week", "redone after skip", "review",
    )
    for row in rows:
        if row["optional"]:
            status = "optional"
        elif row["review_due"]:
            status = "[yellow]due[/yellow]"
        elif row["reviewed"]:
            status = "answered"
        else:
            status = ""
        table.add_row(
            str(row["workflow_id"]), escape(str(row["workflow"])),
            _scope_label(str(row["scope"])), escape(str(row["step"])),
            str(row["took"]), str(row["skipped"]), str(row["skips_this_week"]),
            str(row["rework"]), status,
        )
    console.print(table)
    if any(row["review_due"] for row in rows):
        console.print(
            f"A step is due for review after {REVIEW_AFTER_SKIPS} skips within "
            f"{REVIEW_WINDOW.days} days with no work redone after any skip of it; older "
            "skips expire and the step stays. Decide with `varyz workflows review "
            "<workflow id> <step> keep|optional|drop`; agents also ask you once."
        )


@workflows_app.command("learn")
def workflows_learn(
    apply: Annotated[
        bool, typer.Option("--apply", help="Hand the learned orders to agents now")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Find the order you usually guide agents in. Prints counts and step orders only."""
    from functools import partial

    from varyz.resolution.workflow_miner import learn_workflows, record_learned

    cfg = _cfg(config)
    conn = _db(cfg)
    try:
        learned, stats = learn_workflows(conn)
    finally:
        conn.close()
    counts = stats.as_dict()
    table = Table("measure", "count")
    table.add_row("turns read", str(counts["turns_read"]))
    table.add_row("sensitive turns skipped", str(counts["sensitive_skipped"]))
    for name, units in counts["units"].items():
        table.add_row(f"{name} sessions and projects", str(units))
    for level, votes in counts["votes"].items():
        table.add_row(f"order votes: {level.replace('_', ' ')}", str(votes))
    table.add_row("started too early, redone later", str(counts["corrections"]))
    table.add_row("workflows found", str(counts["workflows"]))
    written = unchanged = 0
    if apply and learned:
        client = _resolution_client(cfg)
        record = partial(
            client.add_workflow,
            caller=_local_owner_caller(cfg, "owner_knowledge"),
            device_id=cfg.resolution.device_id,
            basis="learned",
        )
        written, unchanged = _rules_call(lambda: record_learned(record, learned))
        table.add_row("handed to agents", str(written))
        table.add_row("unchanged, yours, or withdrawn", str(unchanged))
    console.print(table)
    for workflow in learned:
        provenance = workflow.provenance
        console.print(
            f"[bold]{escape(workflow.name)}[/bold]: "
            + escape(" → ".join(step.step for step in workflow.steps))
            + f"  [dim]({provenance['units']} {provenance['unit']}"
            + (f"; left out: {', '.join(provenance['dropped'])}" if provenance["dropped"] else "")
            + ")[/dim]"
        )
    if not apply:
        console.print("[dim]Dry run. Add --apply to hand these orders to agents.[/dim]")


@daemon_app.command("run", hidden=True)
def daemon_run(config: ConfigOpt = None) -> None:
    """Run the resolution daemon in this process."""
    from varyz.config import find_config_file
    from varyz.resolution.daemon import run_resolution_daemon

    cfg = _cfg(config)
    config_file = find_config_file(config)
    run_resolution_daemon(
        cfg.db_path,
        config=cfg,
        host=cfg.resolution.bind_host,
        port=cfg.resolution.port,
        auth_mode=cfg.resolution.auth_mode,
        device_id=cfg.resolution.device_id,
        watch_code_changes=True,
        config_path=config_file.resolve() if config_file is not None else None,
    )


@daemon_app.command("start")
def daemon_start(
    foreground: Annotated[
        bool, typer.Option("--foreground", help="Keep the daemon attached to this terminal")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Start the local owner-resolution daemon."""
    from varyz.install import resolve_config_path
    from varyz.resolution.client import ArchiveMismatchError, ResolutionClientError, ensure_daemon

    path = resolve_config_path(config)
    cfg = _cfg(path)
    client = _resolution_client(cfg)
    try:
        status = client.status()
    except ArchiveMismatchError:
        raise
    except ResolutionClientError:
        status = None
    if status is not None and status.running:
        console.print(f"Already running on {status.host}:{status.port}.")
        return
    if foreground:
        daemon_run(path)
        return
    status = ensure_daemon(client, path)
    if status is None:
        console.print("[red]Resolution daemon did not start.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Resolution daemon running.[/green] {status.host}:{status.port}")


@daemon_app.command("status")
def daemon_status(config: ConfigOpt = None) -> None:
    """Read daemon health through the resolution client."""
    from varyz.resolution.client import ArchiveMismatchError, ResolutionClientError

    try:
        status = _resolution_client(_cfg(config)).status()
    except ArchiveMismatchError as exc:
        console.print(f"not this archive's daemon · {escape(str(exc))}")
        raise typer.Exit(1) from exc
    except ResolutionClientError as exc:
        console.print(f"stopped · {escape(str(exc))}")
        raise typer.Exit(1) from exc
    console.print(f"running · {status.host}:{status.port} · auth {status.auth_mode}")
    console.print(f"pending questions: {status.pending_escalations}")
    from varyz.resolution.availability import directory, is_paused, startup_path

    cfg = _cfg(config)
    root = directory(cfg)
    registered = (
        sys.platform == "win32" and cfg.source_file is not None
        and startup_path(cfg.source_file).is_file() and (root / "ENABLED").exists()
    )
    console.print(f"login recovery: {'registered' if registered else 'not registered'}")
    console.print(f"recovery pause: {'ENGAGED' if is_paused(cfg) else 'not engaged'}")
    if (root / "status.json").is_file():
        console.print(f"latest health check: {root / 'status.json'}")


@daemon_app.command("stop")
def daemon_stop(config: ConfigOpt = None) -> None:
    """Stop the daemon through its authenticated local endpoint."""
    from varyz.resolution.client import ResolutionClientError

    client = _resolution_client(_cfg(config))
    try:
        client.stop()
    except ResolutionClientError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    # `/shutdown` acknowledges before the serving thread leaves. On Windows a
    # process whose current directory is inside an archive keeps that directory
    # undeletable, and a command that says "stopped" before then is observably
    # false. Wait on the public status boundary rather than guessing from time.
    for _ in range(50):
        try:
            client.status()
        except ResolutionClientError:
            console.print("[green]Resolution daemon stopped.[/green]")
            return
        time.sleep(0.1)
    console.print("[red]Resolution daemon accepted shutdown but is still running.[/red]")
    raise typer.Exit(1)


@daemon_app.command("check")
def daemon_check(config: ConfigOpt = None) -> None:
    """Check the local service once and recover it when unavailable."""
    from varyz.resolution.availability import check_once

    status = check_once(_config_path(config).resolve())
    console.print(f"Daemon: {status}")
    if status.startswith("failed:"):
        raise typer.Exit(1)


@daemon_app.command("install")
def daemon_install(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show startup; write nothing")] = False,
    config: ConfigOpt = None,
) -> None:
    """Start at Windows login and recover the local daemon every five seconds."""
    from varyz.resolution.availability import install

    try:
        target = install(_config_path(config).resolve(), dry_run=dry_run)
    except (ValueError, OSError) as exc:
        console.print(f"[red]Daemon startup failed:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    console.print(f"{'Would register' if dry_run else 'Registered'} login startup: {target}")
    if not dry_run:
        console.print(
            "Daemon health checked every five seconds; source sync and reading unchanged."
        )


@daemon_app.command("pause")
def daemon_pause(config: ConfigOpt = None) -> None:
    """Pause automatic recovery and stop the daemon gracefully."""
    from varyz.resolution.availability import directory, set_paused
    from varyz.resolution.client import ResolutionClientError

    cfg = _cfg(config)
    set_paused(cfg, True)
    with contextlib.suppress(ResolutionClientError):
        _resolution_client(cfg).stop()
    console.print(f"Daemon recovery paused. Sentinel: {directory(cfg) / 'PAUSED'}")


@daemon_app.command("resume")
def daemon_resume(config: ConfigOpt = None) -> None:
    """Resume automatic recovery and check the daemon now."""
    from varyz.resolution.availability import set_paused

    set_paused(_cfg(config), False)
    daemon_check(config)


@daemon_app.command("uninstall")
def daemon_uninstall(config: ConfigOpt = None) -> None:
    """Remove daemon login startup, pause recovery, and stop the service."""
    from varyz.resolution.availability import uninstall
    from varyz.resolution.client import ResolutionClientError

    path = _config_path(config).resolve()
    try:
        uninstall(path)
    except (ValueError, OSError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    with contextlib.suppress(ResolutionClientError):
        _resolution_client(_cfg(path)).stop()
    console.print("Daemon login startup removed; recovery paused; archive retained.")


@daemon_app.command("on-demand")
def daemon_on_demand(config: ConfigOpt = None) -> None:
    """Remove login recovery and allow first-request startup; start nothing now."""
    from varyz.resolution.availability import use_on_demand

    try:
        use_on_demand(_config_path(config).resolve())
    except (ValueError, OSError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    console.print("Daemon uses on-demand startup; login recovery removed; no process started.")


def _connect_agent_client(client: str, *, dry_run: bool, config: Path | None) -> None:
    from varyz.install import connect_agent

    try:
        credential, results = connect_agent(client, config_path=config, dry_run=dry_run)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    for result in results:
        console.print(f"[bold]{result.client}[/bold]: {escape(str(result.detail))}")
    if credential is not None:
        console.print(
            f"[green]Caller registered.[/green] {credential.caller.agent_id} · "
            f"purpose {credential.caller.purpose}"
        )
    if not dry_run and any(result.changed for result in results):
        console.print("Restart the client to pick up Varyz resolution.")


@connect_app.command("codex")
def connect_codex_agent(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show changes, write nothing")] = False,
    config: ConfigOpt = None,
) -> None:
    """Connect Codex to owner resolution with a named caller capability."""
    _connect_agent_client("codex", dry_run=dry_run, config=config)


@connect_app.command("cursor")
def connect_cursor_agent(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show changes, write nothing")] = False,
    config: ConfigOpt = None,
) -> None:
    """Connect Cursor to owner resolution with a named caller capability."""
    _connect_agent_client("cursor", dry_run=dry_run, config=config)


@connect_app.command("claude-code")
def connect_claude_code_agent(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show changes, write nothing")] = False,
    config: ConfigOpt = None,
) -> None:
    """Connect Claude Code to owner resolution with a named caller capability."""
    _connect_agent_client("claude-code", dry_run=dry_run, config=config)


@agent_app.command("list")
def agent_list(config: ConfigOpt = None) -> None:
    """List registered callers and their trust state."""
    conn = _db(_cfg(config))
    try:
        rows = conn.execute(
            """SELECT id, display_name, publisher, trust_state, created_at, revoked_at
               FROM agent_principals ORDER BY created_at, id"""
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        console.print("No agent callers are registered.")
        return
    table = Table("id", "name", "publisher", "state", "created")
    for row in rows:
        table.add_row(
            str(row["id"]),
            str(row["display_name"]),
            str(row["publisher"] or "-"),
            str(row["trust_state"]),
            str(row["created_at"]),
        )
    console.print(table)


@agent_app.command("register")
def agent_register(
    agent_id: Annotated[str, typer.Argument(help="Stable principal id")],
    display_name: Annotated[str, typer.Option("--name", help="Human-readable client name")],
    purpose: Annotated[str, typer.Option(help="Capability purpose")] = "owner_context",
    config: ConfigOpt = None,
) -> None:
    """Register a caller and print its one-time token."""
    from varyz.trust import register_agent

    cfg = _cfg(config)
    conn = _db(cfg)
    try:
        credential = register_agent(
            conn,
            agent_id=agent_id,
            display_name=display_name,
            purpose=purpose,
            device_id=cfg.resolution.device_id,
        )
    finally:
        conn.close()
    console.print(f"[green]Registered[/green] {credential.caller.agent_id}")
    console.print(f"token: {credential.token}")
    console.print("[dim]Store it now; Varyz keeps only its SHA-256 hash.[/dim]")


@agent_app.command("grant")
def agent_grant(
    agent_id: Annotated[str, typer.Argument(help="Registered principal id")],
    purpose: Annotated[str, typer.Option(help="Capability purpose")] = "owner_context",
    max_items: Annotated[
        int, typer.Option(min=0, help="Maximum records disclosed per request")
    ] = 8,
    max_chars: Annotated[
        int, typer.Option(min=0, help="Maximum disclosed characters per request")
    ] = 4_000,
    config: ConfigOpt = None,
) -> None:
    """Grant a bounded resolve capability to a registered caller."""
    from varyz.trust import grant_agent_capability

    conn = _db(_cfg(config))
    try:
        capability_id = grant_agent_capability(
            conn,
            agent_id,
            purpose=purpose,
            max_items=max_items,
            max_chars=max_chars,
        )
    finally:
        conn.close()
    console.print(f"[green]Capability active.[/green] {capability_id}")


@agent_app.command("token")
def agent_token(
    agent_id: Annotated[str, typer.Argument(help="Registered principal id")],
    label: Annotated[str, typer.Option(help="Credential label")] = "manual",
    config: ConfigOpt = None,
) -> None:
    """Mint and print a replacement one-time token."""
    from varyz.trust import mint_agent_token

    conn = _db(_cfg(config))
    try:
        token, token_id = mint_agent_token(conn, agent_id, label=label)
    finally:
        conn.close()
    console.print(f"token id: {token_id}")
    console.print(f"token: {token}")
    console.print("[dim]Store it now; Varyz keeps only its SHA-256 hash.[/dim]")


@agent_app.command("revoke")
def agent_revoke(
    agent_id: Annotated[str, typer.Argument(help="Registered principal id")],
    token_id: Annotated[str | None, typer.Option(help="Revoke only this token")] = None,
    capability_id: Annotated[str | None, typer.Option(help="Revoke only this capability")] = None,
    config: ConfigOpt = None,
) -> None:
    """Revoke a caller, one token, or one capability."""
    from varyz.trust import revoke_agent

    if token_id is not None and capability_id is not None:
        console.print("[red]Choose --token-id or --capability-id, not both.[/red]")
        raise typer.Exit(1)
    conn = _db(_cfg(config))
    try:
        changed = revoke_agent(conn, agent_id, token_id=token_id, capability_id=capability_id)
    finally:
        conn.close()
    if not changed:
        console.print("Nothing active matched that identity.")
        raise typer.Exit(1)
    console.print("[green]Revoked.[/green]")


@app.command()
def install(
    client: Annotated[
        str | None,
        typer.Option(help="Only configure: codex | claude-code | claude-desktop | cursor"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show changes, write nothing")] = False,
    citations: Annotated[
        str | None,
        typer.Option(help="How much evidence agents should show: minimal | full | off"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Register varyz as an MCP server with the agent clients on this machine."""
    from varyz.agent_style import STYLES
    from varyz.install import CODEX_NOT_FOUND, install_mcp, resolve_config_path

    if citations is not None:
        if citations not in STYLES:
            console.print(f"[red]--citations must be one of: {', '.join(STYLES)}[/red]")
            raise typer.Exit(1)
        from varyz.configfile import set_agent_citations

        try:
            set_agent_citations(resolve_config_path(config), citations)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from exc

    try:
        results = install_mcp(config_path=config, only=client, dry_run=dry_run)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    if not results:
        console.print("No supported MCP clients found on this machine.")
        console.print("Add this to your client's config by hand:")
        console.print(_mcp_snippet(config))
        raise typer.Exit(0)
    for result in results:
        console.print(f"[bold]{result.client}[/bold]: {escape(str(result.detail))}")
    if not dry_run and any(r.changed for r in results):
        console.print("Restart the client to pick up the new server.")
    # Asked for Codex alone, nothing was installed, so the run failed like any
    # other install that could not finish; across all clients it is one skip.
    if client is not None and any(r.detail == CODEX_NOT_FOUND for r in results):
        raise typer.Exit(1)


@app.command()
def uninstall(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show removals and retained state; write nothing")
    ] = False,
    client: Annotated[
        str | None,
        typer.Option(help="Only unregister: codex | claude-code | claude-desktop | cursor"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Remove Varyz integrations while deliberately retaining the local archive."""
    from varyz.autosync import (
        daemon_runner_path,
        enabled_path,
        startup_path,
        uninstall_daemon,
    )
    from varyz.config import find_config_file
    from varyz.setup_flow import unregister_mcp

    # Reading ./config.toml alone meant uninstall reported the autosync it
    # could not find rather than the one the named archive registered.
    cfg_path = (find_config_file(config) or Path.cwd() / "config.toml").resolve()
    cfg = _cfg(cfg_path) if cfg_path.exists() else None
    try:
        # Scoped to this archive. Without that, `varyz uninstall` typed in any
        # directory on the machine removed whatever varyz registration it
        # found -- including the one belonging to the archive the reader uses
        # every day, which they had not named and were not asked about.
        results = unregister_mcp(dry_run=dry_run, only=client, archive=cfg_path)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    for result in results:
        console.print(f"[bold]{result.client}[/bold]: {escape(str(result.detail))}")
    if not results:
        console.print("No supported MCP client config was found.")
    if cfg is not None:
        from varyz.install import uninstall_agent_hooks

        for result in uninstall_agent_hooks(cfg_path, only=client, dry_run=dry_run):
            detail = escape(str(result.detail))
            console.print(f"[bold]{result.client}[/bold]: {detail} ({result.path})")

    if cfg is not None:
        if dry_run:
            # On an install that never enabled autosync none of these three
            # files exists, and naming them as removals told the owner they had
            # a login registration they had never made.
            targets = (
                ("startup", startup_path(cfg_path)),
                ("runner", daemon_runner_path(cfg)),
                ("enable marker", enabled_path(cfg)),
            )
            present = [(label, path) for label, path in targets if path.exists()]
            for label, path in present:
                console.print(f"Would remove autosync {label}: {path}")
            if not present:
                console.print("Autosync is not installed here; nothing to remove.")
        else:
            # uninstall_daemon answers "nothing installed" for an archive that
            # never had it, and the old prefix contradicted that in its own line.
            console.print(f"Autosync: {uninstall_daemon(cfg_path)}")
        if client is None and sys.platform == "win32":
            from varyz.resolution import availability
            from varyz.resolution.client import ResolutionClientError

            recovery = availability.startup_path(cfg_path)
            enabled = availability.directory(cfg) / "ENABLED"
            if recovery.exists() or enabled.exists():
                if dry_run:
                    console.print(f"Would remove daemon recovery startup: {recovery}")
                else:
                    availability.uninstall(cfg_path)
                    with contextlib.suppress(ResolutionClientError):
                        _resolution_client(cfg).stop()
                    console.print("Daemon recovery startup removed and service paused.")
    else:
        console.print(
            f"[yellow]Config not found; autosync could not be resolved: {cfg_path}[/yellow]"
        )

    action = "would remain" if dry_run else "remains"
    console.print(f"\nLocal state {action} deliberately:")
    console.print(f"  config   {cfg_path}")
    if cfg is not None:
        console.print(f"  archive  {cfg.data_dir}")
        console.print("  The archive includes originals, database, indexes, models, and run logs.")
        console.print(
            "  legacy Telegram sessions, if created: "
            f"{cfg.data_dir / 'sessions' / 'telegram_<phone>.session'}"
        )
    console.print(
        "  legacy Telegram API credentials, if created: OS credential store service "
        "'varyz', names 'telegram_api_id' and 'telegram_api_hash'"
    )
    console.print("  Varyz does not delete either legacy item; review and remove them yourself.")
    console.print(
        "  [dim]The credentials: varyz secrets forget telegram_api_id"
        " (and telegram_api_hash).[/dim]"
    )
    backups = {
        backup
        for result in results
        for backup in (
            result.path.with_suffix(result.path.suffix + ".varyz-backup"),
            result.path.with_suffix(result.path.suffix + ".varyz-uninstall-backup"),
        )
        if backup.exists()
    }
    for backup in sorted(backups):
        console.print(f"  client backup  {backup}")
    if not backups:
        console.print("  client backups  none found")
    # varyzai on the wire, varyz everywhere else -- and `uv tool uninstall`
    # takes the distribution, so the obvious command is the one that fails.
    console.print(
        "After this command, remove the executable with: [bold]uv tool uninstall varyzai[/bold]"
    )
    console.print("Delete the archive only after reviewing the exact path printed above.")


@model_app.command("status")
def model_status(config: ConfigOpt = None) -> None:
    """Show the active/staged personal-model generations and compiler checkpoints."""
    import json

    from varyz.llm.local_runtime import compile_command
    from varyz.personal_model.compiler import CompilerStage
    from varyz.personal_model.repository import PersonalModelRepository

    conn = _db(_cfg(config))
    repository = PersonalModelRepository(conn)
    active = repository.active_generation()
    console.print(
        f"active generation: [bold]{active.id}[/bold]" if active else "active generation: none"
    )
    rows = conn.execute(
        """SELECT generation.id,generation.state,generation.record_count,
                  generation.reused_record_count,generation.quarantine_count,
                  generation.created_at,generation.activated_at,generation.failure_reason,
                  progress.state AS observation_state,progress.episodes_annotated,
                  progress.episodes_total,progress.episodes_remaining,
                  progress.entries_reached,progress.budget_exhausted_by
             FROM personal_model_generations AS generation
             LEFT JOIN personal_model_observation_progress AS progress
               ON progress.generation_id=generation.id
             ORDER BY generation.created_at DESC,generation.id DESC LIMIT 10"""
    ).fetchall()
    if not rows:
        console.print("No personal-model generation has been compiled.")
        return
    table = Table("generation", "state", "records", "reused", "quarantine", "created")
    for row in rows:
        state = "partial" if row["observation_state"] == "partial" else row["state"]
        table.add_row(
            row["id"],
            state,
            str(row["record_count"]),
            str(row["reused_record_count"]),
            str(row["quarantine_count"]),
            local_stamp(row["created_at"]),
        )
    console.print(table)
    partial = rows[0] if rows[0]["observation_state"] == "partial" else None
    if partial is not None:
        remaining = partial["episodes_remaining"]
        estimated_minutes = remaining / 4.5
        if estimated_minutes >= 24 * 60:
            estimate = f"{estimated_minutes / (24 * 60):.1f} days"
        elif estimated_minutes >= 60:
            estimate = f"{estimated_minutes / 60:.1f} hours"
        else:
            minute_count = max(1, round(estimated_minutes))
            suffix = "minute" if minute_count == 1 else "minutes"
            estimate = f"{minute_count} {suffix}"
        console.print(
            f"in-progress observations: {partial['episodes_annotated']:,}/"
            f"{partial['episodes_total']:,} annotated; {remaining:,} remaining; "
            f"{partial['entries_reached']:,} entries reached"
        )
        console.print(
            f"  estimated remaining inference at measured 4.5 calls/min: {estimate} "
            f"({remaining:,} annotation calls)"
        )
    latest = rows[0]["id"]
    # Whose checkpoints these are has to be said. The block below is always the
    # newest generation's, and the newest generation is often not the live one:
    # after a failed re-run the header reads "active generation: X" and the last
    # line reads "activate: failed", and nothing in between says they are
    # different generations. That is a working model reported as a broken one.
    if active is not None and latest != active.id:
        console.print(
            f"[yellow]checkpoints below are the newest generation {latest}, "
            f"which is {rows[0]['state']} and not the live one. The active model "
            f"is {active.id}, and nothing here describes it.[/yellow]"
        )
    else:
        console.print(f"checkpoints for generation {latest}:")
    checkpoints = {
        str(row["stage"]): row
        for row in conn.execute(
            """SELECT stage,state,detail_json FROM personal_model_compiler_checkpoints
                 WHERE generation_id=?""",
            (latest,),
        ).fetchall()
    }
    # A stage the compile never reached writes no checkpoint row at all, so
    # listing only what was recorded makes a run that died at ``memory`` look
    # like a short run rather than an unfinished one.  Name every stage.
    # The activate checkpoint is written before the pointer swap, because a
    # generation becomes immutable the instant it activates and the row could
    # not be written afterwards.  So "activate: complete" means the compiler
    # reached the switch, not that the switch landed: a run killed in between
    # leaves that line above a generation still sitting at ``staged``.  The
    # generation row is the authority on whether anything is live, and reading
    # the two together is the only reading that is true of both.
    for stage in CompilerStage:
        recorded = checkpoints.get(str(stage))
        if recorded is None:
            console.print(f"  {stage}: [yellow]not reached[/yellow]")
            continue
        console.print(f"  {stage}: {recorded['state']} · {_checkpoint_detail(recorded)}")
        if (
            str(stage) == str(CompilerStage.ACTIVATE)
            and recorded["state"] == "complete"
            and rows[0]["state"] not in {"current", "superseded"}
        ):
            console.print(
                "    [yellow]but this generation is "
                f"{rows[0]['state']}, so the switch never happened. The checkpoint is "
                "written before the pointer swap; the run ended in between. Re-run "
                f"{compile_command()} -- it replays from cache.[/yellow]"
            )
    stale_row = conn.execute(
        """SELECT metric_json FROM personal_model_run_metrics
             WHERE generation_id=? AND stage='compiler' AND metric_name='stale_stages'""",
        (latest,),
    ).fetchone()
    stale = json.loads(stale_row["metric_json"]) if stale_row is not None else []
    if stale:
        # A generation may activate with these stages stale, so "active" and
        # "reads only what the archive says literally" are both true at once.
        # The owner sees this table and not the compiler's return value.
        console.print(
            f"[yellow]stale: {', '.join(str(item) for item in stale)}. "
            "These stages ran without the semantic model, so this generation "
            "carries no observations or interpretations of its own. Recompile "
            "with the model reachable to fill them in.[/yellow]"
        )
    synthesis = checkpoints.get(str(CompilerStage.SYNTHESIS))
    if synthesis is not None:
        # A synthesis that runs and proposes nothing is not stale and is not a
        # failure: every window answered, and none of the answers survived.  The
        # generation activates, and what it activates is a model with no
        # interpretation in it.  The count is in the detail above and reads as
        # one number among several, which is not how it should have to be found.
        detail = json.loads(synthesis["detail_json"] or "{}")
        if isinstance(detail, dict) and detail.get("interpretations") == 0:
            console.print(
                "[yellow]synthesis produced no interpretations, so this generation "
                "carries facts without a reading of them: interpret has nothing "
                "to offer until a recompile proposes some.[/yellow]"
            )
    if rows[0]["failure_reason"]:
        console.print(f"[red]failure: {escape(str(rows[0]['failure_reason']))}[/red]")
    _print_generation_footprint(conn)


def _print_generation_footprint(conn: sqlite3.Connection) -> None:
    """What the kept generations cost, and which of them can be given back.

    Every compile writes a full generation and keeps the ones before it so a
    compile can be undone. That is worth the disk, but nothing said what the
    disk was: this archive reached 67 GB across ten generations without a line
    anywhere naming the number or the command that recovers part of it.
    """
    from varyz.personal_model.repository import GenerationState

    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    generations = conn.execute(
        "SELECT id,state,record_count FROM personal_model_generations"
    ).fetchall()
    if not generations:
        return
    records = sum(int(row["record_count"] or 0) for row in generations)
    console.print(
        f"[dim]archive file: {page_count * page_size / 2**30:.2f} GB · "
        f"{len(generations)} generation(s) holding {records:,} records[/dim]"
    )
    # Split exactly the way `varyz model prune` splits them, so the two never
    # disagree: a failure goes with the plain command, a staging behind a newer
    # one only with --include-staged, and a superseded generation is rollback
    # history that neither of them touches.
    newest_staged = max(
        (row["id"] for row in generations if row["state"] == str(GenerationState.STAGED)),
        default=None,
    )
    failed = [row for row in generations if row["state"] == str(GenerationState.FAILED)]
    stale_staged = [
        row
        for row in generations
        if row["state"] == str(GenerationState.STAGED) and row["id"] != newest_staged
    ]
    if failed:
        records = sum(int(row["record_count"] or 0) for row in failed)
        console.print(
            f"[dim]{len(failed)} failed ({records:,} records) can be given back: "
            "varyz model prune[/dim]"
        )
    if stale_staged:
        records = sum(int(row["record_count"] or 0) for row in stale_staged)
        console.print(
            f"[dim]{len(stale_staged)} superseded staging attempt(s) ({records:,} records) "
            "with it: varyz model prune --include-staged[/dim]"
        )


def _checkpoint_detail(recorded) -> str:
    """One checkpoint's detail, as a line rather than as its storage format.

    The rows are stored as JSON and were printed as JSON. Most stages record a
    handful of counts and read fine that way, but `readiness` records twelve
    gates each with a measured value, a threshold and a verdict, and it filled
    the terminal with seven hundred characters of braces every time anyone ran
    `varyz model status`. Zeroes are kept: "people=0" is the answer to what
    identity resolution found, and dropping it would leave the stage looking
    like it recorded nothing.
    """
    import json

    try:
        detail = json.loads(recorded["detail_json"] or "{}")
    except (TypeError, ValueError):
        return str(recorded["detail_json"])
    if not isinstance(detail, dict) or not detail:
        return "no detail recorded"
    return ", ".join(f"{key}={_detail_value(value)}" for key, value in sorted(detail.items()))


def _detail_value(value) -> str:
    """A checkpoint value, flattened one level and no further."""
    if not isinstance(value, dict):
        return "none" if value is None else str(value)
    if not value:
        return "none"
    verdicts: dict[str, int] = {}
    for nested in value.values():
        if isinstance(nested, dict) and "verdict" in nested:
            name = str(nested["verdict"])
            verdicts[name] = verdicts.get(name, 0) + 1
    if verdicts:
        return "; ".join(f"{count} {name}" for name, count in sorted(verdicts.items()))
    if all(isinstance(nested, int) for nested in value.values()):
        return "; ".join(f"{key} {count}" for key, count in sorted(value.items()))
    return f"{len(value)} entries"


@model_app.command("compile")
def model_compile(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--run", help="Inspect inputs only, or run the compiler"),
    ] = True,
    semantic: Annotated[
        bool,
        typer.Option(
            "--semantic/--literal-only",
            help="Use the managed-local semantic model, or compile explicit claims only",
        ),
    ] = True,
    frontier: Annotated[
        bool,
        typer.Option(
            "--frontier",
            help=(
                "Explicitly allow public/personal observation episodes to use the "
                "configured cloud model; sensitive/unclassified episodes stay local"
            ),
        ),
    ] = False,
    frontier_estimated_spend_per_call: Annotated[
        float | None,
        typer.Option(
            "--frontier-estimated-spend-per-call",
            min=0,
            help="Estimated USD per frontier call; required for API-key authentication",
        ),
    ] = None,
    frontier_spend_cap: Annotated[
        float | None,
        typer.Option(
            "--frontier-spend-cap",
            min=0,
            help="Maximum estimated USD for this run (defaults to the configured monthly cap)",
        ),
    ] = None,
    max_annotation_calls: Annotated[
        int | None,
        typer.Option(
            "--max-annotation-calls",
            min=0,
            help="Maximum new episode annotations; cache hits are free",
        ),
    ] = None,
    cached_observations_only: Annotated[
        bool,
        typer.Option(
            "--cached-observations-only",
            help=(
                "Reuse completed semantic observations without new model calls; "
                "record missing episodes as coverage gaps"
            ),
        ),
    ] = False,
    observation_deadline: Annotated[
        str | None,
        typer.Option(
            "--observation-deadline",
            help="Stop before starting an annotation at or after this ISO-8601 wall-clock time",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the full compile result as JSON instead of a summary"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Inspect readiness or run the local/privacy-gated personal-model compiler."""
    if frontier and not semantic:
        raise typer.BadParameter(
            "--frontier requires --semantic",
            param_hint="--frontier",
        )
    if cached_observations_only and not semantic:
        raise typer.BadParameter(
            "--cached-observations-only requires --semantic",
            param_hint="--cached-observations-only",
        )
    parsed_deadline = None
    if observation_deadline is not None:
        try:
            parsed_deadline = datetime.fromisoformat(observation_deadline.replace("Z", "+00:00"))
        except ValueError:
            raise typer.BadParameter(
                "must be an ISO-8601 timestamp",
                param_hint="--observation-deadline",
            ) from None
        if parsed_deadline.tzinfo is None or parsed_deadline.utcoffset() is None:
            raise typer.BadParameter(
                "must include a UTC offset or Z",
                param_hint="--observation-deadline",
            )
    cfg = _cfg(config)
    conn = _db(cfg)
    graph = conn.execute(
        """SELECT id,input_content_hash,completed_at FROM graph_runs
             WHERE state='active' ORDER BY activated_at DESC LIMIT 1"""
    ).fetchone()
    excluded = tuple(sorted(set(cfg.graph.excluded_sources)))
    placeholders = ",".join("?" for _ in excluded)
    where = f" WHERE lower(source) NOT IN ({placeholders})" if excluded else ""
    entry_count = conn.execute(
        f"SELECT COUNT(*) FROM entries{where}",  # noqa: S608 - placeholders only
        excluded,
    ).fetchone()[0]
    if graph is None:
        console.print("[red]No active graph. Run: varyz graph rebuild[/red]")
        raise typer.Exit(1)
    console.print(
        f"compile input: graph {graph['id']} · {entry_count} active entries · "
        f"hash {graph['input_content_hash']}"
    )
    from varyz.llm.local_runtime import installer_is_shipped
    from varyz.personal_model.compiler import CompileRequest, build_personal_model_compiler
    from varyz.personal_model.semantic_runtime import (
        LocalModelUnavailable,
        build_local_semantic_compiler,
    )

    # "Start it and retry" named a model the alpha never installs.
    literal_only = (
        "This build of varyz does not install the managed model; pass --literal-only "
        "to compile without a semantic layer."
    )

    if dry_run:
        try:
            # A preview must validate the same immutable graph projection as a
            # real run. Previously it said "reachable" and "ready" for a stale
            # graph that the compiler rejected one minute later, after the
            # unattended harness had already made a large safety backup.
            build_personal_model_compiler(conn, cfg).preflight()
        except (LookupError, RuntimeError, ValueError) as exc:
            console.print(f"[red]Personal-model compile preflight failed: {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None

    if frontier:
        from varyz.llm.providers import NoCredentialsError
        from varyz.personal_model.cloud_runtime import build_hybrid_semantic_compiler

        try:
            hybrid = build_hybrid_semantic_compiler(
                conn,
                cfg,
                estimated_spend_usd_per_call=frontier_estimated_spend_per_call,
                spend_cap_usd=frontier_spend_cap,
            )
        except LocalModelUnavailable as exc:
            console.print(
                f"[red]Managed-local semantic runtime unavailable: {escape(str(exc))}[/red]"
            )
            console.print(
                "The frontier path still needs the local model for sensitive episodes and "
                "downstream reasoning. Start it and retry."
                if installer_is_shipped()
                else literal_only
            )
            raise typer.Exit(1) from None
        except NoCredentialsError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        except ValueError as exc:
            console.print(f"[red]Frontier semantic runtime unavailable: {escape(str(exc))}[/red]")
            console.print(
                "OAuth subscriptions need no per-call estimate; API-key runs must pass "
                "--frontier-estimated-spend-per-call."
            )
            raise typer.Exit(1) from None
        compiler = hybrid.compiler
        selected_cap = (
            cfg.llm.monthly_spend_cap_usd if frontier_spend_cap is None else frontier_spend_cap
        )
        console.print(
            f"Frontier observations: [bold]{hybrid.frontier_model_name}[/bold] via "
            f"{hybrid.destination}; sensitive/unclassified episodes and all downstream "
            f"reasoning: [bold]{hybrid.local_model_name}[/bold]."
        )
        # Both sentences described a run that had not started yet, and were
        # printed by the run itself: on a --run the second one offered to
        # authorize egress that was already under way.
        egress = (
            "A --run explicitly authorizes broker-approved public/personal "
            "episode text to leave this machine."
            if dry_run
            else "Broker-approved public/personal episode text leaves this machine on this run."
        )
        console.print(f"Estimated frontier spend cap: ${selected_cap:.2f}. {egress}")
    elif semantic:
        try:
            composition = build_local_semantic_compiler(conn, cfg)
        except LocalModelUnavailable as exc:
            console.print(
                f"[red]Managed-local semantic runtime unavailable: {escape(str(exc))}[/red]"
            )
            console.print(
                "Start the configured local model and retry, or pass --literal-only "
                "to compile without a semantic layer."
                if installer_is_shipped()
                else literal_only
            )
            raise typer.Exit(1) from None
        compiler = composition.compiler
        mode = (
            "A --run will compile in semantic mode." if dry_run else "Compiling in semantic mode."
        )
        console.print(
            f"Local semantic model reachable: [bold]{composition.model_name}[/bold]. {mode}"
        )
    else:
        compiler = build_personal_model_compiler(conn, cfg)
        console.print(
            "Literal-only mode: the local model will not be contacted and the result "
            "will contain no semantic layer."
        )
    if dry_run:
        call_limit = "unbounded" if max_annotation_calls is None else f"{max_annotation_calls:,}"
        deadline = parsed_deadline.isoformat() if parsed_deadline else "none"
        console.print(
            f"Observation budget: {call_limit} new annotation calls; deadline {deadline}."
        )
        if cached_observations_only:
            console.print(
                "Cache-only observations: missing episodes become explicit coverage gaps; "
                "no new observation calls will be made."
            )
        console.print("Dry run: no generation or checkpoint was written.")
        return

    try:
        result = compiler.compile(
            CompileRequest(
                max_annotation_calls=max_annotation_calls,
                observation_deadline=parsed_deadline,
                cached_observations_only=cached_observations_only,
            )
        )
    except (LookupError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Personal-model compile could not start: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    if as_json:
        console.print_json(result.model_dump_json())
    else:
        _report_compile(result)
    if result.status == "partial":
        raise typer.Exit(3)
    if result.failed or not result.activated:
        raise typer.Exit(1)


def _report_compile(result) -> None:
    """What a compile did, in the words of the person who ran it.

    The compile is the longest-running thing Varyz does -- hours on a full
    archive -- and it ended by dumping two hundred lines of the result model as
    JSON. Twelve readiness gates, each with a numerator, a threshold and a
    failure sentence, scrolled past whether or not any of them had failed, and
    the two facts a person actually waited for (did it finish, is it the model
    my questions now run against) were somewhere in the middle of it. The JSON
    is still there behind --json for anything that parses it.
    """
    from varyz.trust import count_noun

    if result.failed or result.error:
        console.print(f"[red]Compile failed: {escape(result.error or 'no reason recorded')}[/red]")
        console.print(f"[dim]generation {result.generation_id}[/dim]")
        return
    verb = "Compiled and activated" if result.activated else "Compiled, not activated"
    if result.status == "partial":
        verb = "Compiled as far as its budget allowed"
    console.print(f"{verb}: generation {result.generation_id}")
    if result.record_counts:
        kinds = ", ".join(
            count_noun(count, kind.replace("_", " "))
            for kind, count in sorted(result.record_counts.items())
        )
        console.print(f"  {kinds}")
    if result.reused_record_count:
        console.print(
            f"[dim]  {result.reused_record_count:,} reused from the last generation[/dim]"
        )
    if result.quarantine_count:
        quarantined = count_noun(result.quarantine_count, "quarantined record")
        console.print(f"[yellow]  {escape(str(quarantined))}[/yellow]")

    progress = result.observation_progress
    if progress is not None and progress.episodes_total:
        console.print(
            f"  {progress.episodes_annotated:,}/{progress.episodes_total:,} episodes annotated"
            f" ({progress.cache_hits:,} from cache)"
        )
        if progress.budget_exhausted_by:
            console.print(
                f"[yellow]  Stopped early: {progress.budget_exhausted_by.replace('_', ' ')}. "
                "Re-run to continue from here.[/yellow]"
            )

    readiness = result.readiness
    if readiness is not None:
        counts = readiness.verdict_counts
        console.print(
            f"  readiness: {counts.get('ready', 0)} ready, "
            f"{counts.get('limited', 0)} limited, {counts.get('unavailable', 0)} unavailable"
        )
        held_back = [gate for gate in readiness.gates if gate.verdict == "limited"]
        for gate in held_back[:3]:
            name = gate.gate.replace("_", " ")
            console.print(f"[yellow]  · {name}: {escape(gate.failure)}[/yellow]")
        if len(held_back) > 3:
            console.print(f"[dim]  · and {len(held_back) - 3} more; see --json[/dim]")
    if result.stale_stages:
        console.print(f"[dim]  stages recomputed: {', '.join(result.stale_stages)}[/dim]")
    if result.activated:
        console.print(
            "[dim]Your questions now run against this generation: varyz model status[/dim]"
        )


@model_app.command("prune")
def model_prune(
    include_staged: Annotated[
        bool,
        typer.Option(
            "--include-staged",
            help="Also discard staged generations older than the newest one.",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually discard. Without it this only reports."),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Discard failed compile generations, whose rows nothing can ever read again."""
    from varyz.personal_model.repository import GenerationState, PersonalModelRepository

    conn = _db(_cfg(config))
    repository = PersonalModelRepository(conn)
    rows = conn.execute(
        """SELECT id,state,created_at,failure_reason,
                  (SELECT COUNT(*) FROM personal_model_records
                    WHERE generation_id=personal_model_generations.id) AS records
             FROM personal_model_generations ORDER BY created_at,id"""
    ).fetchall()
    # The newest staged generation is the one a resumed compile picks up, and a
    # failed one is finished whatever its age, so age only decides among staged.
    newest_staged = max(
        (row["id"] for row in rows if row["state"] == str(GenerationState.STAGED)),
        default=None,
    )
    doomed = [
        row
        for row in rows
        if row["state"] == str(GenerationState.FAILED)
        or (
            include_staged
            and row["state"] == str(GenerationState.STAGED)
            and row["id"] != newest_staged
        )
    ]
    # Three superseded generations on a real archive held more rows than
    # everything this command can discard, and nothing said so: the database
    # had grown by gigabytes and prune reported nothing to do.
    superseded = [row for row in rows if row["state"] == str(GenerationState.SUPERSEDED)]

    def _say_what_is_kept() -> None:
        if not superseded:
            return
        held = sum(int(row["records"]) for row in superseded)
        # Not "superseded generation(s)": `model status` already uses that
        # phrase for the staging attempts behind the newest one, which are the
        # opposite case -- those this command can discard.
        console.print(
            f"[dim]{len(superseded)} earlier activated generation(s) hold {held:,} records. "
            "Those are kept on purpose: a later compile reuses their records instead of "
            "recomputing them.[/dim]"
        )

    if not doomed:
        console.print("Nothing to prune: every generation is active history or in progress.")
        _say_what_is_kept()
        return
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    before = conn.execute("PRAGMA page_count").fetchone()[0]
    table = Table("generation", "state", "records", "created", "why")
    for row in doomed:
        table.add_row(
            row["id"],
            row["state"],
            f"{row['records']:,}",
            local_stamp(row["created_at"]),
            (row["failure_reason"] or "superseded by a later staging")[:60],
        )
    console.print(table)
    total = sum(int(row["records"]) for row in doomed)
    if not apply:
        console.print(
            f"{len(doomed)} generation(s), {total:,} records. Re-run with --apply to discard them."
        )
        _say_what_is_kept()
        return
    discarded = 0
    for row in doomed:
        discarded += repository.discard_generation(row["id"])
        # Checkpointed between generations rather than at the end: one of these
        # deletes cascades to millions of rows, and letting every generation's
        # pages pile up in the write-ahead log needs the disk space the prune
        # is being run to recover.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = conn.execute("PRAGMA page_count").fetchone()[0]
    freed = (before - after) * page_size
    reusable = conn.execute("PRAGMA freelist_count").fetchone()[0] * page_size
    console.print(
        f"Discarded {len(doomed)} generation(s) and {discarded:,} records. "
        f"The file itself gave back {freed / 2**30:.2f} GB; "
        f"{reusable / 2**30:.2f} GB inside it is now free for the next compile "
        "to write into rather than growing."
    )


@model_app.command("lookup")
def model_lookup(
    text: Annotated[
        str | None,
        typer.Argument(help="Deterministic life lookup, for example: What does Anna like?"),
    ] = None,
    query_json: Annotated[
        str | None,
        typer.Option("--query-json", help="Strict LifeQuery JSON instead of text"),
    ] = None,
    include_sensitive: Annotated[
        bool,
        typer.Option("--include-sensitive", help="Include local sensitive/unclassified records"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the full typed result as JSON instead of an answer"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Query stored facts and interpretations without synthesizing new ones."""
    import json

    from pydantic import ValidationError

    from varyz.personal_model.query import (
        LifeModelQueryService,
        LifeQuery,
        TrustedLocalSurface,
        issue_trusted_local_sensitivity_capability,
    )

    if (text is None) == (query_json is None):
        console.print("[red]Provide exactly one of text or --query-json.[/red]")
        raise typer.Exit(2)
    conn = _db(_cfg(config))
    service = LifeModelQueryService(conn)
    capability = (
        issue_trusted_local_sensitivity_capability(TrustedLocalSurface.CLI)
        if include_sensitive
        else None
    )
    try:
        if query_json is not None:
            query = LifeQuery.model_validate_json(query_json, strict=True)
            result = service.query(query, sensitivity_capability=capability)
        else:
            result = service.ask_text(text or "", sensitivity_capability=capability)
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]Invalid life query: {escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    if as_json or query_json is not None:
        console.print_json(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
        return
    _report_lookup(result)


def _report_lookup(result) -> None:
    """A question asked in words, answered in words.

    `varyz model lookup "What does Anna like?"` printed the typed result model:
    forty lines whose entire content, for an archive that has not compiled a
    semantic layer yet, was `"not_known": true`. The typed form is what agents
    read over MCP and what `--query-json` asks for, so it stays behind --json.
    """
    if result.disposition == "clarification" and result.clarification:
        console.print(result.clarification)
        return
    if result.disposition == "ambiguous":
        console.print("More than one person or project matches that name:")
        for choice in result.ambiguity_choices:
            console.print(f"  · {choice.display_name} ({choice.entity_kind} {choice.entity_id})")
        console.print("[dim]Ask again using the name as it appears above.[/dim]")
        return
    for answer in result.answers:
        when = (answer.observed_at or answer.stated_at or answer.valid_from) or ""
        date = f" · {local_day(when)}" if when else ""
        outdated = " · possibly outdated" if answer.possibly_outdated else ""
        preference = (
            f"{answer.scope_label or 'general'} · {answer.preference_valence}: "
            if answer.record_kind == "preference" and answer.preference_valence is not None
            else ""
        )
        console.print(
            f"{answer.predicate_or_concept or answer.record_kind}: "
            f"{preference}{answer.display_value}"
            f" [dim]({answer.basis}, confidence {answer.confidence:.2f}{date}{outdated})[/dim]"
        )
    if not result.answers:
        console.print("Nothing stored answers that.")
    if result.withheld_records:
        console.print(
            f"[yellow]{len(result.withheld_records)} matching record(s) were withheld as "
            "sensitive. Add --include-sensitive to read them here.[/yellow]"
        )
    elif result.sensitive_records_withheld:
        # This path carries the fact and not the count, and `len(...) or 1`
        # turned "some" into "1": a live archive answered "1 matching record(s)
        # were withheld" without having counted anything.
        console.print(
            "[yellow]Sensitive material was withheld from this answer -- a matching "
            "record, or your own name. Add --include-sensitive to include it.[/yellow]"
        )
    from varyz.llm.local_runtime import compile_command

    if result.uncompiled_evidence_candidates:
        candidates = len(result.uncompiled_evidence_candidates)
        console.print(
            f"[dim]{candidates} entries in the archive look relevant but are not compiled "
            f"into the model yet: {compile_command()}[/dim]"
        )
    if result.generation_id is None:
        console.print(
            f"[dim]No personal-model generation is active: {compile_command()}[/dim]"
        )
    else:
        # Which generation answered. Two compiles a week apart give different
        # answers to the same question, and an answer that does not say which
        # one it came from cannot be checked against anything.
        console.print(f"[dim]from generation {result.generation_id}[/dim]")


@model_app.command("interpret")
def model_interpret(
    question_id: Annotated[str, typer.Argument(help="Question ID Varyz offered locally")],
    config: ConfigOpt = None,
) -> None:
    """Read one active, validated interpretation already offered by Varyz."""
    import json

    from varyz.mcp_server import VaryzServer
    from varyz.personal_model.query import TrustedLocalSurfaceGate

    server = VaryzServer()
    cfg = _cfg(config)
    server._cfg = cfg
    try:
        # The reader is the owner, at their own terminal, on the machine the
        # archive is on. There is no third party to minimize the record for --
        # only the agent boundary has one, and this is not it.
        result = server._stored_interpretation(
            {"question_id": question_id}, gate=TrustedLocalSurfaceGate()
        )
    except ValueError as exc:
        # The argument checks behind this are an agent's, and an agent reads a
        # raised ValueError as a tool error. Here it reached a person as a
        # rendered traceback, for `varyz model interpret "$ID"` with the
        # variable unset.
        console.print(f"[red]{escape(str(exc))}.[/red]")
        raise typer.Exit(2) from None
    finally:
        server.close()
    if isinstance(result, dict) and result.get("error"):
        # A question ID that was never offered came back as a JSON object with
        # an "error" key and exit 0: a failure reported as a success, in the
        # storage format, to a person who mistyped an ID.
        console.print(f"[red]{escape(str(result['error']).replace('_', ' '))}.[/red]")
        if result.get("hint"):
            console.print(f"[dim]{escape(str(result['hint']))}[/dim]")
        raise typer.Exit(1)
    console.print_json(json.dumps(result, ensure_ascii=False))


@model_app.command("correct")
def model_correct(
    target_kind: Annotated[
        str,
        typer.Argument(help="person | project | fact | preference | goal | interpretation"),
    ],
    target_record_id: Annotated[str, typer.Argument(help="Stable active record ID")],
    action: Annotated[str, typer.Argument(help="Typed action valid for the target kind")],
    payload_json: Annotated[
        str,
        typer.Option("--payload", help="Strict JSON payload required by this action"),
    ] = "{}",
    struck_value_json: Annotated[
        str | None,
        typer.Option("--struck-value", help="Optional exact prior value as JSON"),
    ] = None,
    supersedes: Annotated[
        str | None,
        typer.Option("--supersedes", help="Prior override ID this correction replaces"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Append an owner correction; imported evidence and active snapshots stay immutable."""
    import json

    from varyz.llm.local_runtime import installer_is_shipped
    from varyz.personal_model.compiler import build_correction_application_service
    from varyz.personal_model.repository import PersonalModelRepository
    from varyz.personal_model.semantic_runtime import LocalModelUnavailable

    cfg = _cfg(config)
    conn = _db(cfg)
    try:
        payload = json.loads(payload_json)
        struck = json.loads(struck_value_json) if struck_value_json is not None else None
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        repository = PersonalModelRepository(conn)
        active_id = repository.active_generation_id()
        if active_id is None:
            raise ValueError("no active personal-model generation")
        active_record = repository.get_record(target_record_id, generation_id=active_id)
        actual_kind = type(active_record).__name__.casefold()
        if actual_kind != target_kind.casefold():
            raise ValueError(
                f"target {target_record_id} is {actual_kind}, not {target_kind.casefold()}"
            )
        # A correction recompiles the model. Without a config the service
        # recompiles the explicit claims only, which is all a build without
        # the managed model can do; with one it demanded that model and every
        # correction in the alpha was refused.
        semantic_cfg = cfg if installer_is_shipped() else None
        application = build_correction_application_service(conn, semantic_cfg).apply(
            target_kind=target_kind,
            target_record_id=target_record_id,
            action=action,
            payload=payload,
            struck_value=struck,
            supersedes_override_id=supersedes,
            actor="owner",
        )
    except LocalModelUnavailable as exc:
        console.print(
            f"[red]Correction could not recompile the semantic model: {escape(str(exc))}[/red]"
        )
        console.print(
            "Start the configured local model and retry. No correction or generation was written."
        )
        raise typer.Exit(1) from None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        console.print(f"[red]Correction rejected: {escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    console.print(
        f"Correction recorded: [bold]{application.override_id}[/bold] · "
        f"sequence {application.override_sequence}"
    )
    console.print(
        f"materialization: {application.disposition} · generation "
        f"{application.generation_id} · active {application.active_generation_id or 'none'}"
    )
    if application.error:
        console.print(f"[red]{escape(str(application.error))}[/red]")
    if str(application.disposition) != "applied":
        console.print(
            "[yellow]The ledger correction is durable, but the previous active model "
            "remains live until a clean rebuild.[/yellow]"
        )
        raise typer.Exit(1)


@autosync_app.command("doctor")
def autosync_doctor(config: ConfigOpt = None) -> None:
    """Check the archive, configured sources, runtime, and startup before enabling."""
    from varyz.autosync import doctor

    cfg_path = _config_path(config).resolve()
    checks = doctor(cfg_path)
    for check in checks:
        marker = "[green]OK[/green]" if check.ok else "[red]FAIL[/red]"
        console.print(f"{marker}  {escape(str(check.name))}: {escape(str(check.detail))}")
    if not all(check.ok for check in checks):
        raise typer.Exit(1)


@autosync_app.command("run")
def autosync_run(
    trigger: Annotated[str, typer.Option(help="Run origin recorded in the audit log")] = "manual",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show sources; touch no archive, log, or schedule"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Run once before autosync has been installed"),
    ] = False,
    claimed: Annotated[
        bool,
        typer.Option("--claimed", hidden=True),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Run one idempotent all-source refresh and embedding pass."""
    from varyz.autosync import run_once

    result = run_once(
        _config_path(config).resolve(),
        trigger=trigger,
        dry_run=dry_run,
        force=force,
        claimed=claimed,
    )
    _print_autosync(result)
    if result.status == "failed":
        raise typer.Exit(1)


@autosync_app.command("install")
def autosync_install(
    every_minutes: Annotated[
        int | None,
        typer.Option("--every-minutes", min=5, max=1440, help="Daemon recurrence"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the login registration; write nothing"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Start a singleton daemon at login and enable stale-on-use refresh."""
    from varyz.autosync import doctor, install_daemon

    cfg_path = _config_path(config).resolve()
    cfg = _cfg(cfg_path)
    checks = doctor(cfg_path)
    failed = [check for check in checks if not check.ok]
    if failed:
        for check in failed:
            console.print(f"[red]FAIL[/red]  {escape(check.name)}: {escape(check.detail)}")
        console.print("Fix the failed preflight checks, then run autosync install again.")
        raise typer.Exit(1)
    interval = every_minutes or cfg.autosync.schedule_minutes
    try:
        installed = install_daemon(cfg_path, every_minutes=interval, dry_run=dry_run)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    if dry_run:
        console.print("Dry run - no startup entry, daemon, or enable marker was written:")
        console.print(installed)
        return
    console.print(f"[green]Autosync enabled.[/green] Login startup: {installed}")
    console.print(
        f"Every {interval} minutes; MCP use also refreshes when older than "
        f"{cfg.autosync.freshness_minutes} minutes."
    )
    console.print(f"Kill switch: [bold]varyz autosync pause --config {cfg_path}[/bold]")


@autosync_app.command("status")
def autosync_status(config: ConfigOpt = None) -> None:
    """Show daemon, kill switch, freshness, and the latest run."""
    from varyz.autosync import (
        daemon_status,
        is_due,
        is_enabled,
        is_paused,
        load_state,
        report_path,
    )

    cfg_path = _config_path(config).resolve()
    cfg = _cfg(cfg_path)
    daemon = daemon_status(cfg_path)
    console.print(f"enabled: {'yes' if is_enabled(cfg) else 'no'}")
    console.print(f"login startup: {'present' if daemon.registered else 'missing'}")
    console.print(f"daemon: {'running' if daemon.running else 'stopped'} ({daemon.detail})")
    # The label names the switch, so the word after it has to describe the
    # switch. "kill switch: active" on a fresh install read as "something is
    # stopping your syncs" when it meant the opposite.
    console.print(f"kill switch: {'ENGAGED' if is_paused(cfg) else 'not engaged'}")
    console.print(f"stale: {'yes' if is_due(cfg) else 'no'}")
    state = load_state(cfg)
    if state:
        warning_count = len(state.get("warnings", []))
        warning_summary = f"{warning_count} warning{'s' if warning_count != 1 else ''}"
        console.print(
            f"last run: {state.get('finished_at', '?')} · {state.get('status', '?')} · "
            f"{state.get('new', 0)} new · {state.get('errors', 0)} errors · "
            f"{warning_summary} \N{MIDDLE DOT} graph "
            f"{state.get('graph_status', 'not recorded')}"
        )
        # What fails a run is often not a connector: on the author's archive the
        # last run reads "failed", "24 new", "0 errors", "0 warnings", and the
        # reason -- a graph prune that hit a foreign key -- was written to the
        # state file, printed into the digest, and withheld from the one command
        # a person runs to ask what happened.
        if detail := str(state.get("detail") or ""):
            label = "why" if state.get("status") != "ok" else "note"
            console.print(f"  {label}: {escape(detail)}")
    digest = report_path(cfg)
    written = digest.is_file()
    console.print(f"latest digest: {digest}{'' if written else ' (not written yet)'}")


@autosync_app.command("pause")
def autosync_pause(config: ConfigOpt = None) -> None:
    """Engage the persistent kill switch; autonomous refreshes become no-ops."""
    from varyz.autosync import pause

    cfg = _cfg(_config_path(config))
    pause(cfg)
    console.print(f"[yellow]Autosync paused.[/yellow] Sentinel: {cfg.data_dir / 'autosync/PAUSED'}")


@autosync_app.command("resume")
def autosync_resume(config: ConfigOpt = None) -> None:
    """Release the kill switch; the next due job refreshes all sources."""
    from varyz.autosync import is_enabled, is_paused, resume

    cfg = _cfg(_config_path(config))
    # "Autosync resumed." was printed whether or not anything had been paused,
    # and whether or not there was a daemon to resume into. Someone who paused,
    # uninstalled, and then resumed was told refreshes were back on.
    was_paused = is_paused(cfg)
    resume(cfg)
    if not was_paused:
        console.print("The kill switch was not engaged; nothing was paused.")
    else:
        console.print("[green]Autosync resumed.[/green]")
    if not is_enabled(cfg):
        console.print(
            "[dim]Autosync is not enabled, so nothing refreshes on its own yet: "
            "varyz autosync install[/dim]"
        )


@autosync_app.command("uninstall")
def autosync_uninstall(config: ConfigOpt = None) -> None:
    """Remove login startup and disable the daemon and on-use refresh."""
    from varyz.autosync import uninstall_daemon

    cfg_path = _config_path(config).resolve()
    try:
        removed = uninstall_daemon(cfg_path)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    console.print(f"Autosync: {removed}")


def _mcp_snippet(config_path: Path | None) -> str:
    import json

    from varyz.install import server_entry

    return json.dumps({"mcpServers": {"varyz": server_entry(config_path)}}, indent=2)


def _print_autosync(result) -> None:
    excluded = f", {result.excluded} excluded" if result.excluded else ""
    warning_count = len(result.warnings)
    warning_summary = f"{warning_count} warning{'s' if warning_count != 1 else ''}"
    console.print(
        f"[bold]{result.status}[/bold]: {result.new} new, {result.updated} updated, "
        f"{result.unchanged} unchanged{excluded}, {result.deleted} removed, "
        f"{result.errors} errors, {warning_summary}, {result.embedded} chunks embedded, "
        f"graph {result.graph_status}"
    )
    for value in result.connectors:
        excluded = f", {value.excluded} excluded" if value.excluded else ""
        detail = f" - {escape(str(value.detail))}" if value.detail else ""
        console.print(
            f"  {value.connector}: {value.status}; {value.new} new, "
            f"{value.updated} updated{excluded}, {value.errors} errors{detail}"
        )
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(str(warning))}")
    if result.detail:
        console.print(escape(result.detail))


@update_app.command("status")
def update_status(config: ConfigOpt = None) -> None:
    """The update mode, the kill switch, and what the last check did."""
    from varyz.update import (
        checkout_root,
        describe_source,
        install_source,
        is_paused,
        load_state,
        log_path,
    )

    cfg = _cfg(config)
    root = checkout_root()
    paused = "  [yellow](paused)[/yellow]" if is_paused(cfg) else ""
    console.print(f"mode: {cfg.update.mode}{paused}")
    where = f"checkout {root}" if root else describe_source(install_source())
    console.print(f"source: {escape(where)}")
    state = load_state(cfg)
    if state.get("status"):
        console.print(
            f"last: {state['status']} at {state.get('checked_at')}: "
            f"{escape(str(state.get('detail', '')))}"
        )
    else:
        console.print("last: never checked")
    console.print(f"log: {escape(str(log_path(cfg)))}")


@update_app.command("check")
def update_check(config: ConfigOpt = None) -> None:
    """Fetch and say whether an update is there; never apply it."""
    from varyz.update import run

    _print_update(run(_cfg(config), apply=False, config_path=find_config_file(config)))


@update_app.command("apply")
def update_apply(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compare with the last fetch; change and record nothing"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Update Varyz now, whatever the mode (not while paused), and refresh its agents.

    A checkout is fast-forwarded to the branch it tracks; an installed Varyz is
    reinstalled by uv from GitHub or from the folder it was installed from.
    """
    from varyz.update import run

    _print_update(
        run(_cfg(config), apply=True, dry_run=dry_run, config_path=find_config_file(config))
    )


@update_app.command("pause")
def update_pause(config: ConfigOpt = None) -> None:
    """Stop automatic updates until `varyz update resume`."""
    from varyz.update import pause

    pause(_cfg(config))
    console.print("Updates paused.")


@update_app.command("resume")
def update_resume(config: ConfigOpt = None) -> None:
    """Allow updates again."""
    from varyz.update import is_paused, resume

    cfg = _cfg(config)
    resume(cfg)
    if is_paused(cfg):
        console.print("Update pause lifted; the autosync kill switch still stops updates.")
    else:
        console.print("Updates resumed.")


@news_app.callback()
def news_show(ctx: typer.Context, config: ConfigOpt = None) -> None:
    """Whether news is on, when it was last checked, and the latest posts."""
    if ctx.invoked_subcommand is not None:
        return
    from varyz import news

    cfg = _cfg(config)
    state = news.load_state(cfg)
    console.print(f"news: {'on' if news.is_on(cfg) else 'off'} ({escape(cfg.news.url)}, hourly)")
    if state.get("status"):
        console.print(
            f"last check: {state['status']} at {state.get('checked_at')}: "
            f"{escape(str(state.get('detail', '')))}"
        )
    else:
        console.print("last check: never")
    posts = news.cached(cfg)
    if not posts:
        console.print("No posts yet.")
    for item in sorted(posts, key=lambda post: post.date, reverse=True)[:5]:
        console.print(f"  {escape(news.line(item))}")


@news_app.command("off")
def news_off(config: ConfigOpt = None) -> None:
    """Stop checking for news and passing it on."""
    from varyz import news

    news.turn_off(_cfg(config))
    console.print("News off. Turn it back on with varyz news on.")


@news_app.command("on")
def news_on(config: ConfigOpt = None) -> None:
    """Check for news hourly again and pass new posts on through your agent."""
    from varyz import news

    news.turn_on(_cfg(config))
    console.print("News on.")


@news_app.command("check")
def news_check(config: ConfigOpt = None) -> None:
    """Fetch the news now, whatever the hour."""
    from varyz import news

    cfg = _cfg(config)
    result = news.check(cfg)
    console.print(f"{result.status}: {escape(result.detail)}")
    for item in news.unseen(cfg)[:5]:
        console.print(f"  new: {escape(news.line(item))}")
    if result.status == "failed":
        raise typer.Exit(1)


def _print_update(result: UpdateResult) -> None:
    colour = {"applied": "green", "failed": "red", "available": "yellow", "waiting": "yellow"}
    tint = colour.get(result.status)
    label = f"[{tint}]{result.status}[/{tint}]" if tint else result.status
    console.print(f"{label}: {escape(result.detail)}")
    if result.backup:
        console.print(f"database backup: {escape(result.backup)}")
    if result.status == "failed":
        raise typer.Exit(1)


def _refresh_if_stale(cfg: Config, config: Path | None) -> None:
    """Best-effort background freshness for human read commands."""
    import contextlib

    from varyz import news
    from varyz.autosync import maybe_trigger_on_use
    from varyz.config import find_config_file
    from varyz.update import maybe_check_on_use

    with contextlib.suppress(Exception):
        maybe_trigger_on_use(cfg, find_config_file(config))
    with contextlib.suppress(Exception):
        maybe_check_on_use(cfg, find_config_file(config))
    with contextlib.suppress(Exception):
        news.maybe_check_on_use(cfg, find_config_file(config))


def _print_stats(stats: IngestStats) -> None:
    # Deletions are shown only when there are some. They are the one number
    # here that describes something being removed, so it should never be
    # sitting at zero in the corner of every sync inviting a second glance.
    removed = f", {stats.deleted} removed" if stats.deleted else ""
    excluded = f", {stats.excluded} excluded" if stats.excluded else ""
    console.print(
        f"[bold]{stats.connector}[/bold]: "
        f"{stats.new} new, {stats.updated} updated, "
        f"{stats.unchanged} unchanged{excluded}{removed}, {stats.errors} errors"
    )
    # A folder configured for another machine is a legitimate reason for this,
    # so it is a note and not an error. But the counts above describe only what
    # was walked, and without this they described a typo as a clean sync.
    if stats.missing_paths:
        from varyz.connectors.base import is_path_pattern

        patterns = [path for path in stats.missing_paths if is_path_pattern(path)]
        absent = [path for path in stats.missing_paths if not is_path_pattern(path)]
        if absent:
            listed = ", ".join(escape(path) for path in absent)
            console.print(f"[yellow]  not on this machine, so not indexed: {listed}[/yellow]")
        if patterns:
            listed = ", ".join(escape(path) for path in patterns)
            console.print(
                f"[yellow]  a wildcard, not a path, so nothing was walked: {listed}[/yellow]\n"
                "[yellow]  name the folder itself; varyz walks everything inside it.[/yellow]"
            )


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="What to look for")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many to show")] = 10,
    config: ConfigOpt = None,
) -> None:
    """Hybrid keyword + semantic search over all entries."""
    from varyz.retrieval.behavior_search import analyze_behavior_query
    from varyz.retrieval.search import corpus_vectors, hybrid_search

    # `varyz search ""` -- a shell that expanded a variable to nothing -- ran
    # the semantic half against an empty string and listed the archive back
    # under "similar meaning", which is a ranking of nothing against nothing.
    if not query.strip():
        console.print("[red]Nothing to search for: the query is empty.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    _refresh_if_stale(cfg, config)
    conn = _db(cfg)
    # An index with no vectors has no semantic half to run, and asking for
    # one downloads 220 MB of embedding model to discover that. Searching
    # before the first sync spent that download on the words "No matches."
    embedder = Embedder(cfg) if corpus_vectors(conn) else None
    hits = hybrid_search(conn, query, embedder, limit=limit)
    if not hits:
        if analyze_behavior_query(query):
            console.print(
                "No qualifying behavioral evidence found. This is not proof it "
                "never happened; no explicit admission or observable contradiction "
                "matched the person and speaker constraints."
            )
        elif dao.count_entries(conn) == 0:
            # "No matches" reads as "your query found nothing", and on a
            # freshly created archive the truthful answer is that there is
            # nothing yet to search.
            console.print("The archive is empty, so there is nothing to search yet.")
            console.print(
                '[dim]Connect a source with varyz setup, or capture one: varyz add "..."[/dim]'
            )
        else:
            console.print("No matches.")
        raise typer.Exit(0)
    table = Table(show_lines=False)
    table.add_column("date", style="cyan", no_wrap=True)
    table.add_column("kind", style="dim")
    table.add_column("match")
    table.add_column("why", style="dim")
    table.add_column("id", **ID_COLUMN)
    for h in hits:
        date = local_day(h.entry.occurred_start)
        text = h.snippet or (h.entry.title or h.entry.body[:80])
        table.add_row(date, h.entry.kind, _match_cell(text), "; ".join(h.match_reasons), h.entry.id)
    console.print(table)


# One flowed line of prose is what a table cell holds; past this the row is
# reading material, and `varyz search` shows ten of them.
MATCH_CELL_CHARS = 240


def _match_cell(text: str) -> RenderableType:
    """The archive's own words, bolded where they matched, never as markup.

    Rich reads a bracketed lowercase word as a style tag, so a row whose
    snippet said `[alpha]` printed with the searched-for word missing. The
    words in an entry are the user's, and are shown as written.

    Written, but not laid out: an entry keeps the line breaks and blank lines
    of the document it came from, and a cell that reproduces them turns a
    two-hundred-character snippet into fourteen rows of mostly nothing. Where
    nothing matched by keyword there is no window to centre on either, so the
    cell held the whole chunk -- up to a thousand characters per row, ten rows
    a search. Both make the answer harder to find than no answer at all.
    """
    from rich.text import Text

    from varyz.retrieval.search import MATCH_CLOSE, MATCH_OPEN

    flowed = " ".join(text.split())
    if len(flowed) > MATCH_CELL_CHARS:
        flowed = flowed[:MATCH_CELL_CHARS].rstrip() + "\u2026"
    cell = Text()
    for index, part in enumerate(flowed.replace(MATCH_CLOSE, MATCH_OPEN).split(MATCH_OPEN)):
        cell.append(part, style="bold" if index % 2 else None)
    return cell


def _say_which_providers_are_connected(cfg) -> None:
    """Whether some other account could have answered.

    Someone who connected one provider and left the default alone was told to
    go and connect a second account -- "No credentials for 'claude'. Run:
    varyz connect claude" -- with no hint that the key already on this machine
    would have answered the question.
    """
    from varyz.llm.providers import provider_overview

    connected = [row["name"] for row in provider_overview(cfg) if row["connected"]]
    if not connected:
        return
    # Rich reads a bracketed lowercase word as a style tag, and [llm] is the
    # config section this sentence is about.
    console.print(
        f"[dim]Already connected: {', '.join(connected)}. To use one, set "
        f'provider = "{connected[0]}" under {escape("[llm]")} in config.toml.[/dim]'
    )


def ask(
    question: Annotated[str, typer.Argument(help="What to ask your archive")],
    k: Annotated[
        int | None,
        typer.Option(min=1, help="Max excerpts to send to the cloud"),
    ] = None,
    local_distill: Annotated[
        bool,
        typer.Option(
            "--local-distill",
            help="Explicitly start the managed local model for this question",
        ),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Ask your archive a question; the answer cites real entries."""
    # `varyz ask ""` got as far as reporting which providers are connected,
    # which reads as the question having been understood and the credentials
    # having been the problem.
    if not question.strip():
        console.print("[red]Nothing to ask: the question is empty.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    _refresh_if_stale(cfg, config)
    conn = _db(cfg)
    if dao.count_entries(conn) == 0:
        # An empty archive answered "I found no shareable entries matching that
        # question", which blames the question, and reached that answer only
        # after downloading 220 MB of embedding model and filing an egress
        # receipt for a call that could not happen.
        console.print("The archive is empty, so there is nothing to answer from yet.")
        console.print(
            '[dim]Connect a source with varyz setup, or capture one: varyz add "..."[/dim]'
        )
        raise typer.Exit(0)
    from varyz.llm.router import NoApiKeyError
    from varyz.retrieval.rag import ask as rag_ask

    # Installed model weights are capability, not consent to reserve the GPU.
    # The local provider exists only behind this per-call option, and the same
    # instance is reused if an approved cloud request is replayed below.
    local_provider = None
    if local_distill:
        from varyz.llm.local import build_local_provider

        local_provider = build_local_provider(cfg, conservative_resources=True)
    try:
        result = rag_ask(
            conn,
            cfg,
            question,
            Embedder(cfg),
            k=k,
            local_provider=local_provider,
        )
    except NoApiKeyError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        _say_which_providers_are_connected(cfg)
        raise typer.Exit(1) from None
    if result.approval_required:
        from rich.panel import Panel
        from rich.text import Text

        from varyz.llm.providers import provider_overview

        # Approval is consent for a call that is about to happen. With no
        # credentials it cannot, so the payload preview, the hash and the
        # yes/no were asked of someone whose answer changed nothing -- and the
        # first thing a new install does is run `ask` before `connect`.
        active = next((row for row in provider_overview(cfg) if row["active"]), None)
        if active is not None and not active["connected"]:
            console.print(
                f"[red]No credentials for '{cfg.llm.provider}'. "
                f"Run: varyz connect {cfg.llm.provider}[/red]"
            )
            _say_which_providers_are_connected(cfg)
            raise typer.Exit(1)
        if result.preflight is not None:
            console.print(
                Panel(
                    Text(result.preflight.render()),
                    title="Before anything leaves this machine",
                    border_style="yellow",
                )
            )
        console.print(
            Panel(
                Text(result.payload_preview or ""),
                title="Exact cloud payload preview",
                border_style="yellow",
            )
        )
        console.print(
            f"[dim]destination cloud:{cfg.llm.provider} · SHA-256 {result.payload_hash}[/dim]"
        )
        categories = result.preflight.confidential_categories if result.preflight else ()
        if categories:
            from varyz.privacy.categories import describe

            console.print(
                f"[yellow]This includes {describe(categories)} evidence. "
                "Approval covers this one call only.[/yellow]"
            )
        if not _confirm(
            f"Approve this exact payload ({result.payload_hash}) for one cloud call?",
            instead=(
                "Nothing was sent. This approval covers one payload and cannot be given in "
                "advance: run the question again in a terminal."
            ),
        ):
            console.print("Nothing was sent. Inspect the receipt before retrying.")
            raise typer.Exit(0)
        from varyz.domain import utc_now
        from varyz.privacy.models import Approval

        result = rag_ask(
            conn,
            cfg,
            question,
            Embedder(cfg),
            k=k,
            local_provider=local_provider,
            request_id=result.request_id,
            approval=Approval(
                request_id=result.request_id,
                payload_hash=result.payload_hash,
                actor="local_user",
                approved_at=utc_now(),
            ),
        )
    # Show exactly what left the machine before the answer.
    sent = (
        f"{result.sent} excerpt{'s' if result.sent != 1 else ''} "
        f"(~{result.chars_sent:,} chars) sent to {cfg.llm.provider}"
    )
    detail = f"{result.considered} memories searched locally"
    if result.distilled:
        detail += ", distilled by local LLM"
    detail += f", depth {result.depth.level}/5: {result.depth.label}"
    if result.excluded_sensitive:
        detail += f", {result.excluded_sensitive} kept private"
    if result.receipt_id:
        detail += f", receipt {result.receipt_id}"
    console.print(f"[dim]{sent} · {detail}.[/dim]\n")
    for delta in result.stream:
        console.print(delta, end="")
    console.print()
    if result.hits:
        console.print("\n[dim]Sources:[/dim]")
        for n, h in enumerate(result.hits, 1):
            date = local_day(h.entry.occurred_start)
            label = h.entry.title or h.entry.body[:60]
            console.print(f"[dim]  [{n}] {date} · {h.entry.kind} · {label}[/dim]")


@app.command()
def forget(
    entry_id: Annotated[str, typer.Argument(help="Entry id to delete permanently")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation")] = False,
    allow_reimport: Annotated[
        bool,
        typer.Option(
            "--allow-reimport",
            help="Delete it now but let a later sync bring the item back",
        ),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Permanently delete one entry and everything derived from it."""
    from varyz.deletion import forget_entry

    # `varyz forget "$ID"` with an unset variable reported "No entry with id ."
    # -- a sentence about an id that was never given, when what happened is that
    # the shell handed this command nothing. It is the same slip `varyz add ""`
    # makes and is answered the same way.
    if not entry_id.strip():
        console.print("[red]Nothing to forget: the entry id is empty.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    conn = _db(cfg)
    entry = dao.get_entry(conn, entry_id)
    if entry is None:
        console.print(f"[red]No entry with id {escape(entry_id)}.[/red]")
        raise typer.Exit(1)
    label = entry.title or entry.body[:70] or entry.kind
    if not yes and not _confirm(
        f"Delete '{label}'? This cannot be undone",
        instead="Pass --yes to delete without being asked.",
    ):
        raise typer.Exit(0)
    # The connector still owns the upstream item, so a delete that leaves no
    # tombstone is undone by the next sync. The trust centre asks which of the
    # two the person means and recommends the durable one; this command promised
    # "permanently" and then quietly took the other branch, so the deleted mail
    # came back on the next `varyz sync` with nothing said about it.
    with WriteLock(cfg.data_dir):
        forget_entry(
            conn,
            Vault(cfg.vault_dir),
            entry_id,
            prevent_reimport=not allow_reimport,
        )
    if allow_reimport:
        console.print("Forgotten. A later sync may bring it back.")
    else:
        console.print("Forgotten, and blocked from re-import.")


@app.command()
def trust(
    summary: Annotated[
        bool,
        typer.Option("--summary", help="Print the trust overview without prompts"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Open the guided local trust and privacy controls."""
    from varyz.trust import run_trust_center

    cfg_path = _config_path(config).resolve()
    cfg = _cfg(cfg_path)
    conn = _db(cfg)
    try:
        run_trust_center(cfg_path, conn, console, summary=summary)
    finally:
        conn.close()


@app.command()
def backup(
    target: Annotated[Path, typer.Argument(help="Directory to back up into")],
    config: ConfigOpt = None,
) -> None:
    """Consistent copy of the database (SQLite backup API) + the vault."""
    cfg = _cfg(config)
    # `varyz backup data` -- the obvious thing to type -- pointed the backup at
    # the archive it was backing up: it ran the SQLite backup with the live
    # database as both source and destination, then copied the vault over
    # itself and failed with a raw shutil error list.
    resolved = target.expanduser().resolve()
    data_dir = cfg.data_dir.resolve()
    if resolved == data_dir or data_dir in resolved.parents:
        console.print(f"[red]{resolved} is inside the archive it would back up.[/red]")
        console.print(f"[dim]Choose a directory outside {data_dir}.[/dim]")
        raise typer.Exit(2)
    if resolved.exists() and not resolved.is_dir():
        # mkdir over an existing file raises FileExistsError, and the traceback
        # named pathlib rather than the argument that was wrong.
        console.print(f"[red]{escape(str(resolved))} is a file, not a directory.[/red]")
        console.print("[dim]varyz backup writes varyz.db, vault/ and config.toml into it.[/dim]")
        raise typer.Exit(2)
    # The archive-inside check above catches a target under data/, but not a
    # target that *contains* it. `varyz backup .` -- and `varyz backup "$DEST"`
    # with DEST unset, which arrives as the same thing -- copies config.toml
    # onto itself, and shutil answered that with a raw WinError 32 about a file
    # held open by another process, at the end of a backup that had already
    # written the database into the directory the archive lives in.
    config_source = _config_path(config).resolve()
    if config_source == (resolved / "config.toml").resolve():
        console.print(
            f"[red]{escape(str(resolved))} already holds the archive's config.toml.[/red]"
        )
        console.print(f"[dim]Choose a directory outside {config_source.parent}.[/dim]")
        raise typer.Exit(2)
    # A backup written before the rename holds varys.db; this one replaces it.
    from varyz.legacy import legacy_backup_database

    previous = legacy_backup_database(resolved) or resolved / "varyz.db"
    replacing = previous.exists()
    # A backup of an archive that does not fit is a disk filled to the last
    # byte and a truncated copy of the database at the end of it -- on the
    # machine the original lives on. The sizes are known before anything is
    # written, so the refusal is cheap and the numbers belong in it.
    db_bytes = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
    wal = cfg.db_path.with_name(cfg.db_path.name + "-wal")
    db_bytes += wal.stat().st_size if wal.exists() else 0
    vault_bytes = directory_bytes(cfg.vault_dir) if cfg.vault_dir.exists() else 0
    # The database is written first, over whatever copy is already there; the
    # old vault is only removed afterwards, so its space is not free while the
    # database is being written.
    old_db = previous.stat().st_size if replacing else 0
    old_vault = directory_bytes(resolved / "vault") if (resolved / "vault").exists() else 0
    # 64 MiB over the measurement: the destination keeps a journal of its own
    # while the backup runs, and a copy that lands with nothing left over is
    # the next thing to break on this disk.
    needed = max(0, db_bytes - old_db + max(0, vault_bytes - old_vault)) + 64 * 1024 * 1024
    free = free_bytes(resolved)
    if free is not None and needed > free:
        console.print(f"[red]Not enough free space to back up into {escape(str(resolved))}.[/red]")
        console.print(
            f"[dim]The copy needs about {bytes_label(needed)} and "
            f"{bytes_label(free)} is free. Free up {bytes_label(needed - free)} "
            "or name a directory on another disk.[/dim]"
        )
        raise typer.Exit(2)
    resolved.mkdir(parents=True, exist_ok=True)
    src = connect(cfg.db_path)
    dest = sqlite3.connect(resolved / "varyz.db")
    with dest:
        src.backup(dest)
    dest.close()
    src.close()
    if previous.name != "varyz.db":
        previous.unlink()
    if cfg.vault_dir.exists():
        # Backing up twice into one directory replaced the database and merged
        # the vault, so anything varyz forget had deleted in between stayed in
        # the backup -- beside a database that no longer mentions it.
        if replacing and (resolved / "vault").exists():
            shutil.rmtree(resolved / "vault")
        shutil.copytree(cfg.vault_dir, resolved / "vault", dirs_exist_ok=True)
    # A copy of the database and the vault is not the archive: config.toml
    # carries the data directory, the sensitivity defaults and the connector
    # approvals, and without it a restored copy has forgotten which sources it
    # was allowed to read. It holds no secrets -- those live in the OS
    # credential store -- so it belongs beside the two directories it explains.
    shutil.copy2(_config_path(config), resolved / "config.toml")
    console.print(f"Backed up to {resolved}")
    if replacing:
        console.print("[dim]The backup already in that directory was replaced.[/dim]")
    # There is no `varyz restore`: the backup is a plain directory, and saying
    # so is the difference between a copy someone can use and one they merely
    # possess.
    console.print(
        "To restore it, stop varyz and copy [bold]varyz.db[/bold] and "
        f"[bold]vault/[/bold] back into [bold]{cfg.data_dir}[/bold], "
        "with config.toml beside it."
    )


@conversations_app.command("read")
def conversations_read(
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Read at most this many, then stop"),
    ] = None,
    concurrency: Annotated[
        int, typer.Option("--concurrency", help="Conversations in flight at once")
    ] = 1,
    config: ConfigOpt = None,
) -> None:
    """Have the local model read the conversations nobody has read yet.

    Sync does this on its own, a budget at a time. This is the command for
    catching up an archive that was just imported, where waiting for the
    budget would mean waiting days for a history that is already on disk.
    """
    from varyz.pipeline.conclusions import reader, run_pass
    from varyz.pipeline.windows import build_windows

    cfg = _cfg(config)
    conn = _db(cfg)
    built = build_windows(conn, sources=tuple(cfg.conversations.sources))
    console.print(f"{built['windows']} conversations in the archive.")
    author, author_model = reader(cfg)
    with console.status("Reading..."):
        stats = run_pass(
            conn,
            author=author,
            author_model=author_model,
            limit=limit,
            concurrency=concurrency,
        )
    console.print(
        f"Read {stats['read']} of {stats['windows']}: "
        f"{stats['personal']} quotable, {stats['sensitive']} kept shut, "
        f"{stats['failed']} unreadable. "
        f"{stats['conclusions']} conclusions."
    )


@conversations_app.command("status")
def conversations_status(config: ConfigOpt = None) -> None:
    """How much of the archive has been read, and what it was labelled."""
    from varyz.pipeline.window_vectors import counts as window_vector_counts

    cfg = _cfg(config)
    conn = _db(cfg)
    total = conn.execute("SELECT count(*) FROM conversation_windows").fetchone()[0]
    labels = dict(
        conn.execute(
            "SELECT label, count(*) FROM conversation_window_labels GROUP BY label"
        ).fetchall()
    )
    read = sum(labels.values())
    conclusions = conn.execute("SELECT count(*) FROM conversation_window_conclusions").fetchone()[0]
    chunks, embedded = window_vector_counts(conn)
    console.print(f"{total} conversations, {read} read, {total - read} still shut.")
    console.print(
        f"{labels.get('personal', 0)} quotable, {labels.get('sensitive', 0)} private, "
        f"{conclusions} conclusions."
    )
    # Searchable is not the same as read: a conversation is findable by its
    # words and its meaning before any model has decided whether it may be
    # quoted. Both numbers matter, and only one of them costs GPU time.
    console.print(f"Searchable: {embedded} of {chunks} conversation chunks embedded.")


@graph_app.command("rebuild")
def graph_rebuild(
    version: Annotated[
        str | None, typer.Option("--version", help="Extractor generation version")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan the generation without writing")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Run a resumable rebuild beside the active graph, then atomically activate."""
    from varyz.graph.service import rebuild_graph
    from varyz.trust import count_noun

    cfg = _cfg(config)
    conn = _db(cfg)
    with _progress_bar("Building the graph") as bar:
        result = rebuild_graph(
            conn, cfg, version=version, dry_run=dry_run,
            on_progress=lambda done, total: bar(
                f"Building the graph: {done:,} of {total:,} entries",
                done / total if total else 1.0,
            ),
        )
    state = "dry run" if result.dry_run else "no-op" if result.no_op else "activated"
    # A first rebuild seeds one alias for the archive's owner and reported it
    # as "1 aliases".
    console.print(
        f"Graph {state}: {result.entries_processed}/{result.entries_total} entries, "
        f"{count_noun(result.claims, 'claim')}, "
        f"{count_noun(result.aliases, 'alias', 'aliases')}, "
        f"{result.quarantined} quarantined."
    )
    if result.entries_reused:
        extracted = max(0, result.entries_processed - result.entries_reused)
        console.print(
            f"[dim]reused {result.entries_reused} cached extractions; extracted {extracted}[/dim]"
        )
    if result.entries_failed:
        console.print(
            f"[yellow]{result.entries_failed} entries the extractor could not read "
            "were quarantined; the rest of the graph is built[/yellow]"
        )
    if result.run_id:
        console.print(f"[dim]generation {result.run_id} · {result.generation_key}[/dim]")


@graph_app.command("prune")
def graph_prune(
    keep: Annotated[
        int | None,
        typer.Option("--keep", help="Activated generations to keep behind the live one"),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually delete. Without it this only reports."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be deleted (the default)")
    ] = False,
    vacuum: Annotated[
        bool,
        typer.Option("--vacuum", help="Rewrite the database to give the freed space back"),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Delete failed, abandoned, and out-of-window graph generations."""
    from varyz.db.connection import WriteLock
    from varyz.graph.prune import prune_graph

    # Reporting first, deleting on request -- the way `varyz model prune` does
    # it, and the way a command that throws away every generation a rollback
    # could return to has to. This one deleted on sight, so the two commands
    # with the same name and the same shape did opposite things, and the safe
    # habit learned from one was destructive with the other. `--dry-run` still
    # means what it always meant; it is now what happens anyway.
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        result = prune_graph(
            conn,
            keep=cfg.graph.keep_generations if keep is None else keep,
            keep_job_days=cfg.graph.keep_job_days,
            dry_run=not apply or dry_run,
            vacuum=vacuum,
        )
    if not result.generations:
        console.print(
            "No graph has been built yet, so there is nothing to prune."
            if conn.execute("SELECT 1 FROM graph_runs").fetchone() is None
            else "Nothing to prune; every generation is live or within the window."
        )
    else:
        table = Table("generation", "state", "version", "started", "rows")
        for generation in result.generations:
            table.add_row(
                generation.run_id,
                generation.state,
                generation.extractor_version,
                local_stamp(generation.started_at),
                f"{generation.rows:,}",
            )
        console.print(table)
    # Printed whether or not it deleted. A dry run showed the table and then
    # said nothing about the size of what it had listed. Not printed when
    # there is nothing at all, where it only followed "Nothing to prune" with
    # "would delete 0 rows".
    if result.generations or result.rows:
        verb = "would delete" if result.dry_run else "deleted"
        console.print(
            f"{verb} {result.rows:,} rows "
            f"({result.labels:,} policy labels, {result.jobs:,} finished jobs)."
        )
    if result.dry_run and result.generations:
        console.print("[dim]Run with --apply to delete them.[/dim]")
    if result.vacuumed_bytes:
        console.print(f"[green]Reclaimed {result.vacuumed_bytes / 1e6:.0f} MB.[/green]")
    elif result.reclaimable_bytes >= 1_000_000:
        # A few free pages rounded to "0 MB is free inside the database; run
        # with --vacuum to give it back to the disk" -- advice to spend a
        # full-database rewrite on nothing.
        console.print(
            f"[dim]{result.reclaimable_bytes / 1e6:.0f} MB is free inside the database; "
            f"run with --vacuum to give it back to the disk[/dim]"
        )


@graph_app.command("doctor")
def graph_doctor(config: ConfigOpt = None) -> None:
    """Check provenance, intervals, aliases, runs, and evidence integrity."""
    from varyz.graph.doctor import diagnose

    cfg = _cfg(config)
    conn = _db(cfg)
    active = conn.execute("SELECT entries_total FROM graph_runs WHERE state='active'").fetchone()
    if active is None:
        # An archive with no graph has nothing to diagnose, so every check
        # passed and the answer was "healthy" -- a clean bill of health on a
        # graph that was never built. Say which of the two it is.
        console.print("No graph has been built yet. Run: varyz graph rebuild")
        return
    # The same distinction one step further along. Every check here reads the
    # graph, and none of them reads the archive, so a graph built before the
    # last fifty entries arrived is "healthy" -- and the person wondering why
    # `ask` has never heard of last week is told nothing is wrong.
    excluded = tuple(sorted(set(cfg.graph.excluded_sources)))
    placeholders = ",".join("?" for _ in excluded)
    where = f" WHERE lower(source) NOT IN ({placeholders})" if excluded else ""
    entries = int(
        conn.execute(
            f"SELECT COUNT(*) FROM entries{where}",  # noqa: S608 - placeholders only
            excluded,
        ).fetchone()[0]
    )
    behind = max(entries - int(active["entries_total"] or 0), 0)
    if behind:
        console.print(
            f"[yellow]{behind:,} entr{'y is' if behind == 1 else 'ies are'} newer than "
            f"the live graph.[/yellow] [dim]Run: varyz graph rebuild[/dim]"
        )
    issues = diagnose(conn)
    if not issues:
        # "Graph is healthy" straight after "50 entries are newer than the
        # live graph" is two answers to one question.
        console.print(
            "[green]No integrity problems inside the graph.[/green]"
            if behind
            else "[green]Graph is healthy.[/green]"
        )
        return
    table = Table("severity", "code", "count", "detail")
    for issue in issues:
        table.add_row(issue.severity, issue.code, str(issue.count), issue.message)
    console.print(table)
    if any(issue.severity == "error" for issue in issues):
        raise typer.Exit(1)


@graph_app.command("review")
def graph_review(config: ConfigOpt = None) -> None:
    """List unresolved conservative identity candidates."""
    conn = _db(_cfg(config))
    # The same distinction `varyz graph doctor` draws. With no graph there are
    # no candidates, so this reported that nothing needed review -- which reads
    # as "identity resolution is settled" on an archive that has never had a
    # graph to resolve identities in.
    if conn.execute("SELECT 1 FROM graph_runs WHERE state='active'").fetchone() is None:
        console.print("No graph has been built yet. Run: varyz graph rebuild")
        return
    rows = conn.execute(
        """SELECT c.id, l.canonical_label AS left_label,
                  r.canonical_label AS right_label, c.score, c.explanation
           FROM entity_candidates c
           JOIN entities l ON l.id=c.left_entity_id
           JOIN entities r ON r.id=c.right_entity_id
           WHERE c.state='pending' ORDER BY c.score DESC, c.id"""
    ).fetchall()
    if not rows:
        console.print("No identity candidates need review.")
        return
    table = Table(Column("id", **ID_COLUMN), "possible match", "score", "why")
    for row in rows:
        table.add_row(
            row["id"],
            f"{row['left_label']} ↔ {row['right_label']}",
            f"{row['score']:.2f}",
            row["explanation"],
        )
    console.print(table)


# The tables a correction addresses by fingerprint, and the word the message
# uses for each. Claims and evidence quotes were the two the note could not
# check: `varyz graph claim <typo> accept` answered "Claim accept recorded"
# and filed an override that will never match a row -- the mistyped entity id
# this note already exists to catch, one table over.
_FINGERPRINTED = {
    "claim": ("claims", "claim"),
    "evidence": ("claim_evidence", "evidence quote"),
    "alias": ("entity_aliases", "alias"),
}


def _override_note(
    conn,
    *,
    entity_id: str | None = None,
    fingerprint: str | None = None,
    fingerprint_kind: str = "claim",
) -> str:
    """Whether the override just recorded has changed anything yet.

    Overrides are replayed onto every rebuild, so recording one against an
    archive with no graph -- or an id the graph does not contain -- is a
    legitimate thing to do. Reporting it as "Merged." is not: on a fresh
    archive nothing was merged, and a mistyped id read exactly like success.
    """
    if conn.execute("SELECT 1 FROM graph_runs WHERE state='active'").fetchone() is None:
        return "\nNo graph yet; it applies at the next varyz graph rebuild."
    if entity_id is not None and (
        conn.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is None
    ):
        return f"\nNo entity {entity_id} in the current graph; a rebuild would apply it."
    if fingerprint is not None:
        table, noun = _FINGERPRINTED[fingerprint_kind]
        found = conn.execute(
            f"SELECT 1 FROM {table} t JOIN graph_runs g "
            "ON g.id=t.graph_generation_id AND g.state='active' WHERE t.fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if found is None:
            return (
                f"\nNo {noun} with fingerprint {fingerprint} in the current graph; "
                "a rebuild would apply it."
            )
    return ""


def _refuse_an_id_nothing_could_ever_match(what: str, value: str) -> None:
    """Stop an override keyed to an empty id.

    An override is replayed onto every rebuild by exact id, and recording one
    for an id the graph does not hold yet is deliberate -- it applies when the
    rebuild produces it. An empty id is the one that never can. `varyz graph
    merge "$A" "$B"` with an unset variable filed exactly that, and the note
    that says a rebuild would apply it made it read like the ordinary case.
    """
    if value.strip():
        return
    console.print(f"[red]An override needs {what}.[/red]")
    raise typer.Exit(2)


def _is_merged(conn, entity_id: str) -> bool:
    """Whether this entity has a merge for a split to reverse."""
    row = conn.execute(
        "SELECT 1 FROM entities WHERE id=? AND merged_into_id IS NOT NULL", (entity_id,)
    ).fetchone()
    return row is not None


def _override_line(done: str, pending: str, override_id: str, note: str) -> str:
    """The headline for a recorded override, in the tense the note earns.

    The note already said nothing had changed yet, but it arrived under
    "Entity label changed." -- and a reader who stops at the first line has
    been told the opposite of what the second one says.
    """
    return f"{pending if note else done}. Override: {override_id}{note}"


@graph_app.command("merge")
def graph_merge(
    entity_id: Annotated[str, typer.Argument(help="Entity to merge")],
    into_entity_id: Annotated[str, typer.Argument(help="Surviving entity")],
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Persist a reversible entity merge override."""
    from varyz.graph.overrides import append_override

    _refuse_an_id_nothing_could_ever_match("an entity to merge", entity_id)
    _refuse_an_id_nothing_could_ever_match("a surviving entity", into_entity_id)
    if entity_id == into_entity_id:
        # The replay drops a merge into the entity itself, so the override is
        # one that can never do anything -- reported as a merge that did.
        console.print("[red]An entity cannot be merged into itself.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        # Both ids, not just the first: a merge is replayed as merged_into_id
        # = <surviving>, so a typo in the second one merges the entity away
        # into an id nothing resolves to, and that read as "Merged."
        note = _override_note(conn, entity_id=entity_id) or _override_note(
            conn, entity_id=into_entity_id
        )
        override_id = append_override(
            conn,
            "entity",
            entity_id,
            "merge",
            {"merged_into_id": into_entity_id},
            reason=reason,
        )
    console.print(_override_line("Merged", "Merge recorded", override_id, note))


@graph_app.command("split")
def graph_split(
    entity_id: Annotated[str, typer.Argument(help="Merged entity to restore")],
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Reverse a merge through an explicit split override."""
    from varyz.graph.overrides import append_override

    _refuse_an_id_nothing_could_ever_match("an entity to split", entity_id)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        note = _override_note(conn, entity_id=entity_id)
        if not note and not _is_merged(conn, entity_id):
            # A split reverses a merge. Against an entity that was never
            # merged it files an override whose replay is a no-op forever --
            # and "Split recorded." reads exactly like a merge undone.
            console.print(f"[red]Entity {escape(entity_id)} is not merged into anything.[/red]")
            console.print(
                "[dim]A split reverses a varyz graph merge, "
                "or an identity candidate you accepted[/dim]"
            )
            raise typer.Exit(1)
        override_id = append_override(conn, "entity", entity_id, "split", reason=reason)
    console.print(_override_line("Split recorded", "Split recorded", override_id, note))


@graph_app.command("claim")
def graph_claim_override(
    fingerprint: Annotated[str, typer.Argument(help="Stable claim fingerprint")],
    action: Annotated[str, typer.Argument(help="accept | reject")],
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Accept or reject a claim without editing model-derived rows."""
    from varyz.graph.overrides import append_override

    if action not in {"accept", "reject"}:
        console.print("[red]Action must be accept or reject.[/red]")
        raise typer.Exit(2)
    _refuse_an_id_nothing_could_ever_match("a claim fingerprint", fingerprint)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        note = _override_note(conn, fingerprint=fingerprint)
        override_id = append_override(conn, "claim", fingerprint, action, reason=reason)
    console.print(
        _override_line(f"Claim {action} recorded", f"Claim {action} recorded", override_id, note)
    )


@graph_app.command("label")
def graph_change_label(
    entity_id: Annotated[str, typer.Argument(help="Stable entity ID")],
    label: Annotated[str, typer.Argument(help="New canonical display label")],
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Change an entity display label through a durable override."""
    from varyz.graph.overrides import append_override

    _refuse_an_id_nothing_could_ever_match("an entity to label", entity_id)
    if not label.strip():
        console.print("[red]Label cannot be empty.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        note = _override_note(conn, entity_id=entity_id)
        override_id = append_override(
            conn,
            "entity",
            entity_id,
            "change_label",
            {"label": label.strip()},
            reason=reason,
        )
    console.print(
        _override_line("Entity label changed", "Label override recorded", override_id, note)
    )


@graph_app.command("time")
def graph_correct_time(
    fingerprint: Annotated[str, typer.Argument(help="Stable claim fingerprint")],
    valid_from: Annotated[str | None, typer.Option(help="Corrected interval start")] = None,
    valid_to: Annotated[str | None, typer.Option(help="Corrected interval end")] = None,
    precision: Annotated[
        str | None,
        typer.Option(help="exact | day | month | year | decade | unknown (default unknown)"),
    ] = None,
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Correct a claim interval without mutating extractor output."""
    from varyz.domain import TimePrecision
    from varyz.graph.overrides import append_override

    if valid_from is None and valid_to is None and precision is None:
        # The replay writes the payload straight into the row, so bare
        # `varyz graph time <fingerprint>` erased the claim's dates and marked
        # the erasure as the owner's own -- under the words "Time correction
        # recorded", which is what a correction that landed also says.
        console.print("[red]Nothing to correct: pass --valid-from, --valid-to, or both.[/red]")
        console.print(
            "[dim]To say the interval is not known, ask for it: --precision unknown[/dim]"
        )
        raise typer.Exit(2)
    _refuse_an_id_nothing_could_ever_match("a claim fingerprint", fingerprint)
    try:
        normalized_precision = TimePrecision(precision or "unknown")
    except ValueError:
        console.print("[red]Invalid temporal precision.[/red]")
        raise typer.Exit(2) from None
    cfg = _cfg(config)
    conn = _db(cfg)
    note = _override_note(conn, fingerprint=fingerprint)
    try:
        with WriteLock(cfg.data_dir):
            override_id = append_override(
                conn,
                "claim",
                fingerprint,
                "correct_time",
                {
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "temporal_precision": normalized_precision.value,
                },
                reason=reason,
            )
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    console.print(
        _override_line("Claim time corrected", "Time correction recorded", override_id, note)
    )


@graph_app.command("speaker")
def graph_mark_speaker(
    evidence_fingerprint: Annotated[str, typer.Argument(help="Stable evidence fingerprint")],
    speaker_entity_id: Annotated[str, typer.Argument(help="Speaker entity ID")],
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Attribute an evidence quote to a speaker through a durable override."""
    from varyz.graph.overrides import append_override

    _refuse_an_id_nothing_could_ever_match("an evidence fingerprint", evidence_fingerprint)
    _refuse_an_id_nothing_could_ever_match("a speaker entity", speaker_entity_id)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        note = _override_note(conn, entity_id=speaker_entity_id) or _override_note(
            conn, fingerprint=evidence_fingerprint, fingerprint_kind="evidence"
        )
        override_id = append_override(
            conn,
            "evidence",
            evidence_fingerprint,
            "mark_speaker",
            {"speaker_entity_id": speaker_entity_id},
            reason=reason,
        )
    console.print(
        _override_line("Evidence speaker marked", "Speaker override recorded", override_id, note)
    )


@graph_app.command("sensitivity")
def graph_change_sensitivity(
    target_type: Annotated[str, typer.Argument(help="entity | alias | claim | evidence")],
    target_key: Annotated[
        str, typer.Argument(help="Entity ID or stable alias/claim/evidence fingerprint")
    ],
    sensitivity: Annotated[
        str, typer.Argument(help="public | personal | sensitive | unclassified")
    ],
    reason: Annotated[str | None, typer.Option(help="Why you overruled Varyz")] = None,
    config: ConfigOpt = None,
) -> None:
    """Persist a sensitivity correction and its policy-label provenance."""
    from varyz.domain import Sensitivity
    from varyz.graph.overrides import append_override

    if target_type not in {"entity", "alias", "claim", "evidence"}:
        console.print("[red]Target type must be entity, alias, claim, or evidence.[/red]")
        raise typer.Exit(2)
    _refuse_an_id_nothing_could_ever_match("a target to correct", target_key)
    try:
        normalized_sensitivity = Sensitivity(sensitivity)
    except ValueError:
        console.print("[red]Invalid sensitivity.[/red]")
        raise typer.Exit(2) from None
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        note = _override_note(
            conn,
            entity_id=target_key if target_type == "entity" else None,
            fingerprint=None if target_type == "entity" else target_key,
            fingerprint_kind=target_type,
        )
        override_id = append_override(
            conn,
            target_type,
            target_key,
            "change_sensitivity",
            {"sensitivity": normalized_sensitivity.value},
            reason=reason,
        )
    console.print(
        _override_line("Sensitivity changed", "Sensitivity override recorded", override_id, note)
    )


@graph_app.command("assert")
def graph_user_assertion(
    subject_entity_id: Annotated[str, typer.Argument(help="Entity the fact is about")],
    predicate: Annotated[str, typer.Argument(help="What the fact says, e.g. works_at")],
    object_value: Annotated[str, typer.Argument(help="What the predicate points at")],
    object_type: Annotated[
        str, typer.Option(help="text | entity | date | number (default text)")
    ] = "text",
    valid_from: Annotated[str | None, typer.Option(help="When it became true (ISO-8601)")] = None,
    valid_to: Annotated[
        str | None, typer.Option(help="When it stopped being true (ISO-8601)")
    ] = None,
    sensitivity: Annotated[
        str, typer.Option(help="public | personal | sensitive (default personal)")
    ] = "personal",
    config: ConfigOpt = None,
) -> None:
    """Record a user-authored fact with explicit provenance."""
    from varyz.domain import Sensitivity
    from varyz.graph.overrides import append_override

    try:
        Sensitivity(sensitivity)
    except ValueError:
        console.print("[red]Invalid sensitivity.[/red]")
        raise typer.Exit(2) from None
    cfg = _cfg(config)
    conn = _db(cfg)
    active = conn.execute("SELECT id FROM graph_runs WHERE state='active'").fetchone()
    if active is None:
        console.print("[red]Build the graph before adding an assertion.[/red]")
        # An assertion is a claim in the graph, so there has to be a graph to
        # put it in -- and the reader who has just been refused is the one
        # person who needs the command that makes one.
        console.print("[dim]Run: varyz graph rebuild[/dim]")
        raise typer.Exit(1)
    # This is the one command that writes a claim out of nothing, so every
    # field in it is typed by hand -- and each wrong one came back as a
    # traceback. An entity id the graph does not have raised `IntegrityError:
    # FOREIGN KEY constraint failed`; a predicate outside the registry raised
    # `ValueError: user assertion predicate is not registered`, without saying
    # which predicates there are.
    if conn.execute("SELECT 1 FROM entities WHERE id=?", (subject_entity_id,)).fetchone() is None:
        console.print(f"[red]No entity {subject_entity_id} in the graph.[/red]")
        raise typer.Exit(2)
    known = [
        row[0]
        for row in conn.execute(
            "SELECT predicate FROM predicate_registry "
            "WHERE version='1' AND active=1 ORDER BY predicate"
        )
    ]
    if predicate not in known:
        console.print(f"[red]Unknown predicate {predicate!r}.[/red]")
        console.print(f"[dim]Known: {', '.join(known)}[/dim]")
        raise typer.Exit(2)
    if not object_value.strip():
        # A shell that expanded a variable to nothing filed "<entity> lived_in"
        # with no object, as a fact carrying the owner's own provenance -- the
        # highest-trust row in the graph, saying nothing.
        console.print("[red]An assertion needs an object: what is being said about it.[/red]")
        raise typer.Exit(2)
    payload = {
        "subject_entity_id": subject_entity_id,
        "predicate": predicate,
        "object_type": object_type,
        "object_value": object_value,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "sensitivity": sensitivity,
    }
    try:
        with WriteLock(cfg.data_dir):
            override_id = append_override(
                conn,
                "claim",
                f"user:{subject_entity_id}:{predicate}:{object_value}",
                "user_assertion",
                payload,
            )
    except ValueError as exc:
        # --valid-from, --valid-to and --sensitivity are checked where the
        # claim is written, and reached the person as a traceback too.
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    console.print(f"User assertion recorded. Override: {override_id}")


@privacy_app.command("doctor")
def privacy_doctor(config: ConfigOpt = None) -> None:
    """Check labels, rules, grants, receipts, and provider bypass paths."""
    from varyz.privacy.doctor import diagnose

    conn = _db(_cfg(config))
    issues = diagnose(conn, source_root=Path(__file__).parent)
    if not issues:
        console.print("[green]Privacy boundary is healthy.[/green]")
        return
    table = Table("severity", "code", "count", "detail")
    for issue in issues:
        table.add_row(issue.severity, issue.code, str(issue.count), issue.message)
    console.print(table)
    if any(issue.severity == "error" for issue in issues):
        raise typer.Exit(1)


@privacy_app.command("release")
def privacy_release(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Only this source (repeatable); omit for all"),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Lower the labels; without it, only count")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Re-label entries held back by a rule you have since changed.

    A sensitive label never drops on sync, so changing a source default or a
    pattern protects new material only. This applies today's rules to what is
    already held back and lowers only what they no longer catch.
    """
    from varyz.pipeline.sensitivity import release

    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        result = release(
            conn, SensitivityGate(cfg.sensitivity), sources=tuple(source or ()), apply=apply
        )
    table = Table(
        "source", Column("still held back", justify="right"),
        Column("released" if apply else "would be released", justify="right"),
        title="Sensitive entries under today's rules",
    )
    for name in sorted(set(result.held) | set(result.released)):
        table.add_row(
            name, f"{result.held.get(name, 0):,}", f"{result.released.get(name, 0):,}"
        )
    console.print(table)
    if not apply and result.released:
        console.print("Nothing changed. Add --apply to lower them.")


@privacy_app.command("receipts")
def privacy_receipts(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many to show")] = 20,
    config: ConfigOpt = None,
) -> None:
    """Show recent disclosure attempts without duplicating their payloads."""
    conn = _db(_cfg(config))
    rows = conn.execute(
        """SELECT r.id, r.created_at, q.purpose, q.destination,
                  r.payload_item_count, r.payload_char_count,
                  r.decision_summary_json,
                  (SELECT state FROM egress_executions x
                    WHERE x.receipt_id=r.id ORDER BY attempt DESC LIMIT 1) AS state
           FROM egress_receipts r JOIN context_requests q ON q.id=r.request_id
           ORDER BY r.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        console.print("No egress receipts yet.")
        return
    table = Table("time", "purpose", "destination", "sent", "state", Column("receipt", **ID_COLUMN))
    for row in rows:
        table.add_row(
            local_stamp(row["created_at"]),
            row["purpose"],
            row["destination"],
            f"{row['payload_item_count']} · {row['payload_char_count']} chars",
            row["state"] or "prepared",
            row["id"],
        )
    console.print(table)


@privacy_app.command("verify")
def privacy_verify(
    receipt_id: Annotated[str, typer.Argument(help="Receipt id `receipts` shows")],
    payload_hash: Annotated[str, typer.Argument(help="sha256 of the prepared payload")],
    config: ConfigOpt = None,
) -> None:
    """Verify a receipt against a known prepared payload hash."""
    from varyz.privacy.receipts import ReceiptStore

    conn = _db(_cfg(config))
    # Reported "No receipt with id ." for `varyz privacy verify "$R" "$H"` with
    # the variables unset -- a verdict on a receipt nobody named. This command
    # exists to answer whether a disclosure matched its record, so the one
    # thing it must never do is give that answer about nothing.
    missing = [
        name
        for name, value in (("a receipt id", receipt_id), ("a payload hash", payload_hash))
        if not value.strip()
    ]
    if missing:
        console.print(f"[red]Nothing to verify without {' and '.join(missing)}.[/red]")
        raise typer.Exit(2)
    if ReceiptStore(conn).verify(receipt_id, payload_hash):
        console.print("[green]Receipt verifies.[/green]")
        return
    # A mistyped id and a payload that does not match the receipt are the same
    # sentence and very different news. The second says something disclosed was
    # not what the receipt recorded, which is the whole point of keeping one.
    known = conn.execute("SELECT 1 FROM egress_receipts WHERE id=?", (receipt_id,)).fetchone()
    if known is None:
        console.print(f"[red]No receipt with id {escape(receipt_id)}.[/red]")
        console.print("[dim]The ids are in: varyz privacy receipts[/dim]")
    else:
        console.print("[red]That receipt exists, but its payload hash is not this one.[/red]")
    raise typer.Exit(1)


@privacy_app.command("revoke")
def privacy_revoke(
    grant_id: Annotated[str, typer.Argument(help="Id `grant` printed when it created it")],
    config: ConfigOpt = None,
) -> None:
    """Revoke a consent grant for all future use."""
    from varyz.privacy.receipts import ReceiptStore

    # `varyz privacy revoke "$ID"` with the variable unset answered "Grant was
    # not active or does not exist" -- a sentence about a grant that was looked
    # up, when nothing was ever named. Revoking is the command someone runs in
    # a hurry, and it has to be clear which of the two happened.
    if not grant_id.strip():
        console.print("[red]Nothing to revoke: the grant id is empty.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir):
        revoked = ReceiptStore(conn).revoke_grant(grant_id)
    if not revoked:
        console.print("[red]Grant was not active or does not exist.[/red]")
        raise typer.Exit(1)
    console.print("Consent revoked.")


def _refuse_a_scope_nothing_would_match(what: str, value: str, known: frozenset[str]) -> None:
    """Stop a grant whose destination or purpose no request could ever carry."""
    if value in known:
        return
    import difflib

    if not value.strip():
        console.print(f"[red]A grant needs a {what}.[/red]")
    else:
        console.print(f"[red]{escape(value)} is not a {what} Varyz recognizes.[/red]")
        close = difflib.get_close_matches(value, sorted(known), n=1, cutoff=0.6)
        if close:
            console.print(f"[dim]Did you mean {close[0]}?[/dim]")
    console.print(f"[dim]The {what}s: {', '.join(sorted(known))}[/dim]")
    raise typer.Exit(2)


@privacy_app.command("grant")
def privacy_grant(
    destination: Annotated[str, typer.Argument(help="e.g. export:file")],
    purpose: Annotated[str, typer.Argument(help="e.g. export")],
    minutes: Annotated[int, typer.Option(min=1, help="Grant lifetime")] = 30,
    request_id: Annotated[str | None, typer.Option(help="Limit to one request")] = None,
    conversation_id: Annotated[str | None, typer.Option(help="Limit to one conversation")] = None,
    resource_type: Annotated[str | None, typer.Option(help="Limit to one kind of resource")] = None,
    resource_id: Annotated[
        str | None, typer.Option(help="Limit to one resource; needs --resource-type")
    ] = None,
    entity_id: Annotated[str | None, typer.Option(help="Limit to one entity")] = None,
    topic: Annotated[str | None, typer.Option(help="Limit to one topic")] = None,
    scope_from: Annotated[str | None, typer.Option(help="Earliest content time (ISO-8601)")] = None,
    scope_to: Annotated[str | None, typer.Option(help="Latest content time (ISO-8601)")] = None,
    max_items: Annotated[
        int | None, typer.Option(min=0, help="Most items this grant may ever release")
    ] = None,
    max_chars: Annotated[
        int | None, typer.Option(min=0, help="Most characters this grant may ever release")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Create a destination-, purpose-, time-, and budget-scoped consent grant."""
    from datetime import datetime, timedelta

    from varyz.domain import new_id, utc_now
    from varyz.privacy.models import ConsentGrant
    from varyz.privacy.policy import KNOWN_DESTINATIONS, KNOWN_PURPOSES, KNOWN_RESOURCE_TYPES
    from varyz.privacy.receipts import ReceiptStore

    # A grant is matched on its exact destination and purpose, so one naming
    # neither -- or naming a destination with a typo in it -- can never
    # authorize anything. Both were accepted and reported as created, which
    # reads as consent given for a disclosure that will still be refused.
    _refuse_a_scope_nothing_would_match("destination", destination, KNOWN_DESTINATIONS)
    _refuse_a_scope_nothing_would_match("purpose", purpose, KNOWN_PURPOSES)
    # The narrower scopes were never held to the same standard. A resource type
    # Varyz labels nothing with is refused at the item, so a grant carrying one
    # authorizes nothing and was still reported as created; and the two shapes
    # the stored model refuses outright -- an id with no type beside it, a
    # content window that ends before it starts -- reached the owner as a
    # pydantic traceback over a typo in one flag, on the one surface where
    # Varyz is asking to be trusted about what leaves this machine.
    if resource_type is not None:
        _refuse_a_scope_nothing_would_match("resource type", resource_type, KNOWN_RESOURCE_TYPES)
    elif resource_id is not None:
        console.print(
            "[red]--resource-id names one resource of a kind, so Varyz needs "
            "--resource-type beside it to know which kind.[/red]"
        )
        console.print(f"[dim]The resource types: {', '.join(sorted(KNOWN_RESOURCE_TYPES))}[/dim]")
        raise typer.Exit(2)

    cfg = _cfg(config)
    conn = _db(cfg)
    try:
        scope_valid_from = datetime.fromisoformat(scope_from) if scope_from else None
        scope_valid_to = datetime.fromisoformat(scope_to) if scope_to else None
    except ValueError as exc:
        raise typer.BadParameter("content scope times must be ISO-8601") from exc
    if scope_valid_from and scope_valid_to and scope_valid_to < scope_valid_from:
        console.print(
            "[red]--scope-to is earlier than --scope-from, so this grant would "
            "cover no content at all.[/red]"
        )
        raise typer.Exit(2)
    grant = ConsentGrant(
        id=new_id(),
        actor="local_user",
        destination=destination,
        purpose=purpose,
        request_id=request_id,
        conversation_id=conversation_id,
        resource_type=resource_type,
        resource_id=resource_id,
        entity_id=entity_id,
        topic=topic,
        scope_valid_from=scope_valid_from,
        scope_valid_to=scope_valid_to,
        valid_from=utc_now(),
        valid_until=utc_now() + timedelta(minutes=minutes),
        max_items=max_items,
        max_chars=max_chars,
    )
    with WriteLock(cfg.data_dir):
        ReceiptStore(conn).create_grant(grant)
    console.print(
        f"Consent grant {grant.id} created for {destination} / {purpose}; "
        f"expires {grant.valid_until.isoformat(timespec='minutes')}."
    )


@todo_app.command("add")
def todo_add(
    text: Annotated[str, typer.Argument(help="The commitment in your own words")],
    config: ConfigOpt = None,
) -> None:
    """Record one commitment in your own words."""
    if not text.strip():
        console.print("[red]A todo needs text.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir), conn:
        todo_id = dao.create_todo(conn, text)
    console.print(f"TODO added [dim]{todo_id}[/dim]")


@todo_app.command("list")
def todo_list(
    status: Annotated[str, typer.Option(help="open | done")] = "open",
    config: ConfigOpt = None,
) -> None:
    """Show the open commitments, or another status with --status."""
    cfg = _cfg(config)
    conn = _db(cfg)
    rows = dao.list_todos(conn, status)
    if not rows:
        console.print(f"No {status} todos.")
        return
    table = Table()
    table.add_column("due", style="cyan")
    table.add_column("todo")
    table.add_column("id", **ID_COLUMN)
    for r in rows:
        table.add_row(r["due"] or "", r["text"], r["id"])
    console.print(table)


@todo_app.command("done")
def todo_done(
    todo_id: Annotated[str, typer.Argument(help="Short id `list` shows")],
    config: ConfigOpt = None,
) -> None:
    """Close one commitment by the id `list` shows."""
    # `varyz todo done "$ID"` with an unset variable said "No todo with that
    # id", which reads as a lookup that ran. It is the same nothing `varyz todo
    # add ""` already refuses.
    if not todo_id.strip():
        console.print("[red]Nothing to close: the todo id is empty.[/red]")
        raise typer.Exit(2)
    cfg = _cfg(config)
    conn = _db(cfg)
    with WriteLock(cfg.data_dir), conn:
        ok = dao.complete_todo(conn, todo_id)
    if ok:
        console.print("Done.")
        return
    # Closing a todo that is already closed is the outcome the caller asked
    # for; an id naming nothing is a typo. Both printed the same red line and
    # exited 0, so a script could not tell one from the other -- and neither
    # could a person who mistyped an id.
    row = conn.execute("SELECT status FROM todos WHERE id=?", (todo_id,)).fetchone()
    if row is not None:
        console.print(f"Already {row[0]}.")
        return
    console.print("[red]No todo with that id.[/red]")
    raise typer.Exit(1)


def _refuse_a_name_nothing_reads(name: str) -> None:
    """A secret stored under a name varyz never looks up is a silent dead end.

    Windows Credential Manager takes any string, so a key typed under
    "anthropic" or under the environment variable's own name was accepted,
    kept, and then reported by `varyz secrets show` as present.
    """
    from varyz import secrets as secret_store

    if secret_store.is_known_secret(name):
        return
    console.print(f"[red]varyz never reads a secret called {escape(name)!r}.[/red]")
    suggestion = secret_store.suggest_secret(name)
    if suggestion:
        console.print(f"[dim]Did you mean {suggestion}?[/dim]")
    else:
        console.print(
            "[dim]Names varyz reads: "
            + ", ".join(secret_store.KNOWN_SECRETS)
            + ". Connector logins are written by varyz connect.[/dim]"
        )
    raise typer.Exit(2)


@secrets_app.command("set")
def secrets_set(name: Annotated[str, typer.Argument(help="e.g. anthropic_api_key")]) -> None:
    """Store one secret in the OS credential store; the value is never echoed."""
    from varyz import secrets as secret_store

    # A hidden prompt reads the Windows console directly, so redirecting stdin
    # does not answer it: called from a script, this waited forever at a
    # console nobody was watching. Say so and stop instead.
    _refuse_a_name_nothing_reads(name)
    _require_tty("Storing a secret", why="the value is typed, never echoed")
    value = typer.prompt(f"Value for '{name}'", hide_input=True)
    secret_store.set_secret(name, value)
    console.print(f"Stored '{name}' in the OS credential store.")


@secrets_app.command("show")
def secrets_show(name: Annotated[str, typer.Argument(help="e.g. anthropic_api_key")]) -> None:
    """Say where one secret comes from. The value itself is never printed."""
    from varyz import secrets as secret_store

    _refuse_a_name_nothing_reads(name)
    source = secret_store.secret_source(name)
    if source == "store":
        console.print(f"'{name}': in the OS credential store.")
    elif source == "env":
        env = secret_store.ENV_FALLBACKS.get(name, "the environment")
        console.print(f"'{name}': from {env} in this shell, which ends when the shell does.")
    else:
        console.print(f"'{name}': not set.")
        raise typer.Exit(1)


@secrets_app.command("forget")
def secrets_forget(name: Annotated[str, typer.Argument(help="e.g. anthropic_api_key")]) -> None:
    """Delete one secret from the OS credential store.

    `varyz secrets set` could write a key and nothing could take it back: a
    value typed into the wrong name, or a key that has since been revoked at
    the provider, stayed in Windows Credential Manager with no command to
    remove it.
    """
    from varyz import secrets as secret_store

    # Deleting under a name varyz never reads is deliberate -- an older version
    # may have written one. The empty string is not that: it is what a shell
    # hands this command when `$NAME` is unset, and answering it with a
    # sentence about a secret called '' and an exit code of 0 reports a
    # credential removed that was never named.
    if not name.strip():
        console.print("[red]Nothing to forget: the secret name is empty.[/red]")
        raise typer.Exit(2)
    stored = secret_store.secret_source(name) == "store"
    secret_store.delete_secret(name)
    if stored:
        console.print(f"Deleted '{name}' from the OS credential store.")
    else:
        from varyz import secrets as known

        if known.is_known_secret(name):
            console.print(f"'{name}' was not in the OS credential store; nothing to delete.")
        else:
            # Deleting under an unread name still runs, so a value an older
            # version wrote can be cleaned up -- but "nothing to delete" would
            # have implied varyz had somewhere to look.
            console.print(f"varyz never reads a secret called '{name}', and none was stored.")
    # An exported variable is not ours to unset, and reporting a deletion while
    # the key still answers would be the same lie in the other direction.
    if secret_store.secret_source(name) == "env":
        env = secret_store.ENV_FALLBACKS.get(name, "an environment variable")
        console.print(
            f"[yellow]{env} is still set in this shell, so varyz can still read it. "
            "Unset it there.[/yellow]"
        )


def ensure_config_section(cfg_path: Path, section: str, content: str) -> bool:
    from varyz.configfile import ensure_section

    return ensure_section(cfg_path, section, content)


def _config_path(config: Path | None) -> Path:
    from varyz.config import find_config_file

    path = find_config_file(config)
    if path is None or not path.exists():
        console.print("[red]No config.toml found. Run: varyz setup[/red]")
        raise typer.Exit(1)
    return path


def _confirm(prompt: str, *, instead: str, default: bool = False) -> bool:
    """Ask, or -- where nothing answers -- name the flag that answers in advance.

    click sees EOF on a pipe, a scheduled task or a double-clicked window and
    raises Abort, which prints a bare "Aborted." -- so a command that was
    waiting for consent it could not collect looked broken instead. Every one
    of these prompts has a flag that answers it beforehand; the three
    `varyz connect` commands already say so and the rest did not.
    """
    try:
        return typer.confirm(prompt, default=default)
    except typer.Abort:
        console.print("[red]Nothing was approved: the question went unanswered.[/red]")
        console.print(f"[dim]{instead}[/dim]")
        raise typer.Exit(1) from None


def _require_tty(what: str, why: str = "browser sign-in") -> None:
    import sys

    if not sys.stdin.isatty():
        console.print(
            f"[red]{what} needs an interactive terminal[/red] ({why}).\n"
            "Open PowerShell in the project folder and run the command there."
        )
        raise typer.Exit(1)


def _api_key_value(label: str, env_var: str) -> str | None:
    """The key to store, or None when the environment already supplies it.

    varyz reads these keys from the environment when the credential store has
    none, so a machine that exports one has already answered what this prompt
    asks. Where nothing is exported and nothing can be typed, name the variable:
    every other `varyz connect` command answers a non-interactive terminal with
    the way to pass the value, and these three echoed a prompt at a terminal
    that could not fill it and then printed a bare "Aborted."
    """
    import os
    import sys

    if os.environ.get(env_var):
        console.print(f"Using the key already in [bold]{env_var}[/bold].")
        return None
    if not sys.stdin.isatty():
        console.print(
            "[red]No interactive terminal available for prompts.[/red]\n"
            f"Set [bold]{env_var}[/bold] in the environment instead:\n"
            f'  $env:{env_var} = "…"   (PowerShell)\n'
            f"  export {env_var}=…     (bash)"
        )
        raise typer.Exit(1)
    return typer.prompt(label, hide_input=True).strip()


def _set_llm_provider(cfg_path: Path, provider: str) -> None:
    from varyz.configfile import set_provider

    set_provider(cfg_path, provider)


@connect_app.command("claude")
def connect_claude(
    api_key: Annotated[
        bool, typer.Option("--api-key", help="Use an Anthropic API key instead of OAuth")
    ] = False,
    make_default: Annotated[
        bool, typer.Option("--default/--no-default", help="Make this the active provider")
    ] = True,
    config: ConfigOpt = None,
) -> None:
    """Sign in with your Claude.ai account (OAuth) — uses your subscription."""
    from varyz import secrets as secret_store

    cfg_path = _config_path(config)
    if api_key:
        value = _api_key_value("Anthropic API key", "ANTHROPIC_API_KEY")
        if value is not None:
            secret_store.set_secret("anthropic_api_key", value)
            console.print("[green]API key stored.[/green]")
    else:
        _require_tty("Claude sign-in")
        from varyz.llm.claude import account_label, login
        from varyz.llm.oauth import OAuthError

        try:
            login(console)
        except OAuthError as exc:
            console.print(f"[red]Sign-in failed: {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        who = account_label()
        console.print(f"[green]Signed in with Claude{f' as {who}' if who else ''}.[/green]")
    if make_default:
        _set_llm_provider(cfg_path, "claude")
        console.print("Active provider: [bold]claude[/bold]")


@connect_app.command("openai")
def connect_openai(
    api_key: Annotated[
        bool, typer.Option("--api-key", help="Use an OpenAI API key instead of OAuth")
    ] = False,
    make_default: Annotated[
        bool, typer.Option("--default/--no-default", help="Make this the active provider")
    ] = True,
    config: ConfigOpt = None,
) -> None:
    """Sign in with your ChatGPT account (OAuth) — uses your subscription."""
    from varyz import secrets as secret_store

    cfg_path = _config_path(config)
    if api_key:
        value = _api_key_value("OpenAI API key", "OPENAI_API_KEY")
        if value is not None:
            secret_store.set_secret("openai_api_key", value)
            console.print("[green]API key stored.[/green]")
    else:
        _require_tty("ChatGPT sign-in")
        from varyz.llm.oauth import OAuthError
        from varyz.llm.openai_oauth import account_label, login

        try:
            login(console)
        except OAuthError as exc:
            console.print(f"[red]Sign-in failed: {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        who = account_label()
        console.print(f"[green]Signed in with ChatGPT{f' as {who}' if who else ''}.[/green]")
    if make_default:
        _set_llm_provider(cfg_path, "openai")
        console.print("Active provider: [bold]openai[/bold]")


@connect_app.command("deepseek")
def connect_deepseek(
    make_default: Annotated[
        bool, typer.Option("--default/--no-default", help="Make this the active provider")
    ] = True,
    config: ConfigOpt = None,
) -> None:
    """Store a DeepSeek API key (platform.deepseek.com -> API keys)."""
    from varyz import secrets as secret_store

    cfg_path = _config_path(config)
    value = _api_key_value("DeepSeek API key", "DEEPSEEK_API_KEY")
    if value is not None:
        secret_store.set_secret("deepseek_api_key", value)
        console.print("[green]DeepSeek key stored.[/green]")
    if make_default:
        _set_llm_provider(cfg_path, "deepseek")
        console.print("Active provider: [bold]deepseek[/bold]")


@connect_app.command("status")
def connect_status(config: ConfigOpt = None) -> None:
    """Show which LLM provider is active and what's signed in."""
    from varyz import secrets as secret_store
    from varyz.llm import claude, openai_oauth

    cfg = _cfg(config)
    table = Table(title="LLM providers")
    table.add_column("provider")
    table.add_column("auth")
    table.add_column("account / status")
    table.add_column("active", justify="center")

    # A key exported into the shell counts as connected -- varyz reads it -- but
    # it is gone when that terminal closes, and a table that prints the same
    # "API key" for both tells someone their machine is set up when it is not.
    def _key_auth(name: str) -> str:
        source = secret_store.secret_source(name)
        if source == "env":
            return f"API key ({secret_store.ENV_FALLBACKS[name]})"
        return "API key" if source else "-"

    claude_oauth = claude.token_store().load()
    claude_auth = "OAuth" if claude_oauth else _key_auth("anthropic_api_key")
    claude_status = claude.account_label() or (
        "connected" if claude_auth != "-" else "not connected"
    )
    table.add_row("claude", claude_auth, claude_status, "●" if cfg.llm.provider == "claude" else "")

    oa_oauth = openai_oauth.token_store().load()
    oa_auth = "OAuth" if oa_oauth else _key_auth("openai_api_key")
    oa_status = openai_oauth.account_label() or ("connected" if oa_auth != "-" else "not connected")
    table.add_row("openai", oa_auth, oa_status, "●" if cfg.llm.provider == "openai" else "")

    ds = _key_auth("deepseek_api_key")
    table.add_row(
        "deepseek",
        ds,
        "connected" if ds != "-" else "not connected",
        "●" if cfg.llm.provider == "deepseek" else "",
    )
    console.print(table)

    # The dot marks the provider varyz would call, not one that would answer.
    # A machine with only OPENAI_API_KEY exported read as set up: a connected
    # row, a dot on another row, and nothing saying they were different lines.
    from varyz.llm.providers import provider_overview

    active = cfg.llm.provider
    if any(not row["connected"] and row["active"] for row in provider_overview(cfg)):
        console.print(f"\n[yellow]The active provider, {active}, is not connected.[/yellow]")
        _say_which_providers_are_connected(cfg)
        if not any(row["connected"] for row in provider_overview(cfg)):
            console.print(f"[dim]Sign in with: varyz connect {active}[/dim]")

    from varyz.llm.local import build_local_provider

    local_provider = build_local_provider(cfg)
    console.print(
        f"\n[dim]Local distillation ({cfg.local_model.backend} "
        f"{local_provider.model}): {local_provider.status()} "
        f"· mode={cfg.retrieval.distill}[/dim]"
    )


def connect_gmail(
    account: Annotated[
        str | None, typer.Option("--account", help="Gmail address (skips the prompt)")
    ] = None,
    app_password: Annotated[
        str | None,
        typer.Option(
            "--app-password",
            help="16-char app password (skips the prompt; spaces are stripped)",
            envvar="GMAIL_APP_PASSWORD",
        ),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Link a Gmail account via IMAP app password (no OAuth, no exports)."""
    import sys

    from imapclient import IMAPClient

    from varyz import secrets as secret_store
    from varyz.connectors.gmail_imap import password_secret_name

    cfg_path = _config_path(config)
    if not (account and app_password) and not sys.stdin.isatty():
        console.print(
            "[red]No interactive terminal available for prompts.[/red]\n"
            "Pass credentials directly:\n"
            '  varyz connect gmail --account you@gmail.com --app-password "xxxx xxxx xxxx xxxx"\n'
            "(create the app password at https://myaccount.google.com/apppasswords -\n"
            " requires 2-step verification on the account)"
        )
        raise typer.Exit(1)
    if not account or not app_password:
        console.print(
            "Gmail setup (one time):\n"
            "  1. Turn on 2-step verification for the account (required for app passwords)\n"
            "  2. Create an app password: [bold]https://myaccount.google.com/apppasswords[/bold]\n"
        )
    account = account or typer.prompt("Gmail address")
    password = (app_password or typer.prompt("App password", hide_input=True)).replace(" ", "")

    console.print("Verifying IMAP login…")
    try:
        with IMAPClient("imap.gmail.com", ssl=True) as client:
            client.login(account, password)
    except Exception as exc:
        console.print(f"[red]Login failed: {escape(str(exc))}[/red]")
        console.print("Check that IMAP is allowed and the app password is fresh.")
        raise typer.Exit(1) from None
    secret_store.set_secret(password_secret_name(account), password)

    added = ensure_config_section(
        cfg_path,
        "gmail",
        f'\n[connectors.gmail]\naccount = "{account}"\n'
        'folders = ["[Gmail]/All Mail"]\n'
        '# since = "2020-01-01"   # bound the FIRST backfill; omit for full history\n',
    )
    console.print(
        f"[green]Gmail linked as {account}.[/green] "
        + ("Config section added." if added else "Config section already present.")
    )
    console.print("Run [bold]varyz sync gmail[/bold] to start the backfill (resumable).")


def connect_calendar(
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help="Secret iCal/webcal subscription URL (skips the prompt)",
            envvar="VARYZ_CALENDAR_URL",
        ),
    ] = None,
    name: Annotated[
        str,
        typer.Option("--name", help="Local label for this calendar subscription"),
    ] = "default",
    config: ConfigOpt = None,
) -> None:
    """Link a calendar with one secret iCal subscription URL."""
    import re
    import sys

    from varyz import secrets as secret_store
    from varyz.configfile import upsert_connector
    from varyz.connectors.calendar_ics import (
        CalendarSubscriptionError,
        _request_url,
        subscription_secret_name,
    )

    cfg_path = _config_path(config)
    if not url and not sys.stdin.isatty():
        console.print(
            "[red]No interactive terminal available for prompts.[/red]\n"
            'Pass the private feed directly:\n  varyz connect calendar --url "webcal://…"'
        )
        raise typer.Exit(1)
    label = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        console.print("[red]Calendar name may contain only letters, numbers, ., _, and -.[/red]")
        raise typer.Exit(1)
    secret_url = (url or typer.prompt("Secret iCal/webcal URL", hide_input=True)).strip()
    try:
        _request_url(secret_url)
    except CalendarSubscriptionError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    secret_store.set_secret(subscription_secret_name(label), secret_url)
    upsert_connector(
        cfg_path,
        "calendar_import",
        {
            "approved": True,
            "account": label,
            "subscription_secret": label,
        },
    )
    console.print(
        f"[green]Calendar subscription {label} stored in the OS credential manager.[/green]"
    )
    console.print("Run [bold]varyz sync calendar_import[/bold] to import and refresh events.")


# Held back from the alpha wheel (docs/SHIPPING.md): these register only where
# their connector module is present, which is the source tree.
if connector_registry.is_shipped("gmail_imap"):
    connect_app.command("gmail")(connect_gmail)
if connector_registry.is_shipped("calendar_ics"):
    connect_app.command("calendar")(connect_calendar)


def _link_github(
    cfg_path: Path,
    login: str,
    token: str,
    *,
    api_url: str | None = None,
    affiliation: str = "owner",
) -> bool:
    """Keep a token GitHub accepted for ``login``; True if the config section was added.

    The token goes to the OS credential store, never to config.toml. Shared by
    `varyz connect github` and the browser setup page.
    """
    from varyz import secrets as secret_store
    from varyz.connectors.github import token_secret_name

    secret_store.set_secret(token_secret_name(login), token)
    section = (
        f'\n[connectors.github]\naccount = "{login}"\n'
        f'affiliation = "{affiliation}"   # widen to include org and collaborator repos\n'
        'visibility = "all"\n'
        "include_threads = true   # issues, PR descriptions, and review comments\n"
        "include_forks = false\n"
        '# since = "2024-01-01"   # bound the FIRST backfill of issues; omit for all history\n'
    )
    if api_url:
        section += f'api_url = "{api_url}"\n'
    return ensure_config_section(cfg_path, "github", section)


@connect_app.command("github")
def connect_github(
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Personal access token (skips the prompt)",
            envvar="GITHUB_TOKEN",
        ),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", help="GitHub Enterprise API base, e.g. https://gh.corp/api/v3"),
    ] = None,
    affiliation: Annotated[
        str,
        typer.Option(
            "--affiliation",
            help="owner | owner,collaborator | owner,collaborator,organization_member",
        ),
    ] = "owner",
    config: ConfigOpt = None,
) -> None:
    """Link GitHub with a personal access token — your repos, issues and PR threads."""
    import sys

    from varyz.connectors.github import DEFAULT_API_URL, token_from_gh, verify_token

    cfg_path = _config_path(config)
    base_url = api_url or DEFAULT_API_URL
    if not token:
        token = token_from_gh()
        if token:
            console.print("Using the existing [bold]gh auth[/bold] login.")
    if not token and not sys.stdin.isatty():
        console.print(
            "[red]No interactive terminal available for prompts.[/red]\n"
            "Pass the token directly:\n"
            "  varyz connect github --token ghp_xxx\n"
            "(create one at https://github.com/settings/tokens - a classic token needs\n"
            " the 'repo' scope for private repositories, or 'public_repo' for public only)"
        )
        raise typer.Exit(1)
    if not token:
        console.print(
            "GitHub setup (one time):\n"
            "  1. Open [bold]https://github.com/settings/tokens[/bold]\n"
            "  2. Generate a token with [bold]repo[/bold] scope "
            "(or [bold]public_repo[/bold] for public repositories only)\n"
            "  3. Paste it below - it goes to the OS credential store, never to config.toml\n"
        )
    secret = token or typer.prompt("Personal access token", hide_input=True)

    console.print("Verifying token…")
    try:
        login = verify_token(secret.strip(), api_url=base_url)
    except Exception as exc:
        console.print(f"[red]GitHub rejected the token: {escape(str(exc))}[/red]")
        console.print("Check that the token is current and has repository read access.")
        raise typer.Exit(1) from None
    added = _link_github(cfg_path, login, secret.strip(), api_url=api_url,
                         affiliation=affiliation)
    console.print(
        f"[green]Connected data source: GitHub, as {login}.[/green] "
        + ("Config section added." if added else "Config section already present.")
    )
    console.print(
        "Import it now with [bold]varyz sync github[/bold] (resumable); "
        "[bold]varyz sources[/bold] shows every source and its item count."
    )


@connect_app.command("ai-conversations")
def connect_ai_conversations(
    adapter: Annotated[
        list[str] | None,
        typer.Option("--adapter", help="Limit to specific agents (repeatable)"),
    ] = None,
    path: Annotated[
        list[Path] | None,
        typer.Option(
            "--path",
            help="Read these directories instead of the default locations (repeatable)",
        ),
    ] = None,
    include_assistant: Annotated[
        bool,
        typer.Option(help="Also import the assistant's replies as context"),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Ignore turns before this date (YYYY-MM-DD)"),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Approve local AI conversation history — the hero Personal Insight source."""
    from varyz.configfile import upsert_connector
    from varyz.connectors.ai_conversations import ADAPTERS_BY_ID, detect_adapters

    cfg_path = _config_path(config)
    roots = [p.expanduser().resolve() for p in (path or [])]
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        # Telegram and Instagram both refuse an export that is not there. This
        # one wrote the typo into config.toml, said "approved", and left the
        # discovery to fail quietly at the next sync.
        console.print(
            "[red]No directory at "
            + ", ".join(escape(str(r)) for r in missing)
            + ".[/red] Point --path at a folder of conversation history."
        )
        raise typer.Exit(1)
    if since is not None:
        # An unparseable date was stored verbatim and read back as None, so the
        # cut-off the user asked for silently did not happen and the whole
        # history was imported instead.
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            console.print(
                f"[red]--since {escape(since)} is not a date.[/red] Write it as YYYY-MM-DD."
            )
            raise typer.Exit(1) from None
    extra = [str(r) for r in roots]
    found = detect_adapters()

    if adapter:
        unknown = [a for a in adapter if a.lower() not in ADAPTERS_BY_ID]
        if unknown:
            console.print(
                f"[red]Unknown adapter(s): {', '.join(unknown)}.[/red] "
                f"Known: {', '.join(ADAPTERS_BY_ID)}"
            )
            raise typer.Exit(1)
        selected = [a.lower() for a in adapter]
    else:
        selected = [a.adapter_id for a, _ in found]

    if not selected and not extra:
        console.print(
            "[yellow]No AI conversation history found in the default locations "
            f"for {', '.join(ADAPTERS_BY_ID)}.[/yellow]"
        )
        console.print("Point Varyz at yours with: varyz connect ai-conversations --path <dir>")
        raise typer.Exit(1)

    console.print("Varyz will read, from these histories:")
    if extra:
        for root in extra:
            console.print(f"  {root}")
        console.print("  [dim]default locations are not read while --path is set[/dim]")
    else:
        for adapter_id, roots in ((a.adapter_id, r) for a, r in found if a.adapter_id in selected):
            console.print(f"  [bold]{ADAPTERS_BY_ID[adapter_id].label}[/bold]")
            for root in roots:
                console.print(f"    {root}")
    console.print(
        "\n  your turns"
        + (" and the assistant's replies" if include_assistant else "")
        + "\n  [dim]never: tool calls, tool results, command output, file contents, "
        "thinking blocks, or subagent transcripts[/dim]"
    )

    options: dict[str, object] = {
        "approved": True,
        "adapters": selected,
        "include_assistant": include_assistant,
    }
    if extra:
        options["paths"] = extra
    if since:
        options["since"] = since
    upsert_connector(cfg_path, "ai_conversations", options)
    console.print("\n[green]Connected data source: AI conversations.[/green]")
    console.print(
        "Import it now with [bold]varyz sync ai_conversations[/bold]; "
        "[bold]varyz sources[/bold] shows every source and its item count."
    )


def _configure_logging() -> None:
    """Send library logs to stderr at the level the caller asked for.

    Without a handler, Python's last-resort one prints warnings and drops
    everything below them, which is right for a command somebody is watching:
    a note about one unreadable window is worth a line, progress is not.

    A compile is the other case.  Its longest stage writes nothing to the
    database for tens of minutes, so a run that is working and a run that has
    hung look identical from outside, and the ``log.info`` calls that would
    tell them apart are exactly the ones the default drops.  The supervisor
    sets this; nothing else does, so no ordinary command gets noisier.
    """

    level = os.environ.get("VARYZ_LOG_LEVEL", "").strip().upper()
    if not level:
        return
    formatter = logging.Formatter(
        "%(asctime)sZ  %(name)s  %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    # This package's logger, not the root: an INFO root would also turn on
    # every HTTP request the local model server is sent, which is one line per
    # annotation and the archive's own text in the ones that fail.
    package = logging.getLogger("varyz")
    package.handlers = [handler]
    package.setLevel(getattr(logging, level, logging.WARNING))
    package.propagate = False


def main() -> None:
    """Entry point.

    Four things that end a command are ordinary conditions rather than bugs,
    and every command can meet one of them. Handling them once here beats
    a try/except around each of the fifteen: the alternative was a Python
    traceback, which on a tool somebody installed five minutes ago reads as a
    broken install rather than "something else is running".
    """
    from varyz.db.connection import NotAnArchiveError
    from varyz.llm.local_runtime import LocalModelNotInstalled
    from varyz.resolution.client import ResolutionClientError

    _configure_logging()
    try:
        app()
    except LockHeldError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise SystemExit(1) from None
    except NotAnArchiveError as exc:
        # A path pointed at the wrong file -- a stray varyz.db, half of a copy,
        # a sync conflict -- and every command, read ones included, ended in a
        # traceback that named sqlite rather than the file.
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise SystemExit(1) from None
    except LocalModelNotInstalled as exc:
        # `varyz graph rebuild` on a fresh install printed forty lines of Rich
        # traceback around this sentence. The sentence was always the whole
        # answer, and it names the command that fixes it.
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise SystemExit(1) from None
    except ResolutionClientError as exc:
        # Twenty-odd commands talk to the daemon, and with it stopped each one
        # printed a page of urllib traceback around "connection refused".
        if "unavailable" in str(exc):
            console.print(
                "[red]The resolution daemon is not running.[/red] "
                "Start it with `varyz daemon start`."
            )
        else:
            console.print(f"[red]{escape(str(exc))}[/red]")
        raise SystemExit(1) from None
    except DownloadPaused:
        console.print(
            "\n[yellow]Paused. What has downloaded is kept: run the same command "
            "again to carry on from there.[/yellow]"
        )
        raise SystemExit(SETUP_PAUSED) from None
    except KeyboardInterrupt:
        # A first sync takes minutes, so this is a button people press. What
        # has been ingested is committed and the cursor is saved; the next run
        # resumes.
        console.print("\n[yellow]Interrupted. Everything ingested so far is saved.[/yellow]")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

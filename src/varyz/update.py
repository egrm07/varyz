"""Keep Varyz up to date: say when a new version is out, and install it when asked.

The owner chose this on 2026-09-26: on their own machines Varyz updates itself,
checking once a day on the first use of the day. On 2026-10-26 they made
``[update] mode = "notify"`` the default for everyone: the same daily check, and
the agent tells the user once per new version. Nothing changes until they say
"update Varyz" (``varyz update apply``). ``mode = "off"`` stops the check.

Only a fast-forward is ever applied. A checkout with local changes, local
commits, a detached HEAD or no upstream is left exactly as it is. An update
that brings a database migration backs the database up first and is not
applied when the backup cannot be written; migrations have no way back. The new
code must import before it stays: otherwise the checkout returns to the commit
it came from. A running daemon notices its source changed and restarts on it
(see ``code_fingerprint``). Open agents need no restart either: each one's
Varyz connection restarts onto the new code between two requests (see
``update_worker``).

An installed Varyz (``uv tool install``) is updated by uv from where it was
installed: GitHub, or the folder it was built from, which is fast-forwarded
first when it is a clean git checkout. Its connected agents' Varyz files (the
routing skill, the reader agent, the hook launchers) are refreshed straight
after, so nobody runs ``varyz connect`` again. On Windows, where open agents
hold Varyz's files, the install runs in the background from outside them
(``update_worker``) and the command returns at once.

A Varyz from the release installers is compared with the latest release on
GitHub. Applying it downloads that release's installer for this computer and
checks it against the release's ``SHA256SUMS.txt``. On Windows the background
worker runs it silently; on a Mac it is opened, since it asks for an
administrator's password. Either way the archive is kept. An automatic check
never downloads or opens anything.

Every attempt is appended to ``data/autosync/updates.jsonl``. ``varyz update
pause`` (or the autosync kill switch) stops automatic updates.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from varyz import update_worker
from varyz.config import Config, load_config
from varyz.db.connection import FileLock
from varyz.diskspace import free_bytes
from varyz.domain import iso, local_day, utc_now

# Where Varyz is published while it is not on a package index.
REPOSITORY = "https://github.com/egrm07/varyz"
# The latest release, for installs made by the release installers.
LATEST_RELEASE = "https://api.github.com/repos/egrm07/varyz/releases/latest"
RELEASES_PAGE = f"{REPOSITORY}/releases/latest"
RELEASE_DOWNLOADS = f"{REPOSITORY}/releases/download/"
CHECKSUMS = "SHA256SUMS.txt"
CHECK_TIMEOUT = 10
# An installer is about 100 MB; anything far past that is not one.
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
# Room left for the download and for the installer's own backups after it.
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
# The distribution uv installs the `varyz` command from.
DISTRIBUTION = "varyzai"
MIGRATIONS = "src/varyz/db/migrations/"
DEPENDENCY_FILES = frozenset({"uv.lock", "pyproject.toml"})
GIT_TIMEOUT = 120
SYNC_TIMEOUT = 900
# The imports every long-lived Varyz process starts from. If the new code
# cannot get this far, nothing that runs it would.
SMOKE_IMPORTS = "import varyz.cli, varyz.mcp_server, varyz.autosync_daemon"

Step = Callable[[Path], str | None]
"""Runs one step in a checkout; returns why it failed, or None."""


@dataclass(frozen=True)
class UpdateResult:
    status: str
    """up_to_date, available, applied, started, opened, waiting, skipped, failed,
    paused, busy, off or dry_run. ``started``: the install runs in the background
    and records its own result (trigger ``background``) when done. ``opened``: a
    release installer was downloaded and opened; Varyz is replaced when the owner
    runs it."""

    detail: str
    trigger: str = "manual"
    checked_at: str = ""
    current: str | None = None
    target: str | None = None
    commits: int = 0
    migrations: bool = False
    dependencies: bool = False
    backup: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dir(cfg: Config) -> Path:
    return cfg.data_dir / "autosync"


def state_path(cfg: Config) -> Path:
    return _dir(cfg) / "update.json"


def log_path(cfg: Config) -> Path:
    return _dir(cfg) / "updates.jsonl"


def paused_path(cfg: Config) -> Path:
    return _dir(cfg) / "UPDATE_PAUSED"


def lock_path(cfg: Config) -> Path:
    return _dir(cfg) / "UPDATE.lock"


def backup_path(cfg: Config) -> Path:
    return _dir(cfg) / "pre-update.db"


def installers_path(cfg: Config) -> Path:
    return _dir(cfg) / "installers"


def pause(cfg: Config) -> None:
    target = paused_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"paused_at = {iso(utc_now())}\n", encoding="utf-8")


def resume(cfg: Config) -> None:
    paused_path(cfg).unlink(missing_ok=True)


def is_paused(cfg: Config) -> bool:
    """Either switch stops updates: this one, or the autosync kill switch."""
    return paused_path(cfg).is_file() or (_dir(cfg) / "PAUSED").is_file()


def load_state(cfg: Config) -> dict[str, Any]:
    try:
        value = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_due(cfg: Config, *, now: datetime | None = None) -> bool:
    """Is today's check still to come? Days are the owner's local days."""
    if cfg.update.mode == "off" or is_paused(cfg):
        return False
    attempted = load_state(cfg).get("attempted_on")
    return attempted != local_day(now or utc_now())


def maybe_check_on_use(cfg: Config, config_path: Path | None) -> str:
    """On the first use of the day, check in a detached process; never wait for it."""
    if not is_due(cfg):
        return "not_due"
    if config_path is None or not config_path.is_file():
        return "no_config"
    # Marked before the launch, so the next call a second later does not start
    # a second one. A check that then fails waits for tomorrow, or for
    # `varyz update check`.
    _mark_attempted(cfg)
    command = [
        sys.executable,
        "-m",
        "varyz.update",
        "--config",
        str(config_path.resolve()),
        "--trigger",
        "on-use",
    ]
    try:
        _launch(command, config_path.resolve().parent)
    except OSError:
        return "launch_failed"
    return "started"


def _launch(command: list[str], cwd: Path) -> None:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # A hidden console, not DETACHED_PROCESS: a process with no console at
        # all makes every console tool it runs open a terminal window of its own.
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            # Out of the caller's job where it allows that: an agent's shell may
            # end its job, and all it started, when the command returns.
            subprocess.Popen(command, **kwargs, creationflags=flags | int(
                getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)))
            return
        except OSError:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def checkout_root() -> Path | None:
    """The git checkout this code runs from, or None for an installed package."""
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def run(
    cfg: Config,
    *,
    trigger: str = "manual",
    apply: bool | None = None,
    dry_run: bool = False,
    root: Path | None = None,
    sync: Step | None = None,
    verify: Step | None = None,
    config_path: Path | None = None,
) -> UpdateResult:
    """Check the tracked branch and, when allowed, fast-forward to it.

    ``apply`` None follows the configured mode; ``varyz update check`` passes
    False and ``varyz update apply`` True. Automatic triggers do nothing while
    the mode is off. A dry run fetches nothing and changes nothing: it compares
    with what the last fetch saw.
    """
    now = utc_now()
    base: dict[str, Any] = {"trigger": trigger, "checked_at": iso(now) or ""}
    automatic = trigger != "manual"
    if automatic and cfg.update.mode == "off":
        return UpdateResult("off", "Updates are off: [update] mode = \"off\"", **base)
    if is_paused(cfg):
        return UpdateResult("paused", "Kill switch is engaged: varyz update resume", **base)
    if update_worker.updating(cfg.data_dir):
        return UpdateResult("busy", "Varyz is installing an update right now", **base)
    if apply is None:
        apply = cfg.update.mode == "auto"
    lock = FileLock(lock_path(cfg))
    if not lock.acquire():
        return UpdateResult("busy", "Another update is running", **base)
    try:
        root = root if root is not None else checkout_root()
        if root is None:
            result = _run_installed(
                cfg, base, apply=apply, dry_run=dry_run, automatic=automatic,
                config_path=config_path,
            )
        else:
            result = _run(
                cfg,
                base,
                apply=apply,
                dry_run=dry_run,
                automatic=automatic,
                root=root,
                sync=sync or _uv_sync,
                verify=verify or _imports,
            )
            if result.status == "applied":
                _refresh_agents(config_path, sys.executable)
                # Open agent connections restart onto the new code between requests.
                update_worker.record_installed(cfg.data_dir, base["checked_at"])
        if not dry_run:
            _record(cfg, result, now)
        return result
    finally:
        lock.release()


def _run(
    cfg: Config,
    base: dict[str, Any],
    *,
    apply: bool,
    dry_run: bool,
    automatic: bool,
    root: Path,
    sync: Step,
    verify: Step,
) -> UpdateResult:
    if shutil.which("git") is None:
        return UpdateResult("skipped", "git is not on PATH", **base)
    git = _Git(root)
    branch = git.out("rev-parse", "--abbrev-ref", "HEAD")
    if branch in (None, "HEAD"):
        return UpdateResult("skipped", "The checkout is not on a branch", **base)
    upstream = git.out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream is None:
        return UpdateResult(
            "skipped", f"Branch {branch} tracks no remote branch to update from", **base
        )
    if not dry_run:
        remote = git.out("config", f"branch.{branch}.remote") or "origin"
        if (error := git.run("fetch", "--quiet", "--no-tags", remote)) is not None:
            return UpdateResult("failed", f"Could not fetch {upstream}: {error}", **base)
    current = git.out("rev-parse", "HEAD")
    target = git.out("rev-parse", "@{u}")
    if current is None or target is None:
        return UpdateResult("failed", "Could not read the checkout's commits", **base)
    found = {**base, "current": current[:12], "target": target[:12]}
    if current == target:
        return UpdateResult("up_to_date", f"On the latest {upstream}", **found)
    if git.run("merge-base", "--is-ancestor", target, current) is None:
        return UpdateResult(
            "up_to_date", f"{branch} has local commits {upstream} does not have", **found
        )
    if git.run("merge-base", "--is-ancestor", current, target) is not None:
        return UpdateResult(
            "skipped",
            f"{branch} and {upstream} have diverged; only a fast-forward is applied",
            **found,
        )
    commits = int(git.out("rev-list", "--count", f"{current}..{target}") or 0)
    changed = (git.out("diff", "--name-only", current, target) or "").splitlines()
    migrations = any(name.startswith(MIGRATIONS) for name in changed)
    dependencies = any(name in DEPENDENCY_FILES for name in changed)
    found.update(commits=commits, migrations=migrations, dependencies=dependencies)
    brings = _brings(commits, migrations, dependencies)
    if dry_run:
        verb = "Would apply" if apply else "Available"
        return UpdateResult("dry_run", f"{verb}: {brings} from {upstream}", **found)
    if not apply:
        return UpdateResult(
            "available", f"{brings} on {upstream}: varyz update apply", **found
        )
    if git.out("status", "--porcelain", "--untracked-files=no"):
        return UpdateResult(
            "skipped", f"{brings} waiting; the checkout has local changes", **found
        )
    if dependencies and automatic and _libraries_locked_while_loaded():
        return UpdateResult(
            "waiting",
            f"{brings} waiting: new dependencies cannot replace ones open agent "
            "sessions hold. Close them, then run: varyz update apply",
            **found,
        )
    backup = None
    if migrations:
        backup, error = _backup_database(cfg)
        if error is not None:
            return UpdateResult(
                "skipped", f"{brings} waiting: the database backup failed: {error}", **found
            )
    if (error := git.run("merge", "--ff-only", "--quiet", target)) is not None:
        return UpdateResult("failed", f"The fast-forward failed: {error}", backup=backup, **found)
    failure = sync(root) if dependencies else None
    failure = failure or verify(root)
    if failure is not None:
        undone = git.run("reset", "--keep", current)
        if dependencies:
            sync(root)
        back = "returned to" if undone is None else f"could NOT return to ({undone})"
        return UpdateResult(
            "failed",
            f"The new code failed ({failure}); {back} {current[:12]}",
            backup=backup,
            **found,
        )
    return UpdateResult(
        "applied", f"Updated to {target[:12]}: {brings}", backup=backup, **found
    )


def install_source() -> dict[str, Any] | None:
    """What uv installed this Varyz from, per its tool receipt.

    ``{"git": url}``, ``{"directory": path}``, or only a name for a package
    index; None when uv did not install it as a tool.
    """
    try:
        receipt = tomllib.loads((Path(sys.prefix) / "uv-receipt.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for requirement in receipt.get("tool", {}).get("requirements", []):
        if isinstance(requirement, dict) and requirement.get("name") == DISTRIBUTION:
            return requirement
    return None


def describe_source(source: dict[str, Any] | None) -> str:
    if source is None:
        return "not installed with uv"
    if source.get("git"):
        return f"installed from {source['git']}"
    if source.get("directory"):
        return f"installed from the folder {source['directory']}"
    if (source.get("url") or source.get("path") or "").split("?", 1)[0].endswith(".whl"):
        return "installed by the release installer"
    return "installed from a package index"


def _installed_commit() -> str | None:
    try:
        text = importlib.metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
        return json.loads(text or "{}").get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, ValueError, AttributeError):
        return None


def _remote_commit(url: str, ref: str) -> str | None:
    done = _Git(Path.cwd())._call(["ls-remote", url, ref])
    if isinstance(done, str) or done.returncode != 0:
        return None
    first = done.stdout.split()
    return first[0] if first else None


def _installed_package() -> Path:
    return Path(__file__).resolve().parent


def _folder_differs(folder: Path) -> bool:
    """Whether the folder's Varyz source differs from the installed copy.

    Only files the build ships are compared, so a module the build leaves out
    (pyproject's source-exclude) is not a difference.
    """
    installed = _installed_package()
    source = folder / "src" / "varyz"
    try:
        project = tomllib.loads((folder / "pyproject.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return True
    backend = project.get("tool", {}).get("uv", {}).get("build-backend", {})
    excluded = {Path(name) for name in backend.get("source-exclude", [])}

    def shipped(root: Path, relative_to: Path) -> dict[Path, Path]:
        return {
            path.relative_to(relative_to): path
            for suffix in ("py", "sql", "toml", "md", "html", "js", "json", "txt")
            for path in root.rglob(f"*.{suffix}")
            if "__pycache__" not in path.parts
        }

    ours = {
        relative: path for relative, path in shipped(source, source).items()
        if Path("src/varyz") / relative not in excluded
    }
    theirs = shipped(installed, installed)
    if ours.keys() != theirs.keys():
        return True
    return any(ours[name].read_bytes() != theirs[name].read_bytes() for name in ours)


def _new_migrations(folder: Path | None) -> bool:
    """Whether installing would bring a database migration; True when unknown."""
    if folder is None:
        return True
    have = {path.name for path in (_installed_package() / "db" / "migrations").glob("*.sql")}
    coming = {path.name for path in (folder / MIGRATIONS).glob("*.sql")}
    return bool(coming - have)


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    name = "uv.exe" if os.name == "nt" else "uv"
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (folder / name).is_file():
            return str(folder / name)
    return None


def _reinstall() -> str | None:
    """Rebuild the installed Varyz from its source; returns why it failed, or None.

    `uv tool upgrade --reinstall` fetches GitHub again or rebuilds from the
    folder, and keeps whatever the install was given besides (the DirectML
    build of onnxruntime on Windows).
    """
    uv = _uv()
    if uv is None:
        return "uv was not found"
    try:
        done = subprocess.run(
            [uv, "tool", "upgrade", DISTRIBUTION, "--reinstall"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SYNC_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"uv: {type(exc).__name__}"
    if done.returncode == 0:
        return None
    lines = (done.stderr or done.stdout).strip().splitlines()
    return f"uv: {lines[-1] if lines else done.returncode}"


def _starts() -> str | None:
    """Import the installed code in a fresh interpreter, as every process will."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", SMOKE_IMPORTS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"import check: {type(exc).__name__}"
    if done.returncode == 0:
        return None
    lines = done.stderr.strip().splitlines()
    return f"import check: {lines[-1] if lines else done.returncode}"


def _refresh_agents(config_path: Path | None, python: str) -> None:
    """Refresh the connected agents with the new code, in a process of its own:
    this one still holds the old modules."""
    command = [python, "-m", "varyz.update", "--refresh-connections"]
    if config_path is not None:
        command += ["--config", str(config_path)]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            command,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )


def _nothing(_root: Path) -> None:
    return None


def _run_installed(
    cfg: Config,
    base: dict[str, Any],
    *,
    apply: bool,
    dry_run: bool,
    automatic: bool,
    config_path: Path | None,
) -> UpdateResult:
    source = install_source()
    if source is None:
        return UpdateResult(
            "skipped", "Varyz was not installed with uv; update it the way it was installed",
            **base,
        )
    if (source.get("url") or source.get("path") or "").split("?", 1)[0].endswith(".whl"):
        # Never a package-index update: the release installers own this install.
        return _run_release(cfg, base, apply=apply, dry_run=dry_run, automatic=automatic,
                            config_path=config_path)
    found: dict[str, Any] = dict(base)
    folder: Path | None = None
    backed_up: str | None = None
    if source.get("git"):
        url = str(source["git"])
        ref = str(source.get("rev") or source.get("tag") or source.get("branch") or "HEAD")
        current, target = _installed_commit(), _remote_commit(url, ref)
        if target is None:
            return UpdateResult("failed", f"Could not reach {url}", **base)
        found.update(current=(current or "")[:12] or None, target=target[:12])
        if current == target:
            return UpdateResult("up_to_date", f"On the latest {url}", **found)
        brings = f"a newer Varyz on {url}"
    elif source.get("directory"):
        folder = Path(str(source["directory"]))
        if not (folder / "pyproject.toml").is_file():
            return UpdateResult(
                "skipped", f"Varyz was installed from {folder}, which is not there now", **base
            )
        if (folder / ".git").exists():
            pulled = _run(
                cfg, base, apply=apply, dry_run=dry_run, automatic=automatic,
                root=folder, sync=_nothing, verify=_nothing,
            )
            if pulled.status == "failed":
                return pulled
            found.update(current=pulled.current, target=pulled.target, commits=pulled.commits)
            backed_up = pulled.backup
            if pulled.status in ("available", "dry_run") and not _folder_differs(folder):
                return UpdateResult(pulled.status, pulled.detail, **found)
            if automatic and _Git(folder).out("status", "--porcelain", "--untracked-files=no"):
                return UpdateResult(
                    "skipped",
                    f"{folder} has changes not committed; the installed Varyz waits for "
                    "`varyz update apply`",
                    **found,
                )
        if not _folder_differs(folder):
            return UpdateResult("up_to_date", f"The installed Varyz matches {folder}", **found)
        brings = f"the Varyz in {folder}"
    else:
        brings = "the latest Varyz from the package index"
    migrations = _new_migrations(folder)
    found.update(migrations=migrations)
    if dry_run:
        verb = "Would install" if apply else "Available"
        return UpdateResult("dry_run", f"{verb}: {brings}", **found)
    if not apply:
        return UpdateResult("available", f"{brings}: varyz update apply", **found)
    locked = _libraries_locked_while_loaded()
    if locked and automatic and (_uv() is None or update_worker.base_python(
            Path(sys.prefix)) is None):
        return UpdateResult(
            "waiting",
            f"{brings} waiting: open agent sessions hold the installed Varyz. Close "
            "them, then run: varyz update apply",
            **found,
        )
    backup = backed_up
    if migrations and backup is None:
        backup, error = _backup_database(cfg)
        if error is not None:
            return UpdateResult(
                "skipped", f"{brings} waiting: the database backup failed: {error}", **found
            )
    if locked:
        uv = _uv()
        handed = None if uv is None else _hand_over(
            cfg, config_path, [uv, "tool", "upgrade", DISTRIBUTION, "--reinstall"],
            what="uv", brings=brings, found=found, backup=backup,
        )
        if handed is not None:
            return handed
        if automatic:
            return UpdateResult(
                "waiting",
                f"{brings} waiting: open agent sessions hold the installed Varyz. Close "
                "them, then run: varyz update apply",
                backup=backup, **found,
            )
    # Agent connections pause while the files change, then restart onto them.
    with update_worker.holding(cfg.data_dir):
        error = _reinstall()
        if error is not None:
            return UpdateResult("failed", f"Could not install {brings}: {error}",
                                backup=backup, **found)
        if (error := _starts()) is not None:
            return UpdateResult(
                "failed", f"The new Varyz does not start ({error}); install it again",
                backup=backup, **found,
            )
        update_worker.record_installed(cfg.data_dir, base["checked_at"])
    _refresh_agents(config_path, sys.executable)
    return UpdateResult(
        "applied",
        f"Installed {brings}. Open agents reconnect to it by themselves; nothing needs "
        "restarting",
        backup=backup,
        **found,
    )


def _hand_over(
    cfg: Config,
    config_path: Path | None,
    command: list[str],
    *,
    what: str,
    brings: str,
    found: dict[str, Any],
    backup: str | None,
) -> UpdateResult | None:
    """Have the update worker install from outside the environment, so open
    agents need not close. None when this computer cannot."""
    from uuid import uuid4

    from varyz import background
    from varyz.install import resolve_config_path

    python = update_worker.base_python(Path(sys.prefix))
    if python is None:
        return None
    try:
        config = resolve_config_path(config_path)
    except ValueError:
        return None
    environment, launchers = background.install_paths()
    job: dict[str, Any] = {
        "id": uuid4().hex,
        "data_dir": str(cfg.data_dir),
        "config": str(config),
        "archive": str(cfg.db_path),
        "python": sys.executable,
        "environment": str(environment),
        "launchers": [str(path) for path in launchers],
        "command": command,
        "what": what,
        "brings": brings,
        "backup": backup,
        "found": {key: value for key, value in found.items()
                  if key not in ("status", "detail", "trigger", "checked_at")},
    }
    folder = _dir(cfg) / update_worker.FOLDER
    try:
        launch = update_worker.prepare(job, folder, python)
        # Marked before the worker starts, so a second `update apply` waits for it.
        update_worker.mark(cfg.data_dir, job["id"])
        _launch(launch, folder)
    except OSError as exc:
        update_worker.clear(cfg.data_dir)
        return UpdateResult(
            "failed", f"Could not start the background update: {type(exc).__name__}",
            backup=backup, **found,
        )
    return UpdateResult(
        "started",
        f"Installing {brings} in the background. It takes a minute or two; Varyz's "
        "tools pause meanwhile, then every open agent reconnects to the new Varyz by "
        "itself. Nothing needs restarting.",
        backup=backup,
        **found,
    )


def finish(job_path: Path) -> int:
    """After the worker's install: record it and bring Varyz back up.

    Runs in the newly installed Varyz. The installed stamp moves before the
    mark goes, so the agent connections that start then are already the new
    code and do not restart a second time.
    """
    job = json.loads(job_path.read_text(encoding="utf-8"))
    config_path = Path(job["config"])
    cfg = load_config(config_path)
    now = utc_now()
    found = {**job.get("found", {}), "trigger": update_worker.TRIGGER,
             "checked_at": iso(now) or ""}
    outcome = job.get("outcome")
    if outcome is None and (error := _starts()) is not None:
        outcome = {"status": "failed",
                   "detail": f"The new Varyz does not start ({error}); install it again"}
    if outcome is None:
        update_worker.record_installed(cfg.data_dir, found["checked_at"])
        result = UpdateResult(
            "applied", f"Installed {job['brings']}; open agents reconnected to it by themselves",
            backup=job.get("backup"), **found,
        )
    else:
        result = UpdateResult(str(outcome["status"]), str(outcome["detail"]),
                              backup=job.get("backup"), **found)
    _record(cfg, result, now)
    update_worker.clear(cfg.data_dir)
    _bring_back(config_path, refresh=result.status == "applied")
    return 0


def _bring_back(config_path: Path, *, refresh: bool) -> None:
    """Start what the worker stopped, and point the agents at the new Varyz."""
    from varyz import autosync
    from varyz.resolution import availability

    if refresh:
        with contextlib.suppress(Exception):
            from varyz.install import refresh_connections_if_changed

            refresh_connections_if_changed(config_path)
    with contextlib.suppress(Exception):
        if autosync.is_enabled(load_config(config_path)):
            autosync.spawn_daemon(config_path)
    with contextlib.suppress(Exception):
        availability.check_once(config_path)


def installed_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        from varyz import __version__

        return __version__


def _version(text: str) -> tuple[int, ...] | None:
    """A release number such as 0.1.5; None for anything else (a pre-release)."""
    parts = text.split(".")
    if not 1 < len(parts) < 5 or not all(part.isdigit() for part in parts):
        return None
    numbers = tuple(int(part) for part in parts)
    return numbers + (0,) * (4 - len(numbers))


def installer_name(version: str) -> str | None:
    """This computer's release installer, as releases name it; None when none is made."""
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64", "arm64"):
        # Windows on Arm runs the x64 installer.
        return f"VaryzSetup-{version}-windows-x64.exe"
    if sys.platform == "darwin" and machine in ("arm64", "x86_64"):
        return f"VaryzSetup-{version}-macos-{machine}.pkg"
    return None


def _user_agent() -> dict[str, str]:
    from varyz import __version__

    return {"User-Agent": f"Varyz/{__version__}"}


def _http_get(url: str) -> bytes:
    response = httpx.get(
        url, timeout=CHECK_TIMEOUT, follow_redirects=True, headers=_user_agent()
    )
    response.raise_for_status()
    if len(response.content) > 1024 * 1024:
        raise ValueError("the response is over 1 MB")
    return response.content


def _download(url: str, target: Path, expected: str) -> None:
    """Download to a part file, kept only when its SHA-256 is the expected one."""
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            httpx.stream(
                "GET",
                url,
                timeout=httpx.Timeout(CHECK_TIMEOUT, read=60),
                follow_redirects=True,
                headers=_user_agent(),
            ) as response,
            part.open("wb") as handle,
        ):
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_INSTALLER_BYTES:
                    raise ValueError("the download is far larger than an installer")
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("its checksum does not match the release's")
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def _open(path: Path) -> None:
    """Hand the installer to the system, as a double click would."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # exists on Windows only
    else:
        subprocess.run(
            ["open", str(path)], check=True, capture_output=True, timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _checksums(text: str) -> dict[str, str]:
    """`sha256sum` lines: the digest, two spaces, the file name."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        digest, _, name = line.strip().partition("  ")
        if len(digest) == 64 and name:
            sums[name.lstrip("*")] = digest.lower()
    return sums


def _run_release(
    cfg: Config, base: dict[str, Any], *, apply: bool, dry_run: bool, automatic: bool,
    config_path: Path | None = None,
) -> UpdateResult:
    """Compare a release install with the latest release; applying installs it on
    Windows and opens its installer elsewhere."""
    current = installed_version()
    try:
        release = json.loads(_http_get(LATEST_RELEASE))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return UpdateResult(
            "failed", f"Could not reach GitHub for the latest release: {type(exc).__name__}",
            **base,
        )
    if not isinstance(release, dict):
        release = {}
    tag = str(release.get("tag_name") or "")
    latest = tag.removeprefix("v")
    newest = _version(latest)
    if newest is None or release.get("draft") or release.get("prerelease"):
        return UpdateResult(
            "failed", f"The latest release has no usable version: {tag!r}", **base
        )
    found = {**base, "current": current, "target": latest}
    have = _version(current)
    if have is not None and newest <= have:
        return UpdateResult("up_to_date", f"On the latest release, Varyz {current}", **found)
    name = installer_name(latest)
    if name is None:
        return UpdateResult(
            "available",
            f"Varyz {latest} is out (this is {current}). No installer is made for this "
            f"computer; get it from {RELEASES_PAGE}",
            **found,
        )
    silent = _libraries_locked_while_loaded()
    if dry_run:
        verb = ("Would download and run" if silent else "Would download and open"
                ) if apply else "Available:"
        return UpdateResult("dry_run", f"{verb} the Varyz {latest} installer", **found)
    if not apply or automatic:
        return UpdateResult(
            "available",
            f"Varyz {latest} is out (this is {current}). `varyz update apply` downloads "
            f"its installer and {'installs it' if silent else 'opens it'}",
            **found,
        )
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url") or "")
        for asset in release.get("assets") or []
        if isinstance(asset, dict)
    }
    links = {key: assets.get(key, "") for key in (name, CHECKSUMS)}
    if not all(link.startswith(RELEASE_DOWNLOADS) for link in links.values()):
        return UpdateResult(
            "failed",
            f"The Varyz {latest} release has no {name} with a checksum; get it from "
            f"{RELEASES_PAGE}",
            **found,
        )
    try:
        expected = _checksums(_http_get(links[CHECKSUMS]).decode("utf-8")).get(name)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return UpdateResult(
            "failed", f"Could not read the release's checksums: {type(exc).__name__}", **found
        )
    if expected is None:
        return UpdateResult("failed", f"{CHECKSUMS} does not list {name}", **found)
    folder = installers_path(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if not (target.is_file() and _sha256(target) == expected):
        free = free_bytes(folder)
        if free is not None and free < MIN_FREE_BYTES:
            return UpdateResult(
                "skipped",
                f"Varyz {latest} waiting: {free // 1_048_576} MiB free is too little to "
                "download and run its installer",
                **found,
            )
        try:
            _download(links[name], target, expected)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return UpdateResult(
                "failed", f"Could not download the Varyz {latest} installer: {reason}", **found
            )
    # An older installer is no use once a newer one is here.
    for old in folder.glob("VaryzSetup-*"):
        if old.name != name:
            old.unlink(missing_ok=True)
    if silent:
        log = _dir(cfg) / update_worker.FOLDER / "installer.log"
        handed = _hand_over(
            cfg, config_path,
            [str(target), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
             f"/LOG={log}"],
            what=f"the Varyz {latest} installer", brings=f"Varyz {latest}", found=found,
            backup=None,
        )
        if handed is not None:
            return handed
    try:
        _open(target)
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateResult(
            "failed",
            f"Downloaded {target} but could not open it ({type(exc).__name__}); open it "
            "yourself",
            **found,
        )
    return UpdateResult(
        "opened",
        f"Opened the Varyz {latest} installer ({target}). Close your AI agents before you "
        "click Install; it keeps your archive, settings and connected agents. Restart the "
        "agents afterwards.",
        **found,
    )


def _libraries_locked_while_loaded() -> bool:
    """Windows will not replace a library a running process has loaded, and
    every open agent session has Varyz's loaded."""
    return sys.platform == "win32"


def _brings(commits: int, migrations: bool, dependencies: bool) -> str:
    parts = [f"{commits} new commit{'s' if commits != 1 else ''}"]
    if migrations:
        parts.append("a database migration")
    if dependencies:
        parts.append("new dependencies")
    return ", ".join(parts)


class _Git:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _call(self, args: Sequence[str]) -> subprocess.CompletedProcess[str] | str:
        env = {
            **os.environ,
            # A background process has no one to type a password; fail instead.
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT,
                check=False,
                env=env,
                stdin=subprocess.DEVNULL,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return type(exc).__name__

    def run(self, *args: str) -> str | None:
        done = self._call(args)
        if isinstance(done, str):
            return done
        if done.returncode == 0:
            return None
        lines = (done.stderr or done.stdout).strip().splitlines()
        return lines[-1] if lines else f"git {args[0]} exited {done.returncode}"

    def out(self, *args: str) -> str | None:
        done = self._call(args)
        if isinstance(done, str) or done.returncode != 0:
            return None
        return done.stdout.strip()


def _uv_sync(root: Path) -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return "uv is not on PATH"
    try:
        done = subprocess.run(
            [uv, "sync", "--frozen"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SYNC_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"uv sync: {type(exc).__name__}"
    if done.returncode == 0:
        return None
    lines = done.stderr.strip().splitlines()
    return f"uv sync: {lines[-1] if lines else done.returncode}"


def _imports(root: Path) -> str | None:
    """Import the new code in a fresh interpreter, without opening the archive."""
    paths = [str(root / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    try:
        done = subprocess.run(
            [sys.executable, "-c", SMOKE_IMPORTS],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"import check: {type(exc).__name__}"
    if done.returncode == 0:
        return None
    lines = done.stderr.strip().splitlines()
    return f"import check: {lines[-1] if lines else done.returncode}"


def _backup_database(cfg: Config) -> tuple[str | None, str | None]:
    """Copy the database before a migration; returns (path, error).

    One copy is kept and replaced each time: the archive is gigabytes, and the
    copy only has to outlive the migration that follows it.
    """
    source = cfg.db_path
    if not source.is_file():
        return None, None
    target = backup_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    wal = source.with_name(source.name + "-wal")
    size = source.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    old = target.stat().st_size if target.exists() else 0
    free = free_bytes(target.parent)
    needed = max(0, size - old) + 64 * 1024 * 1024
    if free is not None and needed > free:
        return None, f"needs {needed // 1_048_576} MiB, {free // 1_048_576} MiB free"
    part = target.with_name(target.name + ".part")
    try:
        with (
            contextlib.closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src,
            contextlib.closing(sqlite3.connect(part)) as dst,
        ):
            src.backup(dst)
        os.replace(part, target)
    except (OSError, sqlite3.Error) as exc:
        part.unlink(missing_ok=True)
        return None, f"{type(exc).__name__}: {exc}"
    return str(target), None


def _mark_attempted(cfg: Config, now: datetime | None = None) -> None:
    state = load_state(cfg)
    state["attempted_on"] = local_day(now or utc_now())
    _write_state(cfg, state)


def _record(cfg: Config, result: UpdateResult, now: datetime) -> None:
    state = {**result.as_dict(), "attempted_on": local_day(now)}
    _dir(cfg).mkdir(parents=True, exist_ok=True)
    _write_state(cfg, state)
    with log_path(cfg).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _write_state(cfg: Config, state: dict[str, Any]) -> None:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def announced_path(cfg: Config) -> Path:
    return _dir(cfg) / "announced.json"


def agent_notice(cfg: Config) -> str | None:
    """An update nobody has told the owner about yet: one a check found, or the
    end of one installed in the background.

    `notify` mode used to record what it found in update.json only, where no one
    looked. The ``user-prompt`` hook hands this line to the owner's agent, once per
    new version; a check the owner ran by hand (``varyz update check``) already
    told them, so it is never repeated. A background install ends after the
    command that started it returned, so its result is told once as well.
    """
    state = load_state(cfg)
    target = state.get("target")
    status, trigger = state.get("status"), state.get("trigger")
    finished = trigger == update_worker.TRIGGER
    if finished:
        key, value = "finished", state.get("checked_at")
    elif status in ("available", "waiting") and trigger != "manual":
        key, value = "target", target
    else:
        return None
    if not isinstance(value, str) or not value or is_paused(cfg):
        return None
    try:
        told = json.loads(announced_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        told = {}
    told = told if isinstance(told, dict) else {}
    if told.get(key) == value:
        return None
    path = announced_path(cfg)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({**told, key: value}) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    detail = " ".join(str(state.get("detail", "")).split())[:300]
    if finished and status == "applied":
        return (
            f"Varyz finished updating{f' to {target}' if target else ''} in the background "
            "and its tools are back. Tell the user in one short line, once; nothing needs "
            "restarting."
        )
    if finished:
        return (
            "Varyz's background update did not finish. Tell the user in one short line, "
            f"once, in plain words: {detail}"
        )
    return (
        "A Varyz update is ready for the user. End your reply with it in one short line, "
        "once: they can say \"update Varyz\" or run `varyz update apply`. What the check "
        f"found: {detail}"
    )


def doctor_detail(cfg: Config) -> tuple[bool, str]:
    """For `autosync doctor`: is updating healthy, and what did it last do?"""
    mode = cfg.update.mode
    state = load_state(cfg)
    last = (
        f"; last: {state.get('status')} at {state.get('checked_at')}: {state.get('detail')}"
        if state.get("status")
        else ""
    )
    if mode == "off":
        return True, "off ([update] mode); update now with varyz update apply"
    if is_paused(cfg):
        return True, f"{mode}, paused{last}"
    root = checkout_root()
    where = f"checkout {root}" if root else describe_source(install_source())
    return state.get("status") != "failed", (
        f"{mode} ([update] mode), daily on first use, {where}{last}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m varyz.update")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--trigger", default="on-use")
    parser.add_argument("--refresh-connections", action="store_true")
    parser.add_argument("--finish", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.finish is not None:
        return finish(args.finish)
    if args.refresh_connections:
        from varyz.install import refresh_connections

        try:
            refresh_connections(args.config)
        except ValueError:  # no archive to refresh the agents for
            return 1
        return 0
    if args.config is None:
        parser.error("--config is required")
    cfg = load_config(args.config)
    result = run(cfg, trigger=args.trigger, config_path=args.config)
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

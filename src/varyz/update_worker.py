"""Install a Varyz update while the agents stay open (stdlib only, inert on import).

Windows will not replace a file a running process has loaded, and every open
agent session runs Varyz. So on Windows `varyz update apply` hands the install
to this script, which the base Python runs from outside the tool's environment:

1. It marks the update as running: the UPDATING file, kept fresh while it
   works. Each agent's Varyz connection finishes the request it is on and
   ends; its launcher (windows_hook) holds the agent's next requests until the
   mark goes.
2. It waits for those connections to end and stops Varyz's other processes
   (the services, learning), which start again afterwards.
3. It runs the install: uv for a uv install, the release installer, silently,
   for one from the installers.
4. The new Varyz finishes up (`python -m varyz.update --finish`): it checks it
   starts, records the result, removes the mark, refreshes the agents' settings
   and restarts the services. The launchers then start the new Varyz for every
   open agent, which carries on where it was.

Elsewhere a file can be replaced while it runs, so `varyz update` installs in
its own process and only uses the mark and the stamp: UPDATING pauses the
agent connections, and INSTALLED changing tells them to restart onto the new
code.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The mark counts only while it was touched this recently: a worker that died
# must not leave every agent's Varyz paused.
STALE_AFTER = 120
HEARTBEAT = 5
# How long open agent connections get to finish the request they are on.
DRAIN_SECONDS = 120
INSTALL_SECONDS = 900
FINISH_SECONDS = 300
POLL = 1.0
FOLDER = "update-worker"
TRIGGER = "background"
OLD_SESSIONS = (
    "Some agent sessions were opened before Varyz could update without a restart. "
    "Close them, then say \"update Varyz\" again; from then on updates need no restart"
)
BUSY = (
    "An agent's Varyz request was still running after two minutes. Nothing was "
    "changed; say \"update Varyz\" again in a moment"
)
_MCP = re.compile(r"-m\s+\"?varyz\"?\s+mcp\b", re.IGNORECASE)
_HOOK = re.compile(r"-m\s+\"?varyz\.hooks\b", re.IGNORECASE)
_MCP_LAUNCHER = re.compile(r"varyz-mcp-[0-9a-f]{12}\.exe", re.IGNORECASE)


def sentinel_path(data_dir: Path) -> Path:
    return data_dir / "autosync" / "UPDATING"


def stamp_path(data_dir: Path) -> Path:
    return data_dir / "autosync" / "INSTALLED"


def updating(data_dir: Path) -> bool:
    """Is an update being installed right now?"""
    try:
        age = time.time() - sentinel_path(data_dir).stat().st_mtime
    except OSError:
        return False
    return age < STALE_AFTER


def installed(data_dir: Path) -> str:
    """Changes with every installed update; open agent connections restart when it does."""
    try:
        return stamp_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def record_installed(data_dir: Path, token: str) -> None:
    _write_text(stamp_path(data_dir), token + "\n")


def clear(data_dir: Path) -> None:
    sentinel_path(data_dir).unlink(missing_ok=True)


def mark(data_dir: Path, token: str) -> None:
    _write_text(sentinel_path(data_dir), token + "\n")


@contextlib.contextmanager
def holding(data_dir: Path, token: str | None = None) -> Iterator[None]:
    """Hold the mark, kept fresh, while the block runs."""
    path = sentinel_path(data_dir)
    mark(data_dir, token or str(os.getpid()))
    done = threading.Event()

    def beat() -> None:
        while not done.wait(HEARTBEAT):
            # Only touch: once the finishing step removed it, it stays removed.
            with contextlib.suppress(OSError):
                os.utime(path)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()
        path.unlink(missing_ok=True)


def base_python(environment: Path) -> Path | None:
    """The Python the tool environment was made from: it lives outside it."""
    try:
        text = (environment / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    home = next((line.split("=", 1)[1].strip() for line in text.splitlines()
                 if "=" in line and line.split("=", 1)[0].strip().lower() == "home"), "")
    if not home:
        return None
    names = ("pythonw.exe", "python.exe") if os.name == "nt" else ("python3", "python")
    for name in names:
        candidate = Path(home) / name
        if candidate.is_file() and not candidate.resolve().is_relative_to(environment.resolve()):
            return candidate
    return None


def prepare(job: dict[str, Any], folder: Path, python: Path) -> list[str]:
    """Copy the worker out of the environment it replaces, write its job, and
    return the command that runs it."""
    folder.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    for name in ("update_worker.py", "background.py"):
        shutil.copyfile(here / name, folder / name)
    path = folder / "job.json"
    _write_text(path, json.dumps(job, indent=2) + "\n")
    return [str(python), "-I", "-B", str(folder / "update_worker.py"), str(path)]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _background() -> Any:
    """varyz.background, imported from the copy beside this file when run outside Varyz."""
    if __package__:
        from varyz import background

        return background
    name = "varyz_update_background"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("background.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses look their module up while the class is made.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _relays(process: Any, processes: list[Any], hooks: Path) -> bool:
    """Was this agent connection started by a launcher that holds requests across a restart?"""
    parent = next((item for item in processes if item.pid == process.parent), None)
    found = _MCP_LAUNCHER.search(parent.command) if parent is not None else None
    if found is None:
        return False
    try:
        lines = (hooks / (found.group(0) + ".launch")).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return len(lines) >= 4


def _theirs(background: Any, job: dict[str, Any], processes: list[Any]) -> list[Any]:
    """This installation's processes, other than this worker and what it started."""
    mine = {os.getpid(), *background._descendants(processes, os.getpid())}
    environment = Path(job["environment"])
    launchers = [Path(path) for path in job["launchers"]]
    return [process for process in processes if process.pid not in mine
            and background.matches(process.command, environment, launchers)]


def _outermost(background: Any, processes: list[Any], chosen: list[Any]) -> list[Any]:
    pids = {process.pid for process in chosen}
    return [process for process in chosen
            if not (background._ancestors(processes, process.pid) - {process.pid}) & pids]


def drain(job: dict[str, Any], log: Any) -> dict[str, str] | None:
    """Wait for the agent connections to end, stopping everything else. None when clear."""
    background = _background()
    hooks = Path(job["data_dir"]) / "hooks"
    deadline = time.monotonic() + DRAIN_SECONDS
    while True:
        processes = background._listing()
        found = _theirs(background, job, processes)
        if not found:
            return None
        ancestors = background._ancestors(processes, os.getpid())
        waiting, stopping = [], []
        for process in found:
            if process.pid in ancestors or _HOOK.search(process.command):
                # `varyz update apply` that started this, and hooks: both end in seconds.
                waiting.append(process)
            elif _MCP.search(process.command):
                if not _relays(process, processes, hooks):
                    log("an agent connection from an older launcher is open")
                    return {"status": "waiting", "detail": OLD_SESSIONS}
                waiting.append(process)
            else:
                stopping.append(process)
        if stopping:
            outer = _outermost(background, processes, stopping)
            log("stopping " + ", ".join(background.describe(process) for process in outer))
            background.stop(outer)
        if time.monotonic() > deadline:
            return {"status": "waiting", "detail": BUSY}
        time.sleep(POLL)


def _checkpoint(archive: Path) -> None:
    """Fold a stopped process's write-ahead log into the archive.

    A process ended by force leaves its -wal and -shm files behind, and the
    release upgrade will not touch an archive that has them. The last
    connection to close removes them.
    """
    if not archive.with_name(archive.name + "-wal").exists():
        return
    with contextlib.closing(sqlite3.connect(archive, timeout=30)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def install(job: dict[str, Any], log: Any, output: Path) -> dict[str, str] | None:
    """Run the install, stopping any Varyz process that starts meanwhile. None when it worked."""
    background = _background()
    with contextlib.suppress(OSError, sqlite3.Error):
        if job.get("archive"):
            _checkpoint(Path(job["archive"]))
    log(f"installing with {job['what']}")
    with output.open("ab") as stream:
        try:
            process = subprocess.Popen(  # noqa: S603 - the command `varyz update` wrote
                job["command"], stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, cwd=job["data_dir"],
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except OSError as exc:
            return {"status": "failed",
                    "detail": f"Could not start {job['what']}: {type(exc).__name__}"}
        deadline = time.monotonic() + INSTALL_SECONDS
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                process.wait()
                return {"status": "failed",
                        "detail": f"{job['what']} took too long and was stopped"}
            # A login job or an agent opened just now would start the old
            # Varyz and hold the files the install is replacing.
            with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError, KeyError):
                processes = background._listing()
                late = _theirs(background, job, processes)
                if late:
                    background.stop(_outermost(background, processes, late))
            time.sleep(2)
    if process.returncode:
        return {"status": "failed",
                "detail": f"{job['what']} stopped with code {process.returncode}; "
                          f"its output is in {output}"}
    return None


def finish(job: dict[str, Any], job_path: Path, log: Any) -> bool:
    """Let the newly installed Varyz record the result and come back up."""
    command = [job["python"], "-m", "varyz.update", "--config", job["config"],
               "--finish", str(job_path)]
    try:
        done = subprocess.run(  # noqa: S603 - the tool's own Python
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=FINISH_SECONDS,
            check=False, cwd=job["data_dir"],
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"finishing failed: {type(exc).__name__}")
        return False
    if done.returncode:
        log(f"finishing failed with code {done.returncode}")
    return done.returncode == 0


def _record_failure(job: dict[str, Any], detail: str) -> None:
    """What `--finish` would have recorded, when the installed Varyz cannot run at all."""
    folder = Path(job["data_dir"]) / "autosync"
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    state = {**job.get("found", {}), "status": "failed", "detail": detail,
             "trigger": TRIGGER, "checked_at": now, "backup": job.get("backup")}
    with contextlib.suppress(OSError):
        with (folder / "updates.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(state, sort_keys=True) + "\n")
        day = datetime.now().date().isoformat()
        _write_text(folder / "update.json", json.dumps(
            {**state, "attempted_on": day}, indent=2, sort_keys=True) + "\n")


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    data = Path(job["data_dir"])
    folder = job_path.parent
    log_file = folder / "worker.log"

    def log(line: str) -> None:
        with contextlib.suppress(OSError), log_file.open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {line}\n")

    try:
        mine = sentinel_path(data).read_text(encoding="utf-8").strip() == job["id"]
    except OSError:
        mine = False
    if updating(data) and not mine:
        log("another update is being installed")
        return 1
    with holding(data, job["id"]):
        log(f"updating: {job['brings']}")
        outcome = drain(job, log) if os.name == "nt" else None
        if outcome is None:
            outcome = install(job, log, folder / "install.log")
        job["outcome"] = outcome
        _write_text(job_path, json.dumps(job, indent=2) + "\n")
        if not finish(job, job_path, log):
            _record_failure(job, (outcome or {}).get("detail") or (
                "The new Varyz does not start. Run the Varyz installer again, or "
                "`uv tool install --reinstall varyzai` for a uv install"))
    log(f"done: {(outcome or {}).get('status', 'applied')}")
    return 0 if outcome is None else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: update_worker.py JOB", file=sys.stderr)
        return 2
    return run(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())

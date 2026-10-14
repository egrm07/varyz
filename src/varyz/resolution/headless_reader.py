"""Varyz starts the rule reader itself, on the owner's Claude Code or Codex sign-in.

The briefing used to ask the working agent to start a reader subagent, and an
agent busy with its task often did not. Now the hook (and start_task) hands the
job to this supervisor instead: it starts ``claude -p`` or ``codex exec`` in the
background with only varyz's two reading tools, on the cheapest model, and
watches the owner's plan limits while it reads.

Limits (`[workflows]`, percent left of each window):

- the full reading of the backlog (large) never starts, and stops, once the
  five-hour window has `large_reading_min_left_5h` (30) or less left or the
  week `large_reading_min_left_weekly` (25) or less;
- the live reading of the last day (small) at 10 and 5.

Usage that cannot be read allows only the live reading, and only until the
first reading of it: a run whose usage stays unknown is stopped. A stopped run
loses nothing -- a task's turns count as read only once it is submitted -- and
the job waits for the window that stopped it to start over; the resolution
daemon starts it again then (`maybe_resume`).

The runner is the one with more of its limits left (`reader_runner = "auto"`).
Kill switches: `varyz hooks pause`, `[workflows] live_reading = false` (live),
`rule_reader_agent = false` (all reading), `reader_runner = "agent"` (back to
asking the working agent), `varyz rules read --stop`.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from varyz.db.connection import FileLock
from varyz.resolution import usage as usage_mod
from varyz.resolution.usage import Gate, Usage, describe, gates

ENV = "VARYZ_HEADLESS_READER"
TOKEN_ENV = "VARYZ_AGENT_TOKEN"
KINDS = ("full", "live")
TOOLS = ("profile_reading_task", "submit_profile_reading")
# A job whose usage could not be read is checked again this often.
RECHECK_SECONDS = 30 * 60
# Usage that is still unknown this long after a run started stops it.
UNKNOWN_GRACE_SECONDS = 120.0
# A stopped reader has this long to end on its own before it is killed.
STOP_GRACE_SECONDS = 90.0
MONITOR_SECONDS = 2.0
RUNS_PER_START = 6
NO_PROGRESS_LIMIT = 2
STATUS_LOCK_SECONDS = 5.0

HEADLESS_NOTE = (
    "\n\nYou run unattended: use only the two varyz tools, never ask anything. Do at most "
    "{tasks} tasks in this run and then stop; varyz starts a fresh reader for the rest. "
    "When profile_reading_task says done or stopped, stop at once."
)


# -- where things are kept ---------------------------------------------------------


def _rules_dir(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "rules"


def status_path(cfg: Any) -> Path:
    return _rules_dir(cfg) / "headless_reader.json"


def log_path(cfg: Any) -> Path:
    return _rules_dir(cfg) / "headless_reader.log.jsonl"


def _lock_path(cfg: Any) -> Path:
    return _rules_dir(cfg) / "headless_reader.lock"


def load_status(cfg: Any) -> dict[str, Any]:
    try:
        stored = json.loads(status_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


@contextlib.contextmanager
def _status_guard(cfg: Any) -> Iterator[None]:
    lock = FileLock(_rules_dir(cfg) / "headless_reader.status.lock")
    deadline = time.monotonic() + STATUS_LOCK_SECONDS
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise TimeoutError("the reader's status is busy")
        time.sleep(0.02)
    try:
        yield
    finally:
        lock.release()


def update_status(cfg: Any, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Load, change and save the status under its lock; the saved status."""
    with _status_guard(cfg):
        status = load_status(cfg)
        change(status)
        path = status_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(status, indent=1), encoding="utf-8")
        temporary.replace(path)
        return status


def log(cfg: Any, event: str, **fields: Any) -> None:
    """Append one line to the reader's log; counts and reasons, never tokens or messages."""
    line = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
    with contextlib.suppress(OSError):
        path = log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")


def _pending(status: dict[str, Any]) -> set[str]:
    return {kind for kind in status.get("pending") or () if kind in KINDS}


def stop_requested(cfg: Any, run_id: str) -> bool:
    """Whether the reader running as ``run_id`` was told to stop (the MCP tool ends it then)."""
    stop = load_status(cfg).get("stop_run")
    return stop in {run_id, "*"}


# -- the runners --------------------------------------------------------------------


@dataclass(frozen=True)
class Runner:
    name: str
    executable: str
    token: str = field(repr=False)


def _agent_token(client: str, config_file: Path) -> str | None:
    """The token the owner granted this agent, from its MCP entry or the plugin's keyring."""
    from varyz.install import client_paths, registered_archive, same_archive
    from varyz.plugin import credential_name, registration

    path = client_paths().get(client)
    entry = registration(client, path) if path is not None else None
    environment = entry.get("env") if isinstance(entry, dict) else None
    token = environment.get(TOKEN_ENV) if isinstance(environment, dict) else None
    registered = registered_archive(entry)
    if isinstance(token, str) and token and (registered is None
                                             or same_archive(registered, config_file)):
        return token
    try:
        import keyring

        stored = keyring.get_password("varyz-plugin", credential_name(client, config_file))
    except Exception:
        return None
    return stored or None


def _codex_executable() -> str | None:
    """Codex's own program, not the npm shim: a .cmd would mangle the reader's arguments."""
    found = shutil.which("codex")
    if found is None:
        return None
    if sys.platform != "win32" or found.lower().endswith(".exe"):
        return found
    root = Path(found).parent / "node_modules" / "@openai" / "codex"
    pattern = str(root / "node_modules" / "@openai" / "codex-win32-*" / "vendor" / "*"
                  / "bin" / "codex.exe")
    native = sorted(glob.glob(pattern))
    return native[0] if native else None


def find_runners(cfg: Any, config_file: Path) -> dict[str, Runner]:
    """The sign-ins the reader may run on, by name: 'claude', 'codex'."""
    choice = cfg.workflows.reader_runner
    if choice == "agent":
        return {}
    found: dict[str, Runner] = {}
    wanted = ("claude", "codex") if choice == "auto" else (choice,)
    for name in wanted:
        executable = shutil.which("claude") if name == "claude" else _codex_executable()
        if executable is None:
            continue
        token = _agent_token("claude-code" if name == "claude" else "codex", config_file)
        if token:
            found[name] = Runner(name, executable, token)
    return found


def enabled(cfg: Any, kind: str) -> bool:
    workflows = cfg.workflows
    if not workflows.rule_reader_agent or workflows.reader_runner == "agent":
        return False
    return kind == "full" or workflows.live_reading


def _paused(cfg: Any) -> bool:
    from varyz.hooks import pause_path

    return pause_path(cfg).exists()


def _deferred(cfg: Any, now: float) -> float | None:
    from varyz.resolution.profile_reader import deferred_until

    return deferred_until(cfg, now)


def _local(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).astimezone().strftime("%a %d %b %H:%M")


# -- the commands -------------------------------------------------------------------


def prompt_for(cfg: Any, kind: str) -> str:
    from varyz.resolution.profile_reader import AGENT_PROMPT, LIVE_PROMPT

    base = AGENT_PROMPT if kind == "full" else LIVE_PROMPT
    return base + HEADLESS_NOTE.format(tasks=cfg.workflows.reader_tasks_per_run)


def _mcp_server(config_file: Path) -> tuple[str, list[str]]:
    return sys.executable, ["-m", "varyz", "mcp", "--config", str(config_file)]


def claude_command(cfg: Any, runner: Runner, kind: str, config_file: Path) -> list[str]:
    """``claude -p`` with only varyz's reading tools.

    No settings, hooks, other MCP servers or transcript of its own.
    """
    command, args = _mcp_server(config_file)
    mcp = {"mcpServers": {"varyz": {"command": command, "args": args, "env": {
        TOKEN_ENV: "${" + TOKEN_ENV + "}", ENV: "${" + ENV + "}"}}}}
    path = _rules_dir(cfg) / "headless_reader_mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mcp), encoding="utf-8")
    return [
        runner.executable, "-p", prompt_for(cfg, kind),
        "--model", cfg.workflows.reader_claude_model,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config", "--mcp-config", str(path),
        "--setting-sources", "",
        "--tools", "",
        "--allowedTools", *(f"mcp__varyz__{tool}" for tool in TOOLS),
        "--permission-prompts", "none",
    ]


# Codex features a reader has no use for; each is left off so its tools and
# instructions do not reach the model (about 39k tokens a call instead of 107k).
CODEX_FEATURES_OFF = (
    "hooks", "apps", "plugins", "browser_use", "computer_use", "image_generation",
    "shell_tool", "multi_agent", "skill_search", "tool_suggest", "shell_snapshot",
    "sleep_tool", "skill_mcp_dependency_install",
)


def _toml_string(text: str) -> str:
    return json.dumps(text)


def codex_command(cfg: Any, runner: Runner, kind: str, config_file: Path,
                  model: str) -> list[str]:
    """``codex exec`` without the owner's config: no hooks, plugins, shell or session file."""
    command, args = _mcp_server(config_file)
    overrides = [
        f"model_reasoning_effort={_toml_string('low')}",
        f"approval_policy={_toml_string('never')}",
        f"web_search={_toml_string('disabled')}",
        f"mcp_servers.varyz.command={_toml_string(command)}",
        f"mcp_servers.varyz.args=[{', '.join(_toml_string(arg) for arg in args)}]",
        f"mcp_servers.varyz.env_vars=[{_toml_string(TOKEN_ENV)}, {_toml_string(ENV)}]",
        f"mcp_servers.varyz.enabled_tools=[{', '.join(_toml_string(t) for t in TOOLS)}]",
        *(f"features.{name}=false" for name in CODEX_FEATURES_OFF),
    ]
    return [
        runner.executable, "exec", "--ephemeral", "--skip-git-repo-check",
        "--ignore-user-config", "--ignore-rules", "--json", "-s", "read-only",
        "-m", model,
        *(part for override in overrides for part in ("-c", override)),
        prompt_for(cfg, kind),
    ]


def codex_models(cfg: Any, rejected: Iterable[str] = ()) -> list[str]:
    """Codex models to try, newest first: the configured one, or Codex's luna (cheapest) models."""
    configured = cfg.workflows.reader_codex_model
    if configured != "auto":
        return [configured]
    try:
        cache = json.loads((usage_mod.codex_home() / "models_cache.json")
                           .read_text(encoding="utf-8"))
        listed = [model for model in cache.get("models") or () if isinstance(model, dict)]
    except (OSError, ValueError, AttributeError):
        listed = []
    lunas = sorted(
        (model for model in listed
         if "luna" in str(model.get("slug", "")) and model.get("visibility") != "hide"),
        key=lambda model: model.get("priority", 999),
    )
    skip = set(rejected)
    return [str(model["slug"]) for model in lunas if model["slug"] not in skip]


# -- choosing -----------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    runner: str | None
    reason: str
    resume_after: float | None = None


def choose(kind: str, gate: Gate, usages: dict[str, Usage | None], now: float) -> Choice:
    """The runner with the most room above ``gate``, or why none may run and when to look again.

    Unknown usage lets only the live reading run, on Claude first: its stream
    reports the usage with the first model call.
    """
    room = {name: gate.headroom(found) for name, found in usages.items() if found is not None}
    open_runners = {name: value for name, value in room.items()
                    if value is not None and value > 0}
    if open_runners:
        best = max(open_runners, key=lambda name: open_runners[name])
        return Choice(best, f"{best}: {describe(usages[best])}")
    unknown = [name for name, found in usages.items() if found is None or not found.known]
    if kind == "live" and unknown:
        name = "claude" if "claude" in unknown else unknown[0]
        return Choice(name, f"{name}: usage unknown; it must show within the first call")
    resumes: list[float] = []
    reasons: list[str] = []
    for name, found in usages.items():
        if found is None or not found.known:
            reasons.append(f"{name}: usage unknown")
            resumes.append(now + RECHECK_SECONDS)
            continue
        blocking = gate.blocks(found)
        reasons.append(f"{name}: {describe(found)}")
        resets = [window.resets_at for window in blocking]
        resumes.append(max((at for at in resets if at is not None), default=now + RECHECK_SECONDS)
                       if all(at is not None for at in resets) else now + RECHECK_SECONDS)
    return Choice(None, "; ".join(reasons) or "no runner",
                  min(resumes) if resumes else now + RECHECK_SECONDS)


# -- running one reader -------------------------------------------------------------


@dataclass
class RunResult:
    exit_code: int | None
    stopped: str | None = None
    usage: Usage | None = None
    error: str = ""
    resume_after: float | None = None
    rejected_model: str | None = None


class _Output:
    """Reads the reader's JSON lines in a thread: its usage reports and its errors."""

    def __init__(self, stream: Iterable[str]) -> None:
        self.usage: Usage | None = None
        self.error = ""
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: Iterable[str]) -> None:
        with contextlib.suppress(Exception):
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                reported = usage_mod.from_claude_event(event)
                if reported is not None:
                    self.usage = _merged(self.usage, reported)
                kind = event.get("type")
                if kind in {"error", "turn.failed"} or (kind == "result" and event.get("is_error")):
                    message = event.get("message") or event.get("error") or event.get("result")
                    if isinstance(message, dict):
                        message = message.get("message")
                    self.error = str(message or kind)[:300]

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)


def _merged(old: Usage | None, new: Usage) -> Usage:
    if old is None:
        return new
    return Usage(new.provider, new.five_hour or old.five_hour, new.weekly or old.weekly,
                 new.read_at, new.source)


def _fresher(*readings: Usage | None) -> Usage | None:
    known = [found for found in readings if found is not None and found.known]
    return max(known, key=lambda found: found.read_at) if known else None


def kill_tree(process: Any) -> None:
    """The reader and everything it started: its model client and varyz's MCP server."""
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 - fixed system tool, numeric pid
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=30, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        process.kill()


def _launch(command: list[str], env: dict[str, str], cwd: Path) -> Any:
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    return subprocess.Popen(  # noqa: S603 - the owner's own CLI, fixed arguments
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=flags, start_new_session=sys.platform != "win32",
    )


def _blocked_history(status: dict[str, Any], runner: str, gate: Gate, now: float) -> Usage | None:
    """An unreadable endpoint cannot override a known limit whose reset is still ahead."""
    raw = (status.get("usage") or {}).get(runner)
    prior = Usage.from_dict(raw) if isinstance(raw, dict) else None
    if prior is None or not prior.known:
        return None
    blocks = gate.blocks(prior)
    return prior if any(w.resets_at is not None and w.resets_at > now for w in blocks) else None


def run_one(
    cfg: Any, runner: Runner, kind: str, command: list[str], *,
    gate: Gate,
    read_usage: Callable[[str], Usage | None] = usage_mod.read,
    launch: Callable[[list[str], dict[str, str], Path], Any] = _launch,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """Run one reader to its end, stopping it when the usage, time or owner says so."""
    workflows = cfg.workflows
    run_id = uuid.uuid4().hex
    env = {**os.environ, TOKEN_ENV: runner.token, ENV: run_id}
    started = clock()
    update_status(cfg, lambda status: status.update({
        "state": "running", "kind": kind, "runner": runner.name, "run_id": run_id,
        "started_at": started, "checked_at": started, "stop_run": None, "pid": None,
        "supervisor_pid": os.getpid(), "reason": "reader running",
        "resume_after": None, "next_check": None}))
    process = launch(command, env, _rules_dir(cfg))
    update_status(cfg, lambda status: status.update({"pid": process.pid}))
    output = _Output(process.stdout if process.stdout is not None else ())
    polled: Usage | None = None
    polled_at = 0.0
    stopped: str | None = None
    stop_at: float | None = None
    resume_after: float | None = None
    shown_at = 0.0
    while process.poll() is None:
        now = clock()
        if stop_at is not None:
            if now >= stop_at:
                kill_tree(process)
                break
            sleep(MONITOR_SECONDS)
            continue
        if now - polled_at >= workflows.reader_usage_poll_seconds:
            polled_at = now
            polled = read_usage(runner.name) or polled
        latest = _fresher(polled, output.usage)
        if latest is None or not latest.known:
            latest = _blocked_history(load_status(cfg), runner.name, gate, now) or latest
        reason: str | None = None
        if latest is not None and (blocking := gate.blocks(latest)):
            reason = f"usage: {describe(latest)}"
            resets = [window.resets_at for window in blocking]
            resume_after = max(at for at in resets if at is not None) \
                if all(at is not None for at in resets) else now + RECHECK_SECONDS
        elif latest is None and now - started >= UNKNOWN_GRACE_SECONDS:
            reason = "usage: unknown"
            resume_after = now + RECHECK_SECONDS
        elif now - started >= workflows.reader_max_minutes * 60:
            reason = "time"
        elif _paused(cfg) or not enabled(cfg, kind):
            reason = "turned off"
        elif _deferred(cfg, now) is not None:
            reason = "put off"
        elif load_status(cfg).get("stop_run") in {run_id, "*"}:
            reason = "asked"
        if reason is not None:
            stopped = reason
            # The MCP tool answers "done" from now on, so the reader ends after
            # the task in hand; a reader that does not is killed.
            update_status(cfg, lambda status: status.update({"stop_run": run_id}))
            log(cfg, "stopping", run_id=run_id, runner=runner.name, kind=kind, reason=reason)
            stop_at = now + STOP_GRACE_SECONDS
        if latest is not None and latest.read_at != shown_at:
            shown_at = latest.read_at
            update_status(cfg, lambda status, found=latest: status.setdefault(
                "usage", {}).update({runner.name: found.as_dict()}))
        sleep(MONITOR_SECONDS)
    exit_code = process.poll()
    output.join(5.0)
    final = _fresher(polled, output.usage)
    rejected = None
    if exit_code and runner.name == "codex" and _model_rejected(output.error):
        rejected = command[command.index("-m") + 1] if "-m" in command else None
    return RunResult(exit_code, stopped, final, output.error, resume_after, rejected)


def _model_rejected(error: str) -> bool:
    text = error.lower()
    return "model" in text and any(
        phrase in text for phrase in ("not supported", "does not exist", "not found", "unknown"))


# -- the supervisor -----------------------------------------------------------------


def _work_left(cfg: Any, kind: str, now: float) -> tuple[int, int, int, int]:
    """(turns waiting, projects to merge, turns read, notes) for ``kind``'s reading."""
    from varyz.resolution.profile_reader import ProfileState, live_since, profile_path, waiting

    state = ProfileState(profile_path(cfg, "cloud"))
    db = Path(cfg.db_path)
    if not db.exists():
        return 0, len(state.dirty), len(state.read), len(state.notes)
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        unread = waiting(state, conn, "cloud", live_since(cfg, now) if kind == "live" else None)
    finally:
        conn.close()
    return unread, len(state.dirty), len(state.read), len(state.notes)


def _commands(cfg: Any, runner: Runner, kind: str, config_file: Path,
              rejected: list[str]) -> list[list[str]]:
    if runner.name == "claude":
        return [claude_command(cfg, runner, kind, config_file)]
    return [codex_command(cfg, runner, kind, config_file, model)
            for model in codex_models(cfg, rejected)]


def supervise(
    cfg: Any, config_file: Path, *,
    kind: str | None = None,
    runners: dict[str, Runner] | None = None,
    read_usage: Callable[[str], Usage | None] = usage_mod.read,
    launch: Callable[[list[str], dict[str, str], Path], Any] = _launch,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Read until nothing is pending, the limits say wait, or a run goes nowhere; the status."""
    lock = FileLock(_lock_path(cfg))
    if not lock.acquire():
        if kind is not None:
            update_status(cfg, lambda status: status.update(
                {"pending": sorted(_pending(status) | {kind})}))
        return load_status(cfg)
    try:
        return _supervise(cfg, config_file, kind, runners, read_usage, launch, clock, sleep)
    except Exception as exc:
        # A detached worker must leave an actionable state, not a dead running PID.
        reason = f"reader supervisor failed: {type(exc).__name__}"
        frames = [{"module": Path(frame.filename).name, "function": frame.name,
                   "line": frame.lineno} for frame in traceback.extract_tb(exc.__traceback__)]
        log(cfg, "supervisor_failed", reason=reason, frames=frames)
        return update_status(cfg, lambda status: status.update(
            {"state": "failed", "pid": None, "checked_at": clock(), "reason": reason}))
    finally:
        lock.release()


def _supervise(cfg: Any, config_file: Path, kind: str | None,
               runners: dict[str, Runner] | None,
               read_usage: Callable[[str], Usage | None],
               launch: Callable[[list[str], dict[str, str], Path], Any],
               clock: Callable[[], float], sleep: Callable[[float], None]) -> dict[str, Any]:
    if kind is not None:
        update_status(cfg, lambda status: status.update(
            {"pending": sorted(_pending(status) | {kind})}))
    runners = dict(runners if runners is not None else find_runners(cfg, config_file))
    every_gate = gates(cfg)
    no_progress = {name: 0 for name in KINDS}

    def settle(state: str, **fields: Any) -> dict[str, Any]:
        def change(status: dict[str, Any]) -> None:
            status.update({"state": state, "pid": None, "stop_run": None,
                           "checked_at": clock(), **fields})
        return update_status(cfg, change)

    for _ in range(RUNS_PER_START):
        now = clock()
        status = load_status(cfg)
        pending = {name for name in _pending(status) if enabled(cfg, name)}
        if _paused(cfg):
            return settle("paused", reason="varyz hooks are paused")
        if (until := _deferred(cfg, now)) is not None:
            return _settle_scheduled(cfg, until, settle)
        if not pending:
            return settle("idle", pending=[], reason="nothing waiting to be read",
                          resume_after=None, next_check=None)
        if not runners:
            return settle("no_runner", reason="neither Claude Code nor Codex can run the "
                          "reader here; use the working agent fallback",
                          resume_after=None, next_check=now + RECHECK_SECONDS)
        usages = {name: read_usage(name) for name in runners}
        readings = {name: found.as_dict() for name, found in usages.items() if found is not None}
        update_status(cfg, lambda status, readings=readings: status.setdefault(
            "usage", {}).update(readings))
        chosen: tuple[str, Choice] | None = None
        waits: list[Choice] = []
        for job in [name for name in KINDS if name in pending]:
            guarded = {name: (found if found is not None and found.known else
                             _blocked_history(status, name, every_gate[job], now) or found)
                       for name, found in usages.items()}
            choice = choose(job, every_gate[job], guarded, now)
            if choice.runner is not None:
                chosen = (job, choice)
                break
            waits.append(choice)
        if chosen is None:
            resume = min((c.resume_after for c in waits if c.resume_after), default=None)
            reason = waits[0].reason if waits else "no runner"
            log(cfg, "waiting", pending=sorted(pending), reason=reason, resume_after=resume)
            return settle("waiting_for_usage", reason=reason, resume_after=resume,
                          next_check=min(resume or now + RECHECK_SECONDS, now + RECHECK_SECONDS))
        job, choice = chosen
        runner = runners[str(choice.runner)]
        before = _work_left(cfg, job, now)
        rejected = list(status.get("rejected_models") or [])
        commands = _commands(cfg, runner, job, config_file, rejected)
        if not commands:
            runners.pop(runner.name)
            log(cfg, "no_model", runner=runner.name)
            continue
        result: RunResult | None = None
        for command in commands:
            log(cfg, "started", runner=runner.name, kind=job, reason=choice.reason,
                turns_waiting=before[0])
            result = run_one(cfg, runner, job, command, gate=every_gate[job],
                             read_usage=read_usage, launch=launch, clock=clock, sleep=sleep)
            if result.rejected_model is None:
                break
            model = result.rejected_model
            update_status(cfg, lambda status, model=model: status.update({"rejected_models": sorted(
                set(status.get("rejected_models") or []) | {model})}))
            log(cfg, "model_rejected", runner=runner.name, model=model)
        assert result is not None
        after = _work_left(cfg, job, clock())
        log(cfg, "ended", runner=runner.name, kind=job, exit_code=result.exit_code,
            stopped=result.stopped, error=result.error, turns_waiting=after[0],
            notes=after[3], usage=describe(result.usage))
        if result.usage is not None:
            reading = {runner.name: result.usage.as_dict()}
            update_status(cfg, lambda status, reading=reading: status.setdefault(
                "usage", {}).update(reading))
        last = {"kind": job, "runner": runner.name, "ended_at": clock(),
                "exit_code": result.exit_code, "stopped": result.stopped, "error": result.error}
        update_status(cfg, lambda status, last=last: status.update({"last_run": last}))
        if result.stopped == "put off" and (until := _deferred(cfg, clock())) is not None:
            return _settle_scheduled(cfg, until, settle)
        if result.stopped in {"asked", "turned off"}:
            return settle("stopped", reason=result.stopped, pending=[])
        if result.stopped and result.stopped.startswith("usage"):
            if result.stopped == "usage: unknown":
                # Not run blind again: without a reading it waits for the next check.
                runners.pop(runner.name)
            # The other sign-in may still have room: the next pass looks.
            continue
        done = after[0] == 0 and after[1] == 0
        if done:
            finished = set(KINDS) if job == "full" else {job}
            update_status(cfg, lambda status, finished=finished: status.update(
                {"pending": sorted(_pending(status) - finished)}))
            continue
        if after == before:
            no_progress[job] += 1
            if no_progress[job] >= NO_PROGRESS_LIMIT:
                update_status(cfg, lambda status, job=job: status.update(
                    {"pending": sorted(_pending(status) - {job})}))
                return settle("failed", reason=f"{runner.name} read nothing in "
                              f"{NO_PROGRESS_LIMIT} runs: {result.error or 'no error given'}")
        else:
            no_progress[job] = 0
    return settle("waiting_for_usage" if _pending(load_status(cfg)) else "idle",
                  reason="started again later", next_check=clock() + 60)


def _settle_scheduled(cfg: Any, until: float,
                      settle: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    pending = sorted(_pending(load_status(cfg)) | ({"full"} if enabled(cfg, "full") else set()))
    return settle("scheduled", reason="the owner asked to read later", pending=pending,
                  resume_after=None, next_check=until)


# -- starting it --------------------------------------------------------------------


def schedule(cfg: Any, until: float, now: float) -> str:
    """Put the reading off until ``until``, then read: what the owner's agent confirms.

    A reading under way stops after the task in hand (`run_one`, and the MCP
    tool answers done). varyz's own reader starts again at that time by itself
    (`maybe_resume`, the daemon's `ResumeWatcher`); the working agent's reader
    is asked for again in the first session after it.
    """
    from varyz.resolution.profile_reader import defer_reading

    defer_reading(cfg, until)
    when = _local(until)
    if not enabled(cfg, "full"):
        return (f"Reading put off until {when}. Your agent starts it in your first "
                "session after that.")

    def change(status: dict[str, Any]) -> None:
        status["pending"] = sorted(_pending(status) | {"full"})
        if status.get("state") != "running":
            status.update({"state": "scheduled", "reason": "the owner asked to read later",
                           "next_check": until, "resume_after": None, "checked_at": now})
    update_status(cfg, change)
    log(cfg, "scheduled", until=until)
    return (f"Reading put off until {when}; varyz starts it then by itself, while this "
            "computer is on. `varyz rules read --now` starts it sooner.")


def _running(cfg: Any) -> bool:
    lock = FileLock(_lock_path(cfg))
    if not lock.acquire():
        return True
    lock.release()
    return False


def spawn(config_file: Path | None, kind: str | None = None) -> None:
    """Start the supervisor in the background, with no window, and do not wait for it."""
    command = [sys.executable, "-m", "varyz", "rules", "read"]
    if kind is not None:
        command += ["--kind", kind]
    if config_file is not None:
        command += ["--config", str(config_file)]
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(  # noqa: S603 - fixed local module invocation
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
        start_new_session=sys.platform != "win32",
    )


def request(cfg: Any, kind: str, config_file: Path | None, *,
            runners: dict[str, Runner] | None = None,
            start: Callable[[Path | None, str | None], None] | None = None,
            now: float | None = None) -> bool:
    """Hand a reading to varyz's own reader; False when it cannot take it (ask the agent then).

    A job waiting for the limits stays queued until its next check.
    """
    if not enabled(cfg, kind) or config_file is None:
        return False
    config_file = Path(config_file).resolve()
    if runners is None:
        runners = find_runners(cfg, config_file)
    if not runners:
        return False
    now = now if now is not None else time.time()
    status = update_status(cfg, lambda status: status.update(
        {"pending": sorted(_pending(status) | {kind})}))
    if _running(cfg):
        return True
    if status.get("state") == "waiting_for_usage" and now < float(status.get("next_check") or 0):
        return True
    update_status(cfg, lambda status: status.update({"state": "queued", "checked_at": now}))
    try:
        (start or spawn)(config_file, None)
    except (OSError, subprocess.SubprocessError):
        update_status(cfg, lambda status: status.update(
            {"state": "failed", "reason": "reader supervisor could not start", "pid": None}))
        return False
    return True


def hand_off(cfg: Any, offer: dict[str, Any] | None, config_file: Path | None,
             now: float) -> dict[str, Any] | None:
    """The reader's ask for the agent, or None once varyz's own reader took it.

    A major reading varyz's own reader took still comes back as a notice: no
    subagent to start, only the one line the owner is told. With no ask due, a
    job that waited for the limits is started again when its check is due.
    """
    if offer is None:
        with contextlib.suppress(Exception):
            maybe_resume(cfg, config_file, now)
        return None
    kind = "live" if offer.get("live") else "full"
    try:
        taken = request(cfg, kind, config_file, now=now)
    except Exception:
        taken = False
    if not taken:
        return offer
    if offer.get("setup"):
        from varyz.resolution.profile_reader import NOT_NOW

        return {
            "turns_waiting": offer.get("turns_waiting", 0),
            "setup": offer["setup"],
            "do": ("This is the last step of the owner's varyz setup, and varyz's own "
                   "reader already started it in the background; start nothing. Before "
                   "you plan the task, tell the user in a short block, in your own words "
                   f"and without shortening what it does: {offer['setup']} {NOT_NOW} "
                   "`varyz rules read --status` shows how far it got."),
        }
    if offer.get("announce"):
        return {
            "turns_waiting": offer.get("turns_waiting", 0),
            "announce": offer["announce"],
            "do": ("Varyz's own reader started this in the background; start nothing. "
                   "Tell the user once, in one line and in your own words: "
                   f"{offer['announce']}"),
        }
    return None


def maybe_resume(cfg: Any, config_file: Path | None, now: float, *,
                 start: Callable[[Path | None, str | None], None] | None = None) -> bool:
    """Start a waiting job again once its check is due; True when one was started."""
    status = load_status(cfg)
    if not _pending(status) or status.get("state") not in {
        None, "queued", "no_runner", "waiting_for_usage", "running", "scheduled",
    }:
        return False
    if status.get("state") in {"waiting_for_usage", "no_runner", "scheduled"} and now < float(
        status.get("next_check") or 0
    ):
        return False
    if _running(cfg) or _paused(cfg) or _deferred(cfg, now) is not None:
        return False
    if not any(enabled(cfg, kind) for kind in _pending(status)) or config_file is None:
        return False
    update_status(cfg, lambda status: status.update({"state": "queued", "checked_at": now}))
    (start or spawn)(config_file, None)
    return True


def health(cfg: Any, *, now: float | None = None) -> tuple[bool, str]:
    """Distinguish healthy quota waits from overdue work and crashed workers."""
    from varyz.autosync import process_alive

    now = time.time() if now is None else now
    status = load_status(cfg)
    pending = _pending(status)
    if _paused(cfg) or not any(enabled(cfg, kind) for kind in pending or {"full"}):
        return True, "reader paused or delegated to the working agent"
    state = status.get("state")
    if state == "failed":
        return False, "last reading failed; run varyz rules read --status"
    if state == "running":
        pid = status.get("pid")
        if isinstance(pid, int) and process_alive(pid):
            return True, "reader process running"
        # The reader has exited but its supervisor is still recording the result
        # (output, counts, log) before it settles; that is not a crash. Bounded
        # by the longest run so a reused supervisor pid cannot hide one.
        supervisor = status.get("supervisor_pid")
        longest = (float(getattr(cfg.workflows, "reader_max_minutes", 60)) * 60
                   + STOP_GRACE_SECONDS + 300)
        if (isinstance(supervisor, int) and process_alive(supervisor)
                and now - float(status.get("started_at") or 0) <= longest):
            return True, "reader finishing"
        if pid is None and now - float(status.get("started_at") or 0) <= 120:
            return True, "reader starting"
        return False, "reader process is no longer running; run varyz rules read --status"
    if pending and state == "scheduled":
        due = float(status.get("next_check") or 0)
        if now > due + 180:
            return False, "scheduled reading has not started; run varyz rules read --status"
        return True, "reading scheduled for " + _local(due)
    if pending and state == "waiting_for_usage":
        due = float(status.get("next_check") or status.get("resume_after") or 0)
        if not due or now > due + 180:
            return False, "reader quota recheck overdue; run varyz rules read --status"
        return True, "waiting for usage limits; next check scheduled"
    if pending and state in {None, "queued"}:
        if now - float(status.get("checked_at") or 0) > 120:
            return False, "queued reading has not started; run varyz rules read --status"
        return True, "reading queued"
    last = status.get("last_run") or {}
    if state not in {"waiting_for_usage", "no_runner", "paused", "stopped", "scheduled"} and (
        last.get("exit_code") not in {None, 0} and not last.get("stopped")
    ):
        return False, "last reading failed; run varyz rules read --status"
    return True, ("working agent fallback required" if state == "no_runner"
                  else "no recorded reader failure")


class ResumeWatcher:
    """The daemon's side: starts a job that waited for the limits once they allow it."""

    def __init__(self, config_file: Path, *, every: float = 60.0) -> None:
        self.config_file = config_file
        self.every = every
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="varyz-reader-resume", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from varyz.config import load_config

        while not self._stop.wait(self.every):
            with contextlib.suppress(Exception):
                maybe_resume(load_config(self.config_file), self.config_file, time.time())


def summary(cfg: Any, now: float | None = None) -> str:
    """One line for the doctor and `varyz rules read --status`."""
    status = load_status(cfg)
    now = now if now is not None else time.time()
    state = status.get("state") or "never run"
    parts = [state]
    pending = sorted(_pending(status))
    if pending:
        parts.append("waiting to read: " + ", ".join(pending))
    if status.get("reason"):
        parts.append(str(status["reason"]))
    if state == "scheduled" and status.get("next_check"):
        parts.append("starts " + _local(float(status["next_check"])))
    resume = status.get("resume_after")
    if state == "waiting_for_usage" and resume:
        parts.append("resumes " + datetime.fromtimestamp(float(resume), UTC)
                     .astimezone().strftime("%a %H:%M"))
    last = status.get("last_run")
    if isinstance(last, dict):
        ended = last.get("ended_at")
        ago = f"{(now - float(ended)) / 3600:.1f}h ago" if ended else ""
        outcome = last.get("stopped") or ("ok" if last.get("exit_code") == 0 else "failed")
        parts.append(f"last {last.get('kind')} run on {last.get('runner')} {ago}: {outcome}")
    return "; ".join(parts)


__all__ = [
    "ENV",
    "Choice",
    "ResumeWatcher",
    "RunResult",
    "Runner",
    "choose",
    "claude_command",
    "codex_command",
    "codex_models",
    "enabled",
    "find_runners",
    "hand_off",
    "load_status",
    "log",
    "maybe_resume",
    "request",
    "run_one",
    "schedule",
    "spawn",
    "stop_requested",
    "summary",
    "supervise",
    "update_status",
]

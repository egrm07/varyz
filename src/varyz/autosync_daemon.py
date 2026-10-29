"""Singleton, console-free recurrence loop for Varyz autosync."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from varyz import desktop_notify
from varyz.autosync import (
    _atomic_json,
    _read_json,
    code_fingerprint,
    daemon_heartbeat_path,
    daemon_launch_log_path,
    daemon_lock_path,
    is_enabled,
    is_paused,
    is_schedule_due,
    run_once,
    spawn_daemon,
)
from varyz.config import load_config
from varyz.db.connection import FileLock
from varyz.domain import iso, utc_now
from varyz.filesystem import make_private

POLL_SECONDS = 5.0
# An edit to the source is often several files written one after another. A
# successor that imports between two of them dies on the half-applied change,
# so the handoff waits until the source has stopped moving for this many polls.
SETTLE_POLLS = 6
# How long the first successor gets to show a heartbeat, doubling per retry.
HANDOFF_WAIT_SECONDS = 60.0
HANDOFF_WAIT_CAP_SECONDS = 600.0


def run_daemon(
    config_path: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_loops: int | None = None,
    restart: Callable[[Path], bool] = spawn_daemon,
) -> int:
    """Refresh when due until disabled; return immediately if another daemon owns the lock."""
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    if not is_enabled(cfg):
        return 0
    lock = _acquire_lock(daemon_lock_path(cfg))
    if lock is None:
        return 0
    with contextlib.suppress(Exception):
        make_private(cfg)
    _trim_log(daemon_launch_log_path(cfg))
    _one_line_network_errors()
    started_at = iso(utc_now()) or ""
    fingerprint = code_fingerprint()
    loops = 0
    stale = False
    moved_to: str | None = None
    settled = 0
    failures = 0
    try:
        while True:
            # One bad poll (a full disk, a config mid-write, a connector that
            # raised) used to end the daemon until the next login. Now it waits
            # a little longer each time and tries again.
            try:
                cfg = load_config(config_path)
                if not is_enabled(cfg):
                    break
                # This process is running the code it imported at launch. Once the
                # installed source moves on, keep nothing of it: hand off to a
                # fresh daemon rather than writing output from a previous release.
                current = code_fingerprint()
                if current != fingerprint:
                    settled = settled + 1 if current == moved_to else 1
                    moved_to = current
                    if settled >= SETTLE_POLLS:
                        stale = True
                        break
                    # No sync from code that is already replaced on disk.
                    _heartbeat(cfg, started_at, "restarting")
                else:
                    moved_to, settled = None, 0
                    status = "paused" if is_paused(cfg) else "idle"
                    _heartbeat(cfg, started_at, status)
                    if status == "idle" and is_schedule_due(cfg):
                        result_status = _sync_with_heartbeat(cfg, config_path, started_at)
                        _heartbeat(cfg, started_at, result_status)
                    if status == "idle":
                        # Shared Varyz invitations and questions, once a minute; never raises.
                        with contextlib.suppress(Exception):
                            desktop_notify.maybe_check(config_path)
                failures = 0
            except Exception as exc:
                failures += 1
                _log(f"poll failed ({failures} in a row), retrying: {_short(exc)}")
            loops += 1
            if max_loops is not None and loops >= max_loops:
                break
            sleep(_backoff(failures))
    finally:
        with contextlib.suppress(Exception):
            _heartbeat(cfg, started_at, "restarting" if stale else "stopped")
        _release_lock(lock)
    if stale:
        # Only after the lock is released, so the successor can take it.
        return 0 if _hand_off(config_path, started_at, restart=restart, sleep=sleep) else 1
    return 0


def _hand_off(
    config_path: Path,
    started_at: str,
    *,
    restart: Callable[[Path], bool],
    sleep: Callable[[float], None],
) -> bool:
    """Launch a successor and stay until one is heartbeating; False once disabled.

    Nothing else restarts the daemon before the next login, so a successor that
    dies at launch (a broken import, a locked environment) would otherwise end
    autosync silently. Retries back off to one every ten minutes.
    """
    wait = HANDOFF_WAIT_SECONDS
    while True:
        cfg = load_config(config_path)
        if not is_enabled(cfg):
            return False
        with contextlib.suppress(Exception):
            restart(config_path)
        waited = 0.0
        while waited < wait:
            sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            if _successor_running(cfg):
                return True
        with contextlib.suppress(Exception):
            _heartbeat(cfg, started_at, "restarting")
        wait = min(wait * 2, HANDOFF_WAIT_CAP_SECONDS)


def _successor_running(cfg) -> bool:
    beat = _read_json(daemon_heartbeat_path(cfg))
    return bool(beat) and beat.get("pid") != os.getpid()


def _sync_with_heartbeat(cfg, config_path: Path, started_at: str) -> str:
    """Keep liveness observable while a connector performs a long blocking scan."""
    outcomes = []
    errors: list[BaseException] = []

    def sync() -> None:
        try:
            outcomes.append(run_once(config_path, trigger="daemon"))
        except BaseException as exc:  # preserve unexpected worker failure after joining
            errors.append(exc)

    worker = threading.Thread(target=sync, name="varyz-autosync-run")
    worker.start()
    while worker.is_alive():
        _heartbeat(cfg, started_at, "syncing")
        worker.join(timeout=POLL_SECONDS)
    if errors:
        from varyz.analytics import record

        record(cfg, "sync_failed")
        raise errors[0]
    from varyz.analytics import record

    if outcomes[0].status in {"ok", "failed", "partial"}:
        record(cfg, "sync_ok" if outcomes[0].status == "ok" else "sync_failed")
    return outcomes[0].status


def _heartbeat(cfg, started_at: str, status: str) -> bool:
    """Write the liveness file; False when the disk refused it, never raises.

    The daemon on a nearly full Mac disk died here, on ENOSPC writing
    daemon.json, and stayed down until the next login.
    """
    try:
        _atomic_json(
            daemon_heartbeat_path(cfg),
            {
                "pid": os.getpid(),
                "started_at": started_at,
                "heartbeat_at": iso(utc_now()),
                "status": status,
            },
        )
    except OSError as exc:
        _log(f"could not write the heartbeat: {_short(exc)}")
        return False
    return True


def _backoff(failures: int) -> float:
    """POLL_SECONDS while healthy; doubling after each failed poll, up to five minutes."""
    return POLL_SECONDS if failures == 0 else min(POLL_SECONDS * 2 ** failures, 300.0)


_last_logged: dict[str, float] = {}


def _log(message: str) -> None:
    """One timestamped line in the launch log; the same line at most every ten minutes."""
    now = time.monotonic()
    if now - _last_logged.get(message, -1e9) < 600:
        return
    _last_logged[message] = now
    with contextlib.suppress(Exception):
        print(f"{iso(utc_now())} varyz autosync: {message}", file=sys.stderr, flush=True)


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:300]


LOG_LIMIT = 2 * 1024 * 1024
LOG_KEEP = 256 * 1024


def _trim_log(log: Path) -> None:
    """Keep the launch log small: past LOG_LIMIT, its last LOG_KEEP bytes move
    to `.1` and it starts again. Trimmed in place, because the launcher holds
    it open for appending."""
    with contextlib.suppress(OSError):
        if log.stat().st_size <= LOG_LIMIT:
            return
        with log.open("rb") as handle:
            handle.seek(-LOG_KEEP, os.SEEK_END)
            tail = handle.read()
        log.with_name(log.name + ".1").write_bytes(tail)
        os.truncate(log, 0)


_NETWORK_ERRORS = ("ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
                   "URLError", "gaierror", "ConnectionError", "TimeoutError")


def _one_line_network_errors() -> None:
    """A thread that dies because the computer is offline says so in one line.

    The test Mac's log held four whole httpx tracebacks for "nodename nor
    servname provided". Anything else keeps its traceback, with a time.
    """
    def is_network(exc: BaseException | None) -> bool:
        while exc is not None:
            if type(exc).__name__ in _NETWORK_ERRORS:
                return True
            exc = exc.__cause__ or exc.__context__
        return False

    previous = threading.excepthook
    if getattr(previous, "varyz_one_line", False):
        return

    def hook(args: threading.ExceptHookArgs) -> None:
        if is_network(args.exc_value):
            _log(f"offline? {_short(args.exc_value)}")  # type: ignore[arg-type]
            return
        _log(f"thread {getattr(args.thread, 'name', '?')} failed")
        previous(args)

    hook.varyz_one_line = True  # type: ignore[attr-defined]
    threading.excepthook = hook


def _acquire_lock(path: Path) -> FileLock | None:
    lock = FileLock(path)
    if not lock.acquire():
        return None
    handle = lock.handle
    handle.seek(0)
    handle.write(str(os.getpid()).encode("ascii").ljust(20, b" "))
    handle.flush()
    return lock


def _release_lock(lock: FileLock) -> None:
    # A released lock names no one: a stale pid in it read as an owner.
    with contextlib.suppress(Exception):
        lock.handle.seek(0)
        lock.handle.write(b" " * 20)
        lock.handle.flush()
    lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep every Varyz source fresh")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_daemon(args.config)


if __name__ == "__main__":
    raise SystemExit(main())

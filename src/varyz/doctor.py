"""Read-only operational checks and quiet, session-scoped failure notices.

No repair, model inference, archive text or credential values leave this module.
Session checks run at most once a minute, and unchanged failures are shown once.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Check = tuple[str, bool, str]
# A clean scan is good for a day; doctor starts the next one after that.
INTEGRITY_MAX_AGE = 24 * 60 * 60


def token_check(conn: sqlite3.Connection, token: str | None) -> tuple[bool, str]:
    """Authenticate without caller_from_token's last-used write."""
    from varyz.trust import _capability_for, hash_agent_token

    if not token or not token.strip():
        return False, "missing credential; reconnect the agent"
    row = conn.execute(
        """SELECT t.agent_id FROM agent_tokens t
           JOIN agent_principals p ON p.id=t.agent_id
           WHERE t.token_hash=? AND t.revoked_at IS NULL AND t.valid_until>?
             AND p.revoked_at IS NULL AND p.trust_state='active'""",
        (hash_agent_token(token.strip()), datetime.now(UTC).isoformat()),
    ).fetchone()
    if row is None:
        return False, "invalid, expired or revoked credential; reconnect the agent"
    if _capability_for(conn, str(row[0]), purpose="owner_context",
                       tool_name="resolve") is None:
        return False, "no active briefing capability; reconnect the agent"
    return True, "credential and briefing capability active"


def diagnose(cfg: Any, *, client_configs: dict[str, Path] | None = None,
             active_token: str | None = None, check_active: bool = False,
             quick: bool = False) -> list[Check]:
    from varyz import hooks, install

    checks: list[Check] = []
    try:
        checks.extend(hooks.doctor(cfg, client_configs=client_configs, quick=quick))
        checks = [(name, ok, "recent session hook failure; inspect the local hook log"
                   if name == "hook errors" and not ok else detail)
                  for name, ok, detail in checks]
    except Exception as exc:
        # A session hook once failed this twice and passed a minute later, and
        # with no cause kept a repair agent had nothing to go on. The type and
        # line are kept, never the message: it can carry paths or SQL.
        kind, where = type(exc).__name__, _where(exc)
        _keep_integration_error(cfg, kind, where)
        checks.append(("integration checks", False,
                       f"could not complete ({kind} at {where}); "
                       "check the archive and agent configuration"))
    if not cfg.db_path.exists():
        return checks
    try:
        with contextlib.closing(hooks._open_readonly(cfg.db_path)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            required = {"entries", "owner_principles", "agent_tokens", "agent_capabilities"}
            checks.append(("archive schema", required <= tables,
                           "required tables present" if required <= tables
                           else "archive needs migration; run varyz daemon start"))
            configs = client_configs if client_configs is not None else install.client_paths()
            for client, path in configs.items():
                if not path.exists():
                    continue
                # Only Varyz-connected clients are expected to have a credential.
                if client == install.CODEX_CLIENT:
                    entry = install._codex_server(path)
                else:
                    data = install._load(path)
                    entry = (data.get("mcpServers") or {}).get(install.SERVER_KEY)
                if entry:
                    from varyz.config import load_config

                    archive_config = install.registered_archive(entry)
                    if archive_config is not None and (
                        load_config(archive_config).db_path.resolve() != cfg.db_path.resolve()
                    ):
                        continue
                    destination = install.instructions_destination(client, path)
                    if destination is not None:
                        instructions = (destination.read_text(encoding="utf-8")
                                        if destination.exists() else "")
                        expected = install.expected_instructions(cfg.source_file, client)
                        ready = expected in instructions
                        checks.append((f"{client} instructions", ready,
                                       "mining and repair instructions current" if ready
                                       else f"missing or outdated; run varyz connect {client}"))
                    token = install._existing_agent_token(client, path)
                    if not token and archive_config is not None:
                        from varyz.resolution.headless_reader import _agent_token

                        token = _agent_token(client, archive_config)
                    checks.append((f"{client} credential", *token_check(conn, token)))
            if check_active:
                checks.append(("current session credential", *token_check(conn, active_token)))
            from varyz.sources import source_statuses

            for source in source_statuses(cfg, conn):
                if not source.connected or source.info.connector_id == "manual":
                    continue
                latest = conn.execute(
                    "SELECT last_run_at,last_status FROM sync_state WHERE connector_id=?"
                    " ORDER BY last_run_at DESC LIMIT 1", (source.info.connector_id,),
                ).fetchone()
                ok = latest is not None and bool(latest[0]) and latest[1] == "ok"
                checks.append((f"{source.info.label} sync", ok,
                               "last sync succeeded" if ok
                               else "no successful latest sync; run varyz sync"))
    except Exception:
        checks.append(("archive checks", False, "could not read operational state"))
    integrity = integrity_check(cfg, start=not quick)
    if integrity is not None:
        checks.append(("archive integrity", *integrity))
    if cfg.workflows.rule_reader_agent:
        from varyz.resolution.headless_reader import health

        checks.append(("background reader", *health(cfg)))
    if cfg.source_file is not None:
        # Every source synced while Cloud said "storage full" for days, and
        # doctor had no line for it.
        try:
            from varyz.cloud.background import health as cloud_health

            cloud = cloud_health(cfg.source_file)
        except Exception:
            cloud = (False, "could not read the Cloud sync state; run varyz cloud status")
        if cloud is not None:
            checks.append(("cloud sync", *cloud))
    from varyz.filesystem import claude_config_exposed, exposed

    open_to_others = exposed(cfg)
    checks.append(("private files", not open_to_others,
                   "other accounts on this computer can read " + ", ".join(map(str, open_to_others))
                   + "; varyz doctor --fix closes them" if open_to_others
                   else "the archive and config are readable only by you"))
    claude_json = claude_config_exposed()
    if claude_json is not None:
        checks.append(("agent settings file", False,
                       f"other accounts can read {claude_json}, which holds Varyz's "
                       f"sign-in token; run: chmod 600 {claude_json}"))
    from varyz.autosync import is_enabled, is_paused

    if is_enabled(cfg) and not is_paused(cfg):
        from varyz.autosync import (
            DAEMON_HEARTBEAT_MAX_AGE,
            _parse_time,
            _read_json,
            daemon_heartbeat_path,
        )

        heartbeat = _read_json(daemon_heartbeat_path(cfg))
        beat = _parse_time(heartbeat.get("heartbeat_at"))
        running = beat is not None and datetime.now(UTC) - beat <= DAEMON_HEARTBEAT_MAX_AGE
        checks.append(("autosync daemon", running,
                       "heartbeat current" if running
                       else "no current heartbeat; run varyz autosync install"))
    from varyz.resolution import availability

    if (availability.directory(cfg) / "ENABLED").exists():
        if availability.is_paused(cfg) or not cfg.resolution.autostart:
            checks.append(("daemon recovery", True, "paused by owner"))
        else:
            from varyz.autosync import _parse_time, _read_json

            record = _read_json(availability.directory(cfg) / "status.json")
            checked_at = _parse_time(record.get("checked_at"))
            current = (checked_at is not None
                       and 0 <= (datetime.now(UTC) - checked_at).total_seconds() <= 180)
            healthy = current and record.get("status") in {"running", "recovered"}
            checks.append(("daemon recovery", healthy,
                           "health check current" if healthy
                           else "no healthy recent check; run varyz daemon install"))
    from varyz.privacy.secrets_scan import redact_tree

    safe, _ = redact_tree({"checks": checks})
    return [tuple(check) for check in safe["checks"]]


def _integrity_path(cfg: Any) -> Path:
    return cfg.data_dir / "runtime" / "doctor" / "integrity.json"


def _integrity_lock(cfg: Any) -> Any:
    from varyz.db.connection import FileLock

    return FileLock(_integrity_path(cfg).with_suffix(".lock"))


def scan_running(cfg: Any) -> bool:
    lock = _integrity_lock(cfg)
    if not lock.acquire():
        return True
    lock.release()
    return False


def last_scan(cfg: Any) -> dict[str, Any]:
    from varyz.hooks import _load_state

    return _load_state(_integrity_path(cfg))


def scan_integrity(cfg: Any, *, now: float | None = None) -> dict[str, Any] | None:
    """Read every archive page and record the result; None when no scan ran."""
    from varyz.hooks import _open_readonly
    from varyz.install import _write_text_atomic

    if not cfg.db_path.exists():
        return None
    lock = _integrity_lock(cfg)
    if not lock.acquire():
        return None
    try:
        started = time.monotonic()
        try:
            with contextlib.closing(_open_readonly(cfg.db_path)) as conn:
                healthy = conn.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        except sqlite3.OperationalError:
            return None  # busy or locked right now, which is not damage
        except sqlite3.DatabaseError:
            healthy = False
        record = {"checked_at": time.time() if now is None else now, "ok": healthy,
                  "seconds": round(time.monotonic() - started, 1)}
        _write_text_atomic(_integrity_path(cfg), json.dumps(record))
        return record
    finally:
        lock.release()


def _start_scan(cfg: Any) -> None:
    command = [sys.executable, "-m", "varyz.doctor", "scan"]
    if cfg.source_file is not None:
        command += ["--config", str(cfg.source_file)]
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW",
                 "BELOW_NORMAL_PRIORITY_CLASS"):
        flags |= getattr(subprocess, name, 0)
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603 - fixed local module invocation
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
            start_new_session=sys.platform != "win32",
        )


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 60 * 60:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def integrity_check(cfg: Any, *, start: bool,
                    now: float | None = None) -> tuple[bool, str] | None:
    """The last archive scan's result, starting a fresh scan in the background.

    The scan reads the whole archive: about five minutes for 2.7 GB, with no
    output, and agents running `varyz doctor` timed out on it. So no caller
    waits for it; `varyz doctor --full` is the one that does.
    """
    if not cfg.db_path.exists():
        return None
    record = last_scan(cfg)
    now = time.time() if now is None else now
    checked_at = record.get("checked_at")
    age = now - checked_at if isinstance(checked_at, int | float) else None
    stale = age is None or not 0 <= age <= INTEGRITY_MAX_AGE
    scanning = False
    if start and stale:
        scanning = True
        if not scan_running(cfg):
            _start_scan(cfg)
    if age is None:
        if not scanning:
            return None
        return True, "not scanned yet; scanning in the background, see the next varyz doctor"
    again = "; scanning again in the background" if scanning else ""
    when = _ago(max(age, 0))
    if not record.get("ok"):
        return False, (f"damage found {when}{again}; back up the archive before any repair, "
                       "then run varyz doctor --full")
    return True, f"ok, scanned {when}{again}"


def _where(exc: BaseException) -> str:
    """The innermost Varyz line an exception passed through, as file:line."""
    import traceback

    try:
        package = Path(__file__).resolve().parent
        for frame in reversed(traceback.extract_tb(exc.__traceback__)):
            path = Path(frame.filename).resolve()
            if path.is_relative_to(package):
                return f"{path.relative_to(package).as_posix()}:{frame.lineno}"
    except (OSError, ValueError):
        pass
    return "outside Varyz"


def _keep_integration_error(cfg: Any, kind: str, where: str) -> None:
    """The last integration failure, for `varyz doctor` and a repair agent."""
    with contextlib.suppress(OSError):
        path = cfg.data_dir / "runtime" / "doctor" / "integration-error.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "type": kind, "where": where}),
                        encoding="utf-8")


def session_notice(cfg: Any, session_id: str, *, active_token: str | None = None,
                   check_active: bool = False, now: float | None = None,
                   config_path: Path | None = None) -> str:
    """Agent-facing instruction to relay a short note, without blocking work."""
    if not session_id or os.environ.get("VARYZ_HEADLESS_READER"):
        return ""
    from varyz.hooks import _load_state, _save_state

    key = hashlib.sha256(session_id.encode()).hexdigest()[:24]
    path = cfg.data_dir / "runtime" / "doctor" / f"{key}.json"
    state = _load_state(path)
    now = time.time() if now is None else now
    if 0 <= now - state.get("checked_at", -60) < 60:
        return ""
    checks = diagnose(cfg, active_token=active_token, check_active=check_active, quick=True)
    failures = sorted(name for name, ok, _ in checks if not ok)
    previous = state.get("failures", [])
    # Persist no credentials, excerpts or raw exception details.
    _save_state(path, {"checked_at": now, "failures": failures})
    from varyz.doctor_repair import observe_health, offer, render

    observe_health(cfg, failures, now=now)
    if failures:
        job = offer(cfg, failures, config_path, now=now)
        if job:
            return render(job)
    if not failures or failures == previous:
        return ""
    shown = ", ".join(failures[:3])
    more = f" and {len(failures) - 3} more checks" if len(failures) > 3 else ""
    return (
        "Varyz operational notice: tell the user once, as a small note in this chat: "
        f"'Varyz needs attention: {shown}{more}. Run varyz doctor for details.' "
        "Continue the task; this notice does not block work or authorise repairs."
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m varyz.doctor scan`: the background archive scan doctor starts."""
    import argparse

    from varyz.config import load_config

    parser = argparse.ArgumentParser(prog="python -m varyz.doctor")
    parser.add_argument("command", choices=["scan"])
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    scan_integrity(load_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

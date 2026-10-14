"""Read-only operational checks and quiet, session-scoped failure notices.

No repair, model inference, archive text or credential values leave this module.
Session checks run at most once a minute, and unchanged failures are shown once.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Check = tuple[str, bool, str]


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
    except Exception:
        checks.append(("integration checks", False,
                       "could not complete; check the archive and agent configuration"))
    if not cfg.db_path.exists():
        return checks
    try:
        with contextlib.closing(hooks._open_readonly(cfg.db_path)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            required = {"entries", "owner_principles", "agent_tokens", "agent_capabilities"}
            checks.append(("archive schema", required <= tables,
                           "required tables present" if required <= tables
                           else "archive needs migration; run varyz daemon start"))
            if not quick:
                healthy = conn.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
                checks.append(("archive integrity", healthy,
                               "ok" if healthy else "archive integrity check failed"))
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
    if cfg.workflows.rule_reader_agent:
        from varyz.resolution.headless_reader import health

        checks.append(("background reader", *health(cfg)))
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

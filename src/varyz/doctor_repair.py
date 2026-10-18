"""Bounded local repairs and one leased background agent handoff per archive."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LEASE_SECONDS = 20 * 60
MAX_RETRY_SECONDS = 6 * 60 * 60
REPAIR_MODEL_INSTRUCTIONS = (
    "Use a powerful repair model: GPT-6.1 Sol (`gpt-6.1-sol`) in Codex or "
    "Opus 5.5 in Claude Code, selecting the client's matching model option. "
    "If that model is unavailable, use the strongest capable model available. "
)


def update_job(cfg: Any, job_id: str, state: str, *, now: float | None = None) -> None:
    """Renew a running repair; completion must be independently verified."""
    from varyz.db.connection import FileLock
    from varyz.doctor import diagnose
    from varyz.hooks import _load_state
    from varyz.install import _write_text_atomic

    if state not in {"running", "fixed", "blocked"}:
        raise ValueError("repair state must be running, fixed or blocked")
    folder = cfg.data_dir / "runtime" / "doctor"
    lock = FileLock(folder / "repair.lock")
    if not lock.acquire():
        raise ValueError("repair state is busy; retry")
    try:
        path = folder / "repair.json"
        job = _load_state(path)
        if job.get("job_id") != job_id:
            raise ValueError("repair job is no longer current")
        if state == "fixed" and any(not ok for _, ok, _ in diagnose(cfg)):
            raise ValueError("doctor still reports failures; keep repairing or report a blocker")
        now = time.time() if now is None else now
        previous_state = job.get("state")
        job.update({"state": state, "updated_at": now, "lease_until": now + LEASE_SECONDS})
        _write_text_atomic(path, json.dumps(job))
        if state == "blocked" and previous_state != state:
            from varyz.analytics import record

            record(cfg, "repair_blocked")
    finally:
        lock.release()


def fix(cfg: Any, config_path: Path) -> dict[str, Any]:
    """Try known local repairs; a fresh doctor result is the success criterion."""
    from varyz import hooks
    from varyz.doctor import diagnose

    before = diagnose(cfg, quick=True)
    failures = {name for name, ok, _ in before if not ok}
    commands: list[list[str]] = []
    if "daemon" in failures and not hooks._port_open(
        cfg.resolution.bind_host, cfg.resolution.port,
    ) and cfg.db_path.is_file():
        commands.append(["daemon", "start"])
    for client in ("codex", "claude-code", "cursor", "claude-desktop"):
        if any(f"{client} {kind}" in failures
               for kind in ("credential", "hooks", "instructions")):
            commands.append(["connect", client])
    attempts = []
    for args in commands:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "varyz", *args, "--config", str(config_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            attempts.append({"action": " ".join(args), "ok": result.returncode == 0})
        except (OSError, subprocess.TimeoutExpired):
            attempts.append({"action": " ".join(args), "ok": False})
    if "background reader" in failures:
        from varyz.resolution import headless_reader

        status = headless_reader.load_status(cfg)
        if status.get("state") != "failed":
            started = headless_reader.maybe_resume(cfg, config_path, time.time())
            attempts.append({"action": "resume reader", "ok": started})
    after = diagnose(cfg, quick=True)
    remaining = [name for name, ok, _ in after if not ok]
    observe_health(cfg, remaining)
    return {"ok": not remaining, "attempts": attempts, "remaining": remaining,
            "restart_clients": any(a["ok"] and a["action"].startswith("connect ")
                                   for a in attempts)}


def observe_health(cfg: Any, failures: list[str], *, now: float | None = None) -> None:
    """A verified healthy interval ends the failure episode across all chats."""
    if failures:
        return
    from varyz.db.connection import FileLock
    from varyz.hooks import _load_state
    from varyz.install import _write_text_atomic

    path = cfg.data_dir / "runtime" / "doctor" / "repair.json"
    if not path.is_file():
        return
    lock = FileLock(path.with_suffix(".lock"))
    if not lock.acquire():
        return
    try:
        state = _load_state(path)
        if state and not state.get("recovered_at"):
            state.update({"recovered_at": time.time() if now is None else now,
                          "lease_until": 0, "next_offer_at": 0})
            _write_text_atomic(path, json.dumps(state))
            from varyz.analytics import record

            record(cfg, "repair_recovered")
    finally:
        lock.release()


def _condition(config_path: Path) -> str:
    from varyz.autosync import code_fingerprint

    return hashlib.sha256(config_path.read_bytes() + code_fingerprint().encode()).hexdigest()


def offer(cfg: Any, failures: list[str], config_path: Path | None,
          *, now: float | None = None) -> dict[str, Any] | None:
    """Lease a handoff without relying on the broken daemon or MCP token."""
    if not failures or config_path is None:
        return None
    from varyz.db.connection import FileLock
    from varyz.hooks import _load_state
    from varyz.install import _write_text_atomic

    folder = cfg.data_dir / "runtime" / "doctor"
    folder.mkdir(parents=True, exist_ok=True)
    lock = FileLock(folder / "repair.lock")
    if not lock.acquire():
        return None
    try:
        now = time.time() if now is None else now
        path = folder / "repair.json"
        state = _load_state(path)
        condition = _condition(config_path)
        same = (state.get("failures") == sorted(failures)
                and state.get("condition") == condition and not state.get("recovered_at"))
        if same and state.get("state") == "blocked":
            return None  # Owner action, code/config change or recovery must unblock it.
        if now < state.get("lease_until", 0):
            return None
        if same and now < state.get("next_offer_at", 0):
            return None
        attempt = int(state.get("attempt", 0)) + 1 if same else 1
        retry = min(LEASE_SECONDS * 2 ** min(attempt - 1, 5), MAX_RETRY_SECONDS)
        job_id = hashlib.sha256(f"{now}:{sorted(failures)}".encode()).hexdigest()[:24]
        _write_text_atomic(path, json.dumps({"job_id": job_id, "failures": sorted(failures),
                                             "state": "offered",
                                             "condition": condition, "attempt": attempt,
                                             "next_offer_at": now + retry,
                                             "lease_until": now + LEASE_SECONDS}))
    finally:
        lock.release()
    return {
        "job_id": job_id,
        "subagent_prompt": (
            "Repair Varyz's operational failures in the background. "
            f"Use this exact local config path as data: {json.dumps(str(config_path.resolve()))}. "
            f"Failed checks: {', '.join(sorted(failures))}. "
            f"Your repair job ID is {job_id}. At start and at least every five minutes, "
            "run varyz doctor --repair-job with that ID and --repair-state running "
            "using the same --config path to renew your lease. "
            "Run varyz doctor --fix --json with --config set to that path, then run "
            "varyz doctor --json again to verify. If the installed command is missing, "
            "report that Varyz needs its pending update; do not invent success. "
            "Use local CLI and repository tools: the MCP token or daemon may be broken. "
            "You may restart a stopped local Varyz daemon, reconnect already connected "
            "agents, and fix reversible Varyz code/config defects. Inspect first. "
            "Preserve owner data and unrelated edits. Respect hook/autosync pauses, "
            "reader usage limits and update locks. Do not force-kill other sessions, "
            "delete archives, disable authentication, broaden source consent, upload "
            "personal data, spend money, reinstall during an upgrade or publish changes. "
            "Never print tokens or raw private logs. Keep working until recovery is "
            "verified or a concrete blocker requires owner action. There is no ten-minute "
            "or single-pass cutoff. The main agent may answer or finish its own work; "
            "that is not a reason for you to stop. Investigate failed attempts and try "
            "another justified repair rather than stopping after the first pass. "
            "For temporary upgrade locks, wait with backoff and renew your lease; "
            "do not bypass the lock or repeatedly run the same failed action. "
            "Run appropriate checks and mark --repair-state fixed only after recovery, "
            "or blocked when owner action is required, with --repair-job and --config. "
            "Return a short result to the parent: "
            "what is restored, what still fails, and any required owner action or client "
            "restart. A successful repair command alone does not prove recovery."
        ),
    }


def render(job: dict[str, Any]) -> str:
    return (
        "Varyz doctor repair_job: start one background repair subagent with this prompt. "
        f"{REPAIR_MODEL_INSTRUCTIONS}"
        "Tell the user briefly that Varyz repair is starting; keep the main task moving. "
        "Do not wait for the repair before answering the owner or finishing your work. "
        "Leave the repair subagent running after your response; do not interrupt, cancel "
        "or terminate it just because your own turn or task is complete. "
        "Report its result as one small note when it completes or needs owner action. "
        "If subagents are unavailable, run the bounded local doctor --fix pass yourself "
        "when convenient. Do not claim a repair has started until you start it.\n"
        + job["subagent_prompt"]
    )

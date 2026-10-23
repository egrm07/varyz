"""Hand-offs between sessions and computers, so the owner never re-briefs an agent.

* ``capture`` runs in the background after every turn (the stop hook starts it).
  Turns the agent did not journal with record_progress become mined reports of
  what was done and what is still open, read from this chat's own transcript in
  an approved folder. Reports of projects approved for Cloud go up at once.
* ``open_work`` is what was left unfinished in a project: from Cloud when it is
  set up, so work from any computer shows, and from the local journal otherwise.
  The first briefing of every session carries it as "where you left off". Cloud
  takes seconds to answer, so the briefing reads a local copy of its last answer
  and refreshes that copy in the background; agent tools ask Cloud live.

Both fail quietly: a hand-off is a convenience, never a reason to block a turn.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

MAX_THREADS = 3
CLOUD_TIMEOUT_SECONDS = 25.0
CACHE_FRESH_SECONDS = 120  # older copies are refreshed in the background
CACHE_MAX_AGE_SECONDS = 3 * 24 * 3600  # older copies are not shown at all
_FIELDS = ("task", "status", "done", "pending", "checks", "artifacts", "conversation_id",
           "agent", "occurred_at", "provenance", "reference", "thread_id", "claimed", "match")


# -- capture -------------------------------------------------------------------


def can_capture(cfg: Any) -> bool:
    """Cheap test for the hook: approved chats, an archive, and no pause."""
    from varyz.session_miner import approval_scope
    from varyz.session_progress import pause_path

    return (
        cfg.db_path.exists()
        and approval_scope(cfg) is not None
        and not pause_path(cfg.data_dir).exists()
    )


def capture_in_background(config_file: Path | None, project: str, conversation_id: str,
                          transcript: str) -> None:
    """Start ``capture`` in a hidden process and return at once."""
    _spawn(config_file, ["capture", "--project", project, "--conversation-id",
                         conversation_id, "--transcript", transcript])


def refresh_in_background(config_file: Path | None, project: str) -> None:
    """Start a refresh of the local copy of Cloud's open work and return at once."""
    _spawn(config_file, ["refresh", "--project", project])


def _spawn(config_file: Path | None, arguments: list[str]) -> None:
    command = [sys.executable, "-m", "varyz.handoff", *arguments]
    if config_file is not None:
        command += ["--config", str(config_file)]
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(  # noqa: S603 - fixed local module invocation
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        start_new_session=sys.platform != "win32",
    )


def capture(cfg: Any, config_file: Path | None, *, project: str, conversation_id: str,
            transcript: Path) -> dict[str, Any]:
    """Mine this chat's unjournaled turns, then send approved reports to Cloud."""
    from varyz.db.connection import FileLock, connect
    from varyz.session_miner import mine

    if not can_capture(cfg):
        return {"skipped": "not_approved_or_paused"}
    lock = FileLock(cfg.data_dir / "session-mining.lock")
    if not lock.acquire():
        return {"skipped": "busy"}  # the next turn's capture catches up
    try:
        conn = connect(cfg.db_path)
        try:
            stats = mine(conn, cfg, project=project, apply=True,
                         conversation_id=conversation_id, transcript=transcript, limit=50)
            result: dict[str, Any] = {"reports": stats["reports"], "errors": stats["errors"]}
            if stats["reports"] and config_file is not None:
                result["cloud"] = _send_reports(conn, config_file)
        finally:
            conn.close()
    finally:
        lock.release()
    if result.get("cloud") == "sent":
        refresh_cloud(cfg, config_file, project)
    return result


def _send_reports(conn: Any, config_file: Path) -> str:
    from varyz.cloud.client import CloudClient, credential_name, load_tokens
    from varyz.cloud.session_report_sync import ReportSync, journal_path, read_journal

    try:
        journal = read_journal(journal_path(config_file))
        if not journal["enabled"] or not journal.get("url") or not journal.get("account_id"):
            return "off"
        name = credential_name(config_file, journal["url"])
        client = CloudClient(journal["url"], load_tokens(name), credential=name)
        try:
            ReportSync(conn, config_file).sync(client, journal["account_id"])
        finally:
            client.close()
        return "sent"
    except Exception:
        # The daemon's live sync retries; provider errors can carry private text.
        return "deferred"


# -- open work -------------------------------------------------------------------


def open_work(cfg: Any, config_file: Path | None, *, project: str, task: str = "",
              conversation_id: str | None = None,
              cloud: Literal["live", "cached"] = "live") -> dict[str, Any]:
    """Unfinished threads in this project, newest and most relevant first.

    Cloud covers every computer; the local journal covers this one and answers
    when Cloud is not set up, not approved for the project, or unreachable.
    ``cached`` never waits on the network: it reads the last Cloud answer and
    refreshes it in the background when it is old.
    """
    if cloud == "live":
        remote = _cloud_threads(config_file, project, task, conversation_id)
        if remote is not None:
            _write_cache(cfg, project, remote)
    else:
        remote = _cached_threads(cfg, config_file, project)
    local = _local_current(cfg, project, conversation_id)
    threads: list[dict[str, Any]] = []
    source = "local" if remote is None else "cloud"
    for thread in remote or []:
        mine = local.get(str(thread.get("conversation_id")))
        # This computer may have moved the thread on since Cloud last answered.
        if mine and str(mine["occurred_at"]) > str(thread.get("occurred_at") or ""):
            continue
        threads.append(thread)
    seen = {t.get("conversation_id") for t in threads}
    for conversation, report in local.items():
        if len(threads) >= MAX_THREADS:
            break
        if conversation not in seen and _left_open(report):
            threads.append({k: report[k] for k in _FIELDS if k in report})
            if source == "cloud":
                source = "cloud+local"
    threads = [t for t in threads if t.get("conversation_id") != conversation_id]
    return {"project": project, "source": source, "threads": threads[:MAX_THREADS]}


def _left_open(report: dict[str, Any]) -> bool:
    from varyz.session_progress import left_open

    return left_open(report)


def refresh_cloud(cfg: Any, config_file: Path | None, project: str) -> bool:
    """Ask Cloud for the project's open work and keep a local copy for briefings."""
    remote = _cloud_threads(config_file, project, "", None)
    if remote is None:
        return False
    _write_cache(cfg, project, remote)
    return True


def warm(cfg: Any, config_file: Path | None, project: str) -> None:
    """Refresh the local copy of Cloud's open work in the background if it is old."""
    _cached_threads(cfg, config_file, project)


def _cache_path(cfg: Any, project: str) -> Path:
    name = hashlib.sha256(project.casefold().encode("utf-8")).hexdigest()[:24]
    return Path(cfg.data_dir) / "runtime" / "handoff" / f"{name}.json"


def _write_cache(cfg: Any, project: str, threads: list[dict[str, Any]]) -> None:
    path = _cache_path(cfg, project)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(),
                                         "threads": threads}, ensure_ascii=False),
                             encoding="utf-8")
        os.replace(temporary, path)


def _cached_threads(cfg: Any, config_file: Path | None,
                    project: str) -> list[dict[str, Any]] | None:
    """The last Cloud answer, or None; starts a refresh when it is missing or old."""
    age: float | None = None
    threads: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        cached = json.loads(_cache_path(cfg, project).read_text(encoding="utf-8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(cached["fetched_at"])).total_seconds()
        threads = [t for t in cached["threads"] if isinstance(t, dict) and _left_open(t)]
    if (age is None or age > CACHE_FRESH_SECONDS) and _cloud_configured(config_file, project):
        with contextlib.suppress(Exception):
            refresh_in_background(config_file, project)
    if age is None or age > CACHE_MAX_AGE_SECONDS:
        return None
    return threads


def _cloud_configured(config_file: Path | None, project: str) -> bool:
    """Whether Cloud reports are on for this project (no network, no secrets)."""
    if config_file is None:
        return False
    try:
        from varyz.cloud.session_report_sync import journal_path, read_journal

        journal = read_journal(journal_path(config_file))
    except Exception:
        return False
    return bool(journal["enabled"] and project.casefold() in journal["projects"])


def _cloud_threads(config_file: Path | None, project: str, task: str,
                   conversation_id: str | None) -> list[dict[str, Any]] | None:
    from varyz.cloud.report_bridge import call

    if not _cloud_configured(config_file, project):
        return None
    arguments: dict[str, Any] = {"project": project, "task": task[:240] or project}
    if conversation_id:
        arguments["conversation_id"] = conversation_id
    try:
        result = call(config_file, "open", arguments, timeout=CLOUD_TIMEOUT_SECONDS)
    except Exception:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("threads"), list):
        return None
    return [
        {k: t[k] for k in _FIELDS if k in t} for t in result["threads"]
        if isinstance(t, dict) and _left_open(t)
    ]


def _local_current(cfg: Any, project: str,
                   conversation_id: str | None) -> dict[str, dict[str, Any]]:
    """This computer's current report per recent conversation, newest first."""
    import sqlite3

    from varyz.session_progress import HistoryQuery, history

    if not cfg.db_path.exists():
        return {}
    try:
        uri = cfg.db_path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            query = HistoryQuery(project=project, exclude_conversation_id=conversation_id,
                                 latest=False, limit=10)
            found = history(conn, query, max_chars=12_000, cfg=cfg)["reports"]
    except Exception:
        return {}
    current: dict[str, dict[str, Any]] = {}
    for report in found:
        current.setdefault(str(report["conversation_id"]), report)
    return current


def render(work: dict[str, Any]) -> str:
    """The "where you left off" section of a briefing, or '' with nothing open."""
    from varyz.session_progress import MINED_QUALIFIER

    threads = work.get("threads") or []
    if not threads:
        return ""
    where = {"cloud": "Varyz Cloud (all computers)", "cloud+local": "Varyz Cloud and this computer",
             "local": "this computer"}.get(work.get("source", ""), "this computer")
    lines = [
        f"Where you left off in {work['project']} (unfinished work from earlier sessions, "
        f"from {where}; agent reports, not verified). If the owner's request continues one "
        "of these, or is vague like 'continue', pick it up from here: do not ask the owner "
        "to brief you again. Check the artifacts before relying on a claim. For a Cloud "
        "thread, call claim_work with its thread_id first, and use that thread_id in "
        "record_progress. Ignore threads the request is not about."
    ]
    for number, thread in enumerate(threads, 1):
        when = str(thread.get("occurred_at") or "")[:16].replace("T", " ")
        head = f"{number}. [{thread.get('status')}] {thread.get('task')}"
        meta = [m for m in (when, _client(thread.get("agent")),
                            "claimed by another session" if thread.get("claimed") else "")
                if m]
        lines.append(head + (f" ({', '.join(meta)})" if meta else ""))
        for label, key in (("done", "done"), ("open", "pending"), ("checks", "checks"),
                           ("artifacts", "artifacts")):
            items = [str(item) for item in thread.get(key) or [] if item != MINED_QUALIFIER]
            if items:
                lines.append(f"   {label}: " + "; ".join(items))
        ids = [f"conversation_id {thread['conversation_id']}"] if thread.get(
            "conversation_id") else []
        if thread.get("thread_id"):
            ids.append(f"thread_id {thread['thread_id']}")
        if ids:
            lines.append("   " + ", ".join(ids))
    return "\n".join(lines)


def _client(agent: Any) -> str:
    text = str(agent or "")
    for needle, name in (("codex", "Codex"), ("claude", "Claude Code"),
                         ("openclaw", "OpenClaw"), ("hermes", "Hermes")):
        if needle in text.casefold():
            return name
    return ""


# -- entry point -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m varyz.handoff")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("capture")
    run.add_argument("--project", required=True)
    run.add_argument("--conversation-id", required=True)
    run.add_argument("--transcript", required=True)
    run.add_argument("--config", type=Path)
    show = commands.add_parser("open")
    show.add_argument("--project", required=True)
    show.add_argument("--task", default="")
    show.add_argument("--config", type=Path)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--project", required=True)
    refresh.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    from varyz.config import find_config_file, load_config

    config_file = find_config_file(args.config)
    cfg = load_config(config_file)
    if args.command == "capture":
        try:
            result = capture(cfg, config_file, project=args.project,
                             conversation_id=args.conversation_id,
                             transcript=Path(args.transcript))
        except Exception:
            return 0  # counts only; transcripts may hold private text
    elif args.command == "refresh":
        result = {"refreshed": refresh_cloud(cfg, config_file, args.project)}
    else:
        result = open_work(cfg, config_file, project=args.project, task=args.task)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

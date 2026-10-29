"""Hand-offs between sessions and computers, so the owner never re-briefs an agent.

* ``capture`` runs in the background after every turn (the stop hook starts it).
  Turns the agent did not journal with record_progress become mined reports of
  what was done and what is still open, read from this chat's own transcript in
  an approved folder. Reports of projects approved for Cloud go up at once.
* ``sweep`` runs in the background when a session starts. It mines the
  project's chats written in the last two days, which catches a turn whose stop
  hook never ran: an agent whose usage ran out mid-task stops without one.
* ``open_work`` is what was left unfinished in a project. The local journal
  answers for every agent on this computer, with or without Cloud; Cloud adds
  work from other computers when it is set up. The first briefing of every
  session carries it as "where you left off". Cloud takes seconds to answer, so
  the briefing reads a local copy of its last answer and refreshes that copy in
  the background; agent tools ask Cloud live and fall back to the copy.
* ``claim`` keeps two agents from picking up the same thread at once.

Both fail quietly: a hand-off is a convenience, never a reason to block a turn.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

MAX_THREADS = 3
SWEEP_DAYS = 2
SWEEP_EVERY_SECONDS = 10 * 60
SWEEP_WAIT_SECONDS = 4.0  # the first briefing waits this long for a running sweep
CLOUD_TIMEOUT_SECONDS = 25.0
CACHE_FRESH_SECONDS = 120  # older copies are refreshed in the background
CACHE_MAX_AGE_SECONDS = 3 * 24 * 3600  # older copies are not shown at all
CAPTURE_WAIT_SECONDS = 90.0  # a capture waits this long for a sweep that is mining
SIGNAL_DAYS = 30
_FIELDS = ("task", "status", "done", "pending", "checks", "artifacts", "conversation_id",
           "agent", "occurred_at", "provenance", "reference", "thread_id", "claimed", "match",
           "stopped", "usage_left")


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
                          transcript: str, folder: Path | None = None) -> None:
    """Start ``capture`` in a hidden process and return at once."""
    _spawn(config_file, ["capture", "--project", project, "--conversation-id",
                         conversation_id, "--transcript", transcript,
                         *(["--folder", str(folder)] if folder else [])])


def sweep_in_background(cfg: Any, config_file: Path | None, project: str,
                        folder: Path | None = None) -> bool:
    """Start ``sweep`` unless one ran for this project in the last ten minutes."""
    marker = _sweep_marker(cfg, project)
    with contextlib.suppress(OSError):
        if time.time() - marker.stat().st_mtime < SWEEP_EVERY_SECONDS:
            return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("running", encoding="utf-8")
    _spawn(config_file, ["sweep", "--project", project,
                         *(["--folder", str(folder)] if folder else [])])
    return True


def wait_for_sweep(cfg: Any, project: str) -> None:
    """Let a sweep that just started finish, briefly, so a briefing sees its reports."""
    marker = _sweep_marker(cfg, project)
    deadline = time.monotonic() + SWEEP_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            if marker.read_text(encoding="utf-8") != "running":
                return
            if time.time() - marker.stat().st_mtime > 60:
                return  # a sweep that died does not hold briefings up
        except OSError:
            return
        time.sleep(0.1)


def _sweep_marker(cfg: Any, project: str) -> Path:
    name = hashlib.sha256(project.casefold().encode("utf-8")).hexdigest()[:24]
    return Path(cfg.data_dir) / "runtime" / "handoff" / f"sweep-{name}"


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
            transcript: Path, folder: Path | None = None) -> dict[str, Any]:
    """Mine this chat's unjournaled turns, then send approved reports to Cloud.

    Waits for a sweep or another capture that is mining: a capture at low usage
    may be the last chance to save this chat's state before the agent stops.
    """
    deadline = time.monotonic() + CAPTURE_WAIT_SECONDS
    while True:
        result = _mine(cfg, config_file, project=project, conversation_id=conversation_id,
                       transcript=transcript, folder=folder)
        if result.get("skipped") != "busy" or time.monotonic() >= deadline:
            return result
        time.sleep(0.5)


def sweep(cfg: Any, config_file: Path | None, *, project: str,
          folder: Path | None = None) -> dict[str, Any]:
    """Mine the project's chats from the last two days, from any agent."""
    try:
        for _ in range(int(SWEEP_WAIT_SECONDS / 0.2)):
            result = _mine(cfg, config_file, project=project, folder=folder,
                           modified_since=time.time() - SWEEP_DAYS * 24 * 3600)
            if result.get("skipped") != "busy":
                return result
            time.sleep(0.2)  # a turn's capture is finishing
        return result
    finally:
        with contextlib.suppress(OSError):
            _sweep_marker(cfg, project).write_text("done", encoding="utf-8")


def _mine(cfg: Any, config_file: Path | None, *, project: str, folder: Path | None,
          conversation_id: str | None = None, transcript: Path | None = None,
          modified_since: float | None = None) -> dict[str, Any]:
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
                         conversation_id=conversation_id, transcript=transcript, limit=50,
                         folder=folder, modified_since=modified_since)
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
    """Unfinished threads in this project, the ones the task names first, then the newest.

    This computer's journal always answers, so a hand-off between agents here needs
    no Cloud: a thread stays open until any agent's newest report on it closes it.
    Cloud adds threads from other computers when it is set up for the project.
    ``live`` asks it and falls back to the last copy of its answer; ``cached`` never
    waits on the network and refreshes an old copy in the background. ``cloud`` in
    the result says which of these the threads include.
    """
    remote: list[dict[str, Any]] | None = None
    state = "off"
    as_of = ""
    if cloud == "live":
        remote = _cloud_threads(config_file, project, task, conversation_id)
        if remote is not None:
            _write_cache(cfg, project, remote)
            state = "live"
    if remote is None and _cloud_configured(config_file, project):
        copy = _cached_threads(cfg, config_file, project)
        if copy is not None:
            remote, as_of = copy
            state = "cached"
        else:
            state = "unavailable"
    local = _local_threads(cfg, project)
    claims = _claims(cfg)
    known = {str(t["thread_id"]): t for t in local}
    known.update((str(t["conversation_id"]), t) for t in local)
    threads: list[dict[str, Any]] = []
    taken: set[str] = set()
    for thread in remote or []:
        here = known.get(str(thread.get("thread_id"))) or known.get(
            str(thread.get("conversation_id")))
        # This computer may have moved the thread on, or closed it, since Cloud answered.
        if here and str(here["occurred_at"]) >= str(thread.get("occurred_at") or ""):
            continue
        threads.append(thread)
        taken.update(str(thread.get(key)) for key in ("thread_id", "conversation_id"))
    from_here = False
    for thread in local:
        if not _left_open(thread) or {str(thread["thread_id"]),
                                      str(thread["conversation_id"])} & taken:
            continue
        held = claims.get(str(thread["thread_id"]))
        threads.append({**{k: thread[k] for k in _FIELDS if k in thread},
                        "claimed": bool(held and held["holder"] != conversation_id)})
        from_here = True
    own = _own_thread(local, claims, conversation_id)
    threads = [t for t in threads if t.get("conversation_id") != conversation_id
               and (own is None or t.get("thread_id") != own)]
    source = "local" if remote is None else "cloud+local" if from_here else "cloud"
    result: dict[str, Any] = {"project": project, "source": source, "cloud": state,
                              "threads": _ranked(threads, task)[:MAX_THREADS]}
    if as_of:
        result["cloud_as_of"] = as_of
    return result


def _left_open(report: dict[str, Any]) -> bool:
    from varyz.session_progress import left_open

    return left_open(report)


def _own_thread(local: list[dict[str, Any]], claims: dict[str, dict[str, Any]],
                conversation_id: str | None) -> str | None:
    """The thread the asking chat works on: one it picked up, or its own."""
    if not conversation_id:
        return None
    for thread_id, held in claims.items():
        if held["holder"] == conversation_id:
            return thread_id
    for thread in local:
        if thread["conversation_id"] == conversation_id:
            return str(thread["thread_id"])
    return None


_COMMON = frozenset({"the", "and", "for", "with", "this", "that", "from", "into", "work",
                     "continue", "resume", "finish", "please", "where", "left", "off",
                     "pick", "carry", "task", "what", "were"})


def _ranked(threads: list[dict[str, Any]], task: str) -> list[dict[str, Any]]:
    """Threads the task names first, then the newest: a bare 'continue' gets the latest."""
    words = set(re.findall(r"[^\W_]{3,}", task.casefold())) - _COMMON

    def score(thread: dict[str, Any]) -> int:
        text = " ".join([str(thread.get("task") or ""),
                         *map(str, thread.get("done") or []),
                         *map(str, thread.get("pending") or [])])
        return len(words & set(re.findall(r"[^\W_]{3,}", text.casefold())))

    scored = [(score(t), str(t.get("occurred_at") or ""), t) for t in threads]
    scored.sort(key=lambda item: item[:2], reverse=True)
    return [{**t, "match": "topic" if points else "recent"} for points, _, t in scored]


def _local_threads(cfg: Any, project: str) -> list[dict[str, Any]]:
    """This computer's work threads in the project, open or not, newest first."""
    import sqlite3

    from varyz.session_progress import work_threads

    if not cfg.db_path.exists():
        return []
    try:
        uri = cfg.db_path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            threads = work_threads(conn, project, cfg=cfg)
    except Exception:
        return []
    signals = _signals(cfg.data_dir)
    for thread in threads:
        mark = signals.get(_signal_key(project, str(thread.get("conversation_id") or "")))
        if mark and not thread.get("stopped") and _signal_applies(mark, thread):
            thread["stopped"] = mark["kind"]
            if mark.get("left") is not None:
                thread["usage_left"] = mark["left"]
    return threads


# -- usage signals ----------------------------------------------------------------
#
# A chat's agent nearly out of usage, or stopped by it, is noted here, not in its
# reports: the miner skips a chat whose file has not changed and a turn already
# reported, so a report written before usage ran low cannot be relabelled.


def _signals_path(data_dir: Path) -> Path:
    return Path(data_dir) / "runtime" / "handoff" / "signals.json"


def _signal_key(project: str, conversation_id: str) -> str:
    return f"{project.casefold()}\n{conversation_id}"


def _signals(data_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        signals = json.loads(_signals_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(signals, dict):
        return {}
    return {key: mark for key, mark in signals.items()
            if isinstance(mark, dict) and mark.get("kind") in STOPPED
            and isinstance(mark.get("at"), str)}


def signal(data_dir: Path, *, project: str, conversation_id: str,
           kind: Literal["usage_low", "usage_limit"], left: float | None = None,
           agent: str = "", at: float | None = None,
           resets_at: float | None = None) -> bool:
    """Note that a chat's agent is nearly out of usage, or was stopped by it."""
    from varyz.db.connection import FileLock

    if not project or not conversation_id:
        return False
    path = _signals_path(data_dir)
    lock = FileLock(path.with_suffix(".lock"))
    for _ in range(100):
        if lock.acquire():
            break
        time.sleep(0.02)
    else:
        return False
    try:
        signals = _signals(data_dir)
        oldest = _when(time.time() - SIGNAL_DAYS * 24 * 3600)
        signals = {key: mark for key, mark in signals.items() if mark["at"] >= oldest}
        mark: dict[str, Any] = {"kind": kind, "at": _when(time.time() if at is None else at),
                                "agent": agent}
        if left is not None:
            mark["left"] = round(float(left), 1)
        if resets_at is not None:
            mark["resets_at"] = _when(resets_at)
        key = _signal_key(project, conversation_id)
        if key in signals and signals[key]["at"] > mark["at"]:
            return False  # a re-mined old stop never hides a newer warning
        signals[key] = mark
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(signals, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        lock.release()


def _signal_applies(mark: dict[str, Any], thread: dict[str, Any]) -> bool:
    """Whether a signal still describes the thread's newest report.

    A stop counts when it came after that report. Low usage counts for reports
    from before the limit reset (or within the hour, with no reset time known):
    once the agent works on after a reset, the warning is out of date.
    """
    reported = str(thread.get("occurred_at") or "")
    if mark["kind"] == "usage_limit":
        return mark["at"] >= reported
    until = mark.get("resets_at")
    if not isinstance(until, str):
        with contextlib.suppress(ValueError):
            until = _when(datetime.fromisoformat(mark["at"]).timestamp() + 3600)
    return isinstance(until, str) and reported <= until


# -- claims ----------------------------------------------------------------------

CLAIM_SECONDS = 15 * 60


def _claims_path(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "runtime" / "handoff" / "claims.json"


def _claims(cfg: Any) -> dict[str, dict[str, Any]]:
    """Unexpired claims on this computer, by thread_id."""
    try:
        claims = json.loads(_claims_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(claims, dict):
        return {}
    now = time.time()
    return {thread: held for thread, held in claims.items()
            if isinstance(held, dict) and float(held.get("expires", 0)) > now}


def claim(cfg: Any, *, project: str, thread_id: str, conversation_id: str,
          action: Literal["claim", "renew", "release", "status"] = "claim") -> dict[str, Any]:
    """Claim, renew, release or look up a thread on this computer for 15 minutes.

    Two agents here must not both pick up the same work, so the second is told it
    is taken. A claim lapses by itself when its agent stops (its usage ran out, its
    window closed), so work is never stuck behind an agent that went away.
    """
    from varyz.db.connection import FileLock

    lock = FileLock(_claims_path(cfg).with_suffix(".lock"))
    for _ in range(100):
        if lock.acquire():
            break
        time.sleep(0.02)
    else:
        raise ValueError("Another session is claiming work; try again")
    try:
        claims = _claims(cfg)
        held = claims.get(thread_id)
        yours = bool(held and held["holder"] == conversation_id)
        if action == "status":
            return {"thread_id": thread_id, "claimed": bool(held), "yours": yours,
                    "expires_at": _when(held["expires"]) if held else None}
        if held and not yours:
            return {"thread_id": thread_id, "claimed": False, "conflict": True,
                    "expires_at": _when(held["expires"])}
        if action == "release":
            claims.pop(thread_id, None)
            _write_claims(cfg, claims)
            return {"thread_id": thread_id, "released": True}
        states = {str(t["thread_id"]): t for t in _local_threads(cfg, project)}
        if thread_id in states and not _left_open(states[thread_id]):
            raise ValueError("Thread is no longer open")
        expires = time.time() + CLAIM_SECONDS
        claims[thread_id] = {"holder": conversation_id, "project": project.casefold(),
                             "expires": expires}
        _write_claims(cfg, claims)
        return {"thread_id": thread_id, "claimed": True, "expires_at": _when(expires)}
    finally:
        lock.release()


def settle(cfg: Any, *, project: str, thread_id: str, conversation_id: str,
           status: str) -> None:
    """After a checkpoint on a picked-up thread: renew the claim, or end it with the work."""
    with contextlib.suppress(Exception):
        claim(cfg, project=project, thread_id=thread_id, conversation_id=conversation_id,
              action="release" if status in {"complete", "abandoned"} else "renew")


def _write_claims(cfg: Any, claims: dict[str, dict[str, Any]]) -> None:
    path = _claims_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(claims), encoding="utf-8")
    os.replace(temporary, path)


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


# -- Cloud -----------------------------------------------------------------------


def refresh_cloud(cfg: Any, config_file: Path | None, project: str) -> bool:
    """Ask Cloud for the project's open work and keep a local copy for briefings."""
    remote = _cloud_threads(config_file, project, "", None)
    if remote is None:
        return False
    _write_cache(cfg, project, remote)
    return True


def warm(cfg: Any, config_file: Path | None, project: str) -> None:
    """Refresh the local copy of Cloud's open work in the background if it is old."""
    if _cloud_configured(config_file, project):
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
                    project: str) -> tuple[list[dict[str, Any]], str] | None:
    """The last Cloud answer and when it came, or None. Starts a refresh when the
    copy is missing or old."""
    age: float | None = None
    fetched = ""
    threads: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        cached = json.loads(_cache_path(cfg, project).read_text(encoding="utf-8"))
        fetched = str(cached["fetched_at"])
        age = (datetime.now(UTC) - datetime.fromisoformat(fetched)).total_seconds()
        threads = [t for t in cached["threads"] if isinstance(t, dict) and _left_open(t)]
    if (age is None or age > CACHE_FRESH_SECONDS) and _cloud_configured(config_file, project):
        with contextlib.suppress(Exception):
            refresh_in_background(config_file, project)
    if age is None or age > CACHE_MAX_AGE_SECONDS:
        return None
    return threads, fetched


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


def render(work: dict[str, Any]) -> str:
    """The "where you left off" section of a briefing, or '' with nothing open."""
    from varyz.session_miner import STOPPED as STOP_NOTE
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
        "to brief you again. Check the artifacts before relying on a claim. Call claim_work "
        "with its thread_id first (it works without Cloud), and pass that thread_id to "
        "record_progress so the thread closes when you finish. Ignore threads the request "
        "is not about."
    ]
    if work.get("cloud") == "unavailable":
        lines.append("Varyz Cloud did not answer, so work from other computers may be missing.")
    elif work.get("cloud") == "cached" and work.get("cloud_as_of"):
        lines.append("Work from other computers is as Varyz Cloud last reported it, at "
                     f"{str(work['cloud_as_of'])[:16].replace('T', ' ')} UTC.")
    for number, thread in enumerate(threads, 1):
        when = str(thread.get("occurred_at") or "")[:16].replace("T", " ")
        head = f"{number}. [{thread.get('status')}] {thread.get('task')}"
        stopped = STOPPED.get(str(thread.get("stopped")), "")
        if stopped and isinstance(thread.get("usage_left"), int | float):
            stopped += f" ({float(thread['usage_left']):.0f}% left)"
        meta = [m for m in (when, _client(thread.get("agent")), stopped,
                            "claimed by another session" if thread.get("claimed") else "")
                if m]
        lines.append(head + (f" ({', '.join(meta)})" if meta else ""))
        for label, key in (("done", "done"), ("open", "pending"), ("checks", "checks"),
                           ("artifacts", "artifacts")):
            items = [str(item) for item in thread.get(key) or []
                     if item not in {MINED_QUALIFIER, STOP_NOTE}]
            if items:
                lines.append(f"   {label}: " + "; ".join(items))
        ids = [f"conversation_id {thread['conversation_id']}"] if thread.get(
            "conversation_id") else []
        if thread.get("thread_id"):
            ids.append(f"thread_id {thread['thread_id']}")
        if ids:
            lines.append("   " + ", ".join(ids))
    return "\n".join(lines)


STOPPED = {"usage_limit": "stopped mid-task when its usage ran out",
           "usage_low": "its usage was nearly out, so it may have stopped mid-task"}


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
    run.add_argument("--folder", type=Path)
    run.add_argument("--config", type=Path)
    swept = commands.add_parser("sweep")
    swept.add_argument("--project", required=True)
    swept.add_argument("--folder", type=Path)
    swept.add_argument("--config", type=Path)
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
                             transcript=Path(args.transcript), folder=args.folder)
        except Exception:
            return 0  # counts only; transcripts may hold private text
    elif args.command == "sweep":
        try:
            result = sweep(cfg, config_file, project=args.project, folder=args.folder)
        except Exception:
            return 0
    elif args.command == "refresh":
        result = {"refreshed": refresh_cloud(cfg, config_file, args.project)}
    else:
        result = open_work(cfg, config_file, project=args.project, task=args.task)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

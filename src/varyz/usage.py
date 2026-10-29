"""Usage left on each agent's plan, so its work is handed off before it runs out.

Claude Code tells a status line command how much of its five-hour and weekly
limits is used; Codex writes the same figures into its chat files. When an
agent has 5% or less left, Varyz journals that chat in the background and marks
its thread as nearly out of usage, and does both again at 3%: the next agent
then knows the work may have stopped mid-task and where it got to. An agent
that is nearly out is also told, at its next prompt, to journal its own state.
When the limit is reached, the agent keeps working until the client cuts the
turn off, so the client resumes the session by itself after the reset.

Everything here is local, reads only usage figures and approved chats, and
fails quietly: a hand-off is a convenience, never a reason to break a session.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOW_LEFT = (5, 3)  # percent left at which a hand-off is saved
NOTE_LEFT = 10  # percent left from which the agent is told to journal as it goes
WATCH_SECONDS = 30  # how often agent connections look at Codex's figures
RECENT_SECONDS = 15 * 60  # a Codex chat written this recently counts as running
TAIL_BYTES = 512_000  # how much of a chat file's end is read for its figures
MAX_INPUT_BYTES = 1_000_000
STATUSLINE_MARK = "varyz.usage statusline"


# -- readings ----------------------------------------------------------------------


def _seconds(value: Any) -> float | None:
    """A reset time as Unix seconds, from seconds or an ISO date."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str) and value.strip():
        with contextlib.suppress(ValueError):
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.timestamp()
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def claude_windows(rate_limits: Any) -> list[tuple[float, float | None]]:
    """(percent used, reset time) of each limit in a Claude Code status line input."""
    if not isinstance(rate_limits, dict):
        return []
    windows = []
    for name in ("five_hour", "seven_day"):
        window = rate_limits.get(name)
        if isinstance(window, dict):
            used = _number(window.get("used_percentage"))
            if used is not None:
                windows.append((used, _seconds(window.get("resets_at"))))
    return windows


def codex_windows(rate_limits: Any, at: float | None = None) -> list[tuple[float, float | None]]:
    """(percent used, reset time) of each limit in a Codex token_count event.

    Only the plan's own limit counts: others, such as "premium", cover some
    models and are usually empty.
    """
    if not isinstance(rate_limits, dict) or rate_limits.get("limit_id") not in {None, "codex"}:
        return []
    windows = []
    for name in ("primary", "secondary"):
        window = rate_limits.get(name)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        if used is None:
            continue
        resets = _seconds(window.get("resets_at"))
        after = _number(window.get("resets_in_seconds"))
        if resets is None and after is not None and at is not None:
            resets = at + after
        windows.append((used, resets))
    return windows


def reading(windows: list[tuple[float, float | None]], now: float | None = None,
            ) -> dict[str, Any] | None:
    """The tightest limit that has not reset yet: percent left and when it resets."""
    now = time.time() if now is None else now
    live = [(used, resets) for used, resets in windows if resets is None or resets > now]
    if not live:
        return None
    used, resets = max(live, key=lambda window: window[0])
    left = max(0.0, min(100.0, 100.0 - used))
    return {"left": round(left, 1), "resets_at": resets}


# -- the hand-off at low usage -------------------------------------------------------


def _runtime(data_dir: Path) -> Path:
    return Path(data_dir) / "runtime" / "usage"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record(data_dir: Path, agent: str, value: dict[str, Any], session_id: str = "") -> None:
    """Keep an agent's latest figures for the briefing note and `varyz doctor`."""
    _write_json(_runtime(data_dir) / f"{agent}.json",
                {**value, "at": time.time(), "session_id": session_id})


def latest(data_dir: Path, agent: str, now: float | None = None) -> dict[str, Any] | None:
    """An agent's last recorded figures, unless their limit has reset since."""
    now = time.time() if now is None else now
    value = _read_json(_runtime(data_dir) / f"{agent}.json")
    if not isinstance(value, dict) or _number(value.get("left")) is None:
        return None
    resets = _number(value.get("resets_at"))
    if resets is not None and resets <= now:
        return None
    return value


def _same_window(before: Any, now: Any) -> bool:
    """Whether two reset times name the same limit window (Codex may state it in
    seconds from now, so it drifts a little between readings)."""
    if before is None or now is None:
        return before is None and now is None
    a, b = _number(before), _number(now)
    return a is not None and b is not None and abs(a - b) < 600


def level(left: float) -> int | None:
    """The hand-off level a reading has reached: 5, then 3; None above 5% left."""
    reached = [mark for mark in LOW_LEFT if left <= mark]
    return min(reached) if reached else None


def low(data_dir: Path, config_file: Path | None, *, agent: str, session_id: str,
        transcript: str, cwd: str, value: dict[str, Any]) -> int | None:
    """Save a hand-off when a chat's agent first reaches 5%, and again at 3%, left.

    Returns the level saved now, or None. Each level is saved once per chat and
    limit window; a window that resets starts the count again.
    """
    mark = level(float(value["left"]))
    folder = Path(cwd) if cwd else None
    project = folder.name if folder is not None else ""
    if mark is None or not session_id or not project:
        return None  # a reading that cannot be saved does not use up a level
    key = hashlib.sha256(f"{agent}\n{session_id}".encode()).hexdigest()[:24]
    path = _runtime(data_dir) / f"low-{key}.json"
    from varyz.db.connection import FileLock

    lock = FileLock(path.with_suffix(".lock"))
    if not lock.acquire():
        return None  # another status line of this chat is saving it
    try:
        state = _read_json(path)
        saved: list[int] = []
        if isinstance(state, dict) and _same_window(state.get("resets_at"),
                                                    value.get("resets_at")):
            saved = [int(x) for x in state.get("levels") or [] if isinstance(x, int)]
        if mark in saved:
            return None
        _write_json(path, {"levels": sorted({*saved, *(m for m in LOW_LEFT if m >= mark)}),
                           "resets_at": value.get("resets_at"), "at": time.time()})
    finally:
        lock.release()
    from varyz import handoff

    handoff.signal(data_dir, project=project, conversation_id=session_id, kind="usage_low",
                   left=float(value["left"]), agent=agent,
                   resets_at=_number(value.get("resets_at")))
    if transcript and config_file is not None:
        handoff.capture_in_background(config_file, project, session_id, transcript, folder)
    return mark


def note(data_dir: Path, event: dict[str, Any], now: float | None = None) -> str:
    """For the agent's next prompt: journal as you go, your usage is nearly out."""
    transcript = event.get("transcript_path")
    value: dict[str, Any] | None = None
    if isinstance(transcript, str) and Path(transcript).name.startswith("rollout-"):
        value = codex_reading(Path(transcript), now=now)
    elif transcript:
        value = latest(data_dir, "claude_code", now=now)
    if value is None or float(value["left"]) > NOTE_LEFT:
        return ""
    resets = _number(value.get("resets_at"))
    until = (" until " + datetime.fromtimestamp(resets, UTC).strftime("%H:%M") + " UTC"
             if resets else "")
    return (f"Your plan's usage is nearly out ({float(value['left']):.0f}% left{until}), so "
            "this session may stop mid-task. Call record_progress now and after every step, "
            "with what is done, what is still open and the next step, so another agent "
            "can continue from there.")


# -- Claude Code: the limit itself ---------------------------------------------------

# When the plan runs out mid-turn, Claude Code adds a note telling the agent to
# wrap up and report, then cuts the turn off a little later. A turn the client
# cuts off is resumed by itself once the limit resets; a turn the agent ends
# itself is finished, so nothing resumes it and an overnight task waits for the
# owner. The agent is told to keep working instead (`LIMIT_POLICY`), and the
# Stop hook sends it back once if it stops anyway. Both notes are harness lines
# (`isMeta`), never the owner's words.
GRACE_NOTE = "[Usage limit reached; a short grace allowance remains"
RESET_NOTE = "[Earlier usage-limit notes no longer apply"
GRACE_TAIL_BYTES = 8_000_000  # the note comes a few steps before the turn ends

LIMIT_POLICY = (
    "When the client says your usage limit was reached and tells you to wrap up and "
    "report, do not end the turn: a session resumes by itself after the limit resets "
    "only when the client cuts its turn off, not when you stop. Call record_progress so "
    "the hand-off is current, then keep working on the next open step, with no closing "
    "report, until the turn is cut off. End it yourself only when the whole task is done."
)

KEEP_WORKING = (
    "Usage limit: do not end the turn here. This session resumes by itself after the "
    "reset only if the client cuts the turn off. Call record_progress, then go on with "
    "the next open step and write no closing report. If the whole task is done, end "
    "with one short line."
)


def _words(content: Any) -> str | None:
    """A user line's prose; None for tool results, which ride on user lines too."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    ):
        return None
    return "\n".join(str(block.get("text", "")) for block in content
                     if isinstance(block, dict) and block.get("type") == "text")


def grace(transcript_path: str | None) -> bool:
    """Whether Claude Code said this turn's usage ran out, with no reset or prompt since.

    Reads only the end of the transcript, and parses it only when the note is there.
    A task notification or other client message does not start a new task, so it
    leaves the note standing; the owner's prompt, or the one the client sends
    when it resumes the session after the reset, ends it.
    """
    if not transcript_path:
        return False
    try:
        with Path(transcript_path).open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - GRACE_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return False
    if GRACE_NOTE.encode("utf-8") not in tail:
        return False
    active = False
    for line in tail.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        message = entry.get("message")
        words = _words(message.get("content") if isinstance(message, dict) else None)
        if words is None:
            continue
        words = words.lstrip()
        if entry.get("isMeta"):
            if words.startswith(GRACE_NOTE):
                active = True
            elif words.startswith(RESET_NOTE):
                active = False
        elif not words.startswith(("<", "[SYSTEM NOTIFICATION")):
            active = False
    return active


# -- Claude Code: the status line ----------------------------------------------------


def statusline(data_dir: Path, config_file: Path | None, event: dict[str, Any],
               now: float | None = None) -> str:
    """Record Claude Code's figures; at low usage save a hand-off. Returns the line shown."""
    value = reading(claude_windows(event.get("rate_limits")), now=now)
    if value is None:
        return ""
    session_id = str(event.get("session_id") or "")
    record(data_dir, "claude_code", value, session_id)
    workspace = event.get("workspace")
    folder = workspace.get("current_dir") if isinstance(workspace, dict) else None
    cwd = str(event.get("cwd") or folder or "")
    with contextlib.suppress(Exception):
        low(data_dir, config_file, agent="claude_code", session_id=session_id,
            transcript=str(event.get("transcript_path") or ""), cwd=cwd, value=value)
    left = float(value["left"])
    if left > NOTE_LEFT:
        return ""
    saved = level(left) is not None and bool(session_id) and bool(cwd and Path(cwd).name)
    return f"Varyz: {left:.0f}% usage left" + (", hand-off saved" if saved else "")


# -- Codex: its chat files -----------------------------------------------------------


def codex_reading(path: Path, now: float | None = None) -> dict[str, Any] | None:
    """The last usage figures a Codex chat file states, if their limit has not reset."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    lines = tail.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first piece may start mid-line
    for line in reversed(lines):
        if b'"rate_limits"' not in line:
            continue
        try:
            data = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            continue
        at = _seconds(data.get("timestamp"))
        value = reading(codex_windows(payload.get("rate_limits"), at), now=now)
        if value is not None:
            return value
    return None


def _codex_meta(path: Path) -> tuple[str, str]:
    """A Codex chat's session id and working folder, from its first line."""
    try:
        with path.open("rb") as handle:
            data = json.loads(handle.readline(MAX_INPUT_BYTES * 4))
    except (OSError, ValueError, UnicodeDecodeError):
        return "", ""
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or data.get("type") != "session_meta":
        return "", ""
    return str(payload.get("id") or ""), str(payload.get("cwd") or "")


def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def check_codex(data_dir: Path, config_file: Path | None, *, root: Path | None = None,
                now: float | None = None) -> list[dict[str, Any]]:
    """Look at the Codex chats running now; save hand-offs for those nearly out."""
    now = time.time() if now is None else now
    root = codex_root() if root is None else root
    days = {datetime.fromtimestamp(now - offset, UTC).astimezone() for offset in (0, 86400)}
    files: list[tuple[float, Path]] = []
    for day in days:
        folder = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        with contextlib.suppress(OSError):
            for path in folder.glob("rollout-*.jsonl"):
                modified = path.stat().st_mtime
                if now - modified <= RECENT_SECONDS:
                    files.append((modified, path))
    found: list[dict[str, Any]] = []
    for _, path in sorted(files, reverse=True):
        value = codex_reading(path, now=now)
        if value is None:
            continue
        session_id, cwd = _codex_meta(path)
        if not found:
            record(data_dir, "codex", value, session_id)
        saved = None
        with contextlib.suppress(Exception):
            saved = low(data_dir, config_file, agent="codex", session_id=session_id,
                        transcript=str(path), cwd=cwd, value=value)
        found.append({"session_id": session_id, "left": value["left"], "saved": saved})
    return found


def watch_in_background(config_file: Path | None) -> None:
    """From an agent's Varyz connection: look at Codex's figures every half minute.

    Every running connection starts one; a shared marker lets one of them look
    per half minute. Nothing is read until AI chats are approved.
    """
    def loop() -> None:
        from varyz.config import find_config_file, load_config
        from varyz.handoff import can_capture

        while True:
            time.sleep(WATCH_SECONDS)
            with contextlib.suppress(Exception):
                config = find_config_file(config_file)
                cfg = load_config(config)
                if not can_capture(cfg) or not codex_root().is_dir():
                    continue
                marker = _runtime(cfg.data_dir) / "codex-checked"
                with contextlib.suppress(OSError):
                    if time.time() - marker.stat().st_mtime < WATCH_SECONDS - 1:
                        continue
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
                check_codex(cfg.data_dir, config)

    threading.Thread(target=loop, name="varyz-usage-watch", daemon=True).start()


# -- status line install -------------------------------------------------------------


def statusline_command(config_path: Path, data_dir: Path,
                       previous: dict[str, Any] | None = None) -> str:
    """The shell command Claude Code runs for its status line.

    An owner's own status line keeps running first and its output stays on
    screen; Varyz's line follows it on a line of its own, and is empty until
    usage runs low.
    """
    python = Path(os.path.abspath(sys.executable))
    windowless = python.with_name("pythonw.exe")
    if os.name == "nt" and windowless.is_file():
        python = windowless  # no console window flashes on every refresh
    py = python.as_posix()
    ours = (f"'{py}' -m {STATUSLINE_MARK} --config '{Path(config_path).as_posix()}' "
            f"--data-dir '{Path(data_dir).as_posix()}'")
    command = str((previous or {}).get("command") or "").strip()
    if not command:
        return f"if [ -f '{py}' ]; then {ours}; else cat >/dev/null 2>&1 || :; fi"
    return ("varyz_input=$(cat); varyz_own=$(printf '%s' \"$varyz_input\" | { "
            + command + "\n}); varyz_line=''; "
            f"if [ -f '{py}' ]; then varyz_line=$(printf '%s' \"$varyz_input\" | {ours}); fi; "
            "printf '%s' \"$varyz_own\"; if [ -n \"$varyz_line\" ]; then "
            "if [ -n \"$varyz_own\" ]; then printf '\\n'; fi; printf '%s' \"$varyz_line\"; fi; "
            "printf '\\n'")


def is_ours(setting: Any) -> bool:
    return isinstance(setting, dict) and STATUSLINE_MARK in str(setting.get("command", ""))


def merge_statusline(settings: dict[str, Any], config_path: Path, data_dir: Path,
                     saved: Path, *, dry_run: bool = False) -> bool:
    """Install Varyz's status line, keeping any the owner had; True when changed.

    The owner's own line is kept in ``saved`` so uninstalling puts it back.
    """
    current = settings.get("statusLine")
    if current is not None and not isinstance(current, dict):
        return False  # a setting this version does not understand is left alone
    if current is not None and is_ours(current):
        previous = _read_json(saved)
        if previous is None and "varyz_input=" in str(current.get("command", "")):
            return False  # the owner's own line is in it; without its copy, keep it
    else:
        previous = current
    if previous is not None and not isinstance(previous, dict):
        previous = None
    wanted: dict[str, Any] = {"type": "command",
                              "command": statusline_command(config_path, data_dir, previous)}
    if previous and "padding" in previous:
        wanted["padding"] = previous["padding"]
    if current == wanted:
        return False
    if not dry_run and not is_ours(current):
        if current is None:
            saved.unlink(missing_ok=True)
        else:
            _write_json(saved, current)
    settings["statusLine"] = wanted
    return True


def remove_statusline(settings: dict[str, Any], saved: Path, *, dry_run: bool = False) -> bool:
    """Put back the owner's own status line, or none; True when changed."""
    if not is_ours(settings.get("statusLine")):
        return False
    previous = _read_json(saved)
    if isinstance(previous, dict):
        settings["statusLine"] = previous
    else:
        del settings["statusLine"]
    if not dry_run:
        saved.unlink(missing_ok=True)
    return True


# -- entry point -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m varyz.usage")
    commands = parser.add_subparsers(dest="command", required=True)
    line = commands.add_parser("statusline")
    line.add_argument("--config", type=Path)
    line.add_argument("--data-dir", type=Path)
    show = commands.add_parser("show")
    show.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "statusline":
        try:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES) if sys.stdin else b""
            event = json.loads(raw or b"{}")
            data_dir = args.data_dir
            if data_dir is None:
                from varyz.config import load_config

                data_dir = load_config(args.config).data_dir
            text = statusline(data_dir, args.config, event if isinstance(event, dict) else {})
        except Exception:
            return 0  # a status line never shows an error
        if text and sys.stdout:
            sys.stdout.write(text + "\n")
        return 0
    from varyz.config import find_config_file, load_config

    cfg = load_config(find_config_file(args.config))
    shown = {agent: latest(cfg.data_dir, agent) for agent in ("claude_code", "codex")}
    sys.stdout.write(json.dumps(shown) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

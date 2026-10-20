"""Find and stop the processes this Varyz installation is running.

Setup, learning and the daemons start windowless, detached processes. While
any of them runs, Windows keeps the tool's files open and `uv tool uninstall`
fails, so `varyz uninstall` stops them first. Only processes started from this
installation (its environment or its command launchers) that run Varyz code
are matched; the caller and its parents are never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# `python -m varyz ...`, `-m varyz.news`, `-m varyz.resolution.local_reader`, ...
_MODULE = re.compile(r"(?:^|\s)-m\s+\"?varyz(?:[.\s\"]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Found:
    pid: int
    parent: int
    command: str


def install_paths() -> tuple[Path, list[Path]]:
    """This installation's environment and the command launchers that start it."""
    environment = Path(sys.prefix).resolve()
    launchers = []
    for name in ("varyz", "varyz-acquisition"):
        found = shutil.which(name)
        if found:
            launchers.append(Path(found).resolve())
    return environment, launchers


def _listing() -> list[Found]:
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if sys.platform == "win32":
        result = subprocess.run(  # noqa: S603 - fixed system tool
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -ErrorAction Stop | "
             "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True, stdin=subprocess.DEVNULL, creationflags=flags,
        )
        rows = json.loads(result.stdout or "[]")
        rows = [rows] if isinstance(rows, dict) else rows
        return [Found(int(row["ProcessId"]), int(row.get("ParentProcessId") or 0),
                      f"{row.get('ExecutablePath') or ''} {row.get('CommandLine') or ''}")
                for row in rows]
    result = subprocess.run(  # noqa: S603 - fixed system tool
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
        timeout=60, check=True, stdin=subprocess.DEVNULL,
    )
    found = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            found.append(Found(int(parts[0]), int(parts[1]), parts[2]))
    return found


def _ancestors(processes: list[Found], pid: int) -> set[int]:
    parents = {process.pid: process.parent for process in processes}
    chain = {pid}
    while (pid := parents.get(pid, 0)) and pid not in chain:
        chain.add(pid)
    return chain


def matches(command: str, environment: Path, launchers: list[Path]) -> bool:
    """Varyz code started from this installation, not any other program in its folder."""
    folded = command.casefold()
    if any(str(launcher).casefold() in folded for launcher in launchers):
        return True
    return (str(environment).casefold() + os.sep) in folded and bool(_MODULE.search(command))


def find(environment: Path, launchers: list[Path], *,
         listing: list[Found] | None = None, me: int | None = None) -> list[Found]:
    """This installation's processes, without the caller and the processes that started it."""
    processes = _listing() if listing is None else listing
    spared = _ancestors(processes, os.getpid() if me is None else me)
    found = [process for process in processes
             if process.pid not in spared and matches(process.command, environment, launchers)]
    # A tree kill of the outermost one takes the rest; listing them too only
    # repeats the same work and the same line.
    pids = {process.pid for process in found}
    return [process for process in found
            if not (_ancestors(processes, process.pid) - {process.pid}) & pids]


def _descendants(processes: list[Found], pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(process.parent, []).append(process.pid)
    out, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in out:
                out.append(child)
                stack.append(child)
    return out


def stop(found: list[Found]) -> list[int]:
    """End each process and everything it started; the pids that were told to stop."""
    stopped = []
    if sys.platform == "win32":
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for process in found:
            result = subprocess.run(  # noqa: S603 - fixed system tool, numeric pid
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                timeout=30, check=False, stdin=subprocess.DEVNULL, creationflags=flags,
            )
            if result.returncode == 0:
                stopped.append(process.pid)
        return stopped
    try:
        everything = _listing()
    except (OSError, subprocess.SubprocessError, ValueError):
        everything = []
    targets = []
    for process in found:
        targets += [process.pid, *_descendants(everything, process.pid)]
    for pid in targets:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
    deadline = time.monotonic() + 5
    from varyz.autosync import process_alive

    while time.monotonic() < deadline and any(process_alive(pid) for pid in stopped):
        time.sleep(0.2)
    for pid in stopped:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return [process.pid for process in found if process.pid in stopped]


def describe(process: Found) -> str:
    """A short name for the line uninstall prints: what it is, never its full command."""
    command = process.command
    for marker, label in (
        ("rules read", "preference learning"),
        ("local_reader", "local preference learning"),
        (" setup", "setup"),
        ("daemon run", "Varyz service"),
        ("varyz.resolution.availability", "Varyz service watchdog"),
        ("autosync", "autosync"),
        (" mcp", "agent connection (MCP server)"),
        ("varyz.news", "news check"),
        ("varyz.cloud.bridge", "Cloud bridge"),
    ):
        if marker in command:
            return f"{label} (pid {process.pid})"
    return f"Varyz process (pid {process.pid})"


__all__ = ["Found", "describe", "find", "install_paths", "matches", "stop"]

"""Run a command with the machine held awake for exactly as long as it runs.

The 2026-09-18 v9 rebuild died thirty-three minutes into a five-hour run
because Windows went to sleep: the kernel log shows "entering sleep" one minute
after the last write, and every process in the tree -- the rebuild and the
autosync daemon with it -- exited 0x40010004. A run that is expected to take
hours unattended cannot depend on nobody letting the machine idle.

This holds the same request a media player holds while it plays: the system may
not sleep on an idle timer while the work is running. It is not a settings
change -- nothing is written to the user's power plan, the request lives in this
process and dies with it, however it dies, including a kill. The display is
deliberately left alone: the screen should still go dark.

Usage: python scripts/keep_awake.py <command> [args...]
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def hold() -> bool:
    """Ask Windows not to sleep on an idle timer. False where that is not a thing."""

    if sys.platform != "win32":
        return False
    state = ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    )
    return bool(state)


def release() -> None:
    """Give the request back, so the machine sleeps normally again."""

    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    held = hold()
    print(
        "keep_awake: sleep held for the run" if held
        else "keep_awake: could not hold sleep; the run is unprotected",
        file=sys.stderr,
        flush=True,
    )
    try:
        return subprocess.call(argv)
    finally:
        release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

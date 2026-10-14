"""Launch browser onboarding or removal as the Mac user, never as Installer's root."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if os.getuid() == 0:
        raise SystemExit("Open Varyz from your signed-in Mac account, not as root.")
    payload = Path(__file__).resolve().parent / "payload"
    app = Path.home() / "Library/Application Support/Varyz"
    spec = importlib.util.spec_from_file_location("mac_native", payload / "native.py")
    assert spec and spec.loader
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    removing = "--uninstall" in sys.argv
    try:
        if removing:
            confirmed = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'display dialog "Remove Varyz integrations and its tool? Your archive, '
                    'configuration and models will be kept." buttons {"Cancel", "Remove"} '
                    'default button "Cancel" cancel button "Cancel"',
                ],
                check=False,
            )
            if confirmed.returncode:
                return 0
            worker.uninstall(app)
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'display dialog "Varyz integrations and its tool were removed. Your data '
                    "was kept. You can move Varyz Setup and Varyz Uninstall from Applications "
                    'to the Trash." buttons {"OK"} default button "OK"',
                ],
                check=False,
            )
        else:
            worker.install(app)
            state = worker.read_state(app)
            retained = Path(state["payload"])
            # Onboarding runs from the retained user runtime, independent of app replacement.
            result = subprocess.call(
                [
                    str(retained / "runtime/bin/python3"),
                    "-I",
                    str(retained / "native.py"),
                    "setup",
                    "--app",
                    str(app),
                    "--result",
                    str(app / "setup-result.json"),
                ],
                env=worker.child_environment(),
            )
            if result:
                raise ValueError("Browser setup needs attention.")
    except Exception:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'display alert "Varyz needs attention" message "Close your Varyz agents '
                "and stop its background services before upgrading or removing it. If the "
                'problem persists, keep the existing data and contact support."',
            ],
            check=False,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

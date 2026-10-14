"""The small Varyz app autosync runs through on macOS.

macOS asks before a program reads Documents, Desktop or Downloads, but it can
only ask an app. A bare Python can be let in through Full Disk Access alone,
which opens every folder and every other app's data as well. So autosync runs
through a tiny app built here, on this Mac, from the system's own AppleScript
applet. It has a name, a reason for each folder and a signature of its own,
so macOS asks "Varyz would like to access files in your Documents folder",
and a yes opens that folder and nothing else.

The app only starts `varyz-autosync.sh` beside it. What that script runs can
change with every install while the app stays byte for byte the same, and
macOS keeps the owner's answer for as long as it does.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from varyz.filesystem import publish_directory

APP_NAME = "Varyz.app"
LAUNCHER_NAME = "varyz-autosync.sh"
OPEN = "/usr/bin/open"
# Bumped only when the app itself changes. A rebuilt app is a different app to
# macOS, which then asks the owner again.
APP_VERSION = "1"
FOLDER_REASON = (
    "Varyz keeps the folders you connected to it indexed, so your agents can find "
    "your notes and files. Everything stays on this Mac."
)
_FOLDER_KEYS = (
    "NSDocumentsFolderUsageDescription",
    "NSDesktopFolderUsageDescription",
    "NSDownloadsFolderUsageDescription",
    "NSRemovableVolumesUsageDescription",
    "NSNetworkVolumesUsageDescription",
)
# The applet waits for the launcher: what the daemon reads is the app's to ask
# about only while the app that started it is running. `try` keeps a daemon's
# failing exit from putting AppleScript's error dialog on the owner's screen.
SCRIPT = (
    "try\n"
    '  do shell script "/bin/sh " & quoted form of '
    f'((POSIX path of (path to me)) & "../{LAUNCHER_NAME}")\n'
    "end try\n"
)
_TIMEOUT_SECONDS = 60

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def app_path(directory: Path) -> Path:
    return directory / APP_NAME


def launcher_path(directory: Path) -> Path:
    return directory / LAUNCHER_NAME


def _info(app: Path) -> dict:
    try:
        return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def ready(directory: Path) -> Path | None:
    """The app, when it is built and has a launcher to start."""
    app = app_path(directory)
    if _info(app).get("CFBundleVersion") == APP_VERSION and launcher_path(directory).is_file():
        return app
    return None


def build_app(directory: Path, bundle_id: str, *, run: Run = subprocess.run) -> Path | None:
    """Build the app once; None where this Mac cannot (no osacompile or codesign).

    An app already built at this version is kept as it is: rebuilding it would
    make macOS forget what the owner allowed.
    """
    app = app_path(directory)
    info = _info(app)
    if info.get("CFBundleVersion") == APP_VERSION and info.get("CFBundleIdentifier") == bundle_id:
        return app
    osacompile = shutil.which("osacompile")
    codesign = shutil.which("codesign")
    if osacompile is None or codesign is None:
        return None
    # osacompile builds an app only for a name ending in .app.
    staging = directory / f".building-{APP_NAME}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _run(run, [osacompile, "-o", str(staging), "-e", SCRIPT])
        info_path = staging / "Contents" / "Info.plist"
        # osacompile gives a reason for everything an AppleScript might touch,
        # the camera and contacts included. This app reads folders, nothing else.
        info = {
            key: value
            for key, value in plistlib.loads(info_path.read_bytes()).items()
            if not (key.startswith("NS") and key.endswith("UsageDescription"))
        }
        info.update(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": "Varyz",
                "CFBundleDisplayName": "Varyz",
                "CFBundleShortVersionString": APP_VERSION,
                "CFBundleVersion": APP_VERSION,
                # No Dock icon and no menu bar: it has nothing to show.
                "LSUIElement": True,
                **{key: FOLDER_REASON for key in _FOLDER_KEYS},
            }
        )
        info_path.write_bytes(plistlib.dumps(info))
        # Editing Info.plist broke osacompile's signature; macOS identifies the
        # app by the new one, made here with no certificate.
        _run(run, [codesign, "--force", "--sign", "-", "--identifier", bundle_id, str(staging)])
        shutil.rmtree(app, ignore_errors=True)
        publish_directory(staging, app)
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        shutil.rmtree(staging, ignore_errors=True)
        return None
    return app


def _run(run: Run, command: list[str]) -> None:
    completed = run(command, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False)
    if completed.returncode != 0:
        raise subprocess.SubprocessError(
            f"{Path(command[0]).name} exited {completed.returncode}: {completed.stderr.strip()}"
        )


def write_launcher(
    directory: Path,
    command: list[str],
    *,
    working_directory: Path,
    path: str,
    log: Path,
    marker: str,
) -> Path:
    """The script the app starts. An app started by macOS inherits no PATH and
    no working directory from whoever asked for it, so both are set here."""
    target = launcher_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    invocation = " ".join(shlex.quote(value) for value in command)
    target.write_text(
        "#!/bin/sh\n"
        f"# {marker}\n"
        f"cd {shlex.quote(str(working_directory))} || exit 1\n"
        f"PATH={shlex.quote(path)}; export PATH\n"
        # Background priority, as launchd's ProcessType gave the daemon when
        # launchd started it directly.
        f"exec /usr/sbin/taskpolicy -b {invocation} >>{shlex.quote(str(log))} 2>&1\n",
        encoding="utf-8",
    )
    return target


def launcher_runs(directory: Path, *needles: str) -> bool:
    try:
        text = launcher_path(directory).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return all(needle in text for needle in needles)


def open_command(app: Path, *, wait: bool) -> list[str]:
    """Start a new copy of the app in the background. `wait` keeps the caller
    (launchd) alive for as long as the app runs."""
    return [OPEN, *(["-W"] if wait else []), "-n", "-g", "-a", str(app)]

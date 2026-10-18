"""Desktop notifications for Shared Varyz: invitations, questions and answers.

The autosync daemon asks Varyz Cloud for new notices about once a minute while
the computer is connected, shows each as a system notification and tells Cloud
it was shown, so it never appears twice. A notice says who did what in which
project, never what was asked or shared. Clicking one opens the Cloud page.

Windows shows a Varyz toast with the logo through Windows PowerShell with no
console window. It stays on screen 10 seconds, then waits in the notification
centre. macOS uses ``osascript``; Linux uses ``notify-send`` when it is installed.
Notice text comes from other people, so it reaches the system only as data: environment
variables on Windows (escaped as XML there) and arguments elsewhere, never as
script source. ``varyz cloud notifications desktop off`` turns this off.
"""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from varyz.config import load_config

PATH = "/api/sharing/v1/notices"
CHECK_EVERY = 60.0
TIMEOUT = 5.0
SHOW_AT_ONCE = 3  # More than this become one summary notification.
TITLE = "Varyz"
ON_SCREEN = 10  # Seconds a Windows toast stays up before moving to the notification centre.

Runner = Callable[[Sequence[str], dict[str, str]], None]

LOGO = Path(__file__).resolve().parent / "notify_logo.png"
# Registered per user below so toasts say "Varyz" with its logo, as an installed app's do.
APP_ID = "VaryzAI.Varyz"
# Windows PowerShell's own registered app id, used if registering Varyz's fails.
_FALLBACK_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
_WINRT = ", ContentType = WindowsRuntime] > $null"
_TOAST = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications" + _WINRT,
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument" + _WINRT,
        "$t = [Security.SecurityElement]::Escape($env:VARYZ_NOTICE_TITLE)",
        "$b = [Security.SecurityElement]::Escape($env:VARYZ_NOTICE_BODY)",
        "$u = [Security.SecurityElement]::Escape($env:VARYZ_NOTICE_URL)",
        "$l = [Security.SecurityElement]::Escape($env:VARYZ_NOTICE_LOGO)",
        "$a = if ($u) { ' activationType=\"protocol\" launch=\"' + $u + '\"' } else { '' }",
        "$i = if ($l) { '<image placement=\"appLogoOverride\" src=\"' + $l + '\"/>' } else { '' }",
        "$v = '<binding template=\"ToastGeneric\">' + $i",
        "$v += '<text>' + $t + '</text><text>' + $b + '</text>'",
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument",
        # Windows offers only about 7 or about 25 seconds: show the long one, then at
        # ON_SCREEN seconds take it off screen and put it back silently in the
        # notification centre, unless it was already clicked or dismissed.
        "$s = '<toast duration=\"long\"' + $a + '><visual>' + $v",
        "$x.LoadXml($s + '</binding></visual></toast>')",
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x)",
        "$n.Tag = $env:VARYZ_NOTICE_TAG",
        "$n.Group = 'varyz'",
        "$m = [Windows.UI.Notifications.ToastNotificationManager]",
        "$o = $m::CreateToastNotifier($env:VARYZ_NOTICE_APP)",
        "$o.Show($n)",
        "Start-Sleep -Seconds ([int]$env:VARYZ_NOTICE_SECONDS)",
        "$h = @($m::History.GetHistory($env:VARYZ_NOTICE_APP))",
        "if (@($h | Where-Object { $_.Tag -eq $n.Tag }).Count) {",
        "  $o.Hide($n)",
        "  $q = [Windows.UI.Notifications.ToastNotification]::new($x)",
        "  $q.Tag = $n.Tag",
        "  $q.Group = 'varyz'",
        "  $q.SuppressPopup = $true",
        "  $o.Show($q)",
        "}",
    ]
)


def folder(config_path: Path | None) -> Path:
    return load_config(config_path).data_dir / "notifications"


def enabled(config_path: Path | None) -> bool:
    return not (folder(config_path) / "desktop-off").exists()


def set_enabled(config_path: Path | None, on: bool) -> None:
    off = folder(config_path) / "desktop-off"
    if on:
        off.unlink(missing_ok=True)
    else:
        off.parent.mkdir(parents=True, exist_ok=True)
        off.write_text(
            "Desktop notifications are off. `varyz cloud notifications desktop on`\n",
            encoding="utf-8",
        )


def _run(command: Sequence[str], env: dict[str, str]) -> None:
    """Start the notifier and return at once: a Windows toast's helper lives ON_SCREEN seconds."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen(
        list(command),
        env={**os.environ, **env},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def register_app() -> str:
    """Name Varyz and its logo for toasts in the current user's registry; returns the app id.

    Writes only HKCU\\Software\\Classes\\AppUserModelId\\VaryzAI.Varyz, and only when
    a value differs. Falls back to Windows PowerShell's id when that is not possible.
    """
    try:
        import winreg

        values = {"DisplayName": "Varyz", "IconUri": str(LOGO)}
        path = "Software\\Classes\\AppUserModelId\\" + APP_ID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            for name, value in values.items():
                try:
                    current = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    current = None
                if current != value:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        return APP_ID
    except (ImportError, OSError):
        return _FALLBACK_APP_ID


def show(title: str, body: str, url: str = "", *, runner: Runner = _run) -> bool:
    """Show one system notification. False when this system has no way to."""
    title, body = " ".join(title.split())[:80], " ".join(body.split())[:240]
    if sys.platform == "win32":
        script = base64.b64encode(_TOAST.encode("utf-16-le")).decode("ascii")
        app = register_app()
        env = {
            "VARYZ_NOTICE_TITLE": title,
            "VARYZ_NOTICE_BODY": body,
            "VARYZ_NOTICE_URL": url if url.startswith("https://") else "",
            "VARYZ_NOTICE_LOGO": LOGO.as_uri() if LOGO.is_file() else "",
            "VARYZ_NOTICE_APP": app,
            "VARYZ_NOTICE_TAG": "v" + uuid.uuid4().hex[:15],
            "VARYZ_NOTICE_SECONDS": str(ON_SCREEN),
        }
        runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                script,
            ],
            env,
        )
        return True
    if sys.platform == "darwin":
        runner(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                body,
            ],
            {},
        )
        return True
    if shutil.which("notify-send"):
        icon = ["--icon=" + str(LOGO)] if LOGO.is_file() else []
        runner(["notify-send", "--app-name=Varyz", *icon, "--", title, body], {})
        return True
    return False


def _post(client: Any, path: str, body: dict[str, Any]) -> Any:
    return client.request("POST", path, json=body, timeout=TIMEOUT)


def _client(config_path: Path | None) -> Any | None:
    from varyz.cloud.client import CloudClient, credential_name, load_tokens
    from varyz.cloud.local_access import _identity
    from varyz.config import find_config_file

    config = find_config_file(config_path)
    account = _identity(config) if config is not None else None
    if account is None or config is None:
        return None
    url = account["url"]
    name = credential_name(config, url)
    try:
        tokens = load_tokens(name)
    except (ValueError, RuntimeError):
        return None
    return CloudClient(url, tokens, credential=name)


def check(config_path: Path | None, *, runner: Runner = _run, client: Any = None) -> int:
    """Show new Shared Varyz notices. Returns how many; never raises."""
    if not enabled(config_path):
        return 0
    own = client is None
    try:
        client = client or _client(config_path)
        if client is None:
            return 0
        from varyz.cloud import local_access
        from varyz.config import find_config_file

        config = find_config_file(config_path)
        if config is not None:
            state = local_access.check(config, client=client)
            access = state.get("access", {})
            if (access.get("active") is False and not state.get("notified")
                    and show(TITLE, access["message"], client.url, runner=runner)):
                local_access.mark_notified(config)
                return 1
        found = client.request("GET", PATH, timeout=TIMEOUT)
        items = [n for n in (found or {}).get("notices", []) if isinstance(n, dict)]
        if not items:
            return 0
        if len(items) <= SHOW_AT_ONCE:
            for item in items:
                show(TITLE, str(item.get("text", "")), str(item.get("url", "")), runner=runner)
        else:
            show(
                TITLE,
                f"{len(items)} updates in your shared projects. Ask your agent about them.",
                str(items[-1].get("url", "")),
                runner=runner,
            )
        ids = [str(item.get("id")) for item in items if item.get("id")]
        _post(client, PATH + "/seen", {"ids": ids})
        return len(items)
    except Exception:  # noqa: BLE001 - a notification never stops the daemon
        return 0
    finally:
        if own and client is not None:
            with contextlib.suppress(Exception):
                client.close()


def status(config_path: Path | None) -> dict[str, Any]:
    """Desktop and email state, for `varyz cloud notifications`."""
    result: dict[str, Any] = {"desktop": enabled(config_path), "email": None, "connected": False}
    client = _client(config_path)
    if client is None:
        return result
    try:
        found = client.request("GET", PATH, timeout=TIMEOUT)
        result.update(
            connected=True,
            email=bool((found or {}).get("email", True)),
            waiting=len((found or {}).get("notices", [])),
        )
    except Exception:  # noqa: BLE001 - status reports what it can
        pass
    finally:
        client.close()
    return result


def set_email(config_path: Path | None, on: bool) -> dict[str, Any]:
    client = _client(config_path)
    if client is None:
        raise ValueError("Connect Varyz Cloud first with `varyz cloud connect`.")
    try:
        return dict(_post(client, PATH + "/settings", {"email": on}))
    finally:
        client.close()


def test(config_path: Path | None, *, runner: Runner = _run) -> dict[str, Any]:
    """One sample desktop notification and, when Cloud is connected, one sample email."""
    shown = show(
        TITLE, "Notifications work. This is how Shared Varyz will tell you things.", runner=runner
    )
    result: dict[str, Any] = {"desktop": "shown" if shown else "unsupported", "email": None}
    client = _client(config_path)
    if client is None:
        return result
    try:
        result["email"] = dict(_post(client, PATH + "/test", {})).get("email")
    except Exception as exc:  # noqa: BLE001 - report, never raise, for a test
        result["email"] = "failed: " + str(exc)[:120]
    finally:
        client.close()
    return result


_last: dict[str, float] = {}


def maybe_check(config_path: Path, *, now: Callable[[], float] = time.monotonic) -> None:
    """Called each daemon loop: checks at most once a minute."""
    key = str(config_path)
    if now() - _last.get(key, -CHECK_EVERY) < CHECK_EVERY:
        return
    _last[key] = now()
    check(config_path)

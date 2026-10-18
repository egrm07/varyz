"""How much of the owner's Claude and ChatGPT plan limits is left, before and while a reader runs.

The headless reader (`varyz.resolution.headless_reader`) runs on the owner's own
Claude Code or Codex sign-in, so it spends the same five-hour and weekly
allowance the owner works in. Both providers report it for free:

- Claude: ``GET api.anthropic.com/api/oauth/usage`` with Claude Code's own
  token (``five_hour`` and ``seven_day``, 0-100 used), and a ``rate_limit_event``
  in every ``claude -p --output-format stream-json`` run (0-1 used).
- Codex: ``GET chatgpt.com/backend-api/wham/usage`` with Codex's own token
  (``primary_window`` is five hours, ``secondary_window`` the week).

Tokens are read from the CLIs' files, sent only to their own provider, and
never logged or refreshed here: an expired one reads as unknown until the CLI
refreshes it itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OAUTH_BETA = "oauth-2025-04-20"
TIMEOUT_SECONDS = 8.0
FIVE_HOURS = 5 * 3600
WEEK = 7 * 24 * 3600
PROVIDERS = ("claude", "codex")


@dataclass(frozen=True)
class Window:
    """One limit window: the share used (0-100) and when it starts over (epoch seconds)."""

    used: float
    resets_at: float | None = None

    @property
    def left(self) -> float:
        return max(0.0, 100.0 - self.used)


@dataclass(frozen=True)
class Usage:
    provider: str
    five_hour: Window | None
    weekly: Window | None
    read_at: float
    source: str

    @property
    def known(self) -> bool:
        return self.five_hour is not None and self.weekly is not None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Usage | None:
        try:
            return cls(
                provider=str(data["provider"]),
                five_hour=_window_from(data.get("five_hour")),
                weekly=_window_from(data.get("weekly")),
                read_at=float(data["read_at"]),
                source=str(data.get("source") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Gate:
    """The least left, in percent, a job may start or go on with."""

    five_hour: float
    weekly: float

    def blocks(self, usage: Usage) -> list[Window]:
        """The windows that stop the job (empty when it may run); unknown ones stop nothing here."""
        blocking: list[Window] = []
        if usage.five_hour is not None and usage.five_hour.left <= self.five_hour:
            blocking.append(usage.five_hour)
        if usage.weekly is not None and usage.weekly.left <= self.weekly:
            blocking.append(usage.weekly)
        return blocking

    def headroom(self, usage: Usage) -> float | None:
        """How far above the gate the tighter window is; None when usage is not known."""
        if not usage.known:
            return None
        assert usage.five_hour is not None and usage.weekly is not None
        return min(usage.five_hour.left - self.five_hour, usage.weekly.left - self.weekly)


def gates(cfg: Any) -> dict[str, Gate]:
    """Compatibility for older callers; saved allowance thresholds are ignored."""
    return {"full": Gate(0, 0), "live": Gate(0, 0)}


def _window_from(raw: Any) -> Window | None:
    if not isinstance(raw, Mapping):
        return None
    used = raw.get("used")
    if used is None:
        return None
    resets = raw.get("resets_at")
    return Window(float(used), float(resets) if resets is not None else None)


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        # Milliseconds from some sources, seconds from others.
        return float(value) / 1000 if value > 10**11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude_home() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def claude_token(home: Path | None = None, now: float | None = None) -> str | None:
    """Claude Code's sign-in token while it is still valid; never refreshed here."""
    override = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") if home is None else None
    if override:
        return override
    stores = [_json((home or claude_home()) / ".credentials.json")]
    if sys.platform == "darwin" and home is None:
        # Claude Code uses the Keychain on macOS, with a separate entry for a
        # selected configuration directory. Never fall back to another account.
        selector = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR",
                                  os.environ.get("CLAUDE_CONFIG_DIR", ""))
        suffix = "-" + hashlib.sha256(selector.encode()).hexdigest()[:8] if selector else ""
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s",
                 "Claude Code-credentials" + suffix, "-w"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                stores.append(json.loads(result.stdout))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    candidates: list[tuple[float, str]] = []
    for store in stores:
        stored = store.get("claudeAiOauth") if isinstance(store, dict) else None
        if not isinstance(stored, dict) or not isinstance(stored.get("accessToken"), str):
            continue
        expires = _epoch(stored.get("expiresAt"))
        if expires is None or expires > (now if now is not None else time.time()) + 30:
            candidates.append((expires or 0, str(stored["accessToken"])))
    return max(candidates)[1] if candidates else None


def read_claude(*, token: str | None = None, home: Path | None = None,
                get: Callable[..., Any] = httpx.get) -> Usage | None:
    """Claude's five-hour and weekly use, or None when it cannot be read now."""
    token = token or claude_token(home)
    if token is None:
        return None
    try:
        response = get(CLAUDE_USAGE_URL, timeout=TIMEOUT_SECONDS, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA})
        if response.status_code != 200:
            return None
        data = response.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None

    def window(raw: Any) -> Window | None:
        if not isinstance(raw, Mapping) or raw.get("utilization") is None:
            return None
        return Window(float(raw["utilization"]), _epoch(raw.get("resets_at")))

    return Usage("claude", window(data.get("five_hour")), window(data.get("seven_day")),
                 time.time(), "endpoint")


def from_claude_event(event: Mapping[str, Any]) -> Usage | None:
    """The use a ``claude -p`` stream reports with each model call, or None for other events."""
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    windows = info.get("unifiedWindows") if isinstance(info, Mapping) else None
    if not isinstance(windows, Mapping):
        return None

    def window(raw: Any) -> Window | None:
        if not isinstance(raw, Mapping) or raw.get("utilization") is None:
            return None
        return Window(float(raw["utilization"]) * 100, _epoch(raw.get("resetsAt")))

    usage = Usage("claude", window(windows.get("five_hour")), window(windows.get("seven_day")),
                  time.time(), "stream")
    return usage if usage.five_hour or usage.weekly else None


def codex_credentials(home: Path | None = None) -> tuple[str, str] | None:
    tokens = _json((home or codex_home()) / "auth.json").get("tokens")
    if not isinstance(tokens, dict):
        return None
    access, account = tokens.get("access_token"), tokens.get("account_id")
    if not isinstance(access, str) or not isinstance(account, str):
        return None
    return access, account


def _codex_window(raw: Any, limit_reached: bool) -> Window | None:
    if not isinstance(raw, Mapping) or raw.get("used_percent") is None:
        return None
    used = 100.0 if limit_reached else float(raw["used_percent"])
    return Window(used, _epoch(raw.get("reset_at") or raw.get("resets_at")))


def read_codex(*, credentials: tuple[str, str] | None = None, home: Path | None = None,
               get: Callable[..., Any] = httpx.get,
               rollout_max_age: float = 300.0) -> Usage | None:
    """Codex's five-hour and weekly use, or None when it cannot be read now.

    When the endpoint does not answer, the newest Codex session's last report
    stands in if it is at most ``rollout_max_age`` seconds old.
    """
    credentials = credentials or codex_credentials(home)
    if credentials is not None:
        try:
            response = get(CODEX_USAGE_URL, timeout=TIMEOUT_SECONDS, headers={
                "Authorization": f"Bearer {credentials[0]}",
                "chatgpt-account-id": credentials[1]})
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, OSError, ValueError):
            data = None
        limits = data.get("rate_limit") if isinstance(data, dict) else None
        if isinstance(limits, dict):
            reached = bool(limits.get("limit_reached"))
            windows = {
                raw.get("limit_window_seconds"): _codex_window(raw, reached)
                for raw in (limits.get("primary_window"), limits.get("secondary_window"))
                if isinstance(raw, Mapping)
            }
            usage = Usage("codex", windows.get(FIVE_HOURS), windows.get(WEEK), time.time(),
                          "endpoint")
            if usage.known:
                return usage
    return _codex_from_rollout(home or codex_home(), rollout_max_age)


def _codex_from_rollout(home: Path, max_age: float) -> Usage | None:
    newest: Path | None = None
    newest_time = 0.0
    sessions = home / "sessions"
    now = time.time()
    try:
        days = sorted(sessions.glob("*/*/*"), reverse=True)[:2]
    except OSError:
        return None
    for day in days:
        for path in day.glob("rollout-*.jsonl"):
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified > newest_time:
                newest, newest_time = path, modified
    if newest is None or now - newest_time > max_age:
        return None
    found: Usage | None = None
    try:
        lines = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-400:]):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            continue

        def window(raw: Any) -> Window | None:
            if not isinstance(raw, Mapping) or raw.get("used_percent") is None:
                return None
            return Window(float(raw["used_percent"]), _epoch(raw.get("resets_at")))

        found = Usage("codex", window(limits.get("primary")), window(limits.get("secondary")),
                      newest_time, "session")
        break
    return found if found is not None and found.known else None


READERS: dict[str, Callable[[], Usage | None]] = {"claude": read_claude, "codex": read_codex}


def read(provider: str) -> Usage | None:
    return READERS[provider]()


def describe(usage: Usage | None) -> str:
    if usage is None or not usage.known:
        return "unknown"
    assert usage.five_hour is not None and usage.weekly is not None
    return f"5h {usage.five_hour.left:.0f}% left, weekly {usage.weekly.left:.0f}% left"


__all__ = [
    "PROVIDERS",
    "Gate",
    "Usage",
    "Window",
    "describe",
    "from_claude_event",
    "gates",
    "read",
    "read_claude",
    "read_codex",
]

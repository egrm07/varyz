"""Setup's offer to extract the owner's preferences from their past chats.

Setup reads the chosen sources before it downloads the search models, so the
owner's chats with AI agents are in the archive while the 1.3 GB download
runs. When an agent that can run varyz's own reader was connected (Claude
Code or Codex, `headless_reader.find_runners`), the page asks "Extract
preferences from your existing chats?" and says what it takes: the share of
the plan's 5-hour and weekly limits, in percent, and how long. A yes hands
the full reading to varyz's own reader in the background now, instead of on
the agent's first use (`profile_reader.SETUP_STEP`); a no keeps that.

A reading's cost is estimated from the readings measured so far until the
reading itself shows its pace and its draw on the plan. The page also says
how long the downloads still take, from their measured speed.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from varyz.resolution import usage as usage_mod
from varyz.resolution.usage import Gate, Usage

# Measured on 2026-10-09: Codex's reader read 2,807 turns in 6 min 13 s and
# merged what it found in 2 min 43 s, using 12% of the 5-hour limit and 2% of
# the weekly one. No full Claude reading has been measured; its haiku reader is
# taken to cost the same until its own run shows otherwise.
READ_SECONDS_PER_1000 = 133.0
MERGE_SECONDS_PER_1000 = 58.0
FIVE_HOUR_PER_1000 = 4.3
WEEKLY_PER_1000 = 0.7
# Starting the reader and its first model call, whatever the size.
START_SECONDS = 30.0
# Before this much has been read, the measured pace says little.
SETTLE_SECONDS = 20.0
SETTLE_TURNS = 20

PLANS = {"claude": "Claude", "codex": "ChatGPT"}

# The classifier models setup may pull, as `ollama list` sizes them (2026-10-10).
CLASSIFIER_BYTES = {"qwen3.5:4b": 3_400_000_000}


# -- the offer ----------------------------------------------------------------------


def _best(usages: dict[str, Usage | None], gate: Gate) -> str:
    """The sign-in with the most room above ``gate``; the first when none is known."""
    known = {name: gate.headroom(found) for name, found in usages.items()
             if found is not None and found.known}
    if known:
        return max(known, key=lambda name: known[name] or 0.0)
    return next(iter(usages))


def estimate(turns: int, found: Usage | None, gate: Gate) -> dict[str, Any]:
    """What reading ``turns`` takes: percent of each limit, seconds, and a pause it needs."""
    five = turns / 1000 * FIVE_HOUR_PER_1000
    weekly = turns / 1000 * WEEKLY_PER_1000
    seconds = START_SECONDS + turns / 1000 * (READ_SECONDS_PER_1000 + MERGE_SECONDS_PER_1000)
    view: dict[str, Any] = {
        "five_hour": round(five, 1), "weekly": round(weekly, 1), "seconds": round(seconds),
        "five_hour_left": None, "weekly_left": None, "pauses_until": None,
        # Where the reader stops to leave the owner the rest of their plan.
        "gate": {"five_hour": round(gate.five_hour), "weekly": round(gate.weekly)},
    }
    if found is None or not found.known:
        return view
    assert found.five_hour is not None and found.weekly is not None
    view["five_hour_left"] = round(found.five_hour.left)
    view["weekly_left"] = round(found.weekly.left)
    # The reader stops at the gate and carries on once that window starts over.
    if five > found.five_hour.left - gate.five_hour:
        view["pauses_until"] = found.five_hour.resets_at
    if weekly > found.weekly.left - gate.weekly:
        view["pauses_until"] = max(found.weekly.resets_at or 0.0,
                                   view["pauses_until"] or 0.0) or None
    return view


def offer(
    cfg: Any, config_file: Path, *,
    runners: dict[str, Any] | None = None,
    read_usage: Callable[[str], Usage | None] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """The page's question, or None when there is nothing to read or nothing to read it with."""
    from varyz.resolution import headless_reader as reader
    from varyz.resolution.profile_reader import (
        AGENT_OFFER_MIN_TURNS,
        ProfileState,
        deferred_until,
        profile_path,
    )

    now = time.time() if now is None else now
    read_usage = read_usage or usage_mod.read
    if not reader.enabled(cfg, "full") or deferred_until(cfg, now) is not None:
        return None
    if runners is None:
        runners = reader.find_runners(cfg, Path(config_file).resolve())
    if not runners:
        return None
    turns, merges, _, _ = reader._work_left(cfg, "full", now)
    first = not ProfileState(profile_path(cfg, "cloud")).exists
    if (turns == 0 and not merges) or (not first and turns < AGENT_OFFER_MIN_TURNS):
        return None
    usages = {name: read_usage(name) for name in runners}
    gate = usage_mod.gates(cfg)["full"]
    choice = reader.choose("full", gate, usages, now)
    name = choice.runner or _best(usages, gate)
    found = usages.get(name)
    return {
        "state": "offered",
        "turns": turns,
        "runner": name,
        "plan": PLANS.get(name, name),
        "known": found is not None and found.known,
        # Set when the limits stop it from starting now: when it starts by itself.
        "starts_at": choice.resume_after if choice.runner is None else None,
        **estimate(turns, found, gate),
    }


def start(
    cfg: Any, config_file: Path, offered: dict[str, Any], *,
    request: Callable[..., bool] | None = None,
    read_usage: Callable[[str], Usage | None] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Hand the reading to varyz's own reader; what its progress is measured from, or None."""
    from varyz.resolution import headless_reader as reader
    from varyz.resolution.profile_reader import announced, reading_taken

    now = time.time() if now is None else now
    read_usage = read_usage or usage_mod.read
    if not (request or reader.request)(cfg, "full", Path(config_file).resolve(), now=now):
        return None
    # The owner chose on the page: their agent neither asks again nor announces it.
    reading_taken(cfg, now)
    announced(cfg, now)
    found = read_usage(offered["runner"]) if offered.get("known") else None
    return {
        "at": now,
        "turns": int(offered["turns"]),
        "runner": offered["runner"],
        "seconds": float(offered["seconds"]),
        "five_hour_used": found.five_hour.used if found and found.five_hour else None,
        "weekly_used": found.weekly.used if found and found.weekly else None,
        "merging_since": None,
    }


# -- its progress -------------------------------------------------------------------


def _used_since(status: dict[str, Any], started: dict[str, Any]) -> dict[str, float | None]:
    """How much of each limit went since the start; None where a window started over."""
    raw = (status.get("usage") or {}).get(started["runner"])
    found = Usage.from_dict(raw) if isinstance(raw, dict) else None
    used: dict[str, float | None] = {"five_hour": None, "weekly": None}
    if found is None or found.read_at < started["at"]:
        return used
    for key, window in (("five_hour", found.five_hour), ("weekly", found.weekly)):
        before = started.get(f"{key}_used")
        if window is not None and before is not None and window.used >= before:
            used[key] = round(window.used - before, 1)
    return used


def progress(
    cfg: Any, started: dict[str, Any], now: float, work: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """How far the reading has got, how long it still takes and what it has used.

    ``started`` is `start`'s answer; the time merging began is kept in it.
    """
    from varyz.resolution import headless_reader as reader

    status = reader.load_status(cfg)
    turns_left, _, _, _ = work if work is not None else reader._work_left(cfg, "full", now)
    total = max(int(started["turns"]), turns_left)
    read = total - turns_left
    state = status.get("state")
    pending = "full" in (status.get("pending") or [])
    elapsed = now - float(started["at"])
    merge_seconds = total / 1000 * MERGE_SECONDS_PER_1000
    view: dict[str, Any] = {
        "state": "accepted", "turns": total, "turns_left": turns_left,
        "fraction": read / total if total else 1.0, "seconds_left": None,
        "until": None, "reason": "", **{f"used_{k}": v for k, v in
                                        _used_since(status, started).items()},
    }
    if state in {"failed", "no_runner"}:
        view.update(phase="failed", reason=str(status.get("reason") or state))
    elif state == "waiting_for_usage":
        view.update(phase="waiting", until=status.get("resume_after") or status.get("next_check"))
    elif state in {"scheduled", "paused", "stopped"}:
        view.update(phase="stopped", reason=str(status.get("reason") or state),
                    until=status.get("next_check") if state == "scheduled" else None)
    elif not pending:
        view.update(phase="done", fraction=1.0)
    elif turns_left == 0:
        if started.get("merging_since") is None:
            started["merging_since"] = now
        view.update(phase="merging",
                    seconds_left=max(merge_seconds - (now - started["merging_since"]), 30.0))
    else:
        view["phase"] = "reading"
        if elapsed >= SETTLE_SECONDS and read >= max(SETTLE_TURNS, total * 0.02):
            view["seconds_left"] = elapsed * turns_left / read + merge_seconds
        else:
            view["seconds_left"] = max(float(started["seconds"]) - elapsed, 30.0)
    return view


# -- the downloads ------------------------------------------------------------------


_UNITS = {"bytes": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
_BYTES = re.compile(r": ([\d.]+) (bytes|kB|MB|GB) of ([\d.]+) (bytes|kB|MB|GB)$")


def download_bytes(line: str) -> tuple[int, int] | None:
    """(done, in all) from a download's progress line (`cli._bytes_progress`)."""
    match = _BYTES.search(line)
    if match is None:
        return None
    done, unit, total, total_unit = match.groups()
    return int(float(done) * _UNITS[unit]), int(float(total) * _UNITS[total_unit])


def _ollama_models() -> Path:
    configured = os.environ.get("OLLAMA_MODELS")
    return Path(configured) if configured else Path.home() / ".ollama" / "models"


def classifier_download(model: str) -> int | None:
    """Bytes step 7 still pulls for ``model``: 0 once pulled, None when its size is unknown."""
    name, _, tag = model.partition(":")
    manifest = (_ollama_models() / "manifests" / "registry.ollama.ai" / "library" / name
                / (tag or "latest"))
    if manifest.is_file():
        return 0
    return CLASSIFIER_BYTES.get(model)


class DownloadClock:
    """How long setup's downloads still take: this one at its measured speed, and those after it."""

    SAMPLE_SECONDS = 2.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sample: tuple[float, str, int] | None = None
        self.speed: float | None = None
        self.phase = ""
        self.remaining = 0

    def heard(self, line: str) -> None:
        """A progress line: a download's moves the measure, any other ends the download."""
        from varyz import cli

        found = download_bytes(line) if cli.is_download(line) else None
        if found is None:
            if not cli.is_download(line):
                self.phase, self.remaining, self._sample = "", 0, None
            return
        done, total = found
        phase = line.split(":", 1)[0]
        now = self._clock()
        self.phase, self.remaining = phase, max(total - done, 0)
        if self._sample is None or self._sample[1] != phase or done < self._sample[2]:
            self._sample = (now, phase, done)
            return
        at, _, before = self._sample
        if now - at < self.SAMPLE_SECONDS:
            return
        measured = (done - before) / (now - at)
        self.speed = measured if self.speed is None else 0.7 * self.speed + 0.3 * measured
        self._sample = (now, phase, done)

    def view(self, later: int | None, later_name: str | None) -> dict[str, Any]:
        """For the page: what is downloading, what follows, and the seconds for all of it."""
        seconds = None
        if self.speed and (self.remaining or later):
            seconds = (self.remaining + (later or 0)) / self.speed
        return {
            "now": self.phase.removeprefix("Downloading ") if self.phase else "",
            "then": later_name if later != 0 else None,
            "then_bytes": later or None,
            "seconds": round(seconds) if seconds is not None else None,
        }


__all__ = [
    "CLASSIFIER_BYTES", "DownloadClock", "PLANS", "classifier_download", "download_bytes",
    "estimate", "offer", "progress", "start",
]

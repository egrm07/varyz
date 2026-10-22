"""Setup's offer to extract the owner's preferences from their past chats.

Setup reads the chosen sources before it downloads the search models, so the
owner's chats with AI agents are in the archive while the download (250 MB,
or 1.3 GB with a local judge) runs. When an agent that can run varyz's own
reader was connected (Claude Code or Codex, `headless_reader.find_runners`),
the page asks "Extract
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
# Nothing read this long after the start: the estimate is no longer believable,
# so the page says nothing has been read yet instead of counting down from it.
FIRST_READ_SECONDS = 180.0
# Learning moves a model call at a time, so its pace is read over a longer
# stretch than a download's: the last 2 minutes or 30% of the reading.
RECENT_SECONDS = 120.0
RECENT_SHARE = 0.3
# The local reader before it has measured itself. Measured on 2026-10-22: 16
# reads of 107 messages took 0.68 s a message with qwen3.5:4b on an RTX 5070;
# rounded up for slower graphics cards and the merges after. Its own pace
# replaces this after 20 messages.
LOCAL_START_SECONDS = 60.0
LOCAL_SECONDS_PER_TURN = 1.0

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
    """What reading ``turns`` takes: percent of each limit, seconds and available allowance."""
    five = turns / 1000 * FIVE_HOUR_PER_1000
    weekly = turns / 1000 * WEEKLY_PER_1000
    seconds = START_SECONDS + turns / 1000 * (READ_SECONDS_PER_1000 + MERGE_SECONDS_PER_1000)
    view: dict[str, Any] = {
        "five_hour": round(five, 1), "weekly": round(weekly, 1), "seconds": round(seconds),
        "five_hour_left": None, "weekly_left": None, "pauses_until": None,
    }
    if found is None or not found.known:
        return view
    assert found.five_hour is not None and found.weekly is not None
    view["five_hour_left"] = round(found.five_hour.left)
    view["weekly_left"] = round(found.weekly.left)
    return view


def older_unread(cfg: Any, tier: str) -> int:
    """Unread chat messages too old for a reading, which only the newest take."""
    import sqlite3

    from varyz.resolution import profile_reader

    db = Path(cfg.db_path)
    if not db.exists():
        return 0
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        state = profile_reader.ProfileState(profile_reader.profile_path(cfg, tier))
        return profile_reader.older_unread(state, conn, tier)
    finally:
        conn.close()


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
    if cfg.workflows.inference_mode == "local":
        from varyz.resolution import local_reader

        if not local_reader._allowed(cfg):
            return None
        turns, merges = local_reader.work_left(cfg)
        if not turns and not merges:
            return None
        return {
            "state": "offered", "turns": turns, "older": older_unread(cfg, "local"),
            "runner": "local", "plan": "AI plan",
            "known": True, "five_hour": 0, "weekly": 0,
            "seconds": LOCAL_START_SECONDS + turns * LOCAL_SECONDS_PER_TURN,
            "estimate_note": ("Rough local time estimate; updates as it reads. "
                              "Starts when the model is ready."),
        }
    if not reader.enabled(cfg, "full") or deferred_until(cfg, now) is not None:
        return None
    if runners is None:
        runners = reader.find_runners(cfg, Path(config_file).resolve())
    turns, merges, _, _ = reader._work_left(cfg, "full", now)
    first = not ProfileState(profile_path(cfg, "cloud")).exists
    if (turns == 0 and not merges) or (not first and turns < AGENT_OFFER_MIN_TURNS):
        return None
    if not runners:
        return {
            "state": "offered", "turns": turns, "older": older_unread(cfg, "cloud"),
            "runner": "agent", "plan": "AI account",
            "known": False, "allow_agent_fallback": True,
            "estimate_note": "Estimates vary by plan and model.",
            **estimate(turns, None, usage_mod.gates(cfg)["full"]),
        }
    usages = {name: reader._read_usage(read_usage, name) for name in runners}
    gate = usage_mod.gates(cfg)["full"]
    choice = reader.choose("full", gate, usages, now)
    name = choice.runner or _best(usages, gate)
    found = usages.get(name)
    return {
        "state": "offered",
        "turns": turns,
        "older": older_unread(cfg, "cloud"),
        "runner": name,
        "plan": PLANS.get(name, name),
        "known": found is not None and found.known,
        # Set when the limits stop it from starting now: when it starts by itself.
        "starts_at": choice.resume_after if choice.runner is None else None,
        "estimate_note": ("Based on an earlier Codex reading; "
                          "your plan and model may use more or less."),
        "usage_unknown": found is None or not found.known,
        "allow_agent_fallback": True,
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
    if offered.get("runner") == "local":
        from varyz.resolution import local_reader

        if not local_reader.request(cfg, config_file, wait_model=True):
            return None
        return {"at": now, "turns": int(offered["turns"]), "runner": "local",
                "seconds": float(offered["seconds"]), "merging_since": None}
    if offered.get("runner") == "agent":
        reader.hand_back(cfg, "cli_unavailable")
        return {"at": now, "turns": int(offered["turns"]), "runner": "agent",
                "seconds": float(offered["seconds"])}
    reader.update_status(cfg, lambda status: status.update({
        "setup_runner": offered["runner"], "setup_agent_fallback": False,
        "setup_allow_agent_fallback": bool(offered.get("allow_agent_fallback")),
        "next_check": 0, "resume_after": None,
    }))
    if not (request or reader.request)(cfg, "full", Path(config_file).resolve(), now=now):
        if (offered.get("allow_agent_fallback") and reader.enabled(cfg, "full")
                and not reader._paused(cfg)):
            reader.hand_back(cfg, "cli_unavailable")
            return {"at": now, "turns": int(offered["turns"]), "runner": "agent",
                    "seconds": float(offered["seconds"])}
        return None
    # The owner chose on the page: their agent neither asks again nor announces it.
    reading_taken(cfg, now)
    announced(cfg, now)
    found = reader._read_usage(read_usage, offered["runner"]) if offered.get("known") else None
    return {
        "at": now,
        "turns": int(offered["turns"]),
        "runner": offered["runner"],
        "seconds": float(offered["seconds"]),
        "five_hour_used": found.five_hour.used if found and found.five_hour else None,
        "weekly_used": found.weekly.used if found and found.weekly else None,
        "merging_since": None,
    }


def retry(cfg: Any, config_file: Path, *, runner: str | None = None) -> bool:
    """Recheck the already-approved plan now, without widening the approval."""
    from varyz.resolution import headless_reader as reader

    if runner == "local":
        from varyz.resolution import local_reader

        return local_reader.request(cfg, Path(config_file), wait_model=True, force=True)
    if not reader.enabled(cfg, "full") or reader._paused(cfg):
        return False
    runners = reader.find_runners(cfg, Path(config_file).resolve())
    approved = reader.load_status(cfg).get("setup_runner")
    if approved:
        runners = {name: runner for name, runner in runners.items() if name == approved}
    if not runners:
        reader.hand_back(cfg, "cli_unavailable")
        return False
    reader.update_status(cfg, lambda status: status.update(
        setup_agent_fallback=False, next_check=0, resume_after=None))
    return reader.request(cfg, "full", config_file, runners=runners)


def stop(cfg: Any) -> None:
    """Cancel setup's cloud reading; keep its completed work and other pending jobs."""
    from varyz.resolution import headless_reader as reader

    def cancel(status: dict[str, Any]) -> None:
        status["pending"] = [job for job in status.get("pending", []) if job != "full"]
        if status.get("state") == "running" and status.get("kind") == "full":
            status["stop_run"] = status.get("run_id") or "*"
        elif status.get("state") != "running":
            status.update(state="stopped", reason="Learning skipped in setup",
                          next_check=None, resume_after=None)
    reader.update_status(cfg, cancel)


# -- its progress -------------------------------------------------------------------


# Why a reading stopped, in the owner's words: the readers' statuses keep short
# codes and class names, which the page used to send to the command line.
_PLAIN_REASONS = {
    "time": "it reached the time one run may take",
    "stalled": "it went 10 minutes without reading anything",
    "turned off": "learning was turned off or Varyz was paused",
    "put off": "it was set to learn later",
    "asked": "it was asked to stop",
    "the owner asked to read later": "it was set to learn later",
    "varyz hooks are paused": "Varyz is paused",
    "Learning is incomplete; resume the reader.": "it ended before reading everything",
    "Learning skipped in setup": "it was set to learn later",
    "cli_unavailable": "Varyz could not start Claude or Codex",
    "usage_unavailable": "Varyz could not check your plan's usage",
    "no_runner": "Varyz could not start Claude or Codex",
    "scheduled": "it was set to learn later",
    "paused": "Varyz is paused",
    "stopped": "it stopped before reading everything",
    "failed": "the reader failed",
    # The on-device reader's own sentences, without their advice: the card offers
    # Retry and Learn later itself.
    "The local model made no progress; retry learning later.":
        "the local model read nothing new on its last try",
    "The local model did not arrive; run varyz setup again to finish it and start learning.":
        "the local model has not finished downloading",
    "Local learning was paused or the AI mode changed.": "Varyz was paused or the AI mode changed",
    "local reading was paused or the AI mode changed": "Varyz was paused or the AI mode changed",
}


def plain_reason(reason: str) -> str:
    """Why a reading stopped, said so the owner needs no command line to follow it."""
    reason = reason.strip()
    if reason in _PLAIN_REASONS:
        return _PLAIN_REASONS[reason]
    if reason.startswith("reader supervisor failed: "):
        return f"the background reader crashed ({reason.split(': ', 1)[1]})"
    if re.fullmatch(r"[A-Z]\w*(Error|Exception|Unavailable|Timeout|Interrupt)", reason):
        return f"an unexpected error stopped it ({reason})"
    # The card puts this after "It stopped: " and ends the sentence itself.
    reason = reason.rstrip(". ")
    if reason[:1].isupper() and reason[1:2].islower():
        reason = reason[0].lower() + reason[1:]
    return reason or "it stopped before reading everything"


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

    if started.get("runner") == "local":
        return _local_progress(cfg, started, now)
    status = reader.load_status(cfg)
    turns_left, work_merges, _, _ = (work if work is not None else
                                   reader._work_left(cfg, "full", now))
    if status.get("setup_agent_fallback"):
        done = turns_left == 0 and work_merges == 0
        return {"state": "accepted", "phase": "done" if done else "agent",
                "turns": started["turns"], "turns_left": turns_left,
                "reason": status.get("reason", "cli_unavailable"),
                "fraction": 1.0 if done else None}
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
    elif state == "rate_limited":
        view.update(phase="waiting", reason=str(status.get("reason") or ""),
                    until=status.get("next_check") or status.get("resume_after"))
    elif state in {"scheduled", "paused", "stopped"}:
        view.update(phase="stopped", reason=str(status.get("reason") or state),
                    until=status.get("next_check") if state == "scheduled" else None)
    elif not pending and turns_left == 0 and work_merges == 0:
        view.update(phase="done", fraction=1.0)
    elif not pending:
        view.update(phase="stopped", reason="Learning is incomplete; resume the reader.")
    elif turns_left == 0:
        if started.get("merging_since") is None:
            started["merging_since"] = now
        view.update(phase="merging",
                    seconds_left=max(merge_seconds - (now - started["merging_since"]), 30.0))
    else:
        view["phase"] = "reading"
        pace = _recent(started, now, read, turns_left, float(started["at"]))
        if elapsed >= SETTLE_SECONDS and read >= max(SETTLE_TURNS, total * 0.02) and pace:
            view["seconds_left"] = pace + merge_seconds
        elif read == 0 and elapsed >= FIRST_READ_SECONDS:
            # A reader that has read nothing has no pace; the estimate made
            # before it started is not one either.
            view["nothing_read_for"] = round(elapsed)
        else:
            view["seconds_left"] = max(float(started["seconds"]) - elapsed, 30.0)
    return view


def _recent(
    started: dict[str, Any], now: float, read: int, left: int, origin: float,
) -> float | None:
    """Seconds for ``left`` more turns at the latest pace; the samples live in ``started``.

    Reading moves a call at a time, so the figure is set when the count moves and
    counts down between moves; a stall longer than it falls back to the pace with
    the stall in it.
    """
    from varyz.cli import recent_seconds_left

    # Nothing was read when reading began at ``origin``.
    samples = started.setdefault("samples", [(origin, 0)])
    if read != samples[-1][1]:
        samples.append((now, read))
        started["estimate"] = recent_seconds_left(samples, left, recent=RECENT_SECONDS,
                                                  share=RECENT_SHARE)
    estimate = started.get("estimate")
    if estimate is None:
        return None
    since = now - samples[-1][0]
    if since < estimate:
        return estimate - since
    return recent_seconds_left([*samples, (now, read)], left, recent=RECENT_SECONDS,
                               share=RECENT_SHARE)


def _local_progress(cfg: Any, started: dict[str, Any], now: float) -> dict[str, Any]:
    from varyz.resolution import local_reader

    status = local_reader.load_status(cfg)
    left, merges = local_reader.work_left(cfg)
    total = max(int(started["turns"]), left)
    done = total - left
    phase = status.get("state", "queued")
    view: dict[str, Any] = {
        "state": "accepted", "runner": "local", "turns": total, "turns_left": left,
        "fraction": done / total if total else 0, "used_five_hour": 0, "used_weekly": 0,
        "seconds_left": None, "reason": status.get("reason", ""),
    }
    if phase in {"queued", "waiting_model"}:
        view.update(phase="waiting_model", fraction=None)
    elif phase in {"failed", "stopped"}:
        view["phase"] = phase
    elif phase == "idle" and not left and not merges:
        view.update(phase="done", fraction=1)
    else:
        elapsed = now - float(status.get("reading_at") or now)
        view["phase"] = "merging" if not left else "reading"
        # The merges still to make, at the length the reader's own calls take.
        call = status.get("call_seconds")
        merging = merges * float(call) if isinstance(call, int | float) else None
        pace = _recent(started, now, done, left, now - elapsed) if left else None
        if not left:
            # No measured call yet: no figure, rather than "under a minute" throughout.
            view["seconds_left"] = max(merging, 30) if merging else None
        elif done >= SETTLE_TURNS and pace is not None:
            view["seconds_left"] = max(pace + (merging or 0), 30)
        elif left and done == 0 and elapsed >= FIRST_READ_SECONDS:
            view["nothing_read_for"] = round(elapsed)
        else:
            view["seconds_left"] = max(float(started["seconds"]) - elapsed, 30)
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
    "estimate", "offer", "older_unread", "progress", "start",
]

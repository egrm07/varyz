"""Setup's offer to extract preferences from past chats: its estimate, its start and its progress.

Nothing here runs a reader or reads a real plan's usage: the runners, the
usage and the work left are given, and the reader's status is written as the
supervisor writes it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from varyz import setup_extract
from varyz.config import load_config
from varyz.resolution import headless_reader as hr
from varyz.resolution.profile_reader import profile_path
from varyz.resolution.usage import Gate, Usage, Window

T0 = 1_800_000_000.0
FIVE_HOUR_RESET = T0 + 3_600
WEEK_RESET = T0 + 4 * 86_400
GATE = Gate(30.0, 25.0)


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n',
                    encoding="utf-8")
    return load_config(path)


def _usage(provider: str, five_left: float, week_left: float, at: float = T0) -> Usage:
    return Usage(provider, Window(100 - five_left, FIVE_HOUR_RESET),
                 Window(100 - week_left, WEEK_RESET), at, "endpoint")


def _waiting(monkeypatch: pytest.MonkeyPatch, turns: int, merges: int = 0) -> None:
    monkeypatch.setattr(hr, "_work_left", lambda _cfg, _kind, _now: (turns, merges, 0, 0))


def _status(cfg: Any, **status: Any) -> None:
    path = hr.status_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status), encoding="utf-8")


# -- the estimate -------------------------------------------------------------------


def test_the_measured_codex_reading_is_what_the_estimate_gives_back() -> None:
    # 2,807 turns on 2026-10-09: 12% of the 5-hour limit, 2% of the week, about 9 minutes.
    view = setup_extract.estimate(2807, _usage("codex", 100, 71), GATE)

    assert (view["five_hour"], view["weekly"]) == (12.1, 2.0)
    assert view["seconds"] == 566
    assert (view["five_hour_left"], view["weekly_left"]) == (100, 71)
    assert view["pauses_until"] is None
    assert "gate" not in view


def test_estimates_do_not_reserve_allowance_or_promise_a_reset() -> None:
    view = setup_extract.estimate(2807, _usage("codex", 38, 71), GATE)

    assert view["pauses_until"] is None


def test_unknown_usage_still_estimates_the_share_but_not_what_is_left() -> None:
    view = setup_extract.estimate(1000, None, GATE)

    assert (view["five_hour"], view["weekly"]) == (4.3, 0.7)
    assert view["five_hour_left"] is None and view["pauses_until"] is None


# -- the offer ----------------------------------------------------------------------


def test_the_offer_names_the_plan_with_the_most_room(cfg, monkeypatch) -> None:
    _waiting(monkeypatch, 2807)
    usages = {"claude": _usage("claude", 40, 60), "codex": _usage("codex", 90, 70)}

    offer = setup_extract.offer(cfg, Path("config.toml"), runners={"claude": 1, "codex": 1},
                                read_usage=usages.get, now=T0)

    assert offer is not None
    assert (offer["state"], offer["runner"], offer["plan"]) == ("offered", "codex", "ChatGPT")
    assert offer["turns"] == 2807 and offer["known"] is True
    assert offer["starts_at"] is None
    assert offer["five_hour_left"] == 90


def test_low_allowance_still_offers_an_immediate_start(cfg, monkeypatch) -> None:
    _waiting(monkeypatch, 500)

    offer = setup_extract.offer(cfg, Path("config.toml"), runners={"codex": 1},
                                read_usage=lambda _name: _usage("codex", 20, 70), now=T0)

    assert offer is not None and offer["runner"] == "codex"
    assert offer["starts_at"] is None


@pytest.mark.parametrize("case", ["no runner", "nothing waiting", "too few after a reading"])
def test_no_offer_without_something_to_read_and_something_to_read_it(
    cfg, monkeypatch, case
) -> None:
    runners = {} if case == "no runner" else {"codex": 1}
    _waiting(monkeypatch, 0 if case == "nothing waiting" else 12)
    if case == "too few after a reading":
        profile = profile_path(cfg, "cloud")
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text("{}", encoding="utf-8")

    result = setup_extract.offer(cfg, Path("config.toml"), runners=runners,
                                 read_usage=lambda _name: None, now=T0)
    if case == "no runner":
        assert result["runner"] == "agent" and result["state"] == "offered"
    else:
        assert result is None


# -- its start ----------------------------------------------------------------------


def test_a_yes_hands_the_full_reading_to_varyzs_own_reader(cfg) -> None:
    asked: list[str] = []
    offered = {"turns": 2807, "runner": "codex", "seconds": 566, "known": True}

    def request(_cfg, kind, _config_file, *, now):
        asked.append(kind)
        return True

    started = setup_extract.start(cfg, Path("config.toml"), offered, request=request,
                                  read_usage=lambda _name: _usage("codex", 100, 71), now=T0)

    assert asked == ["full"]
    assert started is not None
    assert (started["at"], started["turns"], started["runner"]) == (T0, 2807, "codex")
    assert (started["five_hour_used"], started["weekly_used"]) == (0, 29)


def test_a_reader_that_cannot_take_it_starts_nothing(cfg) -> None:
    offered = {"turns": 10, "runner": "codex", "seconds": 60, "known": False}

    assert setup_extract.start(cfg, Path("config.toml"), offered,
                               request=lambda *_a, **_k: False, now=T0) is None


# -- its progress -------------------------------------------------------------------


def _started(**extra: Any) -> dict[str, Any]:
    return {"at": T0, "turns": 1000, "runner": "codex", "seconds": 221.0,
            "five_hour_used": 10.0, "weekly_used": 30.0, "merging_since": None, **extra}


def test_reading_says_how_far_it_got_at_its_own_pace_and_what_it_used(cfg) -> None:
    _status(cfg, state="running", pending=["full"],
            usage={"codex": _usage("codex", 86, 69, at=T0 + 90).as_dict()})

    view = setup_extract.progress(cfg, _started(), T0 + 100, work=(500, 0, 500, 0))

    assert view["phase"] == "reading" and view["fraction"] == 0.5
    # 500 turns in 100 s leaves 100 s to read, then the merge.
    assert view["seconds_left"] == pytest.approx(100 + 58)
    assert (view["used_five_hour"], view["used_weekly"]) == (4.0, 1.0)


def test_stuck_reader_reports_elapsed_time_without_a_countdown(cfg):
    _status(cfg, state="running", pending=["full"])
    view = setup_extract.progress(cfg, _started(), T0 + 5400, work=(1000, 0, 0, 0))
    assert view["phase"] == "reading" and view["turns_left"] == 1000
    assert view["nothing_read_for"] == 5400
    assert view["seconds_left"] is None


def test_before_its_pace_shows_the_estimate_counts_down(cfg) -> None:
    _status(cfg, state="queued", pending=["full"])

    view = setup_extract.progress(cfg, _started(), T0 + 10, work=(1000, 0, 0, 0))

    assert view["phase"] == "reading"
    assert view["seconds_left"] == 211.0
    assert view["used_five_hour"] is None


def test_with_every_turn_read_it_merges_then_is_done(cfg) -> None:
    started = _started()
    _status(cfg, state="running", pending=["full"])

    view = setup_extract.progress(cfg, started, T0 + 300, work=(0, 3, 1000, 0))
    assert (view["phase"], started["merging_since"]) == ("merging", T0 + 300)
    assert view["seconds_left"] == 58.0

    _status(cfg, state="idle", pending=[])
    view = setup_extract.progress(cfg, started, T0 + 400, work=(0, 0, 1000, 0))
    assert (view["phase"], view["fraction"]) == ("done", 1.0)


def test_waiting_for_the_limits_and_failing_say_so(cfg) -> None:
    _status(cfg, state="rate_limited", pending=["full"], resume_after=FIVE_HOUR_RESET)
    view = setup_extract.progress(cfg, _started(), T0 + 50, work=(700, 0, 300, 0))
    assert (view["phase"], view["until"]) == ("waiting", FIVE_HOUR_RESET)

    _status(cfg, state="failed", pending=["full"], reason="codex: not signed in")
    view = setup_extract.progress(cfg, _started(), T0 + 50, work=(700, 0, 300, 0))
    assert (view["phase"], view["reason"]) == ("failed", "codex: not signed in")


# -- the downloads ------------------------------------------------------------------


def test_the_downloads_take_as_long_as_their_measured_speed_says() -> None:
    now = [0.0]
    downloads = setup_extract.DownloadClock(clock=lambda: now[0])

    downloads.heard("Downloading the search models: 100.0 MB of 1.3 GB")
    assert downloads.view(None, None)["seconds"] is None  # no speed yet
    now[0] = 4.0
    downloads.heard("Downloading the search models: 500.0 MB of 1.3 GB")

    view = downloads.view(3_400_000_000, "qwen3.5:4b")
    # 100 MB/s: 800 MB left of this one, then the classifier's 3.4 GB.
    assert view == {"now": "the search models", "then": "qwen3.5:4b",
                    "then_bytes": 3_400_000_000, "seconds": 42}

    downloads.heard("Loading the local search models")
    assert downloads.view(None, None) == {"now": "", "then": None, "then_bytes": None,
                                          "seconds": None}
    # The speed is kept for what is still to come.
    assert downloads.view(3_400_000_000, "qwen3.5:4b")["seconds"] == 34


def test_a_classifier_already_pulled_is_not_downloaded_again(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))

    assert setup_extract.classifier_download("qwen3.5:4b") == 3_400_000_000
    assert setup_extract.classifier_download("mystery:1b") is None
    manifest = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "4b"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    assert setup_extract.classifier_download("qwen3.5:4b") == 0

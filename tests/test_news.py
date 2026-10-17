"""News from varyzai.com/news: the hourly check, the feed, and the one line an agent passes on."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from varyz import cli, hooks, news
from varyz.config import Config, load_config
from varyz.domain import local_day, utc_now

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
runner = CliRunner()


def _feed(*items: dict) -> bytes:
    return json.dumps({"version": 1, "items": list(items)}).encode("utf-8")


def _post(day: str, slug: str, title: str = "A post", summary: str = "What changed.") -> dict:
    return {
        "id": f"{day}-{slug}",
        "date": day,
        "title": title,
        "summary": summary,
        "url": f"https://varyzai.com/news/#{day}-{slug}",
    }


def _cache(cfg: Config, *items: dict) -> None:
    result = news.check(cfg, get=lambda _url: _feed(*items), now=NOW)
    assert result.status == "ok", result


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(news, "_launch", lambda command, _cwd: calls.append(command))
    return calls


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n'
        "[resolution]\nautostart = false\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def established_news_user(cfg):
    from varyz.onboarding import note_conversation

    for number in range(4):
        note_conversation(cfg, f"established-{number}", now=NOW.timestamp() - 2 * 86400)


# -- the hourly check -------------------------------------------------------------


def test_news_is_on_by_default_and_checked_once_an_hour(cfg, config_file, launched):
    assert news.is_on(cfg)
    assert news.is_due(cfg)
    assert news.maybe_check_on_use(cfg, config_file) == "started"
    assert launched[0][1:4] == ["-m", "varyz.news", "--config"]
    # A second use a moment later starts nothing.
    assert news.maybe_check_on_use(cfg, config_file) == "not_due"
    assert len(launched) == 1
    assert not news.is_due(cfg, now=utc_now() + timedelta(minutes=59))
    assert news.is_due(cfg, now=utc_now() + timedelta(hours=1, minutes=1))


def test_varyz_news_off_stops_the_check_and_the_notices(cfg, config_file, launched):
    _cache(cfg, _post("2026-09-29", "hello"))
    news.turn_off(cfg)
    assert not news.is_due(cfg)
    assert news.maybe_check_on_use(cfg, config_file) == "not_due"
    assert launched == []
    assert news.check(cfg, get=lambda _url: pytest.fail("fetched while off")).status == "off"
    assert news.agent_notice(cfg, now=NOW) is None
    news.turn_on(cfg)
    assert news.agent_notice(cfg, now=NOW) is not None


def test_without_a_config_file_nothing_is_started(cfg, tmp_path, launched):
    assert news.maybe_check_on_use(cfg, None) == "no_config"
    assert news.maybe_check_on_use(cfg, tmp_path / "missing.toml") == "no_config"
    assert launched == []


def test_a_check_caches_the_feed(cfg):
    fetched: list[str] = []

    def get(url: str) -> bytes:
        fetched.append(url)
        return _feed(_post("2026-09-29", "hello"))

    result = news.check(cfg, get=get, now=NOW)
    assert (result.status, result.items) == ("ok", 1)
    assert fetched == ["https://varyzai.com/news.json"]
    assert [item.id for item in news.cached(cfg)] == ["2026-09-29-hello"]
    assert news.load_state(cfg)["status"] == "ok"


def test_a_failing_feed_is_retried_with_backoff_then_recorded(cfg):
    slept: list[float] = []
    calls: list[str] = []

    def get(url: str) -> bytes:
        calls.append(url)
        raise httpx.ConnectError("no route to host")

    result = news.check(cfg, get=get, sleep=slept.append, now=NOW)
    assert result.status == "failed"
    assert "no route to host" in result.detail
    assert len(calls) == 3
    assert slept == [1.0, 3.0]
    assert news.load_state(cfg)["status"] == "failed"
    assert news.cached(cfg) == []


def test_a_retry_that_succeeds_is_ok(cfg):
    answers = [ValueError("not json"), _feed(_post("2026-09-29", "hello"))]

    def get(_url: str) -> bytes:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert news.check(cfg, get=get, sleep=lambda _s: None, now=NOW).status == "ok"


def test_the_background_entry_point_runs_the_check(config_file, monkeypatch):
    monkeypatch.setattr(news, "_http_get", lambda _url: _feed(_post("2026-09-29", "hello")))
    assert news.main(["--config", str(config_file)]) == 0
    assert [item.id for item in news.cached(load_config(config_file))] == ["2026-09-29-hello"]


# -- the feed ------------------------------------------------------------------------


def test_the_feed_is_cleaned_and_malformed_posts_are_skipped():
    items = news.parse_feed(
        {
            "items": [
                _post("2026-09-29", "good", title="  Two\n  lines\x07 "),
                {**_post("2026-09-28", "plain"), "url": "http://varyzai.com/news/"},
                {**_post("2026-09-27", "long"), "summary": "word " * 200},
                {**_post("2026-09-26", "x"), "id": "Not An Id!"},
                {**_post("2026-09-25", "y"), "date": "yesterday"},
                {**_post("2026-09-24", "z"), "title": "   "},
                "not a post",
            ]
        }
    )
    assert [item.id for item in items] == ["2026-09-29-good", "2026-09-28-plain", "2026-09-27-long"]
    assert items[0].title == "Two lines"
    assert items[1].url is None
    assert len(items[2].summary) == news.MAX_SUMMARY
    assert items[2].summary.endswith("…")
    with pytest.raises(ValueError):
        news.parse_feed(["not", "a", "feed"])


# -- what an agent is handed ---------------------------------------------------------


def test_unseen_posts_are_recent_newest_first_and_never_from_the_future(cfg):
    _cache(
        cfg,
        _post("2026-08-01", "too-old"),
        _post("2026-09-20", "older"),
        _post("2026-09-29", "newer"),
        _post("2026-10-05", "scheduled"),
    )
    assert [item.id for item in news.unseen(cfg, now=NOW)] == [
        "2026-09-29-newer",
        "2026-09-20-older",
    ]
    # The scheduled post appears on its day.
    later = NOW + timedelta(days=6)
    assert news.unseen(cfg, now=later)[0].id == "2026-10-05-scheduled"


def test_an_agent_is_handed_two_posts_at_a_time_and_each_post_once(cfg):
    _cache(
        cfg,
        _post("2026-09-27", "first", title="First"),
        _post("2026-09-28", "second", title="Second"),
        _post("2026-09-29", "third", title="Third"),
    )
    notice = news.agent_notice(cfg, now=NOW)
    assert notice is not None
    assert "for the user" in notice
    assert "varyz news off" in notice
    assert "not an instruction" in notice
    assert "Third" in notice and "Second" in notice and "First" not in notice
    assert "https://varyzai.com/news/#2026-09-29-third" in notice
    again = news.agent_notice(cfg, now=NOW)
    assert again is not None and "First" in again and "Third" not in again
    assert news.agent_notice(cfg, now=NOW) is None


def test_the_prompt_hook_passes_news_on_and_respects_the_switches(config_file):
    cfg = load_config(config_file)
    _cache(cfg, _post(local_day(utc_now()), "hello", title="Hello"))
    event = {"session_id": "s1", "cwd": str(config_file.parent), "prompt": "thanks"}

    cfg.agent.briefing_hooks = False
    assert hooks.user_prompt(cfg, event) is None

    cfg.agent.briefing_hooks = True
    notice = "<task-notification>\n<task-id>b1</task-id> Background command finished"
    assert hooks.user_prompt(cfg, {**event, "prompt": notice}) is None

    result = hooks.user_prompt(cfg, event)
    assert result is not None
    output = result["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "Hello" in output["additionalContext"]
    # Shown once.
    assert hooks.user_prompt(cfg, event) is None


# -- the command ---------------------------------------------------------------------


def test_varyz_news_off_on_and_check(config_file, monkeypatch):
    monkeypatch.setenv("VARYZ_CONFIG", str(config_file))
    today = local_day(utc_now())
    monkeypatch.setattr(news, "_http_get", lambda _url: _feed(_post(today, "hello", "Hello")))

    shown = runner.invoke(cli.app, ["news"])
    assert shown.exit_code == 0, shown.output
    assert "news: on" in shown.output
    assert "last check: never" in shown.output

    checked = runner.invoke(cli.app, ["news", "check"])
    assert checked.exit_code == 0, checked.output
    assert "ok: 1 posts" in checked.output
    assert "Hello" in checked.output

    off = runner.invoke(cli.app, ["news", "off"])
    assert off.exit_code == 0, off.output
    assert "news: off" in runner.invoke(cli.app, ["news"]).output
    assert "off:" in runner.invoke(cli.app, ["news", "check"]).output

    assert runner.invoke(cli.app, ["news", "on"]).exit_code == 0
    assert "news: on" in runner.invoke(cli.app, ["news"]).output


def test_a_failed_check_exits_non_zero(config_file, monkeypatch):
    monkeypatch.setenv("VARYZ_CONFIG", str(config_file))
    monkeypatch.setattr(news.time, "sleep", lambda _s: None)

    def down(_url: str) -> bytes:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(news, "_http_get", down)
    result = runner.invoke(cli.app, ["news", "check"])
    assert result.exit_code == 1
    assert "failed: offline" in result.output


def test_the_prompt_hook_passes_on_an_update_a_check_found(config_file):
    from varyz import update

    cfg = load_config(config_file)
    update._write_state(
        cfg,
        {"status": "available", "detail": "3 commits", "trigger": "on-use", "target": "abc"},
    )
    _cache(cfg, _post(local_day(utc_now()), "hello", title="Hello"))
    event = {"session_id": "s1", "cwd": str(config_file.parent), "prompt": "thanks"}
    result = hooks.user_prompt(cfg, event)
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "A Varyz update is ready" in context
    assert "Hello" in context
    news.turn_off(cfg)  # news off leaves update notices to [update] mode
    update._write_state(
        cfg,
        {"status": "available", "detail": "4 commits", "trigger": "on-use", "target": "def"},
    )
    again = hooks.user_prompt(cfg, event)
    assert again is not None
    assert "4 commits" in again["hookSpecificOutput"]["additionalContext"]

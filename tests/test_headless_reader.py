"""varyz's own rule reader: usage ranks sign-ins but only real provider refusals delay learning.

No CLI or model is started: the readers are fake processes and the usage is handed in.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_profile_reader import _agent_server, _queue_only, _shop, daemon_client  # noqa: F401
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution import headless_reader as hr
from varyz.resolution import usage
from varyz.resolution.rule_reader import UsageGuard, guarded
from varyz.resolution.usage import Gate, Usage, Window

T0 = 1_800_000_000.0
FIVE_HOUR_RESET = T0 + 3_600
WEEK_RESET = T0 + 4 * 86_400


def _config(tmp_path: Path, **workflows: Any) -> Path:
    path = tmp_path / "config.toml"
    lines = [f'[storage]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', "[workflows]\n"]
    lines += [f"{key} = {json.dumps(value)}\n" for key, value in workflows.items()]
    path.write_text("".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return _config(tmp_path, reader_codex_model="gpt-5.6-luna")


@pytest.fixture
def cfg(config_file: Path) -> Any:
    return load_config(config_file)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "data" / "varyz.db")
    try:
        yield connection
    finally:
        connection.close()


def _usage(provider: str, five_left: float, week_left: float, at: float = T0) -> Usage:
    return Usage(provider, Window(100 - five_left, FIVE_HOUR_RESET),
                 Window(100 - week_left, WEEK_RESET), at, "endpoint")


class _Response:
    def __init__(self, data: Any, status: int = 200) -> None:
        self.status_code = status
        self._data = data

    def json(self) -> Any:
        return self._data


# -- reading the usage --------------------------------------------------------------


def test_claudes_usage_is_read_from_its_endpoint_and_its_stream() -> None:
    seen: dict[str, Any] = {}

    def get(url: str, **kwargs: Any) -> _Response:
        seen.update(url=url, **kwargs)
        return _Response({"five_hour": {"utilization": 40, "resets_at": "2026-10-03T17:00:00Z"},
                          "seven_day": {"utilization": 96, "resets_at": "2026-10-07T12:00:00Z"}})

    found = usage.read_claude(token="tok", get=get)
    assert found is not None and found.known
    assert (found.five_hour.left, found.weekly.left) == (60, 4)
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert usage.describe(found) == "5h 60% left, weekly 4% left"

    event = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.25, "resetsAt": 1_800_000_000},
        "seven_day": {"utilization": 0.5, "resetsAt": 1_800_000_000_000}}}}
    streamed = usage.from_claude_event(event)
    assert streamed is not None
    assert (streamed.five_hour.left, streamed.weekly.left) == (75, 50)
    assert streamed.weekly.resets_at == 1_800_000_000  # milliseconds read as seconds
    assert usage.from_claude_event({"type": "assistant"}) is None


def test_an_expired_claude_token_is_unknown_and_never_sent(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "old", "expiresAt": 1_000}}), encoding="utf-8")

    def get(*args: Any, **kwargs: Any) -> _Response:
        raise AssertionError("an expired token is not sent")

    assert usage.read_claude(home=tmp_path, get=get) is None


def test_codex_usage_counts_a_reached_limit_as_all_used(tmp_path: Path) -> None:
    def get(url: str, **kwargs: Any) -> _Response:
        assert kwargs["headers"]["chatgpt-account-id"] == "acct"
        return _Response({"rate_limit": {"limit_reached": reached, "primary_window": {
            "used_percent": 56, "limit_window_seconds": 18_000, "reset_at": T0},
            "secondary_window": {"used_percent": 30, "limit_window_seconds": 604_800,
                                 "reset_at": T0 + 1}}})

    reached = False
    found = usage.read_codex(credentials=("tok", "acct"), home=tmp_path, get=get)
    assert found is not None and (found.five_hour.left, found.weekly.left) == (44, 70)
    reached = True
    found = usage.read_codex(credentials=("tok", "acct"), home=tmp_path, get=get)
    assert found is not None and found.five_hour.left == 0 and found.weekly.left == 0


# -- choosing a runner --------------------------------------------------------------


def test_legacy_threshold_settings_no_longer_block_learning(cfg: Any) -> None:
    cfg.workflows.large_reading_min_left_5h = 99
    full = usage.gates(cfg)["full"]
    assert full.five_hour == 0 and full.weekly == 0
    assert hr.choose("full", full, {"codex": _usage("codex", 0, 0)}, T0).runner == "codex"


def test_the_runner_with_most_room_reads_even_when_every_plan_is_low(cfg: Any) -> None:
    usages = {"claude": _usage("claude", 60, 4), "codex": _usage("codex", 20, 70)}
    for kind in ("full", "live"):
        choice = hr.choose(kind, Gate(30, 25), usages, T0)
        assert choice.runner == "codex" and choice.resume_after is None


def test_unknown_usage_allows_every_reading(cfg: Any) -> None:
    for kind in ("full", "live"):
        choice = hr.choose(kind, Gate(30, 25), {"claude": None, "codex": None}, T0)
        assert choice.runner == "claude" and choice.resume_after is None


def test_codex_reads_with_its_newest_usable_luna(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-5.6", "priority": 1},
        {"slug": "gpt-5.6-luna", "priority": 9},
        {"slug": "gpt-6-luna", "priority": 4},
        {"slug": "gpt-5-luna-old", "priority": 2, "visibility": "hide"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    cfg = load_config(_config(tmp_path))
    assert hr.codex_models(cfg) == ["gpt-6-luna", "gpt-5.6-luna"]
    assert hr.codex_models(cfg, rejected=["gpt-6-luna"]) == ["gpt-5.6-luna"]


# -- one run ------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Process:
    """A reader that ends when varyz tells it to stop, or keeps going until killed."""

    pid = 4321

    def __init__(self, cfg: Any, *, obeys_stop: bool = True, ends_at: float | None = None,
                 clock: _Clock | None = None, lines: list[str] | None = None) -> None:
        self.cfg = cfg
        self.obeys_stop = obeys_stop
        self.ends_at = ends_at
        self.clock = clock
        self.stdout = lines or []
        self.code: int | None = None

    def poll(self) -> int | None:
        if self.code is None and self.obeys_stop and hr.load_status(self.cfg).get("stop_run"):
            self.code = 0
        if self.code is None and self.ends_at is not None and self.clock() >= self.ends_at:
            self.code = 0
        return self.code

    def kill(self) -> None:
        self.code = -9


@pytest.fixture
def killed(monkeypatch) -> list[Any]:
    found: list[Any] = []

    def kill(process: Any) -> None:
        found.append(process)
        process.kill()

    monkeypatch.setattr(hr, "kill_tree", kill)
    return found


RUNNER = hr.Runner("codex", "codex.exe", "agent-token")


def test_a_reader_that_crosses_an_old_threshold_continues(cfg: Any, killed: list[Any]) -> None:
    clock = _Clock()
    process = _Process(cfg, clock=clock, ends_at=T0 + 140)
    result = hr.run_one(cfg, RUNNER, "full", ["codex.exe"], gate=Gate(30, 25),
        read_usage=lambda name: _usage(name, 28, 70, clock.now),
        launch=lambda *a: process, clock=clock, sleep=clock.sleep)
    assert result.stopped is None and result.resume_after is None
    assert result.exit_code == 0 and not killed
    assert clock.now >= T0 + 140


def test_a_reader_that_ignores_the_stop_is_killed_after_the_grace(
    cfg: Any, killed: list[Any]
) -> None:
    cfg.workflows.reader_max_minutes = 1
    clock = _Clock()
    process = _Process(cfg, obeys_stop=False)
    result = hr.run_one(cfg, RUNNER, "full", ["codex.exe"], gate=usage.gates(cfg)["full"],
                        read_usage=lambda name: _usage(name, 20, 70, clock.now),
                        launch=lambda *a: process, clock=clock, sleep=clock.sleep)
    assert result.stopped == "time"
    assert killed == [process]
    assert clock.now >= T0 + hr.STOP_GRACE_SECONDS


def test_a_reading_whose_usage_never_shows_keeps_running(cfg: Any, killed: list[Any]) -> None:
    clock = _Clock()
    process = _Process(cfg, clock=clock, ends_at=T0 + 140)
    result = hr.run_one(cfg, RUNNER, "live", ["codex.exe"], gate=Gate(10, 5),
        read_usage=lambda name: None, launch=lambda *a: process, clock=clock, sleep=clock.sleep)
    assert result.stopped is None and not killed
    assert clock.now >= T0 + 140


def test_claudes_stream_reports_count_while_it_reads(cfg: Any, killed: list[Any]) -> None:
    clock = _Clock()
    event = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.97}}}}
    process = _Process(cfg, clock=clock, ends_at=T0 + 140,
                       lines=[json.dumps(event) + "\n"])
    runner = hr.Runner("claude", "claude", "agent-token")
    result = hr.run_one(cfg, runner, "live", ["claude"], gate=usage.gates(cfg)["live"],
                        read_usage=lambda name: None, launch=lambda *a: process,
                        clock=clock, sleep=clock.sleep)
    assert result.stopped is None and result.usage.weekly.left == 3


# -- the supervisor -----------------------------------------------------------------


def test_reader_with_no_progress_stops_at_stall_limit(cfg, killed):
    cfg.workflows.reader_stall_minutes = 1
    clock = _Clock()
    process = _Process(cfg, obeys_stop=False)
    result = hr.run_one(cfg, RUNNER, "full", ["codex.exe"], gate=Gate(30, 25),
                        read_usage=lambda name: None, launch=lambda *a: process,
                        clock=clock, sleep=clock.sleep)
    assert result.stopped == "stalled" and killed == [process]
    assert 60 <= clock.now - T0 <= 60 + hr.STOP_GRACE_SECONDS + 2 * hr.MONITOR_SECONDS


def test_reader_tool_progress_resets_stall_watchdog(cfg, killed, monkeypatch):
    cfg.workflows.reader_stall_minutes = 1
    clock = _Clock()
    process = _Process(cfg, clock=clock, ends_at=T0 + 140)
    monkeypatch.setattr(hr, "_reading_saved", lambda cfg: int((clock.now - T0) // 40))
    result = hr.run_one(cfg, RUNNER, "full", ["codex.exe"], gate=Gate(30, 25),
                        read_usage=lambda name: None, launch=lambda *a: process,
                        clock=clock, sleep=clock.sleep)
    assert result.stopped is None and not killed


def _work(monkeypatch, *steps: tuple[int, int, int, int]) -> None:
    remaining = list(steps)
    monkeypatch.setattr(hr, "_work_left", lambda cfg, kind, now: remaining.pop(0)
                        if len(remaining) > 1 else remaining[0])


def test_backlog_counts_use_named_sqlite_rows(cfg, conn):
    _shop(conn)
    assert hr._work_left(cfg, "full", T0)[0] > 0
    assert len(hr._work_left(cfg, "live", T0)) == 4


def test_the_supervisor_reads_the_backlog_on_codex_and_clears_the_queue(
    cfg: Any, config_file: Path, monkeypatch
) -> None:
    clock = _Clock()
    launched: list[tuple[list[str], dict[str, str]]] = []

    def launch(command: list[str], env: dict[str, str], cwd: Path) -> _Process:
        launched.append((command, env))
        return _Process(cfg, ends_at=clock.now + 30, clock=clock)

    _work(monkeypatch, (5, 0, 0, 0), (0, 0, 5, 2))
    hr.update_status(cfg, lambda status: status.update({"pending": ["full", "live"]}))
    status = hr.supervise(cfg, config_file, runners={"codex": RUNNER},
                          read_usage=lambda name: _usage(name, 44, 70, clock.now),
                          launch=launch, clock=clock, sleep=clock.sleep)
    assert status["state"] == "idle" and status["pending"] == []
    (command, env), = launched
    assert command[command.index("-m") + 1] == "gpt-5.6-luna"
    assert env[hr.TOKEN_ENV] == "agent-token" and env[hr.ENV]
    log = hr.log_path(cfg).read_text(encoding="utf-8")
    assert '"started"' in log and '"ended"' in log and "agent-token" not in log


def test_a_legacy_quota_wait_resumes_without_waiting_for_reset(cfg, config_file):
    hr.update_status(cfg, lambda status: status.update(
        state="waiting_for_usage", pending=["full"], next_check=WEEK_RESET))
    started = []
    assert hr.maybe_resume(cfg, config_file, T0 + 60, start=lambda *a: started.append(a))
    assert len(started) == 1


def test_a_reading_is_handed_to_varyz_unless_the_owner_keeps_it_for_the_agent(
    tmp_path: Path, config_file: Path, cfg: Any
) -> None:
    started: list[Any] = []
    assert hr.request(cfg, "full", config_file, runners={"codex": RUNNER},
                      start=lambda *a: started.append(a), now=T0)
    assert hr.load_status(cfg)["pending"] == ["full"] and len(started) == 1

    agent_cfg = load_config(_config(tmp_path, reader_runner="agent"))
    assert not hr.request(agent_cfg, "live", config_file, runners={"codex": RUNNER},
                          start=lambda *a: started.append(a), now=T0)
    assert not hr.request(cfg, "full", config_file, runners={}, now=T0)


def test_start_task_leaves_the_reading_to_varyz_when_it_can_run_it(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client, monkeypatch  # noqa: F811
) -> None:
    started: list[Any] = []
    monkeypatch.setattr(hr, "find_runners", lambda cfg, config_file: {"codex": RUNNER})
    monkeypatch.setattr(hr, "spawn", lambda config_file, kind=None: started.append(kind))
    _queue_only(tmp_path)
    _shop(conn)
    server = _agent_server(tmp_path, daemon_client)
    try:
        briefing = server.call("start_task", {"task": "add a settings page"})
    finally:
        server.close()
    # The first reading is major: varyz runs it, the agent only tells the owner.
    assert "subagent_prompt" not in briefing["background_job"]
    assert "start nothing" in briefing["background_job"]["do"]
    assert started and hr.load_status(load_config(tmp_path / "config.toml"))["pending"]


def test_the_reading_tool_tells_a_stopped_reader_to_end(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client, monkeypatch  # noqa: F811
) -> None:
    _shop(conn)
    server = _agent_server(tmp_path, daemon_client)
    try:
        cfg = load_config(tmp_path / "config.toml")
        hr.update_status(cfg, lambda status: status.update({"stop_run": "run-1"}))
        monkeypatch.setenv(hr.ENV, "run-1")
        answer = server.call("profile_reading_task", {})
    finally:
        server.close()
    assert answer["done"] is True and "stop" in answer["stopped"]


def test_a_reading_put_off_waits_and_its_own_reader_starts_it_at_that_time(
    cfg, config_file
) -> None:
    until = T0 + 3 * 3600
    told = hr.schedule(cfg, until, T0)
    assert "varyz starts it then by itself" in told
    status = hr.load_status(cfg)
    assert status["state"] == "scheduled" and status["pending"] == ["full"]
    assert hr.health(cfg, now=T0)[0] and "starts" in hr.summary(cfg, now=T0)
    # Nothing runs before then, not even a supervisor started by hand.
    started: list[Any] = []
    assert not hr.maybe_resume(cfg, config_file, T0 + 60, start=lambda *a: started.append(a))
    status = hr.supervise(cfg, config_file, kind="full", runners={"codex": RUNNER},
                          read_usage=lambda _: _usage("codex", 80, 80), clock=lambda: T0 + 60,
                          launch=lambda *a: pytest.fail("read while put off"))
    assert status["state"] == "scheduled" and status["next_check"] == until
    # At that time the daemon's watcher starts it.
    assert hr.maybe_resume(cfg, config_file, until + 1, start=lambda *a: started.append(a))
    assert len(started) == 1 and hr.load_status(cfg)["state"] == "queued"
    # A schedule missed by more than a few minutes shows in the doctor.
    hr.update_status(cfg, lambda s: s.update({"state": "scheduled"}))
    assert not hr.health(cfg, now=until + 181)[0]


def test_a_reader_under_way_stops_when_the_owner_puts_it_off(
    tmp_path: Path, conn: sqlite3.Connection, daemon_client, monkeypatch  # noqa: F811
) -> None:
    from varyz.resolution import profile_reader

    _shop(conn)
    server = _agent_server(tmp_path, daemon_client)
    try:
        cfg = load_config(tmp_path / "config.toml")
        assert "error" in server.call("schedule_reading", {"when": "tonight"})
        assert "error" in server.call("schedule_reading", {"when": "2020-01-01T21:00"})
        import time as clock

        when = clock.strftime("%Y-%m-%dT%H:%M", clock.localtime(clock.time() + 7200))
        answer = server.call("schedule_reading", {"when": when})
        assert answer["scheduled"] and answer["until"].startswith(when)
        assert profile_reader.deferred_until(cfg, clock.time()) is not None
        stopped = server.call("profile_reading_task", {})
    finally:
        server.close()
    assert stopped["done"] is True and "later" in stopped["stopped"]


def test_its_own_reader_still_has_the_agent_explain_the_setup_step(cfg, config_file,
                                                                     monkeypatch) -> None:
    monkeypatch.setattr(hr, "request", lambda *a, **k: True)
    offer = {"turns_waiting": 40, "major": True, "announce": "x", "setup": "Varyz is now "
             "learning how you work.", "do": "start it", "subagent_prompt": "read"}
    notice = hr.hand_off(cfg, offer, config_file, T0)
    assert notice is not None and "subagent_prompt" not in notice
    assert "start nothing" in notice["do"] and offer["setup"] in notice["do"]
    assert "schedule_reading" in notice["do"]
    from varyz.resolution.briefing import render_background_job

    assert render_background_job(notice).startswith(
        "varyz background job: setup step: tell the user what it does\n")


def test_hooks_do_nothing_inside_varyzs_own_reader(monkeypatch) -> None:
    from varyz import hooks

    assert hooks.HEADLESS_ENV == hr.ENV
    monkeypatch.setenv(hr.ENV, "run-1")
    monkeypatch.setattr(hooks, "_read_event", lambda: {})
    monkeypatch.setattr(hooks, "_load_cfg", lambda config: pytest.fail("briefed the reader"))
    assert hooks.run("UserPromptSubmit", None) == 0


# -- `varyz rules mine` on a sign-in ------------------------------------------------


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return "{}"


def test_rules_mine_on_a_sign_in_ignores_low_and_unknown_allowance() -> None:
    for reading in (_usage("claude", 0, 0), None):
        model = _Model()
        guard = UsageGuard(model, "Claude", Gate(30, 25), lambda reading=reading: reading)
        guard.complete("s", "u")
        assert model.calls == 1


def test_only_a_plan_sign_in_is_guarded(cfg: Any) -> None:
    from varyz.llm.claude import ClaudeProvider

    assert isinstance(guarded(cfg, ClaudeProvider("haiku", oauth=True)), UsageGuard)
    keyed = ClaudeProvider("haiku", api_key="sk-test")
    assert guarded(cfg, keyed) is keyed


# -- the agents' instruction files, for when varyz cannot read itself ---------------


@pytest.mark.parametrize(("client", "config_name", "file_name"), [
    ("claude-code", ".claude.json", ".claude/CLAUDE.md"),
    ("codex", ".codex/config.toml", ".codex/AGENTS.md"),
])
def test_the_agents_instructions_let_them_start_the_reader_and_keep_the_owners_words(
    tmp_path: Path, client: str, config_name: str, file_name: str
) -> None:
    from varyz import install

    client_config = tmp_path / config_name
    instructions = tmp_path / file_name
    instructions.parent.mkdir(parents=True, exist_ok=True)
    owners = "# My rules\n\nUse thin fonts.\n"
    instructions.write_text(owners, encoding="utf-8")

    changed, detail = install._install_instructions(client, client_config, dry_run=False)
    text = instructions.read_text(encoding="utf-8")
    assert changed and str(instructions) in detail
    assert text.startswith(owners) and install.INSTRUCTIONS_START in text
    assert ("varyz-rule-miner" in text) == (client == "claude-code")
    assert instructions.with_suffix(".md.varyz-backup").read_text(encoding="utf-8") == owners
    assert install._install_instructions(client, client_config, dry_run=False) == (False, "")

    # An older block is replaced in place, not added again.
    instructions.write_text(text.replace("You are allowed", "Old words.") + "More of mine.\n",
                            encoding="utf-8")
    assert install._install_instructions(client, client_config, dry_run=False)[0]
    text = instructions.read_text(encoding="utf-8")
    assert text.count(install.INSTRUCTIONS_START) == 1 and "Old words." not in text
    assert "More of mine." in text

    assert install.uninstall_instructions(client, client_config)
    assert instructions.read_text(encoding="utf-8") == owners + "\nMore of mine.\n"
    assert not install.uninstall_instructions(client, client_config)


def test_a_new_instructions_file_holds_only_the_block_and_goes_on_uninstall(
    tmp_path: Path
) -> None:
    from varyz import install

    client_config = tmp_path / ".claude.json"
    assert install._install_instructions("claude-code", client_config, dry_run=True)[0] is False
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    install._install_instructions("claude-code", client_config, dry_run=False)
    created = tmp_path / ".claude" / "CLAUDE.md"
    assert created.read_text(encoding="utf-8") == install.instructions_block("claude-code")
    assert install.uninstall_instructions("claude-code", client_config)
    assert not created.exists()


def test_a_major_reading_varyz_runs_itself_still_tells_the_owner_one_line(
    cfg: Any, config_file: Path, monkeypatch
) -> None:
    from varyz.resolution.briefing import render_background_job, render_markdown
    from varyz.resolution.profile_reader import ANNOUNCEMENT

    monkeypatch.setattr(hr, "request", lambda *a, **k: True)
    routine = {"turns_waiting": 40, "announce": None, "do": "start it",
               "subagent_prompt": "read"}
    assert hr.hand_off(cfg, routine, config_file, T0) is None
    notice = hr.hand_off(cfg, {**routine, "announce": ANNOUNCEMENT}, config_file, T0)
    assert notice is not None and "subagent_prompt" not in notice
    assert ANNOUNCEMENT in notice["do"] and "start nothing" in notice["do"]

    text = render_background_job(notice)
    assert "tell the user once" in text and "subagent's prompt" not in text
    assert "Background job: tell the user once" in render_markdown(
        {"project": "shop", "background_job": notice})
    # When varyz cannot run it, the agent gets the job as it was.
    monkeypatch.setattr(hr, "request", lambda *a, **k: False)
    assert hr.hand_off(cfg, routine, config_file, T0) is routine


def test_connecting_replaces_the_old_reader_agent_but_not_the_owners_own(
    tmp_path: Path
) -> None:
    from varyz import install

    client_config = tmp_path / ".claude.json"
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    old = agents / "varyz-reader.md"
    old.write_text(f"---\nname: varyz-reader\n---\n\n{install.READER_MARKER}\n", encoding="utf-8")
    changed, _ = install._install_reader_agent("claude-code", client_config, dry_run=False)
    new = agents / "varyz-rule-miner.md"
    assert changed and new.is_file() and not old.exists()
    assert "name: varyz-rule-miner" in new.read_text(encoding="utf-8")

    old.write_text("my own reader", encoding="utf-8")
    assert install._install_reader_agent("claude-code", client_config, dry_run=False) == (
        False, "")
    assert install.uninstall_reader_agent("claude-code", client_config)
    assert not new.exists() and old.read_text(encoding="utf-8") == "my own reader"


def test_reader_health_rejects_stale_quota_wait_but_respects_pause(cfg):
    hr.update_status(cfg, lambda s: s.update(
        {"pending": ["full"], "state": "rate_limited", "next_check": T0 + 60}))
    assert hr.health(cfg, now=T0)[0]
    assert not hr.health(cfg, now=T0 + 241)[0]
    from varyz.hooks import pause_path

    pause_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    pause_path(cfg).touch()
    assert hr.health(cfg, now=T0 + 241)[0]


def test_reader_health_tolerates_supervisor_finishing_after_reader_exit(cfg, monkeypatch):
    import varyz.autosync as autosync

    alive = {4242}
    monkeypatch.setattr(autosync, "process_alive", lambda pid: pid in alive)
    hr.update_status(cfg, lambda s: s.update(
        {"state": "running", "pid": 9999, "supervisor_pid": 4242, "started_at": T0}))
    assert hr.health(cfg, now=T0 + 13) == (True, "reader finishing")
    alive.clear()
    assert not hr.health(cfg, now=T0 + 13)[0]


def test_orphaned_queue_is_resumed_without_restarting_stopped_work(cfg, config_file):
    started = []
    hr.update_status(cfg, lambda s: s.update({"pending": ["live"]}))
    assert hr.maybe_resume(cfg, config_file, T0, start=lambda *a: started.append(a))
    assert hr.load_status(cfg)["state"] == "queued"
    assert hr.health(cfg, now=T0)[0]
    assert not hr.health(cfg, now=T0 + 121)[0]
    hr.update_status(cfg, lambda s: s.update({"state": "stopped"}))
    assert not hr.maybe_resume(cfg, config_file, T0 + 200, start=lambda *a: started.append(a))
    assert len(started) == 1


def test_supervisor_launch_failure_is_recorded_without_private_error(cfg, config_file, monkeypatch):
    _work(monkeypatch, (3, 0, 0, 0))
    def fail(*args):
        raise OSError("private-token-and-path")

    status = hr.supervise(cfg, config_file, kind="full", runners={"codex": RUNNER},
                          read_usage=lambda _: _usage("codex", 80, 80), launch=fail,
                          clock=lambda: T0)
    assert status["state"] == "failed" and status["pid"] is None
    assert "private-token" not in json.dumps(status)
    assert not hr.health(cfg, now=T0)[0]


def test_stale_low_usage_does_not_block_a_new_reading(cfg, config_file, monkeypatch):
    hr.update_status(cfg, lambda s: s.update({"usage": {
        "codex": _usage("codex", 80, 1).as_dict()}}))
    clock = _Clock()
    _work(monkeypatch, (3, 0, 0, 0), (0, 0, 3, 1))
    status = hr.supervise(cfg, config_file, kind="live", runners={"codex": RUNNER},
        read_usage=lambda _: None, clock=clock, sleep=clock.sleep,
        launch=lambda *a: _Process(cfg, clock=clock, ends_at=T0 + 10))
    assert status["state"] == "idle" and not status["pending"]

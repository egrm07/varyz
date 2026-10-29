"""Offline contracts for the usage watch: hand-offs before an agent runs out."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import UTC, datetime

import pytest

from varyz import handoff, usage

NOW = 1_800_000_000.0
LATER = NOW + 3600


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(handoff, "capture_in_background",
                        lambda *args, **kwargs: calls.append(args))
    return calls


def claude_event(used, *, resets=LATER, session="chat", cwd="C:/projects/varyz", week=10):
    return {"session_id": session, "transcript_path": f"C:/t/{session}.jsonl", "cwd": cwd,
            "rate_limits": {"five_hour": {"used_percentage": used, "resets_at": resets},
                            "seven_day": {"used_percentage": week, "resets_at": LATER + 9e5}}}


def test_reading_takes_the_limit_nearest_its_marks_that_has_not_reset():
    hours, week = "five_hour", "week"
    assert usage.reading([(40, LATER, hours), (97, LATER, hours)], now=NOW) == {
        "left": 3.0, "resets_at": LATER, "window": hours}
    # 8% of the week is further from its marks than 40% of the five hours, and 5%
    # of the five hours is nearer than 8% of the week.
    assert usage.reading([(60, LATER, hours), (92, LATER, week)], now=NOW)["window"] == week
    assert usage.reading([(95, LATER, hours), (92, LATER, week)], now=NOW)["window"] == hours
    # A limit that reset since it was reported is not counted.
    assert usage.reading([(99, NOW - 1, hours), (20, LATER, week)], now=NOW)["left"] == 80.0
    assert usage.reading([(99, NOW - 1, hours)], now=NOW) is None
    assert usage.reading([(120, None, hours)], now=NOW)["left"] == 0.0
    # Reset times in milliseconds or as ISO dates, and junk that is not a number.
    iso = datetime.fromtimestamp(LATER, UTC).isoformat().replace("+00:00", "Z")
    assert usage.claude_windows({"five_hour": {"used_percentage": 96,
                                               "resets_at": LATER * 1000}}) == [
        (96.0, LATER, hours)]
    assert usage.claude_windows({"seven_day": {"used_percentage": 96,
                                               "resets_at": iso}}) == [(96.0, LATER, week)]
    assert usage.claude_windows({"five_hour": {"used_percentage": True},
                                 "seven_day": None}) == []
    assert usage.claude_windows("bad") == []


def test_codex_reads_only_its_plan_limit_and_reset_in_seconds():
    limits = {"limit_id": "codex", "primary": {"used_percent": 96, "resets_in_seconds": 60},
              "secondary": {"used_percent": 50, "resets_at": LATER}}
    assert usage.codex_windows(limits, at=NOW) == [(96.0, NOW + 60, "five_hour"),
                                                   (50.0, LATER, "week")]
    # A window's stated length wins over its place.
    limits["primary"]["window_minutes"] = 10080
    assert usage.codex_windows(limits, at=NOW)[0] == (96.0, NOW + 60, "week")
    assert usage.codex_windows({"limit_id": "premium", "primary": None,
                                "secondary": None}) == []
    assert usage.codex_windows({"limit_id": "premium",
                                "primary": {"used_percent": 99}}) == []


def test_statusline_saves_a_hand_off_at_five_and_again_at_three(tmp_path, saved):
    data = tmp_path / "data"
    config = tmp_path / "config.toml"
    assert usage.statusline(data, config, claude_event(80), now=NOW) == ""
    assert usage.latest(data, "claude_code", now=NOW)["left"] == 20.0
    assert usage.statusline(data, config, claude_event(92), now=NOW) == "Varyz: 8% usage left"
    assert saved == []
    assert usage.statusline(data, config, claude_event(95.5), now=NOW) == (
        "Varyz: 4% usage left, hand-off saved")
    assert len(saved) == 1
    project, conversation, transcript = saved[0][1:4]
    assert (project, conversation, transcript) == ("varyz", "chat", "C:/t/chat.jsonl")
    usage.statusline(data, config, claude_event(96), now=NOW)  # still 5% level
    assert len(saved) == 1
    usage.statusline(data, config, claude_event(97.5), now=NOW)
    assert len(saved) == 2
    usage.statusline(data, config, claude_event(99), now=NOW)
    assert len(saved) == 2
    # A new window starts the count again.
    usage.statusline(data, config, claude_event(96, resets=LATER + 18000), now=NOW)
    assert len(saved) == 3
    signal = handoff._signals(data)["varyz\nchat"]
    assert signal["kind"] == "usage_low" and signal["left"] == 4.0


def test_eight_percent_of_the_week_is_not_nearly_out(tmp_path, saved):
    """The 2026-10-29 HQ session: 40% of the five hours and 8% of the week left.
    Varyz said "Your plan's usage is nearly out (8% left until 12:00 UTC)", with
    the reset two days away, and the agent cut its task short."""
    data, config = tmp_path / "data", tmp_path / "c.toml"
    event = claude_event(60, week=92)
    assert usage.statusline(data, config, event, now=NOW) == ""
    assert usage.note(data, event, now=NOW) == ""
    assert saved == [] and handoff._signals(data) == {}
    assert usage.latest(data, "claude_code", now=NOW)["window"] == "week"
    # Near the end of the week it notes, then saves hand-offs, naming the limit.
    assert usage.statusline(data, config, claude_event(60, week=97), now=NOW) == (
        "Varyz: 3% weekly usage left")
    assert usage.note(data, event, now=NOW) == usage.NOTE
    assert saved == []
    assert usage.statusline(data, config, claude_event(60, week=98.2), now=NOW) == (
        "Varyz: 2% weekly usage left, hand-off saved")
    assert len(saved) == 1
    usage.statusline(data, config, claude_event(60, week=99.2), now=NOW)
    assert len(saved) == 2


def test_a_first_reading_below_three_saves_once(tmp_path, saved):
    data = tmp_path / "data"
    usage.statusline(data, None, claude_event(100), now=NOW)  # no config: signal only
    usage.statusline(data, tmp_path / "c.toml", claude_event(100), now=NOW)
    assert saved == [] and handoff._signals(data)["varyz\nchat"]["left"] == 0.0


def test_each_chat_is_saved_on_its_own(tmp_path, saved):
    data = tmp_path / "data"
    for session in ("one", "two"):
        usage.statusline(data, tmp_path / "c.toml", claude_event(96, session=session), now=NOW)
    assert [call[2] for call in saved] == ["one", "two"]


def test_statusline_without_folder_or_limits_never_fails(tmp_path, saved):
    data = tmp_path / "data"
    assert usage.statusline(data, None, {}, now=NOW) == ""
    assert usage.statusline(data, None, {"rate_limits": None}, now=NOW) == ""
    event = claude_event(97, cwd="")
    event["workspace"] = "not a dict"
    assert usage.statusline(data, tmp_path / "c.toml", event, now=NOW) == (
        "Varyz: 3% usage left")  # nothing saved, so it does not say so
    assert saved == []
    event = claude_event(97, cwd="")
    event["workspace"] = {"current_dir": "C:/projects/site"}
    usage.statusline(data, tmp_path / "c.toml", event, now=NOW)
    assert saved[0][1] == "site"


def test_statusline_command_prints_nothing_on_bad_input(tmp_path, monkeypatch, capsys):
    for raw in (b"not json", b"[]", b"", "\u00e9".encode("latin-1")):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
        assert usage.main(["statusline", "--data-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_a_stale_or_reset_reading_is_not_used(tmp_path):
    data = tmp_path / "data"
    usage.record(data, "claude_code", {"left": 4.0, "resets_at": NOW - 1})
    assert usage.latest(data, "claude_code", now=NOW) is None
    (data / "runtime" / "usage" / "claude_code.json").write_text("{bad", encoding="utf-8")
    assert usage.latest(data, "claude_code", now=NOW) is None


def test_prompt_note_only_when_nearly_out(tmp_path):
    data = tmp_path / "data"
    event = {"transcript_path": "C:/t/chat.jsonl"}
    assert usage.note(data, event, now=NOW) == ""
    usage.record(data, "claude_code", {"left": 30.0, "resets_at": LATER})
    assert usage.note(data, event, now=NOW) == ""
    usage.record(data, "claude_code", {"left": 7.0, "resets_at": LATER})
    text = usage.note(data, event, now=NOW)
    assert text == usage.NOTE and "record_progress" in text
    assert usage.note(data, {}, now=NOW) == ""  # no transcript: not an agent's chat
    usage.record(data, "claude_code", {"left": 7.0, "resets_at": LATER, "window": "week"})
    assert usage.note(data, event, now=NOW) == ""


def test_the_note_never_invites_cutting_work_short():
    """No figures or reset time to ration by, and an explicit 'keep going'."""
    assert "%" not in usage.NOTE and "UTC" not in usage.NOTE
    assert "nearly out" not in usage.NOTE and "may stop" not in usage.NOTE
    assert "keep the full scope" in usage.NOTE and "do not shorten, defer or stop" in usage.NOTE
    assert "do not tell the owner usage is low" in usage.NOTE
    assert "\u2014" not in usage.NOTE and len(usage.NOTE) < 450


def rollout(path, rows, session="codex-chat", cwd="C:/projects/varyz"):
    lines = [{"type": "session_meta", "payload": {"id": session, "cwd": cwd}}, *rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def counts(used, limit_id="codex", resets=LATER):
    primary = {"used_percent": used, "window_minutes": 300, "resets_at": resets}
    return {"type": "event_msg", "timestamp": "2026-10-26T20:11:20.435Z",
            "payload": {"type": "token_count", "rate_limits": {
                "limit_id": limit_id, "primary": primary if limit_id == "codex" else None,
                "secondary": {"used_percent": 40, "resets_at": LATER + 9e5}
                if limit_id == "codex" else None}}}


def test_codex_reading_skips_premium_lines_and_a_cut_first_line(tmp_path, monkeypatch):
    path = tmp_path / "rollout-1.jsonl"
    rollout(path, [counts(50), {"type": "event_msg", "payload": {"type": "agent_message",
                                                                 "message": "x" * 400}},
                   counts(96), counts(0, "premium"), {"type": "response_item"}])
    assert usage.codex_reading(path, now=NOW)["left"] == 4.0
    lines = path.read_bytes().split(b"\n")
    # Start mid-way through the long message line.
    monkeypatch.setattr(usage, "TAIL_BYTES", sum(len(x) + 1 for x in lines[-4:]) + 100)
    assert usage.codex_reading(path, now=NOW)["left"] == 4.0
    with path.open("ab") as handle:
        handle.write(b'{"type":"event_msg","payload":{"rate_limits":')  # being written
    assert usage.codex_reading(path, now=NOW)["left"] == 4.0
    assert usage.codex_reading(tmp_path / "missing.jsonl", now=NOW) is None


def test_codex_check_saves_running_chats_once(tmp_path, saved):
    data = tmp_path / "data"
    now = time.time()
    day = datetime.fromtimestamp(now, UTC).astimezone()
    folder = tmp_path / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    rollout(folder / "rollout-a.jsonl", [counts(97, resets=now + 3600)])
    rollout(folder / "rollout-b.jsonl", [counts(30, resets=now + 3600)], session="other")
    rollout(folder / "rollout-old.jsonl", [counts(99, resets=now + 3600)], session="old")
    os.utime(folder / "rollout-old.jsonl", (now - 7200, now - 7200))
    found = usage.check_codex(data, tmp_path / "c.toml", root=tmp_path / "sessions", now=now)
    assert sorted((f["session_id"], f["saved"]) for f in found) == [("codex-chat", 3),
                                                                    ("other", None)]
    assert [call[2] for call in saved] == ["codex-chat"]
    assert usage.check_codex(data, tmp_path / "c.toml", root=tmp_path / "sessions",
                             now=now)[0]["saved"] is None
    assert len(saved) == 1
    assert usage.latest(data, "codex", now=now) is not None


def test_codex_reset_time_that_drifts_is_one_window(tmp_path, saved):
    data = tmp_path / "data"
    value = {"left": 4.0, "resets_at": LATER}
    assert usage.low(data, None, agent="codex", session_id="s", transcript="t",
                     cwd="C:/p/varyz", value=value) == 5
    value = {"left": 4.0, "resets_at": LATER + 1}
    assert usage.low(data, None, agent="codex", session_id="s", transcript="t",
                     cwd="C:/p/varyz", value=value) is None


def test_prompt_note_reads_a_codex_chat_itself(tmp_path):
    path = tmp_path / "rollout-1.jsonl"
    rollout(path, [counts(95)])
    assert usage.note(tmp_path, {"transcript_path": str(path)}, now=NOW) == usage.NOTE


def test_statusline_keeps_the_owners_own_and_restores_it(tmp_path):
    saved = tmp_path / "hooks" / "statusline-previous.json"
    config, data = tmp_path / "config.toml", tmp_path / "data"
    settings: dict = {}
    assert usage.merge_statusline(settings, config, data, saved, dry_run=True)
    assert not saved.exists()
    settings = {}
    assert usage.merge_statusline(settings, config, data, saved)
    assert usage.is_ours(settings["statusLine"])
    assert not usage.merge_statusline(settings, config, data, saved)  # already there
    assert usage.remove_statusline(settings, saved) and "statusLine" not in settings
    own = {"type": "command", "command": "ccstatus --theme 'dark'", "padding": 1}
    settings = {"statusLine": dict(own)}
    assert usage.merge_statusline(settings, config, data, saved)
    line = settings["statusLine"]
    assert "ccstatus --theme 'dark'" in line["command"] and line["padding"] == 1
    assert usage.STATUSLINE_MARK in line["command"]
    assert not usage.merge_statusline(settings, config, data, saved)  # stable on reinstall
    assert usage.remove_statusline(settings, saved)
    assert settings["statusLine"] == own and not saved.exists()
    # Its copy lost, a combined line is kept as is rather than dropping the owner's.
    settings = {"statusLine": dict(own)}
    usage.merge_statusline(settings, config, data, saved)
    saved.unlink()
    before = dict(settings["statusLine"])
    assert not usage.merge_statusline(settings, config, data, saved)
    assert settings["statusLine"] == before
    settings = {"statusLine": "not a dict"}
    assert not usage.merge_statusline(settings, config, data, saved)


@pytest.mark.skipif(os.name != "nt", reason="Git Bash runs Claude Code's status line here")
def test_statusline_command_runs_in_bash_with_and_without_the_owners_own(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    git = shutil.which("git")
    bash = Path(git).resolve().parent.parent / "bin" / "bash.exe" if git else None
    if bash is None or not bash.is_file():
        pytest.skip("Git Bash is not installed")
    data = tmp_path / "data"
    event = json.dumps(claude_event(97, resets=time.time() + 3600, cwd=""))
    for previous in (None, {"command": "printf 'mine:'; head -c 13"}, {"command": "true"}):
        command = usage.statusline_command(tmp_path / "config.toml", data, previous)
        done = subprocess.run([bash, "-c", command], input=event.encode(), capture_output=True,
                              timeout=60, check=False)
        out = done.stdout.decode().replace("\r\n", "\n")
        assert done.returncode == 0
        expected = "Varyz: 3% usage left\n"  # no folder, so nothing could be saved
        mine = previous is not None and "mine" in previous["command"]
        assert out == ('mine:{"session_id"\n' + expected if mine else expected)
    assert usage.latest(data, "claude_code")["left"] == 3.0


# -- the limit itself: keep working until the client cuts the turn off ---------------

GRACE = ("[Usage limit reached; a short grace allowance remains, then this turn is cut off "
         "without warning. Judge what's left: ... Either way, end by telling the user their "
         "usage limit was reached, what's done and what's left.]")
RESET = ("[Earlier usage-limit notes no longer apply. Continue working normally. Don't "
         "mention this to the user.]")


def _turn(tmp_path, *lines):
    """A Claude Code transcript: the overnight task, a step, then the given lines."""
    base = [
        {"type": "user", "message": {"role": "user", "content": "Build the HQ dashboard."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}]}},
    ]
    path = tmp_path / "chat.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in [*base, *lines]),
                    encoding="utf-8")
    return str(path)


def _meta(text):
    return {"type": "user", "isMeta": True, "message": {"role": "user", "content": text}}


def _said(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def _prompt(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}}


def test_an_agent_that_stops_at_the_limit_is_sent_back_once(tmp_path, monkeypatch):
    """The 2026-10-29 overnight run: told to wrap up, it wrote its report and
    ended the turn, so the desktop app had no cut-off to resume after the
    reset and the task waited until the owner typed 'continue' at 10:12."""
    from varyz import hooks
    from varyz.config import Config

    monkeypatch.setattr(hooks, "nudge_live_reading", lambda cfg: None)
    cfg = Config(base_dir=tmp_path)
    event = {"session_id": "chat", "transcript_path": _turn(
        tmp_path, _meta(GRACE), _said("Your usage limit was reached, so I stopped."))}
    sent = hooks.stop(cfg, event)
    assert sent == {"decision": "block", "reason": usage.KEEP_WORKING}
    assert len(usage.KEEP_WORKING) < 300 and "\u2014" not in usage.KEEP_WORKING
    assert hooks.stop(cfg, {**event, "stop_hook_active": True}) is None
    # Every session is told up front.
    assert usage.LIMIT_POLICY in hooks.SESSION_START_CONTEXT
    assert "only when the client cuts its turn off" in hooks.SESSION_START_CONTEXT


@pytest.mark.parametrize(("after", "standing"), [
    ([], True),
    ([_said("Saved the hand-off."), _prompt("<task-notification>done</task-notification>")],
     True),
    ([_meta(RESET)], False),
    ([_prompt("You were stopped by a usage limit, which has now reset. Continue.")], False),
    ([_prompt("continue your work, no need for handover")], False),
])
def test_the_limit_note_stands_until_a_reset_or_a_new_prompt(tmp_path, after, standing):
    assert usage.grace(_turn(tmp_path, _meta(GRACE), *after)) is standing


def test_no_note_or_only_the_owner_quoting_it_is_no_limit(tmp_path):
    assert not usage.grace(_turn(tmp_path, _said("Done.")))
    assert not usage.grace(_turn(tmp_path, _prompt("It said " + GRACE + " and stopped.")))
    assert not usage.grace(None) and not usage.grace(str(tmp_path / "missing.jsonl"))

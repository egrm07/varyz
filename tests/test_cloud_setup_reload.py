"""Source refresh must not disconnect an approved browser setup mid-sync."""

from varyz.cloud.computer import ComputerConnection
from varyz.resolution.daemon import CODE_SETTLE_POLLS, watch_code


def test_reload_waits_for_setup_and_final_browser_poll(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr("varyz.cloud.computer.time.monotonic", lambda: clock[0])
    bridge = ComputerConnection(tmp_path / "config.toml")
    bridge.ticket = "browser-capability"
    bridge.expires = 101.0
    bridge.state = "syncing"
    stops = []
    polls = [0]

    def advance(_seconds):
        polls[0] += 1
        if polls[0] == CODE_SETTLE_POLLS + 1:
            # Even an expired browser ticket must not terminate an ongoing upload.
            clock[0] = 102.0
        if polls[0] == CODE_SETTLE_POLLS + 2:
            bridge.state = "complete"
            bridge.expires = 103.0
        if polls[0] == CODE_SETTLE_POLLS + 3:
            clock[0] = 104.0

    versions = iter(["old"] + ["new"] * 20)
    assert watch_code(
        lambda: stops.append(polls[0]), lambda: next(versions),
        sleep=advance, can_stop=lambda: not bridge.active(),
    )
    assert stops == [CODE_SETTLE_POLLS + 3]


def test_detection_keeps_helper_alive_only_until_ticket_expires(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr("varyz.cloud.computer.time.monotonic", lambda: clock[0])
    bridge = ComputerConnection(tmp_path / "config.toml")
    assert not bridge.active()
    bridge.detect()
    assert bridge.active()
    clock[0] = bridge.expires
    assert not bridge.active()


def test_restart_preserves_preview_without_storing_capabilities(tmp_path, monkeypatch):
    import json
    import time

    from varyz.cloud import guided
    from varyz.cloud.computer import _digest

    config = tmp_path / "config.toml"
    bridge = ComputerConnection(config)
    ticket = bridge.detect()["ticket"]
    bridge.state, bridge.account_id = "preview_ready", "owner"
    bridge.preview_data = {"projects": ["alpha"], "history_projects": ["beta"]}
    bridge.approval = "private-approval"
    bridge.approval_hash = _digest(bridge.approval)
    bridge._save()
    saved = bridge.session_path.read_text(encoding="utf-8")
    assert ticket not in saved and bridge.approval not in saved
    restored = ComputerConnection(config)
    assert restored.status(ticket)["state"] == "preview_ready"
    monkeypatch.setattr(guided.GuidedSetup, "choices", lambda _: {})
    choices = restored.setup(ticket, "choices", {})
    assert choices["selected"] == ["alpha"]
    assert choices["history_selected"] == ["beta"]
    # A crash during upload never replays it, even if the browser retries POST.
    bridge.state = "syncing"
    bridge._save()
    restored = ComputerConnection(config)
    assert restored.status(ticket)["state"] == "interrupted"
    assert restored.setup(ticket, "finish", {
        "approve": True, "approval": bridge.approval,
    })["state"] == "interrupted"
    expired = json.loads(saved)
    expired["expires_at"] = time.time() - 1
    bridge.session_path.write_text(json.dumps(expired), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="expired"):
        ComputerConnection(config).status(ticket)


def test_authenticated_poll_renews_idle_expiry(tmp_path, monkeypatch):
    from varyz.cloud.computer import SESSION_SECONDS

    clock = [100.0]
    monkeypatch.setattr("varyz.cloud.computer.time.monotonic", lambda: clock[0])
    bridge = ComputerConnection(tmp_path / "config.toml")
    ticket = bridge.detect()["ticket"]
    clock[0] += SESSION_SECONDS - 1
    bridge.status(ticket)
    clock[0] += 60
    assert bridge.status(ticket)["state"] == "ready"

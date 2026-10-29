"""Updating Varyz under open agents: the agent connection restarts by itself."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from varyz import mcp_server, update_worker, windows_hook

STAND_IN = r'''
import json, os, sys
RESTART = '{"jsonrpc":"2.0","method":"notifications/varyz/restart"}'
for line in sys.stdin:
    if line.strip() == "restart":
        sys.stdout.write(RESTART + "\n")
    else:
        reply = {"echo": line.strip(), "pid": os.getpid(),
                 "relay": os.environ.get("VARYZ_MCP_RELAY")}
        sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
'''


def _compiler() -> Path | None:
    windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    for framework in ("Framework64", "Framework"):
        path = windows / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
        if path.is_file():
            return path
    return None


@pytest.mark.skipif(os.name != "nt" or _compiler() is None, reason="Windows launcher")
def test_the_windows_launcher_restarts_its_connection_between_requests(tmp_path: Path) -> None:
    data = tmp_path / "data"
    folder = data / "hooks"
    config = tmp_path / "home" / "config.toml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    # `-m varyz` runs from the config's folder, so a stand-in package answers.
    (config.parent / "varyz").mkdir()
    (config.parent / "varyz" / "__init__.py").write_text("", encoding="utf-8")
    (config.parent / "varyz" / "__main__.py").write_text(STAND_IN, encoding="utf-8")
    launcher = windows_hook.write_launchers(folder, config, {"mcp": "mcp"})["mcp"]
    lines = Path(str(launcher) + ".launch").read_text(encoding="utf-8").splitlines()
    assert lines[1:] == ["mcp", str(config), str(update_worker.sentinel_path(data))]

    agent = subprocess.Popen([str(launcher)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    assert agent.stdin is not None and agent.stdout is not None

    def ask(text: str) -> None:
        assert agent.stdin is not None
        agent.stdin.write(text.encode() + b"\n")
        agent.stdin.flush()

    def reply() -> dict[str, Any]:
        assert agent.stdout is not None
        return json.loads(agent.stdout.readline())

    try:
        ask("one")
        first = reply()
        assert first["echo"] == "one" and first["relay"] == "1"
        update_worker.mark(data, "job")
        ask("restart")
        time.sleep(1.0)  # what the old connection already had, it would still answer
        ask("two")
        released = threading.Timer(2.0, update_worker.clear, (data,))
        released.start()
        began = time.monotonic()
        second = reply()  # the restart line never reaches the agent
        assert second["echo"] == "two"
        assert second["pid"] != first["pid"]
        assert time.monotonic() - began >= 1.5  # held while the update ran
        ask("three")
        assert reply()["pid"] == second["pid"]
        agent.stdin.close()
        assert agent.wait(timeout=20) == 0
    finally:
        if agent.poll() is None:
            agent.kill()


class _Due:
    def __init__(self) -> None:
        self.due_now = False

    def due(self) -> bool:
        return self.due_now

    def settle(self) -> bool:
        return True


def test_a_relayed_connection_asks_its_launcher_to_restart_and_answers_what_it_has() -> None:
    updates = _Due()
    stdin = io.StringIO("first\nsecond\n")
    stdout = io.StringIO()
    seen = []
    for line in mcp_server._relayed(stdin, stdout, updates):  # type: ignore[arg-type]
        seen.append(line)
        updates.due_now = True
    assert seen == ["first\n", "second\n"]
    assert stdout.getvalue() == mcp_server.RESTART_LINE + "\n"


def test_elsewhere_a_connection_restarts_in_place_with_what_it_has_not_answered(
    tmp_path: Path,
) -> None:
    updates = _Due()
    read, write = os.pipe()
    os.write(write, b"three\n")
    os.close(write)
    handed: list[bytes] = []

    def restart(rest: bytes) -> None:
        handed.append(rest)
        raise SystemExit

    lines = mcp_server._in_place(read, b"one\ntwo\n", updates, restart)  # type: ignore[arg-type]
    assert next(lines) == "one"
    updates.due_now = True
    with pytest.raises(SystemExit):
        next(lines)
    assert handed == [b"two\n"]
    os.close(read)


def test_a_restart_in_place_hands_over_the_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append((path, argv, env)),
                        raising=False)

    class Server:
        closed = False

        def close(self) -> None:
            Server.closed = True

    config = tmp_path / "config.toml"
    mcp_server._restart(Server(), config, b"two\n")  # type: ignore[arg-type]
    path, argv, env = calls[0]
    assert Server.closed and path == sys.executable
    assert argv[-4:] == ["varyz", "mcp", "--config", str(config)]
    assert base64.b64decode(env[mcp_server.PENDING_ENV]) == b"two\n"


def test_updates_are_due_while_installing_and_once_a_new_varyz_is_in(tmp_path: Path) -> None:
    updates = mcp_server._Updates(tmp_path)
    assert not updates.due()
    update_worker.mark(tmp_path, "job")
    assert updates.due() and not updates.replaced()
    update_worker.clear(tmp_path)
    update_worker.record_installed(tmp_path, "2026-10-26T10:00:00Z")
    assert updates.due() and updates.settle()


def test_an_old_update_marker_stops_counting(tmp_path: Path) -> None:
    update_worker.mark(tmp_path, "job")
    old = time.time() - update_worker.STALE_AFTER - 5
    os.utime(update_worker.sentinel_path(tmp_path), (old, old))
    assert not update_worker.updating(tmp_path)


def test_the_worker_runs_from_a_copy_outside_the_environment(tmp_path: Path) -> None:
    python = shutil.which("python") or sys.executable
    job = {"id": "job", "data_dir": str(tmp_path)}
    command = update_worker.prepare(job, tmp_path / "worker", Path(python))
    assert command[:3] == [python, "-I", "-B"]
    assert Path(command[3]).parent == tmp_path / "worker"
    assert (tmp_path / "worker" / "update_worker.py").is_file()
    assert (tmp_path / "worker" / "background.py").is_file()
    assert json.loads(Path(command[4]).read_text(encoding="utf-8"))["id"] == "job"


def _listing(tmp_path: Path, sidecar_lines: int) -> tuple[dict[str, Any], list[Any]]:
    from varyz.background import Found

    environment = tmp_path / "env"
    hooks = tmp_path / "data" / "hooks"
    hooks.mkdir(parents=True)
    launcher = hooks / "varyz-mcp-0123456789ab.exe"
    lines = ["python", "mcp", "config.toml", "UPDATING"][:sidecar_lines]
    Path(str(launcher) + ".launch").write_text("\n".join(lines) + "\n", encoding="utf-8")
    python = environment / "Scripts" / "python.exe"
    processes = [
        Found(10, 1, f"{launcher} {launcher}"),
        Found(11, 10, f"{python} {python} -m varyz mcp --config config.toml"),
        Found(12, 1, f"{python} {python} -m varyz daemon run"),
    ]
    job = {"data_dir": str(tmp_path / "data"), "environment": str(environment),
           "launchers": []}
    return job, processes


def test_the_worker_waits_for_agent_connections_and_stops_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from varyz import background

    job, processes = _listing(tmp_path, 4)
    listings = [processes, [processes[0]]]
    stopped: list[list[int]] = []
    monkeypatch.setattr(background, "_listing", lambda: listings.pop(0))
    monkeypatch.setattr(background, "stop",
                        lambda found: stopped.append([item.pid for item in found]) or [])
    monkeypatch.setattr(update_worker, "POLL", 0)
    assert update_worker.drain(job, lambda _line: None) is None
    assert stopped == [[12]]  # the service; the agent connection ended by itself


def test_the_worker_changes_nothing_while_an_older_launcher_is_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from varyz import background

    job, processes = _listing(tmp_path, 3)
    monkeypatch.setattr(background, "_listing", lambda: processes)
    monkeypatch.setattr(background, "stop", lambda _found: pytest.fail("stopped something"))
    outcome = update_worker.drain(job, lambda _line: None)
    assert outcome == {"status": "waiting", "detail": update_worker.OLD_SESSIONS}


def test_the_copied_worker_runs_outside_varyz_and_records_a_broken_install(
    tmp_path: Path,
) -> None:
    """The copy runs without Varyz importable; a new Varyz that cannot even
    finish up is still recorded, and the agents are released."""
    data = tmp_path / "data"
    python = update_worker.base_python(Path(sys.prefix))
    if python is None:
        pytest.skip("needs the Python this environment was made from")
    marker = tmp_path / "installed.txt"
    job = {
        "id": "job", "data_dir": str(data), "config": str(tmp_path / "config.toml"),
        "environment": str(tmp_path / "no-env"), "launchers": [],
        # A Python without Varyz: `-m varyz.update --finish` fails there.
        "python": str(python), "what": "a stand-in", "brings": "Varyz 0.2.0",
        "command": [str(python), "-c",
                    f"open({str(marker)!r}, 'w').write('done')"],
        "found": {"target": "0.2.0"}, "backup": None,
    }
    data.mkdir()
    command = update_worker.prepare(job, data / "autosync" / update_worker.FOLDER, python)
    command[0] = str(python).replace("pythonw.exe", "python.exe")
    update_worker.mark(data, "job")
    done = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    assert marker.read_text(encoding="utf-8") == "done"
    assert not update_worker.updating(data)
    state = json.loads((data / "autosync" / "update.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed" and state["trigger"] == update_worker.TRIGGER

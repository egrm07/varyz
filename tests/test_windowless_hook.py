"""Real Windows launcher compilation and binary stdin/stdout round trip."""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from varyz.windows_hook import write_launchers


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI subsystem")
def test_gui_launcher_preserves_json_pipes(tmp_path: Path):
    config = tmp_path / "archive with spaces.toml"
    config.write_text("", encoding="utf-8")
    package = tmp_path / "modules" / "varyz"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "hooks.py").write_text(
        "import sys\n"
        "payload = sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(payload)\n"
        "sys.stderr.buffer.write(b'diagnostic')\n"
        "assert sys.argv[-1].endswith('archive with spaces.toml')\n",
        encoding="utf-8",
    )
    launchers = write_launchers(tmp_path / "hooks", config, {"event": "session-start"})
    launcher = launchers["event"]
    binary = launcher.read_bytes()
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    assert struct.unpack_from("<H", binary, pe + 24 + 68)[0] == 2  # Windows GUI
    payload = '{"prompt":"Grüße 😀"}\n'.encode()
    result = subprocess.run(
        [str(launcher)], input=payload, capture_output=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(package.parent)},
        # Deliberately no CREATE_NO_WINDOW: the entry point itself must be GUI.
    )
    assert result.returncode == 0
    assert result.stdout == payload
    assert result.stderr == b"diagnostic"
    sidecar = Path(str(launcher) + ".launch")
    assert sidecar.read_text().splitlines()[0] == os.path.abspath(sys.executable)
    sidecar.unlink()
    result = subprocess.run([str(launcher)], input=payload, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stdout and not result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI subsystem")
def test_mcp_gui_launcher_preserves_protocol_and_exit_status(tmp_path: Path):
    from varyz.install import server_entry

    config = tmp_path / "archive with spaces.toml"
    config.write_text("", encoding="utf-8")
    package = tmp_path / "modules" / "varyz"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        "assert sys.argv[1:3] == ['mcp', '--config']\n"
        "assert sys.argv[3].endswith('archive with spaces.toml')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        "sys.stderr.buffer.write(b'diagnostic')\n"
        "raise SystemExit(7)\n", encoding="utf-8",
    )
    entry = server_entry(config)
    launcher = Path(entry["command"])
    binary = launcher.read_bytes()
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    assert struct.unpack_from("<H", binary, pe + 24 + 68)[0] == 2
    payload = '{"jsonrpc":"2.0","id":1,"method":"ping","text":"Grüße 😀"}\n'.encode()
    result = subprocess.run(
        [str(launcher), *entry["args"]], input=payload, capture_output=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(package.parent)},
    )
    assert result.returncode == 7
    assert result.stdout == payload
    assert result.stderr == b"diagnostic"


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI subsystem")
def test_mcp_gui_launcher_answers_while_stdin_stays_open(tmp_path: Path):
    """An MCP client keeps stdin open, so each request must pass through unbuffered."""
    import threading

    from varyz.install import server_entry

    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    package = tmp_path / "modules" / "varyz"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        "for line in sys.stdin.buffer:\n"
        "    sys.stdout.buffer.write(line)\n"
        "    sys.stdout.buffer.flush()\n", encoding="utf-8",
    )
    entry = server_entry(config)
    child = subprocess.Popen(
        [entry["command"], *entry["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(package.parent)},
    )
    try:
        assert child.stdin is not None and child.stdout is not None
        stdout = child.stdout
        for request_id in (1, 2):
            line = f'{{"jsonrpc":"2.0","id":{request_id},"method":"ping"}}\n'.encode()
            child.stdin.write(line)
            child.stdin.flush()
            answer: list[bytes] = []
            reader = threading.Thread(
                target=lambda into=answer: into.append(stdout.readline()), daemon=True,
            )
            reader.start()
            reader.join(10)
            assert answer == [line]
    finally:
        child.kill()
        child.wait(10)


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI subsystem")
def test_mcp_gui_dry_run_does_not_write_launchers(tmp_path: Path):
    from varyz.install import server_entry

    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    entry = server_entry(config, dry_run=True)
    assert Path(entry["command"]).name.startswith("varyz-mcp-")
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI subsystem")
@pytest.mark.parametrize("runtime", [sys.executable, os.environ.get("VARYZ_TEST_RUNTIME")])
def test_watchdog_gui_launcher_uses_hidden_child_and_preserves_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str | None,
):
    if runtime is None:
        pytest.skip("No alternate installed runtime configured")
    monkeypatch.setattr(sys, "executable", runtime)
    config = tmp_path / "archive with spaces.toml"
    config.write_text("", encoding="utf-8")
    package = tmp_path / "modules" / "varyz" / "resolution"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "availability.py").write_text(
        "import ctypes, sys\n"
        "assert not ctypes.windll.kernel32.GetConsoleWindow()\n"
        "assert sys.argv[1:] == ['--config', " + repr(str(config)) + ", '--watch']\n"
        "sys.stdout.write('diagnostic' * 100000)\n"
        "sys.stderr.write('diagnostic' * 100000)\n"
        "raise SystemExit(7)\n", encoding="utf-8",
    )
    launcher = write_launchers(
        tmp_path / "hooks", config, {"watchdog": "watchdog"}
    )["watchdog"]
    binary = launcher.read_bytes()
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    assert struct.unpack_from("<H", binary, pe + 24 + 68)[0] == 2
    result = subprocess.run(
        [str(launcher)], capture_output=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(package.parent.parent)},
    )
    assert result.returncode == 7
    assert result.stdout == result.stderr == b""
    assert Path(str(launcher) + ".status").read_text() == "child_exit:7"
    sidecar = Path(str(launcher) + ".launch")
    sidecar.write_text(
        str(tmp_path / "missing-python.exe") + "\nwatchdog\n" + str(config) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run([str(launcher)], capture_output=True, timeout=15)
    assert result.returncode == 1
    assert Path(str(launcher) + ".status").read_text() == "failed:missing_launch_file"

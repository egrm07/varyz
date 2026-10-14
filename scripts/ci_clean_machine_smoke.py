"""Clean-machine first run -> daemon -> connected MCP resolve smoke.

Run after ``uv sync --frozen``. The script creates an isolated home and archive,
uses only the installed ``varyz`` console entry point, and launches MCP from the
exact client entry written by ``varyz install``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from varyz.configfile import set_resolution_port

MARKER = "QUASAR-7319"
KNOWLEDGE_QUESTION = "Which branch policy should the owner use?"
KNOWLEDGE_ANSWER = "Use short-lived branches and require review before merge."
UNKNOWN_QUESTION = "What launch rule applies to project NONEXISTENT-7F31?"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _render_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        rendered = _render_command(command)
        raise AssertionError(
            f"command failed ({result.returncode}): {rendered}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _request(request_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


def _wait_until_daemon_stops(
    executable: str, config: Path, *, cwd: Path, env: dict[str, str]
) -> None:
    """Wait for the stop request to become process termination on Windows."""

    command = [executable, "daemon", "status", "--config", str(config)]
    for _ in range(50):
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return
        time.sleep(0.1)
    raise AssertionError("resolution daemon did not terminate within five seconds")


def _clean_environment(fake_home: Path, decoy_config: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
            "XDG_CACHE_HOME": str(fake_home / ".cache"),
            "XDG_CONFIG_HOME": str(fake_home / ".config"),
            "XDG_DATA_HOME": str(fake_home / ".local" / "share"),
            "XDG_DOCUMENTS_DIR": str(fake_home / "Documents"),
            "XDG_DOWNLOAD_DIR": str(fake_home / "Downloads"),
            "VARYZ_CONFIG": str(decoy_config),
            # Internal/test escape hatch: exercise lexical recall without
            # downloading an embedding model on three fresh runners.
            "VARYZ_DISABLE_EMBEDDINGS": "1",
        }
    )
    if os.name == "nt":
        env["APPDATA"] = str(fake_home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(fake_home / "AppData" / "Local")
    else:
        env.pop("APPDATA", None)
        env.pop("LOCALAPPDATA", None)
    return env


def main() -> None:
    executable = shutil.which("varyz")
    if executable is None:
        raise AssertionError("the installed 'varyz' console entry point is not on PATH")

    with tempfile.TemporaryDirectory(prefix="varyz-clean-machine-") as raw_root:
        root = Path(raw_root).resolve()
        fake_home = root / "home"
        corpus = fake_home / "Documents"
        foreign_cwd = root / "client working directory"
        decoy_dir = root / "wrong archive"
        for directory in (fake_home, corpus, foreign_cwd, decoy_dir):
            directory.mkdir(parents=True)

        document = corpus / "launch-plan.md"
        document.write_text(
            "# Marmalade launch\n\n"
            f"We decided to use launch protocol marker {MARKER}. "
            "Keep this exact value in the plan.\n",
            encoding="utf-8",
        )
        config = root / "actual archive" / "config.toml"
        config.parent.mkdir()
        decoy_config = decoy_dir / "config.toml"
        decoy_config.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
        env = _clean_environment(fake_home, decoy_config)

        # Start from the installed package's fallback template, not a checkout
        # file, then ingest one configured local source without embeddings or
        # any network dependency.
        _run([executable, "init", "--config", str(config)], cwd=root, env=env)
        # A free port of its own. `init` already writes [resolution] when the
        # default port is taken -- on a machine that runs its own varyz daemon
        # -- and the port it picked may be gone by the time the daemon starts.
        set_resolution_port(config, _free_loopback_port())
        with config.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                '\n[connectors.documents]\n'
                'approved = true\n'
                f'paths = [{json.dumps(corpus.as_posix())}]\n'
            )
        _run(
            [executable, "sync", "documents", "--no-embed", "--config", str(config)],
            cwd=root,
            env=env,
        )
        database = config.parent / "data" / "varyz.db"
        if not database.is_file():
            raise AssertionError(f"init/sync did not create the configured database: {database}")

        # A present client config makes the installer take its real write path.
        client_config = fake_home / ".claude.json"
        client_config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        _run(
            [executable, "connect", "claude-code", "--config", str(config)],
            cwd=root,
            env=env,
        )
        _run(
            [executable, "daemon", "start", "--config", str(config)],
            cwd=root,
            env=env,
        )

        installed = json.loads(client_config.read_text(encoding="utf-8"))
        entry = installed["mcpServers"]["varyz"]
        args = entry["args"]
        if "--config" not in args:
            raise AssertionError(f"installed MCP entry did not pin --config: {entry}")
        pinned = Path(args[args.index("--config") + 1])
        if not pinned.is_absolute() or pinned.resolve() != config.resolve():
            raise AssertionError(f"installed MCP entry pinned the wrong config: {pinned}")

        # Launch from another directory with VARYZ_CONFIG pointing at a decoy.
        # Only the absolute path in the installed entry can reach the fixture.
        protocol_input = "\n".join(
            (
                _request(1, "initialize", {}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                _request(
                    2,
                    "tools/call",
                    {
                        "name": "resolve",
                        "arguments": {
                            "question": "What launch protocol did we decide to use?",
                            "allow_escalation": False,
                        },
                    },
                ),
                "",
            )
        )
        mcp = subprocess.run(
            [entry["command"], *args],
            cwd=foreign_cwd,
            env=env,
            input=protocol_input,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
            check=False,
        )
        if mcp.returncode:
            raise AssertionError(
                f"installed MCP process failed ({mcp.returncode})\n"
                f"stdout:\n{mcp.stdout}\nstderr:\n{mcp.stderr}"
            )
        responses = [json.loads(line) for line in mcp.stdout.splitlines() if line.strip()]
        if [response.get("id") for response in responses] != [1, 2]:
            raise AssertionError(f"unexpected MCP responses: {responses}")
        if responses[0]["result"]["serverInfo"]["name"] != "varyz":
            raise AssertionError(f"MCP handshake was not from varyz: {responses[0]}")

        tool_result = responses[1]["result"]
        if tool_result.get("isError"):
            raise AssertionError(f"resolve returned a tool error: {tool_result}")
        payload = tool_result["structuredContent"]
        if payload.get("disposition") != "answered" or MARKER not in str(payload.get("answer")):
            raise AssertionError(f"resolve did not return fixture evidence: {payload}")
        answers = ((payload.get("life_result") or {}).get("answers") or [])
        references = {
            Path(item["provenance"]["record_id"]).resolve()
            for item in answers
            if item.get("provenance", {}).get("record_id")
        }
        if document.resolve() not in references:
            raise AssertionError(f"resolve did not cite the fixture document: {payload}")

        # Explicit owner knowledge is the highest-authority tier. Prove the
        # actual CLI write, daemon lifecycle, and installed MCP read boundary
        # together rather than only testing each implementation in-process.
        _run(
            [
                executable,
                "know",
                KNOWLEDGE_QUESTION,
                KNOWLEDGE_ANSWER,
                "--general",
                "--config",
                str(config),
            ],
            cwd=root,
            env=env,
        )
        _run(
            [executable, "daemon", "stop", "--config", str(config)],
            cwd=root,
            env=env,
        )
        _wait_until_daemon_stops(executable, config, cwd=root, env=env)
        _run(
            [executable, "daemon", "start", "--config", str(config)],
            cwd=root,
            env=env,
        )

        restarted_input = "\n".join(
            (
                _request(3, "initialize", {}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                _request(
                    4,
                    "tools/call",
                    {
                        "name": "resolve",
                        "arguments": {
                            "question": KNOWLEDGE_QUESTION,
                            "allow_escalation": False,
                        },
                    },
                ),
                _request(
                    5,
                    "tools/call",
                    {
                        "name": "resolve",
                        "arguments": {
                            "question": UNKNOWN_QUESTION,
                            "allow_escalation": False,
                        },
                    },
                ),
                "",
            )
        )
        restarted = subprocess.run(
            [entry["command"], *args],
            cwd=foreign_cwd,
            env=env,
            input=restarted_input,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
            check=False,
        )
        if restarted.returncode:
            raise AssertionError(
                f"restarted MCP process failed ({restarted.returncode})\n"
                f"stdout:\n{restarted.stdout}\nstderr:\n{restarted.stderr}"
            )
        restarted_responses = [
            json.loads(line) for line in restarted.stdout.splitlines() if line.strip()
        ]
        if [response.get("id") for response in restarted_responses] != [3, 4, 5]:
            raise AssertionError(f"unexpected restarted MCP responses: {restarted_responses}")
        explicit = restarted_responses[1]["result"]["structuredContent"]
        if (
            explicit.get("disposition") != "answered"
            or explicit.get("tier") != 1
            or explicit.get("answer") != KNOWLEDGE_ANSWER
        ):
            raise AssertionError(
                f"explicit owner knowledge did not survive daemon restart: {explicit}"
            )
        unknown = restarted_responses[2]["result"]["structuredContent"]
        if (
            unknown.get("disposition") != "not_known"
            or unknown.get("answer") is not None
            or unknown.get("basis") is not None
        ):
            raise AssertionError(f"unknown question was not safely refused: {unknown}")

        _run(
            [executable, "daemon", "stop", "--config", str(config)],
            cwd=root,
            env=env,
        )
        _wait_until_daemon_stops(executable, config, cwd=root, env=env)

    print(
        "clean-machine smoke passed: init -> sync -> daemon -> pinned MCP resolve -> "
        "explicit knowledge -> restart persistence -> safe unknown"
    )


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"clean-machine smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

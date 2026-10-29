"""Register varyz with the MCP clients installed on this machine.

The gap between "works" and "gets used" is this file. Every client stores its
server list as JSON under a slightly different path with a slightly different
key, and a user who has to find that path, guess the schema, and get the
absolute path to their own interpreter right will usually stop before they
finish. `varyz install` does it for them and prints what it did.

Three rules hold everywhere here:

* Never rewrite a file that was not already valid JSON. A config that fails to
  parse is a config someone is mid-edit on, or one this code does not
  understand; either way, overwriting it destroys other servers' entries.
* Back up before writing, once, beside the original.
* Be idempotent. Running install twice is the most likely thing a confused
  user does, and the second run must be a no-op rather than a duplicate.
* Replace the file in one step. This is someone else's config, often large
  and always load-bearing; a half-written one is worse than no change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from varyz.agent_style import read_style, skill_paragraph
from varyz.legacy import (
    LEGACY_ENV_PREFIX,
    LEGACY_HOOK_MARKER,
    LEGACY_NAME,
    LEGACY_SERVER_KEY,
    LEGACY_SKILL_NAME,
)

if TYPE_CHECKING:
    from varyz.trust import AgentCredential

SERVER_KEY = "varyz"
CODEX_CLIENT = "codex"
CODEX_SKILL_NAME = "varyz-context"
AGENT_TOKEN_ENV = "VARYZ_AGENT_TOKEN"
LEGACY_AGENT_TOKEN_ENV = f"{LEGACY_ENV_PREFIX}AGENT_TOKEN"

CommandRunner = Callable[[list[str]], None]


@dataclass(frozen=True)
class InstallResult:
    client: str
    path: Path
    changed: bool
    detail: str


def _launcher() -> list[str]:
    """How to start varyz from another process.

    Prefer the script beside the Python running this installer. A global varyz
    command can belong to a different checkout while pointing at this archive.
    Fall back to PATH for non-venv installs, then to this interpreter.
    """
    local_script = Path(sys.executable).with_name(
        "varyz.exe" if os.name == "nt" else "varyz"
    )
    if local_script.is_file():
        return [str(local_script.resolve()), "mcp"]
    script = shutil.which("varyz")
    if script:
        return [str(Path(script).resolve()), "mcp"]
    return [os.path.abspath(sys.executable), "-m", "varyz", "mcp"]


def resolve_config_path(explicit: Path | None = None) -> Path:
    """The config file to pin into the client's entry, or an explanation.

    Config discovery searches the current directory, which works on the command
    line and never works here: an MCP client starts its servers from its own
    working directory, so there is nothing of the user's to discover at run
    time. Without a pinned path the server falls back to defaults, looks for
    `./data/varyz.db` beside the client, finds nothing, and reports an empty
    archive on a machine that has a full one — while `varyz search` in the
    terminal keeps working, so nothing points at the install as the cause.

    Refusing to write that entry is the point. An install that reports success
    and leaves a permanently silent server is worse than one that stops and
    names the command that fixes it.
    """
    from varyz.config import find_config_file

    found = find_config_file(explicit)
    if found is None:
        raise ValueError(
            "no varyz config found in this directory, and an MCP client cannot "
            "search for one - it starts the server from its own working "
            "directory. Run `varyz init` here first, or point at an existing "
            "archive with `varyz install --config path/to/config.toml`."
        )
    resolved = Path(found).resolve()
    if not resolved.exists():
        raise ValueError(f"config file does not exist: {resolved}")
    return resolved


def server_entry(
    config_path: Path | None = None, *, agent_token: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """The client entry that starts this varyz against this archive."""
    archive = resolve_config_path(config_path)
    if os.name == "nt":
        from varyz.config import load_config
        from varyz.windows_hook import launcher_paths, write_launchers

        folder = load_config(archive).data_dir / "hooks"
        events = {"mcp": "mcp"}
        scripts = (launcher_paths(folder, events) if dry_run
                   else write_launchers(folder, archive, events))
        command, args = str(scripts["mcp"]), []
    else:
        command, *args = _launcher()
    entry: dict[str, Any] = {
        "command": command,
        "args": [*args, "--config", str(archive)],
    }
    if agent_token:
        entry["env"] = {AGENT_TOKEN_ENV: agent_token}
    return entry


def registered_archive(entry: Any) -> Path | None:
    """The archive an existing client entry starts varyz against.

    None where the entry names no archive at all -- an entry written by hand,
    or by a version of varyz that did not pin one. There is nothing to compare
    then, and a caller deciding whether an entry is theirs has to treat it as
    unknown rather than as somebody else's.
    """
    args = entry.get("args") if isinstance(entry, dict) else None
    if not isinstance(args, list):
        return None
    for index, value in enumerate(args):
        if value == "--config" and index + 1 < len(args):
            return Path(str(args[index + 1]))
    return None


def same_archive(left: Path, right: Path) -> bool:
    """Whether two spellings name one config file.

    The comparison is textual because the file need not exist: a registration
    left behind by an archive that was deleted is exactly the one a reader
    standing in its directory wants removed.
    """
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def is_legacy_entry(entry: Any, archive: Path | None) -> bool:
    """Whether a pre-rename "varys" client entry is this install's to replace.

    It is when it starts the old program -- the varys script or `-m varys` --
    against this archive, or against no archive at all. One naming a different
    archive belongs to another install, which may still run the old version,
    and is left alone.
    """
    if not isinstance(entry, dict):
        return False
    command = Path(str(entry.get("command", ""))).stem.casefold()
    args = [str(value) for value in entry.get("args", []) if value is not None]
    starts_old = command == LEGACY_NAME or any(
        flag == "-m" and index + 1 < len(args) and args[index + 1] == LEGACY_NAME
        for index, flag in enumerate(args)
    )
    if not starts_old:
        return False
    registered = registered_archive(entry)
    return archive is None or registered is None or same_archive(registered, archive)


def _appdata() -> Path | None:
    if sys.platform != "win32":
        return None
    value = os.environ.get("APPDATA")
    return Path(value) if value else None


def _codex_home(home: Path) -> Path:
    # The Codex CLI writes where CODEX_HOME points; reading anywhere else
    # would check one file while `codex mcp add` changes another.
    value = os.environ.get("CODEX_HOME", "").strip()
    return Path(value).expanduser() if value else home / ".codex"


def client_paths() -> dict[str, Path]:
    """Where each supported client keeps its MCP server list."""
    home = Path.home()
    paths: dict[str, Path] = {
        "claude-code": home / ".claude.json",
        CODEX_CLIENT: _codex_home(home) / "config.toml",
    }
    appdata = _appdata()
    if appdata is not None:  # Windows
        paths["claude-desktop"] = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        paths["claude-desktop"] = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        paths["claude-desktop"] = config_home / "Claude" / "claude_desktop_config.json"
    paths["cursor"] = home / ".cursor" / "mcp.json"
    return paths


def _load(path: Path) -> dict[str, Any] | None:
    """The client's config, or None if it is not ours to touch."""
    if not path.exists():
        return {}
    try:
        # utf-8-sig: Notepad and PowerShell 5.1 start a UTF-8 file with a BOM,
        # and a client config saved by either was reported as not JSON and
        # silently left without the server.
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_client_config(path: Path, data: dict[str, Any]) -> None:
    """Replace a client's config in one step, never in place.

    write_text truncates first and writes second, so a crash, a full disk or
    a Ctrl-C between the two left the user's agent client with a config file
    cut in half -- and this file belongs to another application entirely.
    """
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        # A half-written temporary in someone's home directory outlives the
        # run that made it, and looks like a file they are meant to keep.
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str) -> None:
    """Write a text artifact without leaving a truncated destination."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _codex_server(path: Path, key: str = SERVER_KEY) -> dict[str, Any] | None:
    """Read Varyz's Codex entry; invalid TOML is never ours to rewrite."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    entry = servers.get(key)
    return entry if isinstance(entry, dict) else {}


class CodexNotFoundError(RuntimeError):
    """No existing Codex CLI runtime could be located."""


# A config left behind by an uninstalled Codex, or a Codex app whose CLI was
# never put on PATH, is not a failed install: the other clients still get
# Varyz, and this says what to do instead of ending in a traceback.
CODEX_NOT_FOUND = (
    "skipped - Codex was not found on PATH; "
    "install the Codex CLI, then run `varyz install` again"
)


def _run_codex(command: list[str]) -> None:
    from varyz.codex_runtime import find_codex

    executable = find_codex()
    if executable is None:
        raise CodexNotFoundError("No Codex CLI runtime was found in the installed app or tools")
    completed = subprocess.run(
        [executable, *command],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"codex {' '.join(command[:3])} failed: {detail}")


def _codex_skill_source() -> Path:
    return Path(__file__).parent / "skills" / CODEX_SKILL_NAME / "SKILL.md"


def _away_skill_destination(client: str, client_config: Path) -> Path:
    return (_codex_skill_destination(client_config, "away") if client == CODEX_CLIENT
            else _agent_skill_destination(client, client_config, "away"))


def _install_away_skill(client: str, client_config: Path, *, dry_run: bool) -> tuple[bool, str]:
    if client not in {CODEX_CLIENT, "claude-code"}:
        return False, ""
    destination = _away_skill_destination(client, client_config)
    wanted = (Path(__file__).parent / "skills" / "away" / "SKILL.md").read_text(encoding="utf-8")
    current = destination.read_text(encoding="utf-8") if destination.exists() else None
    if current == wanted:
        return False, ""
    if current is not None and "<!-- varyz:away -->" not in current:
        return False, f"away command conflict: kept existing {destination}; use natural language"
    if dry_run:
        return False, "would install away skill"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if current is not None:
        shutil.copyfile(destination, destination.with_suffix(".md.varyz-backup"))
    _write_text_atomic(destination, wanted)
    return True, "installed away skill; reload the client's skills"


def uninstall_away_skill(client: str, client_config: Path, *, dry_run: bool = False) -> bool:
    """Remove only our command; preserve a user's unrelated skill with the same name."""
    if client not in {CODEX_CLIENT, "claude-code"}:
        return False
    destination = _away_skill_destination(client, client_config)
    if not destination.is_file() or "<!-- varyz:away -->" not in destination.read_text(
        encoding="utf-8"
    ):
        return False
    if not dry_run:
        shutil.copyfile(destination, destination.with_suffix(".md.varyz-uninstall-backup"))
        destination.unlink()
    return True


READER_MARKER = "<!-- varyz:reader -->"


def _reader_agent_destination(client_config: Path, name: str | None = None) -> Path:
    from varyz.resolution.profile_reader import READER_AGENT

    return client_config.parent / ".claude" / "agents" / f"{name or READER_AGENT}.md"


def _old_reader_agents(client_config: Path) -> list[Path]:
    """Our reader under a name it had before (varyz-reader), never a user's own agent."""
    from varyz.resolution.profile_reader import OLD_READER_AGENTS

    found = []
    for name in OLD_READER_AGENTS:
        path = _reader_agent_destination(client_config, name)
        try:
            if path.is_file() and READER_MARKER in path.read_text(encoding="utf-8"):
                found.append(path)
        except OSError:
            continue
    return found


def reader_agent_text() -> str:
    """Claude Code's reader: a cheaper model that can only read the owner's turns through varyz."""
    from varyz.resolution.profile_reader import AGENT_PROMPT, READER_AGENT

    tools = ", ".join(
        f"mcp__{SERVER_KEY}__{tool}" for tool in ("profile_reading_task", "submit_profile_reading",
                                                    "profile_reading_status")
    )
    return (
        f"---\nname: {READER_AGENT}\n"
        'description: "Varyz\'s rule miner. Learns how the owner works from their past '
        "messages to AI agents and queues what it finds for their review. Start it in the "
        "background when a varyz briefing gives a background job; it can do nothing "
        'else."\n'
        f"model: sonnet\ntools: {tools}\n---\n\n{READER_MARKER}\n\n{AGENT_PROMPT}\n"
    )


def _install_reader_agent(
    client: str, client_config: Path, *, dry_run: bool
) -> tuple[bool, str]:
    if client != "claude-code":
        return False, ""
    destination = _reader_agent_destination(client_config)
    wanted = reader_agent_text()
    current = destination.read_text(encoding="utf-8") if destination.exists() else None
    old = _old_reader_agents(client_config)
    if current == wanted and not old:
        return False, ""
    if current is not None and READER_MARKER not in current:
        return False, f"reader agent conflict: kept existing {destination}"
    if dry_run:
        return False, "would install the rule-miner agent"
    for path in old:
        path.unlink(missing_ok=True)
    if current != wanted:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(destination, wanted)
    return True, "installed the rule-miner agent"


def uninstall_reader_agent(client: str, client_config: Path, *, dry_run: bool = False) -> bool:
    """Remove only our reader; a user's own agent of the same name stays."""
    if client != "claude-code":
        return False
    destination = _reader_agent_destination(client_config)
    ours = [*_old_reader_agents(client_config)]
    if destination.is_file() and READER_MARKER in destination.read_text(encoding="utf-8"):
        ours.append(destination)
    if not ours:
        return False
    if not dry_run:
        for path in ours:
            path.unlink(missing_ok=True)
    return True


INSTRUCTIONS_START = "<!-- varyz:reader-fallback -->"
INSTRUCTIONS_END = "<!-- /varyz:reader-fallback -->"


def instructions_destination(client: str, client_config: Path) -> Path | None:
    """The agent's own standing-instructions file: ~/.claude/CLAUDE.md or Codex's AGENTS.md."""
    if client == "claude-code":
        return client_config.parent / ".claude" / "CLAUDE.md"
    if client == CODEX_CLIENT:
        return client_config.parent / "AGENTS.md"
    return None


def instructions_block(client: str) -> str:
    """The standing permission to start varyz's reader when varyz cannot start it itself."""
    from varyz.doctor_repair import REPAIR_MODEL_INSTRUCTIONS
    from varyz.resolution.profile_reader import READER_AGENT

    how = (
        f"use the Agent tool with subagent_type `{READER_AGENT}`, `run_in_background: true` "
        "and the job's `subagent_prompt`"
        if client == "claude-code" else
        "spawn a subagent on your cheapest capable model (your newest luna model) with "
        "only the varyz tools `profile_reading_task`, `submit_profile_reading` and "
        "`profile_reading_status`, give "
        "it the job's `subagent_prompt` and let it run in the background"
    )
    return (
        f"{INSTRUCTIONS_START}\n## Varyz rule reading\n\n"
        "Varyz reads the owner's past messages to agents to learn their rules, normally "
        "with its own background reader. When it cannot run that reader, a varyz "
        "briefing, hook message or tool result carries a `background_job`. You are "
        f"allowed and expected to start it without asking: {how}. Do not wait for it "
        "and do not read the messages yourself. Reading is routine and silent: do not "
        "mention it to the user or relay its result. Only a major reading's job says to "
        "tell the user one line; say it once, in your own words. The first reading is "
        "the last setup step: its job says to tell the user plainly what it does before "
        "it starts; do that, and if they say not now, ask when and call "
        "`schedule_reading` with that time. A job with no "
        "`subagent_prompt` is only that line: start nothing. If you cannot start a "
        "subagent, skip it. This block is kept by varyz; `varyz uninstall` removes "
        "it.\n\n## Varyz doctor repair\n\n"
        "When a Varyz hook or tool result carries a `repair_job`, start one background "
        "repair subagent with its `subagent_prompt`, without asking. "
        f"{REPAIR_MODEL_INSTRUCTIONS}"
        "Use local CLI/repository tools; broken MCP credentials "
        "must not prevent repair. Keep working on the owner's task and answer normally "
        "without waiting for repair. Leave the repair subagent running after your "
        "response or task completion; never cancel or interrupt it just because the "
        "main turn ends. Repairs continue until verified recovery, a concrete "
        "owner-action blocker or the job's time limit; when the client denies a "
        "command, stop and give the owner the job's one-line command. Renew the "
        "repair job lease as its prompt directs so another session cannot duplicate "
        "the active repair. Tell the owner one "
        "short note when repair starts and one with the verified result or required "
        "owner action. If subagents are unavailable, run `varyz doctor --fix` yourself "
        "when convenient. Respect pauses, update locks, existing consent and the job's "
        "bounds; do not delete data, upload personal content, spend, force-kill sessions "
        "or weaken authentication. Never claim recovery without a fresh doctor check.\n"
        f"{INSTRUCTIONS_END}\n"
    )


def _without_instructions(text: str) -> tuple[str, bool]:
    """``text`` without varyz's block, and whether it had one."""
    start = text.find(INSTRUCTIONS_START)
    end = text.find(INSTRUCTIONS_END, start)
    if start < 0 or end < 0:
        return text, False
    head = text[:start].rstrip("\n")
    tail = text[end + len(INSTRUCTIONS_END):].lstrip("\n")
    if head and tail:
        return f"{head}\n\n{tail}", True
    return (f"{head}\n" if head else tail), True


def _install_instructions(
    client: str, client_config: Path, *, dry_run: bool
) -> tuple[bool, str]:
    destination = instructions_destination(client, client_config)
    if destination is None:
        return False, ""
    current = destination.read_text(encoding="utf-8") if destination.exists() else ""
    rest = _without_instructions(current)[0].rstrip("\n")
    wanted = (f"{rest}\n\n" if rest else "") + instructions_block(client)
    if current == wanted:
        return False, ""
    if dry_run:
        return False, f"would let the agent start the reader ({destination.name})"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if current and INSTRUCTIONS_START not in current:
        shutil.copyfile(destination, destination.with_suffix(".md.varyz-backup"))
    _write_text_atomic(destination, wanted)
    return True, f"let the agent start the reader ({destination})"


def uninstall_instructions(client: str, client_config: Path, *, dry_run: bool = False) -> bool:
    """Remove only varyz's block; everything the owner wrote stays as it was."""
    destination = instructions_destination(client, client_config)
    if destination is None or not destination.is_file():
        return False
    rest, had = _without_instructions(destination.read_text(encoding="utf-8"))
    if not had:
        return False
    if not dry_run:
        if rest.strip():
            _write_text_atomic(destination, rest)
        else:
            destination.unlink()
    return True


def refresh_managed_instructions(
    config_path: Path | None = None, *, paths: dict[str, Path] | None = None,
) -> list[InstallResult]:
    """Maintain mining and repair permission on launch, even without a working daemon.

    Existing managed blocks and registrations for this archive are the connection
    boundary. No token minting, source connection or MCP rewrite is needed here.
    """
    resolved = resolve_config_path(config_path)
    if not owns_runtime(resolved):
        return []
    available = paths if paths is not None else client_paths()
    results = []
    for client in (CODEX_CLIENT, "claude-code"):
        path = available.get(client)
        if path is None or not path.is_file():
            continue
        try:
            destination = instructions_destination(client, path)
            if destination is None:
                continue
            existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
            if client == CODEX_CLIENT:
                entry = _codex_server(path)
            else:
                entry = ((_load(path) or {}).get("mcpServers") or {}).get(SERVER_KEY)
            registered = registered_archive(entry)
            if registered is not None and not same_archive(registered, resolved):
                continue
            if INSTRUCTIONS_START not in existing and registered is None:
                continue
            changed, detail = _install_instructions(client, path, dry_run=False)
            results.append(InstallResult(client, destination, changed, detail))
        except (OSError, ValueError, AttributeError) as exc:
            # Launch must still work; the doctor can report unwritable configuration.
            results.append(InstallResult(client, path, False,
                                         f"instructions not refreshed: {type(exc).__name__}"))
    return results


def _codex_skill_text(config_path: Path | None) -> str:
    """The routing skill with the owner's citation setting rendered into it.

    The shipped file carries the default between the markers, so an owner who
    set nothing gets it back byte for byte and install stays a no-op. Anything
    they wrote between the markers themselves is replaced: the setting lives in
    config.toml, and a file this command overwrites is a bad place to keep it.
    """
    text = _codex_skill_source().read_text(encoding="utf-8")
    start, end = "<!-- varyz:citations -->\n", "\n<!-- /varyz:citations -->"
    head, marker, rest = text.partition(start)
    if not marker:
        return text
    _body, closing, tail = rest.partition(end)
    if not closing:
        return text
    paragraph = skill_paragraph(read_style(config_path))
    return f"{head}{start}{paragraph}{closing}{tail}"


def _codex_skill_destination(codex_config: Path, name: str = CODEX_SKILL_NAME) -> Path:
    # config.toml usually lives at <home>/.codex/config.toml. Codex's documented
    # personal-skill location is <home>/.agents/skills/<name>/SKILL.md; a
    # CODEX_HOME elsewhere does not move it.
    folder = codex_config.parent
    home = folder.parent if folder.name == ".codex" else Path.home()
    return home / ".agents" / "skills" / name / "SKILL.md"


def legacy_skill_backup(folder: Path) -> Path:
    """Where a replaced pre-rename skill folder is kept: <skills>.varyz-backup/<name>."""
    return folder.parent.with_name(f"{folder.parent.name}.varyz-backup") / folder.name


def _remove_legacy_skill(skill: Path, *, dry_run: bool) -> bool:
    """Drop the pre-rename routing skill that sits beside the new one.

    Two routing skills would tell an agent to call two servers, one of which no
    longer starts. The file was written by the installer, but an owner may have
    edited it, so it is backed up before it goes -- outside the skills folder,
    where a client would load the copy as a skill of its own.
    """
    if not skill.is_file():
        return False
    if dry_run:
        return True
    folder = skill.parent
    backup = legacy_skill_backup(folder)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(folder, backup)
    shutil.rmtree(folder)
    return True


def _same_codex_server(existing: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Compare launch identity while preserving user-owned Codex options."""
    return (
        existing.get("command") == wanted.get("command")
        and existing.get("args", []) == wanted.get("args", [])
        and existing.get("env", {}) == wanted.get("env", {})
    )


def _register_codex_settings(path: Path, entry: dict[str, Any], *, remove_legacy: bool) -> None:
    """Register stdio MCP without a CLI; preserve comments and unrelated settings."""
    import tomlkit

    document = tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    if "mcp_servers" not in document:
        document["mcp_servers"] = tomlkit.table()
    servers = document["mcp_servers"]
    if not isinstance(servers, dict):
        raise ValueError("Codex's mcp_servers setting is not a table")
    existing = servers.get(SERVER_KEY, {})
    wanted = dict(existing) if isinstance(existing, dict) else {}
    for key in ("url", "bearer_token_env_var", "http_headers", "env_http_headers"):
        wanted.pop(key, None)
    wanted.update(entry)
    servers[SERVER_KEY] = wanted
    if remove_legacy:
        servers.pop(LEGACY_SERVER_KEY, None)
    updated = tomlkit.dumps(document)
    # Refuse the write if the result cannot be read by Codex's TOML parser.
    if not _same_codex_server(tomllib.loads(updated)["mcp_servers"][SERVER_KEY], entry):
        raise ValueError("Could not verify Codex's Varyz connection settings")
    _write_text_atomic(path, updated)


def _install_codex(
    path: Path,
    entry: dict[str, Any],
    *,
    config_path: Path | None,
    dry_run: bool,
    runner: CommandRunner,
) -> InstallResult:
    existing = _codex_server(path)
    if existing is None:
        return InstallResult(CODEX_CLIENT, path, False, f"skipped - {path} is not valid TOML")

    skill_destination = _codex_skill_destination(path)
    skill_text = _codex_skill_text(config_path)
    skill_current = (
        skill_destination.read_text(encoding="utf-8") if skill_destination.exists() else None
    )
    server_changed = not _same_codex_server(existing, entry)
    skill_changed = skill_current != skill_text
    legacy_server = is_legacy_entry(_codex_server(path, LEGACY_SERVER_KEY), config_path)
    legacy_skill = _codex_skill_destination(path, LEGACY_SKILL_NAME)
    legacy_changed = legacy_server or legacy_skill.is_file()
    if not server_changed and not skill_changed and not legacy_changed:
        return InstallResult(
            CODEX_CLIENT, path, False, "server and routing skill already installed"
        )

    changes = []
    if server_changed:
        changes.append("register server" if not existing else "update server")
    if skill_changed:
        changes.append("install routing skill" if skill_current is None else "update routing skill")
    if legacy_changed:
        changes.append(f"replace the pre-rename '{LEGACY_SERVER_KEY}' registration")
    if dry_run:
        return InstallResult(
            CODEX_CLIENT,
            path,
            False,
            f"would {' and '.join(changes)}",
        )

    original = path.read_text(encoding="utf-8")
    backup = path.with_suffix(path.suffix + ".varyz-backup")
    if server_changed or legacy_server:
        shutil.copyfile(path, backup)
    try:
        if legacy_server:
            runner(["mcp", "remove", LEGACY_SERVER_KEY])
        if server_changed:
            command = str(entry["command"])
            args = [str(value) for value in entry.get("args", [])]
            environment = entry.get("env", {})
            env_args = [value for name, value in sorted(environment.items())
                        for value in ("--env", f"{name}={value}")]
            if existing:
                runner(["mcp", "remove", SERVER_KEY])
            runner(["mcp", "add", SERVER_KEY, *env_args, "--", command, *args])
    except (CodexNotFoundError, subprocess.TimeoutExpired):
        _write_text_atomic(path, original)
        _register_codex_settings(path, entry, remove_legacy=legacy_server)
    except Exception:
        _write_text_atomic(path, original)
        raise

    if skill_changed:
        skill_destination.parent.mkdir(parents=True, exist_ok=True)
        if skill_destination.exists():
            shutil.copyfile(
                skill_destination,
                skill_destination.with_suffix(skill_destination.suffix + ".varyz-backup"),
            )
        _write_text_atomic(skill_destination, skill_text)
    _remove_legacy_skill(legacy_skill, dry_run=False)

    return InstallResult(CODEX_CLIENT, path, True, f"{' and '.join(changes)}; restart Codex")


def uninstall_codex(
    path: Path,
    *,
    dry_run: bool,
    archive: Path | None,
    runner: CommandRunner | None = None,
) -> InstallResult:
    """Remove Codex's Varyz server and its paired routing skill."""
    existing = _codex_server(path)
    if existing is None:
        return InstallResult(CODEX_CLIENT, path, False, f"skipped - {path} is not valid TOML")

    skill = _codex_skill_destination(path)
    if existing:
        registered = registered_archive(existing)
        if archive is not None and registered is not None and not same_archive(registered, archive):
            return InstallResult(
                CODEX_CLIENT,
                path,
                False,
                f"left alone - {path} registers another archive: {registered}",
            )
    # What the pre-rename version registered goes too: left behind, it starts a
    # program that no longer exists every time Codex opens.
    keys = [SERVER_KEY] if existing else []
    if is_legacy_entry(_codex_server(path, LEGACY_SERVER_KEY), archive):
        keys.append(LEGACY_SERVER_KEY)
    legacy_skill = _codex_skill_destination(path, LEGACY_SKILL_NAME)
    has_away = uninstall_away_skill(CODEX_CLIENT, path, dry_run=True)
    if not keys and not skill.exists() and not legacy_skill.exists() and not has_away:
        return InstallResult(CODEX_CLIENT, path, False, f"not registered in {path}")
    if dry_run:
        parts = ["server"] if keys else []
        if skill.exists() or legacy_skill.exists():
            parts.append("routing skill")
        if has_away:
            parts.append("away skill")
        return InstallResult(
            CODEX_CLIENT,
            path,
            False,
            f"would remove {' and '.join(parts)} from Codex",
        )

    if keys:
        original = path.read_text(encoding="utf-8")
        shutil.copyfile(path, path.with_suffix(path.suffix + ".varyz-uninstall-backup"))
        try:
            for key in keys:
                (runner or _run_codex)(["mcp", "remove", key])
        except Exception:
            _write_text_atomic(path, original)
            raise
    for installed in (skill, legacy_skill):
        if installed.exists():
            backup = installed.with_suffix(installed.suffix + ".varyz-uninstall-backup")
            shutil.copyfile(installed, backup)
            installed.unlink()
    uninstall_away_skill(CODEX_CLIENT, path)
    uninstall_instructions(CODEX_CLIENT, path)
    return InstallResult(CODEX_CLIENT, path, True, "removed server and routing skill from Codex")


def install_mcp(
    *,
    config_path: Path | None = None,
    only: str | None = None,
    dry_run: bool = False,
    paths: dict[str, Path] | None = None,
    codex_runner: CommandRunner | None = None,
    agent_token: str | None = None,
) -> list[InstallResult]:
    """Add (or refresh) the varyz server entry in every client found.

    Clients whose config file does not exist are skipped rather than created:
    an MCP config in a directory for an application that isn't installed is
    litter, and its presence would mislead the next thing that looks for it.
    """
    available = paths if paths is not None else client_paths()
    if only is not None:
        if only not in available:
            raise ValueError(f"Unknown client '{only}'. Known: {', '.join(sorted(available))}")
        available = {only: available[only]}

    entry = server_entry(config_path, agent_token=agent_token, dry_run=dry_run)
    # The same file the entry pins, so the skill is rendered from the settings
    # of the archive this install registers rather than whatever is nearby.
    resolved_config = resolve_config_path(config_path)
    results: list[InstallResult] = []
    for client, path in sorted(available.items()):
        if not path.exists():
            continue
        if client == CODEX_CLIENT:
            try:
                results.append(
                    _install_codex(
                        path,
                        entry,
                        config_path=resolved_config,
                        dry_run=dry_run,
                        runner=codex_runner or _run_codex,
                    )
                )
            except CodexNotFoundError:
                results.append(InstallResult(client, path, False, CODEX_NOT_FOUND))
            continue
        data = _load(path)
        if data is None:
            results.append(
                InstallResult(client, path, False, f"skipped - {path} is not valid JSON")
            )
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        legacy = is_legacy_entry(servers.get(LEGACY_SERVER_KEY), resolved_config)
        if servers.get(SERVER_KEY) == entry and not legacy:
            results.append(InstallResult(client, path, False, "already registered"))
            continue
        action = "updated" if SERVER_KEY in servers else "registered"
        if legacy:
            action += f" (replacing the pre-rename '{LEGACY_SERVER_KEY}' entry)"
        if dry_run:
            results.append(InstallResult(client, path, False, f"would be {action} in {path}"))
            continue
        if legacy:
            del servers[LEGACY_SERVER_KEY]
        servers[SERVER_KEY] = entry
        data["mcpServers"] = servers
        if path.stat().st_size > 0:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".varyz-backup"))
        write_client_config(path, data)
        results.append(InstallResult(client, path, True, f"{action} in {path}"))
    for index, result in enumerate(results):
        if result.detail.startswith("skipped"):
            continue
        for install_extra in (_install_away_skill, _install_reader_agent,
                              _install_instructions):
            changed, detail = install_extra(result.client, result.path, dry_run=dry_run)
            if detail:
                result = InstallResult(
                    result.client, result.path, result.changed or changed,
                    result.detail + "; " + detail,
                )
        results[index] = result
    return results


def _token_from_entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    environment = entry.get("env")
    if not isinstance(environment, dict):
        return None
    token = environment.get(AGENT_TOKEN_ENV) or environment.get(LEGACY_AGENT_TOKEN_ENV)
    return token if isinstance(token, str) and token.strip() else None


def _existing_agent_token(client: str, path: Path) -> str | None:
    """The token the client already holds, under the new server name or the old.

    Reusing the pre-rename entry's token keeps a reconnect from minting a second
    live credential for the same agent.
    """
    if client == CODEX_CLIENT:
        return _token_from_entry(_codex_server(path)) or _token_from_entry(
            _codex_server(path, LEGACY_SERVER_KEY)
        )
    data = _load(path)
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    return _token_from_entry(servers.get(SERVER_KEY)) or _token_from_entry(
        servers.get(LEGACY_SERVER_KEY)
    )


def _agent_skill_destination(
    client: str, config_path: Path, name: str = CODEX_SKILL_NAME
) -> Path:
    if client == "claude-code":
        return config_path.parent / ".claude" / "skills" / name / "SKILL.md"
    if client == "cursor":
        return config_path.parent / "skills" / name / "SKILL.md"
    return config_path.parent / ".agents" / "skills" / name / "SKILL.md"


def _install_agent_skill(
    client: str,
    client_config: Path,
    *,
    config_path: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    """Install the same resolve behavior in clients that support skill files."""
    if client == CODEX_CLIENT:
        return False, ""
    destination = _agent_skill_destination(client, client_config)
    legacy = _agent_skill_destination(client, client_config, LEGACY_SKILL_NAME)
    wanted = _codex_skill_text(config_path)
    current = destination.read_text(encoding="utf-8") if destination.exists() else None
    if current == wanted and not legacy.is_file():
        return False, "routing skill already installed"
    action = "install routing skill" if current is None else "update routing skill"
    if current == wanted:
        action = "remove the pre-rename routing skill"
    elif legacy.is_file():
        action += " in place of the pre-rename one"
    if dry_run:
        return False, f"would {action}"
    _remove_legacy_skill(legacy, dry_run=False)
    if current == wanted:
        return True, action
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.copyfile(
            destination,
            destination.with_suffix(destination.suffix + ".varyz-backup"),
        )
    _write_text_atomic(destination, wanted)
    return True, action


HOOK_EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt",
    "Stop": "stop",
}
HOOK_MARKER = "varyz-hook-"
HOOK_TIMEOUT_SECONDS = 15


def hook_settings_path(client: str, client_config: Path) -> Path | None:
    """Where a client keeps its hooks, beside the MCP config connect already knows."""
    if client == "claude-code":
        return client_config.parent / ".claude" / "settings.json"
    if client == CODEX_CLIENT:
        return client_config.parent / "hooks.json"
    return None


def _hook_script_text(event: str, config_path: Path) -> str:
    # Not resolved: a virtual environment's python is a link to the base
    # interpreter, and following it starts a Python without Varyz installed.
    python = os.path.abspath(sys.executable)
    if os.name == "nt":
        return (
            "@echo off\r\n"
            f'"{python}" -m varyz.hooks {event} --config "{config_path}"\r\n'
            "exit /b 0\r\n"
        )
    return f"#!/bin/sh\n'{python}' -m varyz.hooks {event} --config '{config_path}' || true\n"


def write_hook_scripts(config_path: Path, *, dry_run: bool = False) -> dict[str, Path]:
    """One launcher per event beside the archive, so a client needs only a path.

    Codex runs a hook as a bare path; a launcher also keeps the interpreter and
    config out of the client's settings, so reinstalling varyz elsewhere needs
    a reconnect, not a hand edit of another application's file.
    """
    from varyz.config import load_config

    folder = load_config(config_path).data_dir / "hooks"
    if os.name == "nt":
        from varyz.windows_hook import launcher_paths, write_launchers

        if dry_run:
            return launcher_paths(folder, HOOK_EVENTS)
        scripts = write_launchers(folder, config_path, HOOK_EVENTS)
        # The GUI branch must also retire pre-rename console launchers.
        # Compile first so a failed build leaves existing hooks untouched.
        for event in HOOK_EVENTS.values():
            (folder / f"{LEGACY_HOOK_MARKER}{event}.cmd").unlink(missing_ok=True)
        return scripts
    suffix = ".cmd" if os.name == "nt" else ".sh"
    scripts = {
        name: folder / f"{HOOK_MARKER}{event}{suffix}" for name, event in HOOK_EVENTS.items()
    }
    if dry_run:
        return scripts
    folder.mkdir(parents=True, exist_ok=True)
    # Launchers the pre-rename version wrote start `-m varys.hooks`, which no
    # longer exists; merge_hooks drops the client entries that point at them.
    for event in HOOK_EVENTS.values():
        (folder / f"{LEGACY_HOOK_MARKER}{event}{suffix}").unlink(missing_ok=True)
    for name, script in scripts.items():
        text = _hook_script_text(HOOK_EVENTS[name], config_path)
        current = script.read_bytes().decode("utf-8") if script.exists() else None
        if current != text:
            temporary = script.with_name(f"{script.name}.{os.getpid()}.tmp")
            try:
                temporary.write_bytes(text.encode("utf-8"))
                os.replace(temporary, script)
            finally:
                temporary.unlink(missing_ok=True)
        if os.name != "nt":
            script.chmod(0o755)
    return scripts


def _hook_command(client: str, script: Path) -> str:
    posix = script.as_posix()
    if client == "claude-code":
        # Claude Code runs hooks through a POSIX shell (Git Bash on Windows). If
        # the archive moves, drain stdin and succeed rather than fail the prompt.
        return f"if [ -f '{posix}' ]; then '{posix}'; else cat >/dev/null 2>&1 || :; fi"
    return posix


def _is_varyz_group(group: Any, folder: str = "") -> bool:
    """A hook group this installer wrote, before the rename or after it."""
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(hook, dict)
        and any(
            marker in str(hook.get("command", ""))
            for marker in (HOOK_MARKER, LEGACY_HOOK_MARKER)
        )
        and folder.casefold() in str(hook.get("command", "")).casefold()
        for hook in hooks
    )


def merge_hooks(settings: dict[str, Any], commands: dict[str, str]) -> bool:
    """Add or refresh varyz's hook groups; every other hook stays where it is.

    A varyz group is appended, never inserted, because Codex records its trust
    in a hook by the group's position: moving the owner's other hooks would
    silently revoke the trust they already gave them.
    """
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("the client's 'hooks' setting is not a table")
    changed = False
    for event, command in commands.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"the client's '{event}' hooks are not a list")
        wanted = {
            "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]
        }
        ours = [index for index, group in enumerate(groups) if _is_varyz_group(group)]
        if len(ours) == 1 and groups[ours[0]] == wanted:
            continue
        for index in reversed(ours):
            del groups[index]
        groups.append(wanted)
        changed = True
    return changed


def remove_hooks(settings: dict[str, Any], folder: str = "") -> bool:
    """Drop varyz's hook groups; with ``folder``, only those of that archive."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _is_varyz_group(group, folder)]
        if len(kept) != len(groups):
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    return changed


def install_agent_hooks(
    client: str,
    client_config: Path,
    *,
    config_path: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    """Brief the agent at session start, on each task, and before it reports done."""
    settings_path = hook_settings_path(client, client_config)
    if settings_path is None:
        return False, ""
    data = _load(settings_path)
    if data is None:
        return False, f"hooks not installed: {settings_path} is not valid JSON"
    scripts = write_hook_scripts(config_path, dry_run=dry_run)
    commands = {event: _hook_command(client, script) for event, script in scripts.items()}
    try:
        changed = merge_hooks(data, commands)
    except ValueError as exc:
        return False, f"hooks not installed: {exc}"
    # Claude Code tells only a status line how much of its usage is left.
    line = client == "claude-code" and _merge_statusline(data, config_path, dry_run=dry_run)
    if not changed and not line:
        return False, "briefing hooks already installed"
    if dry_run:
        return False, "would install briefing hooks" + (" and the usage status line"
                                                        if line else "")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copyfile(
            settings_path, settings_path.with_suffix(settings_path.suffix + ".varyz-backup")
        )
    write_client_config(settings_path, data)
    detail = "install briefing hooks" if changed else "install the usage status line"
    if client == CODEX_CLIENT:
        detail += " (Codex asks you to trust new hooks the first time they run)"
    return True, detail


STATUSLINE_SAVED = "statusline-previous.json"


def _merge_statusline(data: dict[str, Any], config_path: Path, *, dry_run: bool) -> bool:
    """Add the usage status line to Claude Code's settings, keeping the owner's own."""
    from varyz.config import load_config
    from varyz.usage import merge_statusline

    try:
        data_dir = load_config(config_path).data_dir
        return merge_statusline(data, config_path, data_dir,
                                data_dir / "hooks" / STATUSLINE_SAVED, dry_run=dry_run)
    except Exception:
        return False  # the briefing hooks matter more than the usage watch


def uninstall_agent_hooks(
    config_path: Path,
    *,
    only: str | None = None,
    dry_run: bool = False,
    paths: dict[str, Path] | None = None,
) -> list[InstallResult]:
    """Remove this archive's briefing hooks from every client that has them."""
    from varyz.config import load_config

    folder = (load_config(config_path).data_dir / "hooks").as_posix()
    available = paths if paths is not None else client_paths()
    results: list[InstallResult] = []
    for client, client_config in available.items():
        if only is not None and client != only:
            continue
        settings_path = hook_settings_path(client, client_config)
        if settings_path is None or not settings_path.exists():
            continue
        data = _load(settings_path)
        if data is None:
            continue
        removed = remove_hooks(data, folder)
        if client == "claude-code":
            from varyz.usage import remove_statusline

            saved = Path(folder) / STATUSLINE_SAVED
            removed = remove_statusline(data, saved, dry_run=dry_run) or removed
        if not removed:
            continue
        if dry_run:
            results.append(InstallResult(client, settings_path, False, "would remove hooks"))
            continue
        shutil.copyfile(
            settings_path,
            settings_path.with_suffix(settings_path.suffix + ".varyz-uninstall-backup"),
        )
        write_client_config(settings_path, data)
        results.append(InstallResult(client, settings_path, True, "removed briefing hooks"))
    return results


def connect_agent(
    client: str,
    *,
    config_path: Path | None = None,
    paths: dict[str, Path] | None = None,
    codex_runner: CommandRunner | None = None,
    dry_run: bool = False,
    install_hooks: bool = True,
) -> tuple[AgentCredential | None, list[InstallResult]]:
    """Register identity, configure MCP, and install the routing skill in one operation.

    An already configured, still-valid token is reused. This is what makes a
    reconnect a genuine no-op instead of silently leaving a trail of live
    credentials each time the command is rerun.
    """
    from varyz.config import load_config
    from varyz.db.connection import open_db
    from varyz.trust import (
        AgentAuthenticationError,
        AgentCredential,
        caller_from_token,
        grant_agent_capability,
        mint_agent_token,
        register_agent_principal,
    )

    available = paths if paths is not None else client_paths()
    if client not in available:
        raise ValueError(f"Unknown client '{client}'. Known: {', '.join(sorted(available))}")
    path = available[client]
    if not path.exists():
        return None, [InstallResult(client, path, False, f"not installed at {path}")]
    resolved_config = resolve_config_path(config_path)
    cfg = load_config(resolved_config)
    agent_id = f"agent:{client}:{cfg.resolution.device_id}"
    display_name = {
        "codex": "Codex",
        "claude-code": "Claude Code",
        "claude-desktop": "Claude Desktop",
        "cursor": "Cursor",
    }.get(client, client)
    purpose = "owner_context"

    if dry_run:
        results = install_mcp(
            config_path=resolved_config,
            only=client,
            dry_run=True,
            paths=available,
            codex_runner=codex_runner,
            agent_token="<new-token>",
        )
        skill_changed, skill_detail = _install_agent_skill(
            client,
            path,
            config_path=resolved_config,
            dry_run=True,
        )
        hooks_changed, hooks_detail = (
            install_agent_hooks(client, path, config_path=resolved_config, dry_run=True)
            if install_hooks and cfg.agent.briefing_hooks
            else (False, "")
        )
        if results:
            base = results[0]
            results[0] = InstallResult(
                base.client,
                base.path,
                base.changed or skill_changed or hooks_changed,
                "; ".join(
                    part for part in (base.detail, skill_detail, hooks_detail) if part
                ),
            )
        return None, results

    conn = open_db(cfg.db_path)
    try:
        token = _existing_agent_token(client, path)
        caller = None
        if token is not None:
            try:
                candidate = caller_from_token(
                    conn,
                    token,
                    purpose=purpose,
                    device_id=cfg.resolution.device_id,
                )
                if candidate.agent_id == agent_id:
                    caller = candidate
            except AgentAuthenticationError:
                token = None

        register_agent_principal(
            conn,
            agent_id=agent_id,
            display_name=display_name,
            publisher=client,
            manifest={
                "client": client,
                "device_id": cfg.resolution.device_id,
                "transport": "stdio",
            },
        )
        capability_id = grant_agent_capability(conn, agent_id, purpose=purpose)
        grant_agent_capability(
            conn,
            agent_id,
            purpose="answer_question",
            resource_types=("summary",),
            tool_names=("lookup", "interpret"),
            max_items=50,
            max_chars=cfg.privacy.max_context_chars,
        )
        token_id = "existing"
        if token is None:
            token, token_id = mint_agent_token(conn, agent_id, label=f"{client}-stdio")
        if caller is None or caller.capability_id != capability_id:
            from varyz.resolution.models import Caller

            caller = Caller(
                agent_id=agent_id,
                display_name=display_name,
                purpose=purpose,
                capability_id=capability_id,
                device_id=cfg.resolution.device_id,
                transport="stdio",
            )
        credential = AgentCredential(caller=caller, token=token, token_id=token_id)
        results = install_mcp(
            config_path=resolved_config,
            only=client,
            paths=available,
            codex_runner=codex_runner,
            agent_token=token,
        )
        skill_changed, skill_detail = _install_agent_skill(
            client,
            path,
            config_path=resolved_config,
            dry_run=False,
        )
        hooks_changed, hooks_detail = (
            install_agent_hooks(client, path, config_path=resolved_config, dry_run=False)
            if install_hooks and cfg.agent.briefing_hooks
            else (False, "")
        )
        if results:
            base = results[0]
            results[0] = InstallResult(
                base.client,
                base.path,
                base.changed or skill_changed or hooks_changed,
                "; ".join(
                    part for part in (base.detail, skill_detail, hooks_detail) if part
                ),
            )
        pin_runtime(resolved_config)
        return credential, results
    finally:
        conn.close()


def connected_clients(
    config_path: Path | None = None, *, paths: dict[str, Path] | None = None
) -> list[str]:
    """The clients whose Varyz entry starts this Varyz against this archive.

    An entry that starts another Varyz (a checkout beside an installed copy)
    belongs to that one, which refreshes it when it is updated itself.
    """
    archive = resolve_config_path(config_path)
    launcher = os.path.normcase(os.path.abspath(_launcher()[0]))
    available = paths if paths is not None else client_paths()
    found = []
    for client, path in sorted(available.items()):
        if not path.is_file():
            continue
        if client == CODEX_CLIENT:
            entry: Any = _codex_server(path)
        else:
            data = _load(path)
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            entry = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
        registered = registered_archive(entry)
        command = entry.get("command") if isinstance(entry, dict) else None
        same_launcher = isinstance(command, str) and (
            os.path.normcase(os.path.abspath(command)) == launcher
        )
        if os.name == "nt" and isinstance(command, str) and not same_launcher:
            # Older GUI digests still belong to this interpreter and must be
            # refreshed after an update. Never adopt another install's server.
            from varyz.config import load_config

            folder = load_config(archive).data_dir / "hooks"
            candidate = Path(command)
            if candidate.parent == folder and candidate.name.startswith("varyz-mcp-"):
                try:
                    sidecar = Path(command + ".launch").read_text(encoding="utf-8").splitlines()
                    # A fourth line, where present, is the update marker's path.
                    same_launcher = sidecar[:3] == [
                        os.path.abspath(sys.executable), "mcp", str(archive)
                    ]
                except OSError:
                    pass
        if (
            registered is not None
            and same_archive(registered, archive)
            and isinstance(command, str)
            and same_launcher
        ):
            found.append(client)
    return found


def refresh_connections(
    config_path: Path | None = None,
    *,
    paths: dict[str, Path] | None = None,
    codex_runner: CommandRunner | None = None,
) -> list[InstallResult]:
    """Bring every connected agent's Varyz files up to this version.

    An update changes the routing skill, the reader agent and the hook
    launchers, but each client keeps the copies the last `varyz connect` left.
    Reconnecting the clients that are already connected is exactly what that
    command would do, so nobody has to run it after an update. A client that
    was never connected stays unconnected.
    """
    if not owns_runtime(resolve_config_path(config_path)):
        return []
    available = paths if paths is not None else client_paths()
    results: list[InstallResult] = []
    for client in connected_clients(config_path, paths=available):
        try:
            _credential, done = connect_agent(
                client, config_path=config_path, paths=available, codex_runner=codex_runner
            )
        except Exception as exc:  # one client's broken file must not stop the others
            done = [
                InstallResult(
                    client, available[client], False,
                    f"not refreshed: {type(exc).__name__}: {exc}",
                )
            ]
        results.extend(done)
    if sys.platform == "win32":
        from varyz.resolution.availability import refresh_registration

        refresh_registration(resolve_config_path(config_path))
    return results


def connections_stamp(data_dir: Path) -> Path:
    """Which installed version last refreshed the connected agents."""
    return data_dir / "hooks" / "connected.json"


def runtime_python(config_path: Path) -> Path | None:
    """The explicitly connected runtime owns automatic integration refreshes."""
    from varyz.config import load_config

    record = load_config(config_path).data_dir / "hooks" / "runtime.json"
    try:
        value = json.loads(record.read_text(encoding="utf-8")).get("python")
        path = Path(value) if isinstance(value, str) else None
        return path if path is not None and path.is_file() else None
    except (OSError, ValueError, AttributeError):
        return None


def owns_runtime(config_path: Path) -> bool:
    canonical = runtime_python(config_path)
    return canonical is None or same_archive(canonical, Path(sys.executable))


def pin_runtime(config_path: Path) -> None:
    """Explicit connect selects the runtime; old background sessions cannot replace it."""
    from varyz.config import load_config

    path = load_config(config_path).data_dir / "hooks" / "runtime.json"
    payload = json.dumps({"python": os.path.abspath(sys.executable),
                          "instructions": {client: instructions_block(client)
                                           for client in (CODEX_CLIENT, "claude-code")}}) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(path, payload)


def expected_instructions(config_path: Path | None, client: str) -> str:
    """Doctor checks the canonical installation's template, even from a dev checkout."""
    from varyz.config import load_config

    if config_path is not None and runtime_python(config_path) is not None:
        try:
            path = load_config(config_path).data_dir / "hooks" / "runtime.json"
            template = json.loads(path.read_text(encoding="utf-8"))["instructions"][client]
            if isinstance(template, str) and INSTRUCTIONS_START in template:
                return template
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return instructions_block(client)


def refresh_connections_if_changed(
    config_path: Path | None = None,
    *,
    paths: dict[str, Path] | None = None,
    codex_runner: CommandRunner | None = None,
) -> list[InstallResult] | None:
    """Refresh the connected agents once per installed version of Varyz.

    None when this version already did, or another process is doing it now.
    A refresh that failed for any client is tried again next time.
    """
    from varyz.autosync import code_fingerprint
    from varyz.config import load_config
    from varyz.db.connection import FileLock

    resolved = resolve_config_path(config_path)
    if not owns_runtime(resolved):
        return None
    refresh_managed_instructions(resolved, paths=paths)
    stamp = connections_stamp(load_config(resolved).data_dir)
    fingerprint = code_fingerprint()

    def done() -> bool:
        try:
            return json.loads(stamp.read_text(encoding="utf-8")).get("code") == fingerprint
        except (OSError, ValueError, AttributeError):
            return False

    if done():
        return None
    lock = FileLock(stamp.with_suffix(".lock"))
    if not lock.acquire():
        return None
    try:
        if done():
            return None
        results = refresh_connections(resolved, paths=paths, codex_runner=codex_runner)
        if not any(result.detail.startswith("not refreshed") for result in results):
            stamp.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(
                stamp,
                json.dumps({"code": fingerprint, "clients": [r.client for r in results]})
                + "\n",
            )
        return results
    finally:
        lock.release()

"""Plugin adapters reuse the local server and its existing, owner-approved grant.

Starting a plugin never installs software, connects a source or creates a grant.
The plugin's setup skill explicitly invokes the ordinary interactive installer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any

CLIENTS = frozenset({"claude-code", "codex"})


def credential_name(client: str, config: Path) -> str:
    identity = os.path.normcase(str(config.expanduser().absolute()))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"plugin:{client}:{digest}"


def migrate(client: str, config: Path, *, dry_run: bool = False) -> str:
    """Explicitly transfer an existing grant to keyring, then remove only MCP.

    Global briefing hooks and every unrelated client setting remain intact.
    A failed keyring write leaves the existing working registration untouched.
    """
    import keyring

    from varyz.config import load_config
    from varyz.db.connection import open_db
    from varyz.install import (
        _run_codex,
        client_paths,
        registered_archive,
        same_archive,
        write_client_config,
    )
    from varyz.trust import caller_from_token

    if client not in CLIENTS:
        raise ValueError("Plugin client must be claude-code or codex")
    path = client_paths()[client]
    entry = registration(client, path)
    name = credential_name(client, config)
    if not entry:
        if dry_run or keyring.get_password("varyz-plugin", name):
            return "No standalone Varyz MCP registration to migrate"
        raise ValueError("Run Varyz setup and connect this agent before migration")
    registered = registered_archive(entry)
    if registered is None or not same_archive(registered, config):
        raise ValueError("The standalone registration belongs to a different archive")
    environment = entry.get("env", {})
    token = environment.get("VARYZ_AGENT_TOKEN") if isinstance(environment, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("The standalone registration has no owner-approved credential")
    if dry_run:
        return "Would preserve the agent credential in keyring and remove only standalone MCP"
    cfg = load_config(config)
    conn = open_db(cfg.db_path)
    try:
        caller_from_token(conn, token)
    finally:
        conn.close()
    keyring.set_password("varyz-plugin", name, token)
    if keyring.get_password("varyz-plugin", name) != token:
        raise ValueError("Credential store could not preserve the existing grant; nothing removed")
    # Re-read after keyring I/O to avoid dropping a concurrently replaced entry.
    if registration(client, path) != entry:
        raise ValueError("Agent config changed during migration; nothing removed")
    if client == "codex":
        from varyz.install import _write_text_atomic

        original = path.read_text(encoding="utf-8")
        try:
            _run_codex(["mcp", "remove", "varyz"])
        except Exception:
            _write_text_atomic(path, original)
            raise
    else:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("mcpServers", {}).get("varyz") != entry:
            raise ValueError("Agent config changed during migration; nothing removed")
        del data["mcpServers"]["varyz"]
        write_client_config(path, data)
    return "Plugin credential preserved; standalone MCP removed; global hooks kept"


def registration(client: str, path: Path) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError("Plugin client must be claude-code or codex")
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(text) if client == "codex" else json.loads(text)
        servers = data.get("mcp_servers" if client == "codex" else "mcpServers", {})
        entry = servers.get("varyz", {})
        return entry if isinstance(entry, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def serve(client: str, config: Path | None = None) -> None:
    """Reuse a matching registration credential in process; never print it."""
    from varyz.config import find_config_file
    from varyz.install import client_paths, registered_archive, same_archive
    from varyz.mcp_server import serve as serve_mcp

    if client not in CLIENTS:
        raise ValueError("Plugin client must be claude-code or codex")
    entry = registration(client, client_paths()[client])
    registered = registered_archive(entry)
    selected = Path(config) if config else registered or find_config_file(None)
    if selected is None:
        raise ValueError("Run the Varyz plugin setup skill first")
    previous = os.environ.get("VARYZ_AGENT_TOKEN")
    environment = entry.get("env", {})
    token = environment.get("VARYZ_AGENT_TOKEN") if isinstance(environment, dict) else None
    if registered and same_archive(registered, selected) and isinstance(token, str):
        os.environ["VARYZ_AGENT_TOKEN"] = token
    elif not previous:
        import keyring

        try:
            stored = keyring.get_password("varyz-plugin", credential_name(client, selected))
        except keyring.errors.KeyringError:
            stored = None
        if stored:
            os.environ["VARYZ_AGENT_TOKEN"] = stored
    previous_plugin = os.environ.get("VARYZ_PLUGIN_CLIENT")
    os.environ["VARYZ_PLUGIN_CLIENT"] = client
    try:
        serve_mcp(config_path=selected)
    finally:
        if previous_plugin is None:
            os.environ.pop("VARYZ_PLUGIN_CLIENT", None)
        else:
            os.environ["VARYZ_PLUGIN_CLIENT"] = previous_plugin
        if previous is None:
            os.environ.pop("VARYZ_AGENT_TOKEN", None)
        else:
            os.environ["VARYZ_AGENT_TOKEN"] = previous


def global_hook_present(client: str, event: str, settings_path: Path) -> bool:
    """An existing installer hook owns this event, so the plugin stays quiet."""
    if client not in CLIENTS:
        raise ValueError("Plugin client must be claude-code or codex")
    from varyz.install import HOOK_EVENTS

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        event_name = next(name for name, value in HOOK_EVENTS.items() if value == event)
        groups = data.get("hooks", {}).get(event_name, [])
        return any(
            isinstance(hook, dict)
            and any(
                marker in str(hook.get("command", "")) for marker in ("varyz-hook-", "varys-hook-")
            )
            for group in groups
            if isinstance(group, dict)
            for hook in group.get("hooks", [])
        )
    except (OSError, ValueError, AttributeError, StopIteration, TypeError):
        return False


def hook(client: str, event: str, config: Path | None = None) -> int:
    from varyz.hooks import run
    from varyz.install import client_paths, hook_settings_path

    if client not in CLIENTS:
        return 0
    settings = hook_settings_path(client, client_paths()[client])
    existing = [settings] if settings else []
    if client == "claude-code":
        existing.extend(
            [Path.cwd() / ".claude/settings.json", Path.cwd() / ".claude/settings.local.json"]
        )
    if any(global_hook_present(client, event, path) for path in existing):
        return 0
    return run(event, config)

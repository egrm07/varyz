"""How Varyz leaves an MCP client.

This module once held `varyz setup`, the resumable first run that ended in a
Mirror. The Mirror is not part of Varyz any more (see `docs/SHIPPING.md`), and
what is left is used by `varyz uninstall`. The list of managed-model downloads
moved to `varyz.llm.local_install`, which the alpha holds back.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any


def unregister_mcp(
    *,
    dry_run: bool = False,
    only: str | None = None,
    paths: dict[str, Path] | None = None,
    archive: Path | None = None,
    codex_runner: Callable[[list[str]], None] | None = None,
) -> list[Any]:
    """Remove only Varyz's MCP key, preserving every other client setting.

    `archive` is the config this removal is about. A client registration names
    the archive it starts varyz against, and one naming a different archive
    belongs to an install the caller has not asked about: `varyz uninstall`
    run anywhere on the machine used to strip it, so a second archive -- or a
    directory that held no archive at all -- silently unregistered the one the
    reader actually uses. Left None, every varyz registration is removed.
    """

    from varyz.install import (
        CODEX_CLIENT,
        SERVER_KEY,
        InstallResult,
        _load,
        client_paths,
        is_legacy_entry,
        registered_archive,
        same_archive,
        uninstall_away_skill,
        uninstall_codex,
        uninstall_instructions,
        uninstall_reader_agent,
        write_client_config,
    )
    from varyz.legacy import LEGACY_SERVER_KEY

    available = paths if paths is not None else client_paths()
    if only is not None:
        if only not in available:
            raise ValueError(f"Unknown client '{only}'. Known: {', '.join(sorted(available))}")
        available = {only: available[only]}
    results = []
    for client, path in sorted(available.items()):
        if not path.exists():
            continue
        if client == CODEX_CLIENT:
            results.append(
                uninstall_codex(
                    path,
                    dry_run=dry_run,
                    archive=archive,
                    runner=codex_runner,
                )
            )
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
        # The pre-rename entry starts a program that no longer exists.
        legacy = is_legacy_entry(servers.get(LEGACY_SERVER_KEY), archive)
        if SERVER_KEY not in servers and not legacy:
            results.append(InstallResult(client, path, False, f"not registered in {path}"))
            continue
        registered = registered_archive(servers.get(SERVER_KEY))
        if archive is not None and registered is not None and not same_archive(registered, archive):
            results.append(
                InstallResult(
                    client,
                    path,
                    False,
                    f"left alone - {path} registers another archive: {registered}",
                )
            )
            continue
        if dry_run:
            results.append(InstallResult(client, path, False, f"would remove from {path}"))
            continue
        backup = path.with_suffix(path.suffix + ".varyz-uninstall-backup")
        shutil.copyfile(path, backup)
        servers.pop(SERVER_KEY, None)
        if legacy:
            del servers[LEGACY_SERVER_KEY]
        data["mcpServers"] = servers
        write_client_config(path, data)
        uninstall_away_skill(client, path)
        uninstall_reader_agent(client, path)
        uninstall_instructions(client, path)
        results.append(InstallResult(client, path, True, f"removed from {path}"))
    return results

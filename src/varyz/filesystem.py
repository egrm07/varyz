"""Atomic filesystem publication with bounded Windows sharing-lock retries."""

from __future__ import annotations

import contextlib
import os
import stat
import time
from pathlib import Path
from typing import Any


def atomic_replace(source: Path, destination: Path) -> None:
    """Keep staging intact until one atomic rename succeeds.

    Windows scanners can briefly hold a newly written file or directory open. Retry
    only access/sharing/lock errors, never replace them with a partial copy or
    remove an existing destination. Persistent failures still reach the caller.
    """
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                raise
            time.sleep(0.05 * 2**attempt)


def publish_directory(source: Path, destination: Path) -> None:
    """Publish a staged directory with bounded sharing-lock retries."""
    atomic_replace(source, destination)


def _private_targets(cfg: Any) -> list[tuple[Path, int]]:
    """The archive's folders and the config, with the mode each should have."""
    targets = [(Path(cfg.data_dir), 0o700)]
    config = cfg.source_file
    if config is not None:
        targets.append((Path(config), 0o600))
        home_folder = Path.home() / ".varyz"
        if Path(config).parent == home_folder:
            targets.append((home_folder, 0o700))
    return targets


def exposed(cfg: Any) -> list[Path]:
    """Varyz files other accounts on this computer can read. None on Windows,
    where the profile folder is already private."""
    if os.name == "nt":
        return []
    found = []
    for path, mode in _private_targets(cfg):
        with contextlib.suppress(OSError):
            info = path.stat()
            if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & ~mode:
                found.append(path)
    return found


def make_private(cfg: Any) -> list[Path]:
    """Close the archive and config to other accounts; return what changed.

    On the test Mac ~/.varyz, the archive and the config were readable by
    every local account (755 and 644). Only files this account owns are
    changed, and only by removing access for others.
    """
    changed = []
    for path in exposed(cfg):
        mode = dict(_private_targets(cfg))[path]
        with contextlib.suppress(OSError):
            path.chmod(stat.S_IMODE(path.stat().st_mode) & mode)
            changed.append(path)
    return changed


def claude_config_exposed() -> Path | None:
    """~/.claude.json holds the agent's MCP settings, Varyz's token among them."""
    path = Path.home() / ".claude.json"
    if os.name == "nt":
        return None
    try:
        return path if stat.S_IMODE(path.stat().st_mode) & 0o077 else None
    except OSError:
        return None

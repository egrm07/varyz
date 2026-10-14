"""Atomic filesystem publication with bounded Windows sharing-lock retries."""

from __future__ import annotations

import os
import time
from pathlib import Path


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

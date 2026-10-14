"""How much room is left, and how to say it.

Nothing in the package used to ask. `varyz backup` copied a 74 GB database
onto whatever was free, and the managed-model install downloaded 7.4 GB the
same way: both found out by running out. The sizes involved are all known
before the first byte is written, so the question is worth asking, and the
answer is worth printing with the numbers in it.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def free_bytes(path: Path) -> int | None:
    """Free space on the disk `path` will live on, or None if it cannot be read.

    The path itself often does not exist yet -- it is the directory a backup or
    a download is about to create -- so the nearest existing ancestor answers
    for it.
    """
    probe = path
    while True:
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            parent = probe.parent
            if parent == probe:
                return None
            probe = parent


def directory_bytes(path: Path) -> int:
    """Size of everything under a directory, skipping what cannot be measured."""
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            # A file that vanished or refuses a stat between the walk and the
            # measurement is one file out of an estimate, not a reason to
            # refuse the work the estimate was for.
            continue
    return total


def bytes_label(count: int) -> str:
    """A byte count a person can read, in the units disks are sold in."""
    for unit, size in (("GB", 1000**3), ("MB", 1000**2), ("kB", 1000)):
        if count >= size:
            return f"{count / size:.1f} {unit}"
    return f"{count} bytes"

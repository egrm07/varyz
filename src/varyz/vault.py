"""Content-addressed vault for immutable original artifacts.

Layout: vault/ab/cd/<sha256><ext>. A vault ref is the path relative to the
vault root ('ab/cd/<sha256>.eml'), stored in entries.vault_ref. Files are
never mutated; deletion happens only through an explicit `varyz forget`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class Vault:
    def __init__(self, root: Path):
        self.root = root

    def put(self, payload: bytes, ext: str) -> str:
        """Store payload if new; return its vault ref. Idempotent."""
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        digest = hashlib.sha256(payload).hexdigest()
        ref = f"{digest[:2]}/{digest[2:4]}/{digest}{ext}"
        target = self.root / ref
        if target.exists():
            return ref
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, target)  # atomic on same volume
        return ref

    def path(self, ref: str) -> Path:
        return self.root / ref

    def has(self, ref: str) -> bool:
        return self.path(ref).exists()

    def remove(self, ref: str) -> None:
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            self.path(ref).unlink()

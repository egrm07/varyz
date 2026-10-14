"""Local project exclusions, shared by every Cloud sync lane."""

from __future__ import annotations

import json
from pathlib import Path

from varyz.cloud.local_sync import _sync_lock, save_journal
from varyz.scopes import normalize_scope


def policy_path(config: Path) -> Path:
    return config.with_name(config.stem + ".cloud-project-policy.json")


def exclusions(config: Path) -> set[str]:
    path = policy_path(config)
    if not path.exists():
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    names = value.get("excluded") if isinstance(value, dict) else None
    if not isinstance(names, list) or any(not isinstance(p, str) for p in names):
        raise ValueError("Invalid Cloud project exclusions; restore the policy backup")
    return {normalize_scope(p) for p in names}


def journal_exclusions(path: Path) -> set[str]:
    for suffix in (".cloud-sync.json", ".cloud-archive.json", ".cloud-reports.json"):
        if path.name.endswith(suffix):
            return exclusions(path.with_name(path.name.removesuffix(suffix) + ".toml"))
    return set()


def exclude(config: Path, projects: list[str]) -> None:
    names = {normalize_scope(p) for p in projects}
    if "*" in names or not names:
        raise ValueError("Exclude exact named projects, not General")
    path = policy_path(config)
    with _sync_lock(path.with_suffix(".lock")):
        save_journal(path, {"excluded": sorted(exclusions(config) | names)})

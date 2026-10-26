"""Rule packs other people sent, kept on this computer once they are used.

The first time a task asks for a pack ("use Egor's design pack"), the local Varyz
saves all of its rules as one JSON file in the data folder's shared-packs/. The
folder sits apart from the owner's own archive: nothing in it is read as the
owner's rules or learned from. The briefing points the agent at the file, so a
long pack is read there instead of page by page, and the saved copy still answers
when Varyz Cloud cannot be reached. A newer version replaces the file; a pack the
sender stopped sharing, or the person removed, is deleted at the next check.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from varyz.config import load_config

from . import pack_words

FOLDER = "shared-packs"
PAGES = 50  # Cloud pages read for one pack; a pack holds at most 128 KiB.
BUDGET = 6500  # Characters of rules per briefing page, as on Cloud.
OFFLINE = (
    "Varyz Cloud could not be reached, so these come from the copies saved on this "
    "computer. The sender may have changed or stopped sharing them since."
)
_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")

Read = Callable[[str, int], dict[str, Any]]


def folder(config: Path) -> Path:
    return load_config(config).data_dir / FOLDER


def path_for(where: Path, pack_id: str) -> Path:
    if not _ID.fullmatch(pack_id):
        raise ValueError("Not a pack id: " + pack_id[:40])
    return where / (pack_id + ".json")


def saved(where: Path) -> dict[str, dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    if not where.is_dir():
        return packs
    for path in sorted(where.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("pack_id") == path.stem:
            packs[path.stem] = value
    return packs


def keep(config: Path, value: dict[str, Any], read: Read | None, *, wait: bool = False) -> None:
    """After a Cloud answer: save used packs, refresh changed ones, drop unshared ones.

    value is a follow list or brief; read(pack_id, offset) is one Cloud pack page.
    Downloads run in the background so a briefing never waits on them; a pack's
    local_copy is named once its current version is saved.
    """
    where = folder(config)
    received = value.get("received") or []
    have = saved(where)
    if len(received) < pack_words.SUMMARY:  # A full list may leave older packs out.
        current = {entry.get("pack_id") for entry in received}
        for pack_id in set(have) - current:
            path_for(where, pack_id).unlink(missing_ok=True)
    versions = {entry.get("pack_id"): entry.get("updated") for entry in received}
    groups = value.get("packs") or []
    stale = [
        str(group.get("pack_id") or "") for group in groups
        if have.get(group.get("pack_id"), {}).get("updated", False)
        != versions.get(group.get("pack_id"))
    ]
    if stale and read is not None:
        worker = threading.Thread(
            target=_download_all, args=(where, stale, read), daemon=True,
            name="varyz-shared-packs",
        )
        worker.start()
        if wait:
            worker.join()
            stale = [pack_id for pack_id in stale if not path_for(where, pack_id).exists()]
    for group in groups:
        if group.get("pack_id") not in stale:
            group["local_copy"] = str(path_for(where, str(group.get("pack_id"))))
    if value.get("packs_more") and groups and not stale:
        value["packs_more"] = "The whole pack is saved at local_copy; read the rest there."


def _download_all(where: Path, pack_ids: list[str], read: Read) -> None:
    for pack_id in pack_ids:
        with contextlib.suppress(Exception):  # The next use tries again.
            download(where, pack_id, read)


def download(where: Path, pack_id: str, read: Read) -> dict[str, Any]:
    """All of one pack's rules, written only when every page arrived."""
    path = path_for(where, pack_id)
    rules: list[Any] = []
    about: dict[str, Any] = {}
    policy = None
    offset = 0
    for _ in range(PAGES):
        page = read(pack_id, offset)
        group = (page.get("packs") or [{}])[0]
        about = about or page.get("pack") or {}
        policy = policy or group.get("policy")
        rules += group.get("rules") or []
        if page.get("next_offset") is None:
            break
        offset = int(page["next_offset"])
    else:
        raise ValueError("Pack " + pack_id + " is longer than expected")
    if not about.get("creator") or not rules:
        raise ValueError("Pack " + pack_id + " came back incomplete")
    value = {
        "pack_id": pack_id,
        **{key: about.get(key) for key in ("creator", "from", "name", "title", "updated", "say")},
        "label": pack_words.LABEL,
        "policy": policy,
        "rules": rules,
        "saved": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    where.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + "." + str(threading.get_ident()) + ".tmp")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(partial, path)
    return value


def brief(config: Path, task: str) -> dict[str, Any] | None:
    """The packs a task asks for, from the saved copies, when Cloud cannot answer."""
    where = folder(config)
    packs = [
        {
            "id": pack_id,
            "owner": value.get("creator"),
            "creator": value.get("creator") or "",
            "from": value.get("from") or value.get("creator") or "",
            "name": value.get("name") or "",
            "title": value.get("title") or "",
            "say": value.get("say") or "",
            "policy": value.get("policy"),
            "rules": value.get("rules") or [],
        }
        for pack_id, value in saved(where).items()
    ]
    asked, unclear = pack_words.activated(packs, task)
    if not asked and not unclear:
        return None
    result: dict[str, Any] = {"packs_label": pack_words.LABEL, "offline": OFFLINE}
    if asked:
        result["packs"] = [group(pack, path_for(where, pack["id"])) for pack in asked]
    if unclear:
        result["unresolved_packs"] = [
            {"pack_id": p["id"], "from": sender(p), "title": p["title"], "say": p["say"]}
            for p in unclear
        ]
        result["unresolved_packs_how"] = (
            "Several people sent a pack with this name. Ask the owner whose to use."
        )
    return result


def sender(pack: dict[str, Any]) -> str:
    return pack_words.who(pack)


def group(pack: dict[str, Any], path: Path) -> dict[str, Any]:
    """One saved pack for a briefing, cut at the page budget and never mid-rule."""
    value: dict[str, Any] = {
        "pack_id": pack["id"],
        "from": sender(pack),
        "title": pack["title"],
        "policy": pack["policy"],
        "rules": [],
        "local_copy": str(path),
    }
    used = 0
    for rule in pack["rules"]:
        size = len(json.dumps(rule, ensure_ascii=False))
        if used + size > BUDGET and used:
            value["more"] = "Read the rest of the rules in local_copy."
            break
        value["rules"].append(rule)
        used += size
    return value

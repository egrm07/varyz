"""Last confirmed Cloud access state; no tokens, account text or sync payloads."""

from __future__ import annotations

import contextlib
import json
import time

from varyz.cloud.access import notice
from varyz.filesystem import atomic_replace


def _identity(config):
    from varyz.cloud.local_sync import read_journal

    journal = read_journal(config.with_name(config.stem + ".cloud-sync.json"))
    if journal.get("url") and journal.get("account_id"):
        return {key: journal[key] for key in ("url", "account_id")}
    return None


def _path(config):
    return config.with_name(config.stem + ".cloud-access.json")


def read(config):
    try:
        state = json.loads(_path(config).read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("identity") == _identity(config):
            return state
    except (OSError, ValueError, TypeError):
        pass
    return {}


def observe(config, access):
    """Keep an expiry notice through outages; clear it only on confirmed recovery."""
    from varyz.db.connection import FileLock

    identity = _identity(config)
    if identity is None or not isinstance(access, dict) or type(access.get("active")) is not bool:
        return {}
    path = _path(config)
    lock = FileLock(path.with_suffix(".lock"))
    if not lock.acquire():
        return read(config)
    try:
        old = read(config)
        normalized = {"active": True} if access["active"] else notice(access.get("reason"))
        state = {"identity": identity, "access": normalized, "checked_at": time.time()}
        if old.get("access", {}).get("reason") == normalized.get("reason"):
            state["notified"] = bool(old.get("notified"))
        _save(path, state)
        return state
    finally:
        lock.release()


def _save(path, state):
    import uuid

    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(state), encoding="utf-8")
        atomic_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check(config, *, client=None, max_age=60):
    """An already connected account only; failures never imply an expired plan."""
    from varyz.cloud.client import CloudClient, credential_name, load_tokens

    state = read(config)
    if time.time() - state.get("checked_at", 0) < max_age:
        return state
    own = client is None
    try:
        identity = _identity(config)
        if identity is None:
            return {}
        if client is None:
            name = credential_name(config, identity["url"])
            client = CloudClient(identity["url"], load_tokens(name), credential=name)
        account = client.request("GET", "/api/account", timeout=2)
        if account.get("id") != identity["account_id"]:
            return state
        access = account.get("cloud_access")
        if not isinstance(access, dict) and type(account.get("entitlement")) is bool:
            from varyz.cloud.access import account_access

            access = account_access({"status": "active" if account["entitlement"]
                                     else account.get("status", "inactive")})
        return observe(config, access)
    except Exception:
        return state
    finally:
        if own and client is not None:
            with contextlib.suppress(Exception):
                client.close()


def briefing_notice(config):
    from varyz.config import find_config_file

    config = find_config_file(config)
    if config is None:
        return None
    access = check(config).get("access", {})
    return access if access.get("active") is False else None


def mark_notified(config):
    from varyz.db.connection import FileLock

    path = _path(config)
    lock = FileLock(path.with_suffix(".lock"))
    if not lock.acquire():
        return
    try:
        state = read(config)
        if state.get("access", {}).get("active") is False:
            state["notified"] = True
            _save(path, state)
    finally:
        lock.release()

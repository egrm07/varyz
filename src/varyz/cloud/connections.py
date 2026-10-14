"""Human connection identities, separate from OAuth registrations and tokens."""

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse


def device_info(value: str) -> dict[str, str]:
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data[key])[:120] for key in ("name", "platform", "alias") if data.get(key)}


def remember_device(store: Any, credential: Mapping[str, Any], headers: Mapping[str, str]) -> None:
    """Only the local sync client reports its computer name; never infer from an IP."""
    value = headers.get("x-varyz-computer", "")
    if credential["client"] != "varyz-local" or len(value) > 1024:
        return
    info = device_info(value)
    if not info.get("name"):
        return
    saved = device_info(credential["redirect"])
    if saved.get("alias"):
        info["alias"] = saved["alias"]
    if saved == info:
        return
    with store.connection() as db:
        db.execute(
            "UPDATE credentials SET redirect=? WHERE account=? AND client=? "
            "AND challenge=? AND kind IN ('access','refresh')",
            (
                json.dumps(info),
                credential["account"],
                credential["client"],
                credential["challenge"],
            ),
        )


def connection_groups(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        client = row.get("client_id", row.get("client", ""))
        info = device_info(row.get("redirect", ""))
        redirects = json.loads(row["redirects"]) if row.get("redirects") else []
        destinations = []
        for uri in redirects:
            parsed = urlparse(uri)
            host = parsed.hostname or ""
            destination = "Desktop apps"
            if parsed.scheme == "https":
                destination = host
                for domain, brand in (
                    ("chatgpt.com", "ChatGPT"),
                    ("chat.openai.com", "ChatGPT"),
                    ("claude.ai", "Claude"),
                ):
                    if host == domain or host.endswith("." + domain):
                        destination = brand
                        break
            elif parsed.scheme != "http":
                destination = f"Desktop app ({parsed.scheme})"
            if destination not in destinations:
                destinations.append(destination)
        if client == "varyz-local":
            label = info.get("alias") or info.get("name", "Unidentified sign-ins")
            category = "computers"
            description = "Varyz syncs the preferences and project history you approve."
            detail = (
                info.get("platform", "") + " · Name reported by Varyz on this computer."
                if info.get("name")
                else "These sign-ins have not reported a computer name. "
                "They may belong to a computer listed above. Name one to identify it."
            )
            # Group missing identities as sign-ins, without asserting a device count.
            key = (category, label)
            if not info:
                description = "Varyz connections with an unknown computer name."
        else:
            category = "apps"
            label = "Desktop apps"
            description = "Apps authorized through a browser callback on a computer."
            detail = (
                "The app and device were not reported. "
                "A local callback does not identify a computer."
            )
            label = ", ".join(destinations) or label
            if label in ("ChatGPT", "Claude"):
                description = f"{label} uses your approved Cloud context."
                detail = (
                    "Connected to your AI account. "
                    "The phone or computer used is not shared with Varyz."
                )
            elif label != "Desktop apps":
                description = "An app connected to your Cloud account."
                detail = "Identified by its registered callback destination; device unavailable."
            key = (category, label)
        if info.get("alias"):
            label = info["alias"]
            detail += " You named this connection."
            key = (category, label)
        group = groups.setdefault(
            key,
            {
                "category": category,
                "label": label,
                "description": description,
                "detail": detail,
                "sessions": [],
            },
        )
        auth_time = row.get("authenticated_at", "")
        session_key = (client, auth_time)
        if any((s["client_id"], s["authenticated_at"]) == session_key for s in group["sessions"]):
            continue
        try:
            date = time.strftime("%d %b %Y, %H:%M UTC", time.gmtime(float(auth_time)))
        except (ValueError, TypeError, OverflowError, OSError):
            date = "Date unavailable"
        scope = row.get("scope") or "varyz"
        access = {
            "varyz.archive.read": "Read approved archive only",
            "varyz.context.read": "Read preferences and approved history",
            "varyz.plugin": "Read and update preferences; read approved history",
            "varyz.shared": "Read shared preferences and ask their owners",
            "varyz": "Read and update preferences",
        }.get(scope, scope)
        group["sessions"].append(
            {"client_id": client, "authenticated_at": auth_time, "date": date, "access": access}
        )
    return sorted(
        groups.values(),
        key=lambda g: (g["category"] != "computers", g["label"] == "Unidentified sign-ins"),
    )

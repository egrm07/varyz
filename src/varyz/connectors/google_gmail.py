"""Gmail API transport used by the canonical ``gmail`` connector."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

from varyz.connectors.google_auth import GoogleAPI, GoogleAPIError
from varyz.domain import Cursor, RawItem

MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
HISTORY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def discover_gmail_api(
    options: dict[str, Any], cursor: Cursor | None, *, connector: Any
) -> Iterator[tuple[RawItem, Cursor | None]]:
    account = str(options.get("account", ""))
    api = GoogleAPI(account, base_dir=_base_dir(options))
    state = dict(cursor.data) if cursor else {}
    phase = state.get("phase", "full")
    if phase == "incremental" and state.get("history_id"):
        try:
            yield from _incremental(api, state, connector)
            return
        except GoogleAPIError as exc:
            if exc.status_code != 404:
                raise
            # Gmail history IDs expire; the official recovery is a full sync.
            state = {}
    yield from _full(api, options, state, connector)


def _full(
    api: GoogleAPI, options: dict[str, Any], state: dict[str, Any], connector: Any
) -> Iterator[tuple[RawItem, Cursor | None]]:
    profile = api.json(PROFILE_URL)
    target_history_id = str(profile.get("historyId", ""))
    page_token = state.get("page_token")
    skip = int(state.get("index", 0))
    while True:
        params: dict[str, Any] = {
            "maxResults": int(options.get("batch_size", 100)),
            "includeSpamTrash": "false",
        }
        if options.get("since"):
            params["q"] = f"after:{str(options['since']).replace('-', '/')}"
        if page_token:
            params["pageToken"] = page_token
        page = api.json(MESSAGES_URL, params=params)
        messages = page.get("messages", [])
        next_token = page.get("nextPageToken")
        for index, ref in enumerate(messages):
            if index < skip:
                continue
            message = api.json(f"{MESSAGES_URL}/{ref['id']}", params={"format": "raw"})
            new_state: dict[str, Any]
            is_last = index == len(messages) - 1 and not next_token
            if is_last:
                new_state = {
                    "phase": "incremental",
                    "history_id": target_history_id or str(message.get("historyId", "")),
                }
            else:
                new_state = {
                    "phase": "full",
                    "page_token": page_token,
                    "index": index + 1,
                }
            new_cursor = Cursor(data=new_state)
            connector.checkpoint = new_cursor
            yield _raw_message(message, api.account), new_cursor
        if not next_token:
            connector.checkpoint = Cursor(
                data={"phase": "incremental", "history_id": target_history_id}
            )
            return
        page_token = next_token
        skip = 0


def _incremental(
    api: GoogleAPI, state: dict[str, Any], connector: Any
) -> Iterator[tuple[RawItem, Cursor | None]]:
    page_token = state.get("page_token")
    skip = int(state.get("index", 0))
    start_history_id = str(state["history_id"])
    while True:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        page = api.json(HISTORY_URL, params=params)
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for history in page.get("history", []):
            for added in history.get("messagesAdded", []):
                ref = added.get("message", {})
                if ref.get("id") and ref["id"] not in seen:
                    refs.append(ref)
                    seen.add(ref["id"])
        next_token = page.get("nextPageToken")
        latest_history_id = str(page.get("historyId", start_history_id))
        for index, ref in enumerate(refs):
            if index < skip:
                continue
            message = api.json(f"{MESSAGES_URL}/{ref['id']}", params={"format": "raw"})
            is_last = index == len(refs) - 1 and not next_token
            new_state = (
                {"phase": "incremental", "history_id": latest_history_id}
                if is_last
                else {
                    "phase": "incremental",
                    "history_id": start_history_id,
                    "page_token": page_token,
                    "index": index + 1,
                }
            )
            new_cursor = Cursor(data=new_state)
            connector.checkpoint = new_cursor
            yield _raw_message(message, api.account), new_cursor
        if not next_token:
            connector.checkpoint = Cursor(
                data={"phase": "incremental", "history_id": latest_history_id}
            )
            return
        page_token = next_token
        skip = 0


def _raw_message(message: dict[str, Any], account: str) -> RawItem:
    from varyz.connectors.gmail_imap import _external_id

    encoded = str(message["raw"])
    encoded += "=" * (-len(encoded) % 4)
    raw = base64.urlsafe_b64decode(encoded)
    return RawItem(
        external_id=_external_id(raw, account, "api", 0, str(message["id"])),
        payload=raw,
        payload_ext=".eml",
        metadata={"internaldate": None, "gmail_id": message["id"]},
    )


def _base_dir(options: dict[str, Any]):
    from pathlib import Path

    return Path(options["_base_dir"]) if options.get("_base_dir") else None

"""Google Calendar and Drive read-only connectors."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from varyz.connectors.base import register
from varyz.connectors.documents import EXTENSIONS as DOCUMENT_EXTENSIONS
from varyz.connectors.documents import extract_document_text
from varyz.connectors.google_auth import GoogleAPI, GoogleAPIError
from varyz.domain import Cursor, Kind, NormalizedEntry, RawItem, TimePrecision

CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3/calendars"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_CHANGES_URL = "https://www.googleapis.com/drive/v3/changes"
DRIVE_START_TOKEN_URL = "https://www.googleapis.com/drive/v3/changes/startPageToken"


class _GoogleConnector:
    checkpoint: Cursor | None = None

    def _api(self, options: dict[str, Any]) -> GoogleAPI:
        account = str(options.get("account", ""))
        if not account:
            raise ValueError(f"{self.connector_id} needs an account")
        base_dir = Path(options["_base_dir"]) if options.get("_base_dir") else None
        return GoogleAPI(account, base_dir=base_dir)


@register
class GoogleCalendarConnector(_GoogleConnector):
    connector_id: ClassVar[str] = "google_calendar"

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        api = self._api(options)
        state = dict(cursor.data) if cursor else {}
        tokens = dict(state.get("sync_tokens", {}))
        calendars = _calendar_list(api)
        active_id = state.get("active_calendar")
        active_index = next(
            (index for index, item in enumerate(calendars) if item["id"] == active_id),
            0,
        )
        for calendar_index, calendar in enumerate(calendars[active_index:], active_index):
            calendar_id = str(calendar["id"])
            page_token = (
                state.get("page_token") if state.get("active_calendar") == calendar_id else None
            )
            skip = (
                int(state.get("index", 0))
                if state.get("active_calendar") == calendar_id
                else 0
            )
            retried_full = False
            while True:
                params: dict[str, Any] = {
                    "maxResults": 2500,
                    "singleEvents": "true",
                    "showDeleted": "true",
                }
                sync_token = tokens.get(calendar_id)
                if sync_token:
                    params["syncToken"] = sync_token
                elif options.get("since"):
                    params["timeMin"] = _since_iso(str(options["since"]))
                if page_token:
                    params["pageToken"] = page_token
                try:
                    page = api.json(
                        f"{CALENDAR_BASE_URL}/{quote(calendar_id, safe='')}/events",
                        params=params,
                    )
                except GoogleAPIError as exc:
                    if exc.status_code == 410 and sync_token and not retried_full:
                        tokens.pop(calendar_id, None)
                        page_token = None
                        skip = 0
                        retried_full = True
                        continue
                    raise
                events = page.get("items", [])
                next_token = page.get("nextPageToken")
                next_sync = page.get("nextSyncToken")
                for index, event in enumerate(events):
                    if index < skip:
                        continue
                    next_tokens = dict(tokens)
                    is_last_page = not next_token
                    if is_last_page and next_sync:
                        next_tokens[calendar_id] = next_sync
                    new_state = {
                        "sync_tokens": next_tokens,
                        "active_calendar": calendar_id,
                        "page_token": page_token,
                        "index": index + 1,
                    }
                    if index == len(events) - 1 and is_last_page:
                        new_state = {
                            "sync_tokens": next_tokens,
                            "active_calendar": (
                                calendars[calendar_index + 1]["id"]
                                if calendar_index + 1 < len(calendars)
                                else None
                            ),
                        }
                    new_cursor = Cursor(data=new_state)
                    self.checkpoint = new_cursor
                    payload = {
                        "calendar": {
                            "id": calendar_id,
                            "summary": calendar.get("summary", calendar_id),
                        },
                        "event": event,
                    }
                    yield (
                        RawItem(
                            external_id=f"{calendar_id}:{event['id']}",
                            payload=json.dumps(payload, ensure_ascii=False),
                            payload_ext=".json",
                            metadata=payload,
                        ),
                        new_cursor,
                    )
                if not next_token:
                    if next_sync:
                        tokens[calendar_id] = next_sync
                    break
                page_token = next_token
                skip = 0
            state = {"sync_tokens": tokens}
        self.checkpoint = Cursor(data={"sync_tokens": tokens})

    def normalize(self, item: RawItem) -> NormalizedEntry:
        payload = item.metadata or json.loads(item.payload_bytes())
        event = payload["event"]
        start, precision, timezone = _google_event_time(event.get("start", {}))
        end, _, _ = _google_event_time(event.get("end", {}))
        lines = [f"Calendar: {payload['calendar']['summary']}"]
        for label, key in (
            ("Location", "location"),
            ("Description", "description"),
            ("Status", "status"),
            ("Organizer", "organizer"),
            ("Video call", "hangoutLink"),
        ):
            value = event.get(key)
            if isinstance(value, dict):
                value = value.get("displayName") or value.get("email")
            if value:
                lines.append(f"{label}: {value}")
        attendees = [
            item.get("displayName") or item.get("email")
            for item in event.get("attendees", [])
            if item.get("displayName") or item.get("email")
        ]
        if attendees:
            lines.append("Attendees: " + ", ".join(attendees))
        return NormalizedEntry(
            kind=Kind.CALENDAR_EVENT,
            external_id=item.external_id,
            title=event.get("summary") or "(untitled event)",
            body="\n".join(lines),
            occurred_start=start,
            occurred_end=end,
            tz=timezone,
            captured_at=_parse_iso(event.get("created")),
            time_precision=precision,
            thread_id=event.get("recurringEventId"),
        )


@register
class GoogleDriveConnector(_GoogleConnector):
    connector_id: ClassVar[str] = "google_drive"

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        api = self._api(options)
        state = dict(cursor.data) if cursor else {}
        if state.get("phase") == "changes" and state.get("change_token"):
            yield from self._changes(api, options, state)
        else:
            yield from self._full(api, options, state)

    def _full(
        self, api: GoogleAPI, options: dict[str, Any], state: dict[str, Any]
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        start_token = state.get("initial_change_token")
        if not start_token:
            start_token = api.json(DRIVE_START_TOKEN_URL).get("startPageToken")
        page_token = state.get("page_token")
        skip = int(state.get("index", 0))
        while True:
            params: dict[str, Any] = {
                "pageSize": 1000,
                "q": "trashed = false",
                "orderBy": "modifiedTime",
                "fields": (
                    "nextPageToken,files(id,name,mimeType,modifiedTime,createdTime,"
                    "description,webViewLink,size,md5Checksum,trashed)"
                ),
            }
            if page_token:
                params["pageToken"] = page_token
            page = api.json(DRIVE_FILES_URL, params=params)
            files = page.get("files", [])
            next_page = page.get("nextPageToken")
            for index, file in enumerate(files):
                if index < skip:
                    continue
                is_last = index == len(files) - 1 and not next_page
                new_state = (
                    {"phase": "changes", "change_token": start_token}
                    if is_last
                    else {
                        "phase": "full",
                        "initial_change_token": start_token,
                        "page_token": page_token,
                        "index": index + 1,
                    }
                )
                new_cursor = Cursor(data=new_state)
                self.checkpoint = new_cursor
                raw = _drive_item(api, file, options)
                if raw:
                    yield raw, new_cursor
            if not next_page:
                self.checkpoint = Cursor(
                    data={"phase": "changes", "change_token": start_token}
                )
                return
            page_token = next_page
            skip = 0

    def _changes(
        self, api: GoogleAPI, options: dict[str, Any], state: dict[str, Any]
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        round_start = str(state["change_token"])
        page_token = str(state.get("page_token") or round_start)
        skip = int(state.get("index", 0))
        while True:
            page = api.json(
                DRIVE_CHANGES_URL,
                params={
                    "pageToken": page_token,
                    "pageSize": 1000,
                    "includeRemoved": "true",
                    "fields": (
                        "nextPageToken,newStartPageToken,"
                        "changes(removed,fileId,file(id,name,mimeType,modifiedTime,"
                        "createdTime,description,webViewLink,size,md5Checksum,trashed))"
                    ),
                },
            )
            changes = page.get("changes", [])
            next_page = page.get("nextPageToken")
            new_start = page.get("newStartPageToken")
            for index, change in enumerate(changes):
                if index < skip:
                    continue
                file = change.get("file") or {
                    "id": change.get("fileId"),
                    "name": "Deleted Drive item",
                    "mimeType": "application/octet-stream",
                    "trashed": True,
                }
                is_last = index == len(changes) - 1 and not next_page
                new_state = (
                    {"phase": "changes", "change_token": new_start or round_start}
                    if is_last
                    else {
                        "phase": "changes",
                        "change_token": round_start,
                        "page_token": page_token,
                        "index": index + 1,
                    }
                )
                new_cursor = Cursor(data=new_state)
                self.checkpoint = new_cursor
                raw = _drive_item(api, file, options)
                if raw:
                    yield raw, new_cursor
            if not next_page:
                self.checkpoint = Cursor(
                    data={"phase": "changes", "change_token": new_start or round_start}
                )
                return
            page_token = next_page
            skip = 0

    def normalize(self, item: RawItem) -> NormalizedEntry:
        metadata = item.metadata
        text = extract_document_text(item.payload_bytes(), item.payload_ext)
        lines = []
        if metadata.get("description"):
            lines.append(str(metadata["description"]))
        if metadata.get("webViewLink"):
            lines.append(f"Drive: {metadata['webViewLink']}")
        if metadata.get("trashed"):
            lines.append("Status: deleted or trashed upstream")
        if text.strip():
            lines.extend(["", text.strip()])
        occurred = _parse_iso(metadata.get("modifiedTime"))
        return NormalizedEntry(
            kind=Kind.DOCUMENT,
            external_id=item.external_id,
            title=metadata.get("name") or "Drive document",
            body="\n".join(lines).strip(),
            occurred_start=occurred,
            captured_at=_parse_iso(metadata.get("createdTime")),
            time_precision=TimePrecision.EXACT if occurred else TimePrecision.UNKNOWN,
            significance=0.55,
        )


def _calendar_list(api: GoogleAPI) -> list[dict[str, Any]]:
    calendars: list[dict[str, Any]] = []
    page_token = None
    while True:
        params = {"maxResults": 250}
        if page_token:
            params["pageToken"] = page_token
        page = api.json(CALENDAR_LIST_URL, params=params)
        calendars.extend(item for item in page.get("items", []) if not item.get("deleted"))
        page_token = page.get("nextPageToken")
        if not page_token:
            return calendars


def _google_event_time(
    value: dict[str, Any],
) -> tuple[datetime | None, TimePrecision, str | None]:
    timezone = value.get("timeZone")
    if value.get("dateTime"):
        return _parse_iso(value["dateTime"]), TimePrecision.EXACT, timezone
    if value.get("date"):
        parsed = date.fromisoformat(value["date"])
        return datetime.combine(parsed, time.min, tzinfo=UTC), TimePrecision.DAY, timezone
    return None, TimePrecision.UNKNOWN, timezone


def _since_iso(value: str) -> str:
    """The first-backfill bound, or say which line of config is not a date.

    fromisoformat raises about isoformat strings, which names neither the
    setting that was typed nor the file it was typed into.
    """

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"since = {value!r} in [connectors.google_calendar] is not a date. "
            "Write it as YYYY-MM-DD."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _max_bytes(options: dict[str, Any]) -> int:
    """The size ceiling for a downloaded file, or which setting is not a size."""

    raw = options.get("max_size_mb", 25)
    try:
        megabytes = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"max_size_mb = {raw!r} in [connectors.google_drive] is not a number."
        ) from None
    if megabytes <= 0:
        raise ValueError(
            f"max_size_mb in [connectors.google_drive] must be greater than zero, not {raw!r}."
        )
    return int(megabytes * 1024 * 1024)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _drive_item(
    api: GoogleAPI, metadata: dict[str, Any], options: dict[str, Any]
) -> RawItem | None:
    if not metadata.get("id") or metadata.get("mimeType") == "application/vnd.google-apps.folder":
        return None
    mime = str(metadata.get("mimeType", ""))
    file_id = str(metadata["id"])
    max_bytes = _max_bytes(options)
    raw = b""
    ext = ".txt"
    try:
        if mime == "application/vnd.google-apps.document":
            raw = api.bytes(
                f"{DRIVE_FILES_URL}/{quote(file_id, safe='')}/export",
                params={"mimeType": "text/plain"},
            )
            ext = ".txt"
        elif mime == "application/vnd.google-apps.spreadsheet":
            raw = api.bytes(
                f"{DRIVE_FILES_URL}/{quote(file_id, safe='')}/export",
                params={"mimeType": "text/csv"},
            )
            ext = ".csv"
        elif mime == "application/vnd.google-apps.presentation":
            raw = api.bytes(
                f"{DRIVE_FILES_URL}/{quote(file_id, safe='')}/export",
                params={"mimeType": "text/plain"},
            )
            ext = ".txt"
        elif (
            Path(str(metadata.get("name", ""))).suffix.lower() in DOCUMENT_EXTENSIONS
            or mime.startswith("text/")
            or mime in {"application/json", "application/rtf"}
        ) and int(metadata.get("size", 0) or 0) <= max_bytes:
            raw = api.bytes(
                f"{DRIVE_FILES_URL}/{quote(file_id, safe='')}",
                params={"alt": "media"},
            )
            ext = _mime_extension(mime, str(metadata.get("name", "")))
    except GoogleAPIError:
        # Metadata remains useful and searchable when export/download is blocked.
        raw = b""
        ext = ".txt"
    return RawItem(
        external_id=file_id,
        payload=raw,
        payload_ext=ext,
        metadata=metadata,
    )


def _mime_extension(mime: str, name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".csv", ".json"}:
        return suffix
    return {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "text/csv": ".csv",
        "text/html": ".html",
        "application/json": ".json",
    }.get(mime, ".txt")

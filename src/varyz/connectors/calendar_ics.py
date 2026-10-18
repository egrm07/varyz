"""Universal Apple/Outlook/Google calendar file import (.ics)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, ClassVar

import httpx
from icalendar import Calendar

from varyz import secrets
from varyz.connectors.base import register
from varyz.domain import Cursor, Kind, NormalizedEntry, RawItem, TimePrecision

log = logging.getLogger(__name__)


@register
class CalendarICSConnector:
    connector_id: ClassVar[str] = "calendar_import"
    # A subscription uses validators and normally emits nothing on a 304.  Its
    # silence therefore means "unchanged", not "deleted".  Keep this explicit:
    # opting into corpus enumeration would prune every event after the first
    # successful conditional refresh.
    enumerates_corpus: ClassVar[bool] = False
    checkpoint: Cursor | None = None

    def discover(
        self, options: dict[str, Any], cursor: Cursor | None
    ) -> Iterator[tuple[RawItem, Cursor | None]]:
        if not options.get("approved", False):
            raise ValueError(
                "calendar files and subscriptions require explicit approval in Varyz Imports"
            )

        # Local files deliberately retain their old identity and display path.
        # A subscription URL, on the other hand, is a bearer secret on Google,
        # Apple and many CalDAV servers.  It must not become an entry field.
        for raw_path in options.get("paths", []):
            root = Path(raw_path)
            files = [root] if root.is_file() else sorted(root.rglob("*.ics"))
            for path in files:
                if not path.is_file() or path.suffix.lower() != ".ics":
                    continue
                try:
                    calendar = Calendar.from_ical(path.read_bytes())
                except (OSError, ValueError) as exc:
                    # Skipping is right -- one bad file must not stop the
                    # others -- but doing it silently left a calendar the user
                    # pointed Varyz at contributing nothing, with no line
                    # anywhere saying which file, or that anything happened.
                    log.warning("skipping calendar %s: %s", path, exc)
                    continue
                for component in calendar.walk("VEVENT"):
                    uid = str(component.get("UID", "")).strip()
                    recurrence = str(component.get("RECURRENCE-ID", "")).strip()
                    if not uid:
                        uid = hashlib.sha256(component.to_ical()).hexdigest()
                    external_id = f"{path.resolve()}:{uid}:{recurrence}"
                    yield (
                        RawItem(
                            external_id=external_id,
                            payload=component.to_ical(),
                            payload_ext=".ics",
                            metadata={"path": str(path.resolve())},
                        ),
                        None,
                    )

        urls = _subscription_urls(options)
        state = _cursor_state(cursor)
        self.checkpoint = _snapshot(state)
        if not urls:
            return

        timeout = float(options.get("timeout", 30.0))
        transport = options.get("_transport")
        with httpx.Client(
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=True,
        ) as client:
            for configured_url in urls:
                request_url = _request_url(configured_url)
                feed_id = hashlib.sha256(request_url.encode("utf-8")).hexdigest()
                previous = state.get(feed_id, {})
                headers = {"Accept": "text/calendar, application/ics;q=0.9, */*;q=0.1"}
                if previous.get("etag"):
                    headers["If-None-Match"] = str(previous["etag"])
                if previous.get("last_modified"):
                    headers["If-Modified-Since"] = str(previous["last_modified"])

                try:
                    # httpx normally logs the complete URL at INFO.  For an
                    # iCal subscription that URL is a credential, so suppress
                    # transport chatter for the small duration of this request.
                    with _without_http_url_logs():
                        response = client.get(request_url, headers=headers)
                except httpx.HTTPError:
                    # httpx exception messages include the complete request URL.
                    # Removing the cause prevents the bearer token from reaching
                    # the ingest loop's exception log.
                    raise CalendarSubscriptionError(
                        f"calendar subscription {feed_id[:12]} could not be fetched"
                    ) from None

                if response.status_code == 304:
                    continue
                if response.status_code != 200:
                    raise CalendarSubscriptionError(
                        f"calendar subscription {feed_id[:12]} returned HTTP "
                        f"{response.status_code}"
                    ) from None

                payload = response.content
                content_hash = hashlib.sha256(payload).hexdigest()
                next_state = {"content_hash": content_hash}
                if response.headers.get("etag"):
                    next_state["etag"] = response.headers["etag"]
                if response.headers.get("last-modified"):
                    next_state["last_modified"] = response.headers["last-modified"]
                state[feed_id] = next_state

                # Some subscription endpoints omit validators.  The body hash
                # still makes an unchanged periodic refresh a no-op.
                if previous.get("content_hash") == content_hash:
                    self.checkpoint = _snapshot(state)
                    continue

                try:
                    calendar = Calendar.from_ical(payload)
                except (TypeError, ValueError):
                    raise CalendarSubscriptionError(
                        f"calendar subscription {feed_id[:12]} returned invalid iCalendar data"
                    ) from None

                items = list(_subscription_items(calendar, feed_id))
                for index, item in enumerate(items):
                    # Do not persist the response validator after the first item:
                    # a crash then followed by a 304 would permanently skip the
                    # rest of this response.  The final item advances atomically.
                    new_cursor = _snapshot(state) if index == len(items) - 1 else None
                    yield item, new_cursor
                self.checkpoint = _snapshot(state)

    def normalize(self, item: RawItem) -> NormalizedEntry:
        calendar = Calendar.from_ical(
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            + item.payload_bytes()
            + b"\r\nEND:VCALENDAR\r\n"
        )
        event = calendar.walk("VEVENT")[0]
        start_raw = event.decoded("DTSTART", None)
        end_raw = event.decoded("DTEND", None)
        start, precision = _as_datetime(start_raw)
        end, _ = _as_datetime(end_raw)
        title = str(event.get("SUMMARY", "")).strip() or "Calendar event"
        fields = []
        source = item.metadata.get("path")
        if source:
            fields.append(f"Calendar file: {source}")
        elif item.metadata.get("calendar_subscription"):
            fields.append("Calendar source: subscription")
        for label, key in (
            ("Location", "LOCATION"),
            ("Description", "DESCRIPTION"),
            ("Organizer", "ORGANIZER"),
            ("Status", "STATUS"),
        ):
            value = event.get(key)
            if value:
                fields.append(f"{label}: {value}")
        attendees = event.get("ATTENDEE")
        if attendees:
            values = attendees if isinstance(attendees, list) else [attendees]
            fields.append("Attendees: " + ", ".join(str(value) for value in values))
        return NormalizedEntry(
            kind=Kind.CALENDAR_EVENT,
            external_id=item.external_id,
            title=title,
            body="\n".join(fields),
            occurred_start=start,
            occurred_end=end,
            tz=_timezone_name(start_raw),
            captured_at=start,
            time_precision=precision,
        )


class CalendarSubscriptionError(RuntimeError):
    """A remote-feed failure whose message never contains its bearer URL."""


def subscription_secret_name(account: str = "default") -> str:
    return f"calendar_subscription_url:{account}"


def _subscription_urls(options: dict[str, Any]) -> list[str]:
    """Return configured feeds, accepting a convenient singular form too."""
    configured: list[Any] = []
    secret_account = options.get("subscription_secret")
    if secret_account:
        secret_url = secrets.get_secret(subscription_secret_name(str(secret_account)))
        if not secret_url:
            raise ValueError(
                "No calendar subscription URL stored. Run: varyz connect calendar"
            )
        configured.append(secret_url)
    for key in ("url", "subscription_url"):
        singular = options.get(key)
        if singular:
            configured.append(singular)
    for key in ("urls", "subscription_urls"):
        plural = options.get(key, [])
        configured.extend(plural if isinstance(plural, (list, tuple)) else [plural])

    urls: list[str] = []
    seen: set[str] = set()
    for value in configured:
        url = str(value).strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _request_url(configured_url: str) -> str:
    """httpx does not know the calendar-specific ``webcal`` scheme."""
    if configured_url.lower().startswith("webcal://"):
        return "https://" + configured_url[len("webcal://") :]
    if not configured_url.lower().startswith("https://"):
        raise CalendarSubscriptionError(
            "calendar subscription URL must use https:// or webcal://"
        )
    return configured_url


def _cursor_state(cursor: Cursor | None) -> dict[str, dict[str, str]]:
    feeds = cursor.data.get("feeds", {}) if cursor else {}
    if not isinstance(feeds, dict):
        return {}
    return {
        str(feed_id): {str(key): str(value) for key, value in values.items()}
        for feed_id, values in feeds.items()
        if isinstance(values, dict)
    }


def _snapshot(state: dict[str, dict[str, str]]) -> Cursor:
    return Cursor(data={"feeds": {feed_id: dict(values) for feed_id, values in state.items()}})


def _subscription_items(calendar: Calendar, feed_id: str) -> Iterator[RawItem]:
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID", "")).strip()
        recurrence = str(component.get("RECURRENCE-ID", "")).strip()
        if not uid:
            uid = hashlib.sha256(component.to_ical()).hexdigest()
        event_id = hashlib.sha256(f"{uid}\0{recurrence}".encode()).hexdigest()
        yield RawItem(
            external_id=f"subscription:{feed_id}:{event_id}",
            payload=component.to_ical(),
            payload_ext=".ics",
            # Deliberately contains no URL, hostname, query or token.
            metadata={"calendar_subscription": True},
        )


@contextmanager
def _without_http_url_logs() -> Iterator[None]:
    """Keep bearer URLs out of httpx/httpcore's otherwise useful diagnostics."""
    loggers = (logging.getLogger("httpx"), logging.getLogger("httpcore"))
    levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            if logger.getEffectiveLevel() < logging.WARNING:
                logger.setLevel(logging.WARNING)
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def _as_datetime(value: Any) -> tuple[datetime | None, TimePrecision]:
    if isinstance(value, datetime):
        return (
            value if value.tzinfo else value.replace(tzinfo=UTC),
            TimePrecision.EXACT,
        )
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC), TimePrecision.DAY
    return None, TimePrecision.UNKNOWN


def _timezone_name(value: Any) -> str | None:
    tz = getattr(value, "tzinfo", None)
    return str(tz) if tz else None

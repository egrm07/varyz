"""Offline coverage for secret-URL iCalendar subscriptions."""

from __future__ import annotations

import logging

import httpx
import pytest
from typer.testing import CliRunner

from varyz import cli
from varyz.connectors.calendar_ics import (
    CalendarICSConnector,
    CalendarSubscriptionError,
    subscription_secret_name,
)

CALENDAR = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    b"BEGIN:VEVENT\r\nUID:planning-1\r\nDTSTART:20260810T090000Z\r\n"
    b"SUMMARY:Planning session\r\nLOCATION:Studio\r\nEND:VEVENT\r\n"
    b"BEGIN:VEVENT\r\nUID:dinner-2\r\nDTSTART:20260810T190000Z\r\n"
    b"SUMMARY:Dinner with Jo\r\nEND:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


def test_subscription_fetch_is_private_and_cursor_advances_on_last_event(caplog):
    secret = "calendar-bearer-token-very-secret"
    url = f"https://calendar.example/private.ics?token={secret}"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            content=CALENDAR,
            headers={"ETag": '"calendar-v1"', "Last-Modified": "Sat, 01 Aug 2026 08:00:00 GMT"},
        )

    caplog.set_level(logging.INFO, logger="httpx")
    connector = CalendarICSConnector()
    discovered = list(
        connector.discover(
            {
                "approved": True,
                "urls": [url],
                "_transport": httpx.MockTransport(handler),
            },
            None,
        )
    )

    assert len(calls) == 1
    assert str(calls[0].url) == url
    assert len(discovered) == 2
    assert discovered[0][1] is None
    assert discovered[1][1] is not None
    assert connector.checkpoint == discovered[1][1]
    assert CalendarICSConnector.enumerates_corpus is False
    cursor_text = connector.checkpoint.model_dump_json()
    assert url not in cursor_text
    assert secret not in cursor_text

    for item, _ in discovered:
        entry = connector.normalize(item)
        stored_text = "\n".join(
            (item.external_id, repr(item.metadata), entry.title or "", entry.body)
        )
        assert url not in stored_text
        assert secret not in stored_text
        assert "Calendar source: subscription" in entry.body
    assert url not in caplog.text
    assert secret not in caplog.text


def test_subscription_uses_etag_and_304_is_a_clean_noop():
    url = "https://calendar.example/private.ics?key=not-for-storage"

    first = CalendarICSConnector()
    first_items = list(
        first.discover(
            {
                "approved": True,
                "url": url,
                "_transport": httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, content=CALENDAR, headers={"ETag": '"v1"'}
                    )
                ),
            },
            None,
        )
    )
    cursor = first_items[-1][1]
    assert cursor is not None

    def unchanged(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304)

    refreshed = CalendarICSConnector()
    assert list(
        refreshed.discover(
            {
                "approved": True,
                "url": url,
                "_transport": httpx.MockTransport(unchanged),
            },
            cursor,
        )
    ) == []
    assert refreshed.checkpoint == cursor

    changed_calendar = CALENDAR.replace(b"LOCATION:Studio", b"LOCATION:Office")

    def changed(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(200, content=changed_calendar, headers={"ETag": '"v2"'})

    updated = CalendarICSConnector()
    updated_items = list(
        updated.discover(
            {
                "approved": True,
                "url": url,
                "_transport": httpx.MockTransport(changed),
            },
            cursor,
        )
    )
    assert {item.external_id for item, _ in updated_items} == {
        item.external_id for item, _ in first_items
    }
    assert "Location: Office" in updated.normalize(updated_items[0][0]).body


def test_subscription_body_hash_skips_unchanged_feed_without_validators():
    url = "https://calendar.example/feed.ics?private=another-secret"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=CALENDAR))

    first = CalendarICSConnector()
    first_items = list(
        first.discover({"approved": True, "urls": [url], "_transport": transport}, None)
    )
    cursor = first_items[-1][1]
    assert cursor is not None

    second = CalendarICSConnector()
    assert list(
        second.discover(
            {"approved": True, "urls": [url], "_transport": transport}, cursor
        )
    ) == []
    assert second.checkpoint == cursor


def test_webcal_and_multiple_urls_are_supported():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        content = CALENDAR.replace(b"planning-1", request.url.host.encode())
        return httpx.Response(200, content=content)

    connector = CalendarICSConnector()
    discovered = list(
        connector.discover(
            {
                "approved": True,
                "url": "webcal://one.example/feed.ics?secret=one",
                "urls": ["https://two.example/feed.ics?secret=two"],
                "_transport": httpx.MockTransport(handler),
            },
            None,
        )
    )

    assert requested == [
        "https://one.example/feed.ics?secret=one",
        "https://two.example/feed.ics?secret=two",
    ]
    assert len(discovered) == 4
    assert len({item.external_id for item, _ in discovered}) == 4


def test_plain_http_subscription_is_rejected_before_fetch():
    with pytest.raises(CalendarSubscriptionError, match="https:// or webcal://"):
        list(
            CalendarICSConnector().discover(
                {"approved": True, "url": "http://calendar.example/private.ics"},
                None,
            )
        )


def test_subscription_failure_does_not_expose_url_in_error_or_logs(caplog):
    secret = "do-not-log-this-token"
    url = f"https://calendar.example/feed.ics?token={secret}"
    caplog.set_level(logging.INFO, logger="httpx")
    connector = CalendarICSConnector()

    with pytest.raises(CalendarSubscriptionError) as caught:
        list(
            connector.discover(
                {
                    "approved": True,
                    "url": url,
                    "_transport": httpx.MockTransport(
                        lambda request: httpx.Response(503, text="unavailable")
                    ),
                },
                None,
            )
        )

    assert url not in str(caught.value)
    assert secret not in str(caught.value)
    assert url not in caplog.text
    assert secret not in caplog.text


def test_local_calendar_path_identity_and_body_are_unchanged(tmp_path):
    path = tmp_path / "life.ics"
    path.write_bytes(CALENDAR)
    connector = CalendarICSConnector()

    item, cursor = next(
        connector.discover({"approved": True, "paths": [str(path)]}, None)
    )
    entry = connector.normalize(item)

    assert item.external_id.startswith(f"{path.resolve()}:planning-1:")
    assert f"Calendar file: {path.resolve()}" in entry.body
    assert cursor is None


def test_a_calendar_file_that_cannot_be_read_says_which_one(tmp_path, caplog):
    """Skipping is right; skipping in silence is not.

    A folder of .ics files where one is truncated used to contribute its good
    events and nothing else — no line anywhere naming the file the user would
    have to fix, and a sync that read 'everything is fine'.
    """

    (tmp_path / "life.ics").write_bytes(CALENDAR)
    (tmp_path / "half.ics").write_bytes(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:t1\r\n")
    connector = CalendarICSConnector()

    with caplog.at_level(logging.WARNING):
        items = list(connector.discover({"approved": True, "paths": [str(tmp_path)]}, None))

    assert len(items) == 2  # the good file is untouched by its neighbour
    assert "half.ics" in caplog.text
    assert "life.ics" not in caplog.text
    assert "Traceback" not in caplog.text


def test_guided_connect_keeps_secret_url_out_of_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    stored = {}
    monkeypatch.setattr(
        "varyz.cli._config_path", lambda supplied: config
    )
    monkeypatch.setattr(
        "varyz.secrets.set_secret", lambda name, value: stored.update({name: value})
    )
    secret_url = "webcal://calendar.example/private.ics?token=never-in-toml"

    result = CliRunner().invoke(
        cli.app,
        ["connect", "calendar", "--url", secret_url, "--name", "work"],
    )

    assert result.exit_code == 0, result.output
    assert stored == {subscription_secret_name("work"): secret_url}
    text = config.read_text(encoding="utf-8")
    assert secret_url not in text
    assert 'subscription_secret = "work"' in text


def test_subscription_url_can_be_loaded_from_os_credentials(monkeypatch):
    url = "https://calendar.example/private.ics?token=from-keyring"
    monkeypatch.setattr(
        "varyz.connectors.calendar_ics.secrets.get_secret",
        lambda name: url if name == subscription_secret_name("work") else None,
    )
    connector = CalendarICSConnector()
    discovered = list(
        connector.discover(
            {
                "approved": True,
                "subscription_secret": "work",
                "_transport": httpx.MockTransport(
                    lambda request: httpx.Response(200, content=CALENDAR)
                ),
            },
            None,
        )
    )
    assert len(discovered) == 2

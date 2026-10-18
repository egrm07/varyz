"""Counter-only public-site metrics. No visitor records, identifiers or request logging."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SOURCES = frozenset({"direct", "internal", "search", "social", "ai", "referral"})
TIMINGS = frozenset({"lt100", "100-299", "300-999", "1000-2999", "gte3000"})
STATUSES = frozenset({"2xx", "3xx", "4xx", "5xx"})


class Counters:
    def __init__(self, path: Path, pages: set[str]):
        self.path = path
        self.pages = pages
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS counts ("
                "day TEXT NOT NULL, kind TEXT NOT NULL, dimension TEXT NOT NULL, "
                "bucket TEXT NOT NULL, total INTEGER NOT NULL, "
                "PRIMARY KEY(day,kind,dimension,bucket)) WITHOUT ROWID"
            )
            db.commit()

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, value: object):
        if not isinstance(value, dict) or set(value) != {
            "page",
            "source",
            "status",
            "timing",
            "kind",
        }:
            raise ValueError("Invalid metric")
        allowed = {
            "page": self.pages,
            "source": SOURCES,
            "status": STATUSES,
            "timing": TIMINGS,
            "kind": {"traffic", "probe"},
        }
        if any(
            not isinstance(value[k], str) or value[k] not in choices
            for k, choices in allowed.items()
        ):
            raise ValueError("Invalid metric")
        day = datetime.now(UTC).date()
        with closing(self.connect()) as db, db:
            db.executemany(
                "INSERT INTO counts VALUES (?,?,?,?,1) ON CONFLICT(day,kind,dimension,bucket) "
                "DO UPDATE SET total=MIN(total+1,1000000000)",
                [
                    (day.isoformat(), value["kind"], dimension, value[dimension])
                    for dimension in ("page", "source", "status", "timing")
                ],
            )
            db.execute(
                "DELETE FROM counts WHERE day < ?", ((day - timedelta(days=89)).isoformat(),)
            )

    def summary(self, days: int, kind: str = "traffic"):
        start = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT day,dimension,bucket,total FROM counts WHERE day>=? AND kind=? "
                "ORDER BY day,dimension,bucket",
                (start, kind),
            ).fetchall()
        totals: dict[str, dict[str, int]] = {k: {} for k in ("page", "source", "status", "timing")}
        daily: dict[str, int] = {}
        for day, dimension, bucket, total in rows:
            totals[dimension][bucket] = totals[dimension].get(bucket, 0) + total
            if dimension == "page":
                daily[day] = daily.get(day, 0) + total
        return {
            "days": days,
            "kind": kind,
            "requests": sum(daily.values()),
            "daily": daily,
            "totals": totals,
            "note": (
                "Public HTML requests, not unique visitors. "
                "Known bots and privacy signals excluded."
            ),
        }


def handler(counters: Counters, collect_token: str, admin_token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self, code: int, value: object = None):
            content = json.dumps(value).encode() if value is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def authorized(self, token: str):
            return bool(token) and hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            )

        def do_POST(self):
            if self.path != "/collect":
                return self.reply(404)
            if not self.authorized(collect_token):
                return self.reply(401)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    return self.reply(413)
                counters.record(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError):
                return self.reply(422)
            except sqlite3.Error:
                return self.reply(503)
            return self.reply(204)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/health":
                return self.reply(200, {"ok": True})
            if url.path != "/summary":
                return self.reply(404)
            if not self.authorized(admin_token):
                return self.reply(401)
            try:
                query = parse_qs(url.query)
                days = int(query.get("days", ["7"])[0])
                kind = query.get("kind", ["traffic"])[0]
                if not 1 <= days <= 90 or kind not in {"traffic", "probe"}:
                    raise ValueError("Invalid query")
                return self.reply(200, counters.summary(days, kind))
            except (ValueError, sqlite3.Error):
                return self.reply(400)

    return Handler


if __name__ == "__main__":
    pages = set(json.loads(Path("/app/pages.json").read_text()))
    store = Counters(Path(os.environ.get("AGGREGATE_DB", "/data/counters.sqlite3")), pages)
    server = ThreadingHTTPServer(
        ("0.0.0.0", 3101),
        handler(store, os.environ["AGGREGATE_COLLECT_TOKEN"], os.environ["AGGREGATE_ADMIN_TOKEN"]),
    )
    server.daemon_threads = True
    server.serve_forever()

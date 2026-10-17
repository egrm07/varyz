"""The default website layer stores counters only; identifiers cannot enter its schema."""

from __future__ import annotations

import importlib.util
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

PATH = Path(__file__).resolve().parents[1] / "deploy/website-analytics/aggregate.py"
SPEC = importlib.util.spec_from_file_location("website_aggregate", PATH)
assert SPEC and SPEC.loader
aggregate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregate)
EVENT = dict(page="/pricing", source="search", status="2xx", timing="lt100", kind="traffic")


@pytest.fixture
def counters(tmp_path):
    return aggregate.Counters(tmp_path / "counters.sqlite3", {"/", "/pricing", "/404"})


def test_requests_collapse_into_four_daily_counters(counters):
    for _ in range(20):
        counters.record(EVENT)
    summary = counters.summary(1)
    assert summary["requests"] == 20
    assert summary["totals"]["page"] == {"/pricing": 20}
    with closing(sqlite3.connect(counters.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM counts").fetchone()[0] == 4
        assert [row[1] for row in db.execute("PRAGMA table_info(counts)")] == [
            "day",
            "kind",
            "dimension",
            "bucket",
            "total",
        ]


@pytest.mark.parametrize(
    "bad",
    [
        EVENT | {"ip": "203.0.113.1"},
        EVENT | {"session": "visitor-id"},
        EVENT | {"page": "/reset/private-token"},
        EVENT | {"source": "https://private.example/user"},
        EVENT | {"page": "/pricing?token=secret"},
        EVENT | {"timing": 123},
        EVENT | {"kind": "unbounded"},
        [],
        None,
    ],
)
def test_unknown_fields_and_values_cannot_be_collected(counters, bad):
    with pytest.raises(ValueError):
        counters.record(bad)
    assert counters.summary(1)["requests"] == 0


def test_probes_never_enter_traffic_totals(counters):
    counters.record(EVENT | {"kind": "probe"})
    assert counters.summary(1)["requests"] == 0
    assert counters.summary(1, "probe")["requests"] == 1


def test_expired_rollups_are_removed(counters):
    old = (datetime.now(UTC).date() - timedelta(days=90)).isoformat()
    with closing(sqlite3.connect(counters.path)) as db, db:
        db.execute("INSERT INTO counts VALUES (?,?,?,?,?)", (old, "traffic", "page", "/", 5))
    counters.record(EVENT)
    with closing(sqlite3.connect(counters.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM counts WHERE day=?", (old,)).fetchone()[0] == 0


def test_http_write_and_report_have_separate_authentication(counters):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), aggregate.handler(counters, "write-token", "read-token")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as c:
            assert c.get("/health").status_code == 200
            assert c.get("/summary").status_code == 401
            assert c.post("/collect", json=EVENT).status_code == 401
            assert (
                c.post(
                    "/collect", json=EVENT, headers={"Authorization": "Bearer read-token"}
                ).status_code
                == 401
            )
            headers = {"Authorization": "Bearer write-token"}
            assert c.post("/collect", json=EVENT, headers=headers).status_code == 204
            invalid = c.post(
                "/collect", json=EVENT | {"private-message": "SECRET"}, headers=headers
            )
            assert invalid.status_code == 422 and "SECRET" not in invalid.text
            assert c.get("/summary", headers=headers).status_code == 401
            result = c.get("/summary", headers={"Authorization": "Bearer read-token"}).json()
            assert result["requests"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

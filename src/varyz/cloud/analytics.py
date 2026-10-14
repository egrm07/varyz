"""Bounded aggregate receipt and owner-only reporting, independent of billing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from varyz.analytics_schema import AggregateReport


def register_analytics(app: Any, settings: Any, store: Any) -> None:
    with store.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS analytics_reports "
            "(id TEXT PRIMARY KEY,day TEXT NOT NULL,version TEXT NOT NULL,os TEXT NOT NULL,"
            "payload TEXT NOT NULL,received TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS analytics_reports_day ON analytics_reports(day)")

    @app.post("/api/analytics")
    async def receive(request: Request):
        if not settings.analytics_enabled:
            raise HTTPException(503, "Analytics receipt is disabled")
        peer = request.client.host if request.client else "unknown"
        if not await asyncio.to_thread(
            store.allow_request, "analytics:" + hashlib.sha256(peer.encode()).hexdigest(), 60, 6
        ):
            raise HTTPException(429, "Analytics rate limit")
        try:
            payload = await request.json()
            report = AggregateReport.model_validate(payload)
        except (ValidationError, ValueError):
            # Validation exceptions can contain user-submitted data. Return no echo.
            raise HTTPException(422, "Expected a bounded aggregate report") from None
        return await asyncio.to_thread(persist, report)

    def persist(report: AggregateReport):
        canonical = json.dumps(report.model_dump(), sort_keys=True, separators=(",", ":"))
        now = datetime.now(UTC)
        cutoff = (now.date() - timedelta(days=30)).isoformat()
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM analytics_reports WHERE day<?", (cutoff,))
            existing = db.execute(
                "SELECT payload FROM analytics_reports WHERE id=?", (report.report_id,)
            ).fetchone()
            if existing:
                if existing["payload"] != canonical:
                    raise HTTPException(409, "Report ID already used")
            else:
                if db.execute("SELECT COUNT(*) FROM analytics_reports").fetchone()[0] >= 100000:
                    raise HTTPException(507, "Analytics capacity reached")
                db.execute(
                    "INSERT INTO analytics_reports VALUES(?,?,?,?,?,?)",
                    (
                        report.report_id,
                        report.day,
                        report.version,
                        report.os,
                        canonical,
                        now.isoformat(),
                    ),
                )
        return {"accepted": report.report_id}

    @app.get("/api/analytics/summary")
    def summary(request: Request):
        provided = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not settings.analytics_admin_token or not hmac.compare_digest(
            provided.encode(), settings.analytics_admin_token.encode()
        ):
            raise HTTPException(401, "Analytics owner authentication required")
        cutoff = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
        totals: dict[str, int] = {}
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        probes = 0
        with store.connection() as db:
            db.execute("DELETE FROM analytics_reports WHERE day<?", (cutoff,))
            for row in db.execute("SELECT day,version,os,payload FROM analytics_reports"):
                payload = json.loads(row["payload"])
                if payload.get("sample_kind") == "probe":
                    probes += 1
                    continue
                key = (row["day"], row["version"], row["os"])
                group = groups.setdefault(
                    key,
                    {"day": key[0], "version": key[1], "os": key[2], "reports": 0, "counters": {}},
                )
                group["reports"] += 1
                for name, count in payload["counters"].items():
                    totals[name] = totals.get(name, 0) + count
                    group["counters"][name] = group["counters"].get(name, 0) + count
        return {
            "retention_days": 30,
            "probes": probes,
            "reports": sum(g["reports"] for g in groups.values()),
            "counters": totals,
            "groups": [groups[k] for k in sorted(groups)],
            "limitations": [
                "Reports are not unique users or independently verified outcomes.",
                "Unrecorded outcomes are unknown; overrides are preference changes.",
                "Only consenting installations contribute.",
            ],
        }

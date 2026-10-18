"""Receipt, deletion and owner export for anonymized Varyz network contributions.

Contributors are identified only by the SHA-256 of a random key their own
installation generated; the server never learns an account, name or device.
The same key deletes everything it sent, which is how turning sharing off
removes it. Rows are kept until deleted, unlike the 30-day aggregate reports.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from varyz.contribution_schema import ContributionBatch

CAPACITY = 2_000_000
PER_CONTRIBUTOR = 50_000
_KEY = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def register_contributions(app: Any, settings: Any, store: Any) -> None:
    with store.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS network_contributions "
            "(id TEXT PRIMARY KEY,contributor TEXT NOT NULL,batch TEXT NOT NULL,"
            "kind TEXT NOT NULL,day TEXT NOT NULL,version TEXT NOT NULL,"
            "payload TEXT NOT NULL,received TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS network_contributions_contributor "
            "ON network_contributions(contributor)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS network_contributions_kind "
            "ON network_contributions(kind,received)"
        )

    def contributor(request: Request) -> str:
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not _KEY.match(key):
            raise HTTPException(401, "Contributor key required")
        return hashlib.sha256(key.encode()).hexdigest()

    async def limited(request: Request) -> None:
        peer = request.client.host if request.client else "unknown"
        if not await asyncio.to_thread(
            store.allow_request, "contrib:" + hashlib.sha256(peer.encode()).hexdigest(), 60, 6
        ):
            raise HTTPException(429, "Contribution rate limit")

    @app.post("/api/contributions")
    async def receive(request: Request):
        if not settings.contributions_enabled:
            raise HTTPException(503, "Contribution receipt is disabled")
        await limited(request)
        who = contributor(request)
        try:
            batch = ContributionBatch.model_validate(await request.json())
        except (ValidationError, ValueError):
            # Validation errors can quote submitted text. Return no echo.
            raise HTTPException(422, "Expected a bounded contribution batch") from None
        return await asyncio.to_thread(persist, who, batch)

    def persist(who: str, batch: ContributionBatch) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        stored = 0
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT COUNT(*) FROM network_contributions").fetchone()[0]
            mine = db.execute(
                "SELECT COUNT(*) FROM network_contributions WHERE contributor=?", (who,)
            ).fetchone()[0]
            if total + len(batch.records) > CAPACITY or mine + len(batch.records) > PER_CONTRIBUTOR:
                raise HTTPException(507, "Contribution capacity reached")
            for record in batch.records:
                canonical = json.dumps(record.model_dump(), sort_keys=True, separators=(",", ":"))
                existing = db.execute(
                    "SELECT contributor FROM network_contributions WHERE id=?", (record.record_id,)
                ).fetchone()
                if existing:
                    continue  # A retried batch, or an id clash: never overwrite.
                db.execute(
                    "INSERT INTO network_contributions VALUES(?,?,?,?,?,?,?,?)",
                    (record.record_id, who, batch.batch_id, record.kind, record.day,
                     batch.version, canonical, now),
                )
                stored += 1
        return {"accepted": batch.batch_id, "stored": stored}

    @app.delete("/api/contributions")
    async def forget(request: Request):
        # Works even while receipt is disabled: deletion is always honoured.
        await limited(request)
        who = contributor(request)

        def delete() -> dict[str, int]:
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "DELETE FROM network_contributions WHERE contributor=?", (who,)
                )
                return {"deleted": cursor.rowcount or 0}

        return await asyncio.to_thread(delete)

    def owner(request: Request) -> None:
        provided = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not settings.analytics_admin_token or not hmac.compare_digest(
            provided.encode(), settings.analytics_admin_token.encode()
        ):
            raise HTTPException(401, "Owner authentication required")

    @app.get("/api/contributions/summary")
    def summary(request: Request):
        owner(request)
        with store.connection() as db:
            kinds = {
                row["kind"]: row["n"]
                for row in db.execute(
                    "SELECT kind,COUNT(*) AS n FROM network_contributions GROUP BY kind"
                )
            }
            contributors = db.execute(
                "SELECT COUNT(DISTINCT contributor) FROM network_contributions"
            ).fetchone()[0]
        return {
            "records": sum(kinds.values()),
            "kinds": kinds,
            "contributors": contributors,
            "limitations": [
                "Contributors are installations, not verified people.",
                "Text is scrubbed on the contributor's machine and can still hold details.",
            ],
        }

    @app.get("/api/contributions/export")
    def export(request: Request, kind: str = "", after: int = 0, limit: int = 1000):
        """Owner-only page of records in received order, without contributor ids."""
        owner(request)
        limit = max(1, min(limit, 5000))
        query = "SELECT rowid AS n,payload FROM network_contributions WHERE rowid>?"
        params: list[Any] = [after]
        if kind:
            query += " AND kind=?"
            params.append(kind)
        with store.connection() as db:
            rows = db.execute(
                query + " ORDER BY rowid LIMIT ?", (*params, limit)
            ).fetchall()
        return {
            "records": [json.loads(row["payload"]) for row in rows],
            "next": rows[-1]["n"] if len(rows) == limit else 0,
        }

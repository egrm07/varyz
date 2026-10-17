"""Bounded, tenant-scoped dashboard reads; preference text is loaded on demand."""

from __future__ import annotations

from .project_identity import project_names

PAGE_SIZE = 20
KINDS = "kind IN ('rule','answer','workflow') AND payload!='null'"


def export_project_preferences(store, account: str, project: str) -> dict:
    """Export this project's confirmed preferences, without unrelated/global context."""
    names = project_names(store, account)
    scope = names(project)
    groups = project_summaries(store, account)
    raw: list[str] = next((g["raw_scopes"] for g in groups if g["scope"] == scope), [])
    records = []
    if raw:
        with store.connection() as db:
            marks = ",".join("?" for _ in raw)
            rows = db.execute(
                f"SELECT * FROM records WHERE account=? AND {KINDS} "
                f"AND {scope_expression(store)} IN ({marks}) ORDER BY cursor",
                (account, *raw),
            ).fetchall()
        records = [store.record(r) for r in rows]
        for record in records:
            record["payload"]["scope"] = scope
    return {"format": "varyz-project-v1", "project": scope, "records": records}


def delete_project_preferences(store, account: str, project: str) -> None:
    """Delete exact canonical scope with versioned sync tombstones, atomically."""
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        names = project_names(store, account, db=db)
        scope = names(project)
        rows = db.execute(
            f"SELECT kind,id,version,{scope_expression(store)} AS scope "
            f"FROM records WHERE account=? AND {KINDS}",
            (account,),
        ).fetchall()
        items = [
            dict(kind=r["kind"], id=r["id"], base_version=r["version"], payload=None)
            for r in rows if names(r["scope"]) == scope
        ]
        if items:
            store.push(account, items, transaction=db)


def preference_usage(store, account: str, quota: int) -> dict:
    with store.connection() as db:
        row = db.execute("SELECT bytes FROM account_usage WHERE account=?", (account,)).fetchone()
        if row is None:
            size = (
                "octet_length(payload)"
                if hasattr(store, "pool")
                else "length(CAST(payload AS BLOB))"
            )
            row = db.execute(
                f"SELECT COALESCE(SUM({size}*2+512),0) AS bytes FROM changes WHERE account=?",
                (account,),
            ).fetchone()
    return dict(bytes=row["bytes"], quota_bytes=quota)


def scope_expression(store) -> str:
    return (
        "COALESCE(payload::jsonb->>'scope','*')"
        if hasattr(store, "pool")
        else "COALESCE(json_extract(payload,'$.scope'),'*')"
    )


def project_summaries(store, account: str) -> list[dict]:
    with store.connection() as db:
        rows = db.execute(
            f"SELECT {scope_expression(store)} AS scope, COUNT(*) AS total "
            f"FROM records WHERE account=? AND {KINDS} GROUP BY 1",
            (account,),
        ).fetchall()
        names = project_names(store, account, db=db)
    groups: dict[str, dict] = {}
    for row in rows:
        scope = names(row["scope"])
        group = groups.setdefault(scope, dict(scope=scope, count=0, raw_scopes=[]))
        group["count"] += row["total"]
        group["raw_scopes"].append(row["scope"])
    return sorted(groups.values(), key=lambda g: (g["scope"] != "*", -g["count"], g["scope"]))


def preference_page(
    store, account: str, scope: str, *, after: int = 0, snapshot: int | None = None
) -> dict:
    # Canonical aliases are resolved from aggregate names, never by reading bodies.
    summaries = project_summaries(store, account)
    names = project_names(store, account)
    group = next((g for g in summaries if g["scope"] == names(scope)), None)
    with store.connection() as db:
        if snapshot is None:
            snapshot = db.execute(
                "SELECT COALESCE(MAX(cursor),0) AS value FROM records WHERE account=?",
                (account,),
            ).fetchone()["value"]
        raw = group["raw_scopes"] if group else []
        if not raw:
            return dict(records=[], next_after=None, snapshot=snapshot)
        marks = ",".join("?" for _ in raw)
        rows = db.execute(
            f"SELECT * FROM records WHERE account=? AND {KINDS} "
            f"AND {scope_expression(store)} IN ({marks}) AND cursor>? AND cursor<=? "
            "ORDER BY cursor LIMIT ?",
            (account, *raw, after, snapshot, PAGE_SIZE + 1),
        ).fetchall()
    records = [store.record(row) for row in rows[:PAGE_SIZE]]
    for record in records:
        record["payload"]["scope"] = names(record["payload"]["scope"])
    return dict(
        records=records,
        snapshot=snapshot,
        next_after=records[-1]["cursor"] if len(rows) > PAGE_SIZE else None,
    )

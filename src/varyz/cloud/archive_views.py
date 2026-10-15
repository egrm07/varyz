"""Account-scoped snapshot controls and dated evidence views; no inference."""

from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime

from varyz.scopes import normalize_scope

from .hosted_data import require_hosted_safe


class ArchiveViews:
    def revision_usage(self, account):
        with self.store.connection() as db:
            return db.execute(
                "SELECT COALESCE(SUM(bytes),0) AS n FROM archive_revisions WHERE account=?",
                (account,),
            ).fetchone()["n"]

    def permissions(self, account):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT p.label,p.revoked,COUNT(DISTINCT c.id) AS records "
                "FROM archive_permissions p LEFT JOIN archive_chunks c "
                "ON c.account=p.account AND c.project=p.project WHERE p.account=? "
                "GROUP BY p.label,p.revoked",
                (account,),
            ).fetchall()
        return [
            {
                "project": self.open(r["label"]),
                "revoked": bool(r["revoked"]),
                "records": r["records"],
            }
            for r in rows
        ]

    def permit(self, account, project):
        project = normalize_scope(project)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO archive_permissions VALUES(?,?,?,0) "
                "ON CONFLICT(account,project) DO UPDATE SET revoked=0",
                (account, self.index_term(account, project), self.seal(project)),
            )

    def revoke(self, account, project):
        project = normalize_scope(project)
        key = self.index_term(account, project)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            identities = db.execute(
                "SELECT DISTINCT id FROM archive_chunks WHERE account=? AND project=?",
                (account, key),
            ).fetchall()
            db.execute(
                "INSERT INTO archive_permissions VALUES(?,?,?,1) "
                "ON CONFLICT(account,project) DO UPDATE SET revoked=1",
                (account, key, self.seal(project)),
            )
            for row in identities:
                for table in ("archive_chunks", "archive_postings", "archive_revisions"):
                    db.execute(
                        f"DELETE FROM {table} WHERE account=? AND id=?", (account, row["id"])
                    )
                db.execute(
                    "UPDATE archive_documents SET payload='null',bytes=256,"
                    "fingerprint=?,version=version+1 WHERE account=? AND id=?",
                    (hashlib.sha256(b"null").hexdigest(), account, row["id"]),
                )
        return len(identities)

    def export(self, account):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id,version,payload,synced_at FROM archive_documents "
                "WHERE account=? AND payload!='null' ORDER BY id",
                (account,),
            ).fetchall()
            revisions = db.execute(
                "SELECT id,version,payload,synced_at FROM archive_revisions "
                "WHERE account=? ORDER BY id,version",
                (account,),
            ).fetchall()

        def decode(row):
            return {
                "id": row["id"],
                "version": row["version"],
                "synced_at": row["synced_at"],
                "source": json.loads(self.open(row["payload"])),
            }

        return {
            "records": [decode(r) for r in rows],
            "revisions": [decode(r) for r in revisions],
            "permissions": self.permissions(account),
            "paused": self.paused(account),
        }

    def timeline(self, account, project=None, limit=10, before=None, after=None):
        if (
            (project is not None and not isinstance(project, str))
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("Invalid timeline filter")
        if self.paused(account):
            return {"records": [], "paused": True}
        scope = normalize_scope(project) if project is not None else None
        dates = {}
        for name, value in (("before", before), ("after", after)):
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("Invalid timeline date")
                dates[name] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dates[name].tzinfo is None:
                    raise ValueError("Timeline dates need a timezone")
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id,payload,synced_at,version FROM archive_documents "
                "WHERE account=? AND payload!='null'",
                (account,),
            ).fetchall()
        records = []
        for row in rows:
            doc = json.loads(self.open(row["payload"]))
            date = datetime.fromisoformat(doc["occurred_at"].replace("Z", "+00:00"))
            if doc["project"] != scope or ("before" in dates and date >= dates["before"]):
                continue
            if "after" in dates and date <= dates["after"]:
                continue
            records.append(
                {
                    "id": doc["id"],
                    "title": doc["title"],
                    "source": doc["source"],
                    "occurred_at": doc["occurred_at"],
                    "synced_at": row["synced_at"],
                    "version": row["version"],
                    "snippet": doc["text"][:2000],
                    "url": self.origin + "/archive/" + doc["id"],
                }
            )
        records.sort(
            key=lambda r: (
                datetime.fromisoformat(r["occurred_at"].replace("Z", "+00:00")),
                r["id"],
            ),
            reverse=True,
        )
        return {
            "project": "General" if scope == "*" else scope,
            "records": records[:limit],
            "coverage": "Latest successfully synced eligible records; not a live task tracker",
            "status_instruction": "Only claim open/completed work where sources explicitly say it.",
        }

    def fetch_version(self, account, identity, version):
        current = self.fetch(account, identity)
        if type(version) is not int or version < 1:
            raise ValueError("Invalid source version")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload,synced_at FROM archive_revisions "
                "WHERE account=? AND id=? AND version=?",
                (account, identity, version),
            ).fetchone()
        if not row:
            raise KeyError("Retained source version unavailable")
        doc = json.loads(self.open(row["payload"]))
        require_hosted_safe(doc)
        return {
            "id": identity,
            "title": doc["title"],
            "text": doc["text"],
            "url": current["url"] + "?version=" + str(version),
            "metadata": {
                "project": doc["project"],
                "source": doc["source"],
                "occurred_at": doc["occurred_at"],
                "synced_at": row["synced_at"],
                "version": version,
            },
        }

    def compare(self, account, identity):
        current = self.fetch(account, identity)
        with self.store.connection() as db:
            first = db.execute(
                "SELECT version,payload,synced_at FROM archive_revisions "
                "WHERE account=? AND id=? ORDER BY version LIMIT 1",
                (account, identity),
            ).fetchone()
        if not first:
            return {
                "id": identity,
                "current": current,
                "comparison_available": False,
                "reason": "No earlier synced version exists; unsynced history is unavailable.",
            }
        original = json.loads(self.open(first["payload"]))
        return {
            "id": identity,
            "comparison_available": True,
            "current": current,
            "original": {
                "url": current["url"] + "?version=" + str(first["version"]),
                "title": original["title"],
                "text": original["text"],
                "occurred_at": original["occurred_at"],
                "version": first["version"],
                "synced_at": first["synced_at"],
            },
            "diff": "\n".join(
                difflib.unified_diff(
                    original["text"].splitlines(),
                    current["text"].splitlines(),
                    fromfile="first synced version",
                    tofile="current version",
                )
            )[:16000],
        }

"""Durable, account-scoped upload pauses without revoking saved context."""

from __future__ import annotations

from varyz.scopes import stored_scope


class ProjectSyncPaused(Exception):
    def __init__(self, project: str):
        self.project = project
        super().__init__("Project syncing is paused")


class ProjectSync:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cloud_project_sync ("
                "account TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,"
                "project TEXT NOT NULL,PRIMARY KEY(account,project))"
            )
        store.project_sync = self

    def paused(self, account: str) -> set[str]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT project FROM cloud_project_sync WHERE account=?", (account,)
            ).fetchall()
        return {r["project"] for r in rows}

    def set_paused(self, account: str, project: str, paused: bool) -> None:
        project = stored_scope(project)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if paused:
                db.execute(
                    "INSERT INTO cloud_project_sync VALUES(?,?) "
                    "ON CONFLICT(account,project) DO NOTHING", (account, project),
                )
            else:
                db.execute(
                    "DELETE FROM cloud_project_sync WHERE account=? AND project=?",
                    (account, project),
                )

    def check(self, db, account: str, project: str) -> None:
        project = stored_scope(project)
        if db.execute(
            "SELECT 1 FROM cloud_project_sync WHERE account=? AND project=?",
            (account, project),
        ).fetchone():
            raise ProjectSyncPaused(project)

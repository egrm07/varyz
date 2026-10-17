"""Durable, account-scoped upload pauses without revoking saved context."""

from __future__ import annotations

from varyz.scopes import stored_scope

from .project_identity import ProjectIdentity, project_names


class ProjectSyncPaused(Exception):
    def __init__(self, project: str):
        self.project = project
        super().__init__("Project syncing is paused")


class ProjectSync:
    def __init__(self, store):
        self.store = store
        if not getattr(store, "project_identity", None):
            ProjectIdentity(store)
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
            names = project_names(self.store, account, db=db)
        return {names(r["project"]) for r in rows}

    def set_paused(self, account: str, project: str, paused: bool) -> None:
        project = stored_scope(project)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            names = project_names(self.store, account, db=db)
            project = names(project)
            if paused:
                db.execute(
                    "INSERT INTO cloud_project_sync VALUES(?,?) "
                    "ON CONFLICT(account,project) DO NOTHING", (account, project),
                )
            else:
                rows = db.execute(
                    "SELECT project FROM cloud_project_sync WHERE account=?", (account,),
                ).fetchall()
                for row in rows:
                    if names(row["project"]) == project:
                        db.execute(
                            "DELETE FROM cloud_project_sync WHERE account=? AND project=?",
                            (account, row["project"]),
                        )

    def check(self, db, account: str, project: str) -> None:
        names = project_names(self.store, account, db=db)
        project = names(project)
        rows = db.execute(
            "SELECT project FROM cloud_project_sync WHERE account=?", (account,),
        ).fetchall()
        if any(names(r["project"]) == project for r in rows):
            raise ProjectSyncPaused(project)

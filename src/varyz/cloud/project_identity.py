"""Durable owner-confirmed project aliases, isolated by Cloud account."""

from __future__ import annotations

import re

from varyz.scopes import checkout_scope, stored_scope


class ProjectNames:
    def __init__(self, rows=()):
        self.aliases = {r["alias"]: r["project"] for r in rows}
        self.families = sorted(
            {r["project"] for r in rows if r["numbered"]}, key=lambda p: (-len(p), p),
        )
        self.patterns = [
            (p, re.compile(re.escape(p) + r"(?:[-_. ]?v?\d+(?:\.\d+)*|\s*\(\d+\))$"))
            for p in self.families
        ]

    def __call__(self, project, *, exact_versions=False):
        scope = checkout_scope(project) if exact_versions else stored_scope(project)
        if scope == "*" or scope.startswith(("org:", "published:")):
            return scope
        seen = set()
        while True:
            if scope in seen:
                raise ValueError("Project aliases contain a cycle")
            seen.add(scope)
            target = self.aliases.get(scope)
            if target is None:
                target = next(
                    (p for p, pattern in self.patterns if pattern.fullmatch(scope)), scope,
                )
            if target == scope:
                return scope
            scope = target


def project_names(store, account, *, db=None):
    identities = getattr(store, "project_identity", None)
    return identities.names(account, db=db) if identities else ProjectNames()


class ProjectIdentity:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cloud_project_aliases ("
                "account TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,"
                "alias TEXT NOT NULL,project TEXT NOT NULL,numbered INTEGER NOT NULL DEFAULT 0,"
                "PRIMARY KEY(account,alias))"
            )
        store.project_identity = self

    def names(self, account, *, db=None):
        if db is None:
            with self.store.connection() as db:
                return self.names(account, db=db)
        return ProjectNames(db.execute(
            "SELECT alias,project,numbered FROM cloud_project_aliases WHERE account=?", (account,),
        ).fetchall())

    def register(self, account, alias, project, *, numbered=False):
        """Record an explicit same-project instruction; never grant upload permission."""
        alias, project = stored_scope(alias), stored_scope(project)
        if any(p == "*" or p.startswith(("org:", "published:")) for p in (alias, project)):
            raise ValueError("Merge exact owner projects, never General or foreign scopes")
        if type(numbered) is not bool or max(len(alias), len(project)) > 128:
            raise ValueError("Invalid project alias")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in db.execute(
                "SELECT alias,project,numbered FROM cloud_project_aliases WHERE account=?",
                (account,),
            ).fetchall() if r["alias"] != alias]
            rows.append(dict(alias=alias, project=project, numbered=int(numbered)))
            proposed = ProjectNames(rows)
            for row in rows:
                proposed(row["alias"])
            db.execute(
                "INSERT INTO cloud_project_aliases VALUES(?,?,?,?) "
                "ON CONFLICT(account,alias) DO UPDATE SET "
                "project=excluded.project,numbered=excluded.numbered",
                (account, alias, project, int(numbered)),
            )

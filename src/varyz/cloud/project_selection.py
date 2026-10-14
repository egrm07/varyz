"""Discover owner-confirmed project names without granting upload permission."""

from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.resolution.workflows import SQLiteWorkflowStore
from varyz.scopes import normalize_scope


def confirmed_scopes(conn) -> list[str]:
    scopes = [r.scope for r in SQLitePrincipleStore(conn).current()]
    scopes += [
        r.scope
        for r in SQLiteKnowledgeStore(conn).current()
        if r.origin == "own" and r.basis.value in {"explicit", "user_confirmed"}
    ]
    scopes += [r.scope for r in SQLiteWorkflowStore(conn).current() if r.basis == "stated"]
    return [normalize_scope(scope) for scope in scopes]

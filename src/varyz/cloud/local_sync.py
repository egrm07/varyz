"""Opt-in projection of confirmed knowledge; never a copy of the archive.

The ledgers remain append-only. Remote changes append local corrections, and
remote deletions append local retractions. The sync journal contains versions
and hashes, not a second plaintext preference archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from varyz.filesystem import atomic_replace
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.knowledge import SQLiteKnowledgeStore, _write_transaction, normalize_scope
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore, normalize_domain
from varyz.resolution.recipes import SQLiteRecipeStore, parse_recipe_steps
from varyz.resolution.recipes import validated as validated_recipe
from varyz.resolution.workflows import SQLiteWorkflowStore, parse_steps

KINDS = ("rule", "answer", "workflow", "recipe")
TABLES = {
    "rule": "owner_principles",
    "answer": "owner_knowledge",
    "workflow": "owner_workflows",
    "recipe": "task_recipes",
}


class SyncTransport(Protocol):
    def push(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def pull(self, cursor: int) -> dict[str, Any]: ...


@contextmanager
def _sync_lock(path: Path):
    from varyz.db.connection import FileLock, LockHeldError

    lock = FileLock(path)
    if not lock.acquire():
        raise LockHeldError("Another cloud sync is running; retry after it finishes.")
    try:
        yield
    finally:
        lock.release()


def fingerprint(payload: dict[str, Any] | None) -> str:
    if payload is not None and "check" in payload:  # a task recipe
        payload = dict(payload)
        payload["scope"] = normalize_scope(payload.get("scope"))
        payload["name"] = " ".join(payload["name"].split()).strip(" -:")
        payload["steps"] = [s.to_dict() for s in parse_recipe_steps(payload["steps"])]
        payload["check"] = " ".join(payload["check"].split())
        payload["pitfalls"] = " ".join(payload.get("pitfalls", "").split())
    elif payload is not None:
        payload = dict(payload)
        payload["scope"] = normalize_scope(payload.get("scope"))
        if "statement" in payload:
            payload["statement"] = " ".join(payload["statement"].split())
            payload.setdefault("kind", "rule")
        if "question" in payload:
            payload["question"] = payload["question"].strip()
            payload["answer"] = payload["answer"].strip()
        else:
            payload["rationale"] = " ".join(payload.get("rationale", "").split())
            payload["domain"] = normalize_domain(payload.get("domain", ""))
        if "steps" in payload:
            payload["name"] = " ".join(payload["name"].split()).strip(" -:")
            payload["steps"] = [s.to_dict() for s in parse_steps(payload["steps"])]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def read_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"cursor": 0, "records": {}, "enabled": False, "projects": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("records"), dict):
        raise ValueError("Cloud sync journal is invalid; restore its backup before syncing.")
    return value


def save_journal(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    atomic_replace(temporary, path)


def select_confirmed(path: Path, projects: list[str] | None = None) -> None:
    journal = read_journal(path)
    scopes = sorted({normalize_scope(p) for p in (projects or [])})
    saved_scopes = sorted({normalize_scope(p) for p in journal.get("projects", [])})
    if journal["records"] and scopes != saved_scopes:
        raise ValueError("Disconnect before changing sync scopes; existing cloud data stays there.")
    journal.update(enabled=True, projects=scopes)
    save_journal(path, journal)


def selected_payloads(
    conn: sqlite3.Connection, projects: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Explicit safe fields only: no provenance, snippets, candidates or imported taste."""
    wanted = {normalize_scope(p) for p in (projects or [])}

    def applies(scope: str) -> bool:
        return not wanted or scope == "*" or scope in wanted

    result: dict[str, dict[str, Any]] = {}
    for rule in SQLitePrincipleStore(conn).current("confirmed"):
        if applies(rule.scope):
            result[f"rule:{rule.id}"] = {
                "statement": rule.statement,
                "kind": rule.kind,
                "scope": rule.scope,
                "rationale": rule.rationale,
                "domain": rule.domain,
            }
    # Reads are bounded to the explicit ledger, not retrieval or personal-model tables.
    for row in conn.execute(
        """SELECT k.* FROM owner_knowledge k
           WHERE k.origin = 'own' AND k.basis IN ('explicit', 'user_confirmed')
           AND NOT EXISTS (SELECT 1 FROM owner_knowledge_retractions r WHERE r.knowledge_id=k.id)
           AND NOT EXISTS (SELECT 1 FROM owner_knowledge newer WHERE newer.supersedes_id=k.id)"""
    ):
        scope = normalize_scope(row["scope"])
        if applies(scope):
            result[f"answer:{row['id']}"] = {
                "question": row["question"],
                "answer": row["answer"],
                "scope": scope,
            }
    for row in conn.execute(
        """SELECT w.* FROM owner_workflows w WHERE w.basis='stated'
           AND NOT EXISTS (SELECT 1 FROM owner_workflow_retractions r WHERE r.workflow_id=w.id)
           AND NOT EXISTS (SELECT 1 FROM owner_workflows newer WHERE newer.supersedes_id=w.id)"""
    ):
        scope = normalize_scope(row["scope"])
        if applies(scope):
            result[f"workflow:{row['id']}"] = {
                "name": row["name"],
                "steps": json.loads(row["steps_json"]),
                "scope": scope,
                "domain": row["domain"],
                "rationale": row["rationale"],
            }
    # A recipe is how an agent got a task done; its provenance (conversation,
    # caller) stays local, and so do its outcomes.
    for row in conn.execute(
        """SELECT t.* FROM task_recipes t
           WHERE NOT EXISTS (SELECT 1 FROM task_recipe_retractions r WHERE r.recipe_id=t.id)
           AND NOT EXISTS (SELECT 1 FROM task_recipes newer WHERE newer.supersedes_id=t.id)"""
    ):
        scope = normalize_scope(row["scope"])
        if applies(scope):
            result[f"recipe:{row['id']}"] = {
                "name": row["name"],
                "steps": json.loads(row["steps_json"]),
                "check": row["check_text"],
                "pitfalls": row["pitfalls"],
                "scope": scope,
            }
    from varyz.privacy.secrets_scan import redact_tree

    for payload in result.values():
        _safe, findings = redact_tree(payload)
        if findings:
            raise ValueError(
                "Selected knowledge contains a possible credential; "
                "review it locally before uploading. No text was sent."
            )
    return result


class LocalSync:
    def __init__(self, conn: sqlite3.Connection, journal_path: Path) -> None:
        self.conn = conn
        self.path = journal_path
        self.journal = read_journal(journal_path)
        self.paused_scopes: set[str] = set()
        self.caller = Caller(
            agent_id="varyz:cloud-sync",
            display_name="Varyz Cloud sync",
            purpose="owner_resolution",
            device_id="cloud-sync",
            transport="sync",
        )

    def plan(self) -> list[dict[str, Any]]:
        if not self.journal.get("enabled"):
            raise ValueError("Cloud uploads are off. Select confirmed knowledge before syncing.")
        selected = selected_payloads(self.conn, self.journal.get("projects", []))
        from varyz.cloud.project_policy import journal_exclusions

        excluded = journal_exclusions(self.path)
        selected = {
            key: payload for key, payload in selected.items()
            if normalize_scope(payload["scope"]) not in excluded
        }
        records = self.journal["records"]
        aliases = {
            f"{key.split(':', 1)[0]}:{item['local_id']}": key
            for key, item in records.items()
            if item.get("local_id")
        }
        # A confirmed ledger correction is a new local row, but the Cloud record
        # keeps its logical identity. Sharing can then show a reviewed update
        # rather than silently forgetting the selected record and selecting a new one.
        lineage_tables = {
            "rule": "owner_principles",
            "answer": "owner_knowledge",
            "workflow": "owner_workflows",
            "recipe": "task_recipes",
        }
        for local_key in selected:
            if local_key in aliases:
                continue
            kind, local_id = local_key.split(":", 1)
            seen = set()
            ancestor = local_id
            for _ in range(100):
                if ancestor in seen:
                    raise ValueError("A confirmed preference has cyclic correction lineage")
                seen.add(ancestor)
                known = aliases.get(f"{kind}:{ancestor}")
                if known:
                    aliases[local_key] = known
                    break
                row = self.conn.execute(
                    f"SELECT supersedes_id FROM {lineage_tables[kind]} WHERE id=?", (ancestor,)
                ).fetchone()
                if not row or not row["supersedes_id"]:
                    break
                ancestor = row["supersedes_id"]
        self._planned_local_ids = {}
        changes: dict[str, dict[str, Any]] = {}
        present: set[str] = set()
        for local_key, payload in selected.items():
            key = aliases.get(local_key, local_key)
            if key in present:
                raise ValueError("Conflicting confirmed corrections need review before Cloud sync")
            self._planned_local_ids[key] = local_key.split(":", 1)[1]
            present.add(key)
            entry = records.get(key, {})
            previous_scope = None
            if entry.get("local_id"):
                kind = key.split(":", 1)[0]
                row = self.conn.execute(
                    f"SELECT scope FROM {TABLES[kind]} WHERE id=?", (entry["local_id"],)
                ).fetchone()
                previous_scope = normalize_scope(row["scope"]) if row else None
            if normalize_scope(payload["scope"]) in self.paused_scopes or (
                previous_scope in self.paused_scopes
            ):
                continue
            if entry.get("hash") != fingerprint(payload):
                kind, identity = key.split(":", 1)
                changes[key] = {
                    "kind": kind,
                    "id": identity,
                    "base_version": entry.get("version", 0),
                    "payload": payload,
                }
        for key, entry in records.items():
            if key not in present and entry.get("hash") != fingerprint(None):
                kind, identity = key.split(":", 1)
                changes[key] = {
                    "kind": kind,
                    "id": identity,
                    "base_version": entry["version"],
                    "payload": None,
                }
        return sorted(changes.values(), key=lambda change: change["payload"] is not None)

    def preview(self) -> dict[str, Any]:
        changes = self.plan()
        return {
            "uploads": sum(r["payload"] is not None for r in changes),
            "deletions": sum(r["payload"] is None for r in changes),
            "by_kind": {k: sum(r["kind"] == k for r in changes) for k in KINDS},
            "sends_text": False,
        }

    def sync(
        self,
        transport: SyncTransport,
        *,
        conflict_policy: str = "stop",
        prepare: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        from varyz.cloud.client import CloudConflict, CloudProjectPaused

        if conflict_policy not in {"stop", "remote", "local"}:
            raise ValueError("Invalid conflict policy.")
        # Prevent parallel clients from racing the sidecar version journal.
        with _sync_lock(self.path.with_suffix(".lock")):
            self.journal = read_journal(self.path)
            if prepare is not None:
                self.journal = prepare(self.journal)
                save_journal(self.path, self.journal)
            self.paused_scopes = getattr(transport, "paused_projects", lambda: set())()
            uploaded = 0
            conflicts = 0
            while changes := self.plan():
                batch: list[dict[str, Any]] = []
                # Keep a request below the cloud's 256 KiB body limit, including JSON overhead.
                for change in changes[:100]:
                    if batch and batch[0]["payload"] is None and change["payload"] is not None:
                        break
                    if batch and len(json.dumps({"records": [*batch, change]}).encode()) > 200_000:
                        break
                    batch.append(change)
                try:
                    acknowledged = transport.push(batch)
                except CloudProjectPaused as exc:
                    if exc.project in self.paused_scopes:
                        raise
                    self.paused_scopes.add(exc.project)
                    continue
                except CloudConflict as exc:
                    if conflict_policy == "stop" or conflicts >= 100:
                        raise
                    record = exc.record
                    key = f"{record['kind']}:{record['id']}"
                    matching = next(
                        (
                            r
                            for r in batch
                            if r["kind"] == record["kind"] and r["id"] == record["id"]
                        ),
                        None,
                    )
                    if matching is None or record["version"] < 1:
                        raise ValueError("Cloud returned an unrelated conflict.") from exc
                    if conflict_policy == "remote":
                        self._apply(record)
                    else:
                        previous = self.journal["records"].get(key, {})
                        self.journal["records"][key] = {
                            "local_id": previous.get("local_id", record["id"]),
                            "version": record["version"],
                            "hash": fingerprint(record["payload"]),
                        }
                    save_journal(self.path, self.journal)
                    conflicts += 1
                    continue
                expected = {(r["kind"], r["id"]): r for r in batch}
                if {(r["kind"], r["id"]) for r in acknowledged} != set(expected):
                    raise ValueError("Cloud did not acknowledge the complete batch; retry sync.")
                for record in acknowledged:
                    request = expected[(record["kind"], record["id"])]
                    if fingerprint(record["payload"]) != fingerprint(request["payload"]):
                        raise ValueError("Cloud acknowledgment changed a payload; sync stopped.")
                    key = f"{record['kind']}:{record['id']}"
                    previous = self.journal["records"].get(key, {})
                    self.journal["records"][key] = {
                        "local_id": self._planned_local_ids.get(
                            key, previous.get("local_id", record["id"])
                        ),
                        "version": record["version"],
                        "hash": fingerprint(record["payload"]),
                    }
                save_journal(self.path, self.journal)
                uploaded += len(batch)
            downloaded = 0
            while True:
                page = transport.pull(self.journal["cursor"])
                for record in page["records"]:
                    self._apply(record)
                    downloaded += 1
                cursor = int(page["cursor"])
                if cursor < self.journal["cursor"]:
                    raise ValueError("Cloud returned a backwards cursor; sync stopped.")
                if page.get("has_more") and cursor == self.journal["cursor"]:
                    raise ValueError("Cloud pagination made no progress; sync stopped.")
                self.journal["cursor"] = cursor
                save_journal(self.path, self.journal)
                if not page.get("has_more"):
                    break
            return {"uploaded": uploaded, "downloaded": downloaded}

    def _apply(self, record: dict[str, Any]) -> None:
        kind, identity, version = record["kind"], record["id"], int(record["version"])
        if kind not in KINDS or version < 1:
            raise ValueError("Invalid cloud record.")
        key = f"{kind}:{identity}"
        previous = self.journal["records"].get(key, {})
        if previous.get("version", 0) >= version:
            return
        payload = record["payload"]
        if payload is not None:
            from varyz.privacy.secrets_scan import redact_tree

            _safe, findings = redact_tree(payload)
            if findings:
                raise ValueError("Cloud knowledge contains a possible credential; sync stopped.")
        if (
            payload is not None
            and self.journal.get("projects")
            and normalize_scope(payload.get("scope"))
            not in {"*", *(normalize_scope(p) for p in self.journal["projects"])}
        ):
            return
        local_id = previous.get("local_id")
        from varyz.cloud.project_policy import journal_exclusions

        excluded = journal_exclusions(self.path)
        scope = normalize_scope(payload.get("scope")) if payload is not None else None
        if payload is None and local_id:
            local_row = self.conn.execute(
                f"SELECT scope FROM {TABLES[kind]} WHERE id=?", (local_id,)
            ).fetchone()
            scope = normalize_scope(local_row["scope"]) if local_row else None
        if scope in excluded:
            # Removing a Cloud copy is not withdrawal of the owner's local rule.
            self.journal["records"][key] = {
                "local_id": local_id, "version": version, "hash": fingerprint(payload),
            }
            return
        if payload is None:
            if local_id:
                self._retract(kind, local_id)
        else:
            source = f"cloud-sync:{kind}:{identity}:v{version}"
            # Recovery after an interrupted local commit, before journal replacement.
            found = self.conn.execute(
                f"SELECT id FROM {TABLES[kind]} WHERE source=?", (source,)
            ).fetchone()
            local_id = str(found[0]) if found else self._append(kind, payload, source, local_id)
        self.journal["records"][key] = {
            "local_id": local_id,
            "version": version,
            "hash": fingerprint(payload),
        }

    def _append(self, kind: str, p: dict[str, Any], source: str, old: str | None) -> str:
        common = {"caller": self.caller, "device_id": "cloud-sync", "source": source}
        if kind == "rule":
            store = SQLitePrincipleStore(self.conn)
            statement, rule_kind, scope, rationale, source = store._validated(
                p["statement"], p.get("kind", "rule"), p["scope"], p.get("rationale", ""), source
            )
            domain = normalize_domain(p.get("domain", ""))
            current = {rule.id for rule in store.current()}
            # Independent cloud identities must not deduplicate into one local
            # row: aliasing them would delete one on the next projection.
            with _write_transaction(self.conn, store._lock):
                return store._insert(
                    statement=statement,
                    kind=rule_kind,
                    scope=scope,
                    rationale=rationale,
                    domain=domain,
                    status="confirmed",
                    source=source,
                    provenance={},
                    supersedes_id=old if old in current else None,
                    caller=self.caller,
                    device_id="cloud-sync",
                )
        if kind == "answer":
            return SQLiteKnowledgeStore(self.conn).remember(
                question=p["question"],
                answer=p["answer"],
                scope=p["scope"],
                basis=AnswerBasis.USER_CONFIRMED,
                supersedes_id=old,
                **common,
            )
        if kind == "recipe":
            recipes = SQLiteRecipeStore(self.conn)
            # The same size cap and credential scan as a local recipe, appended
            # with its remote predecessor rather than deduplicated by name.
            name, steps, check, pitfalls, scope = validated_recipe(
                p["name"], p["steps"], p["check"], p.get("pitfalls", ""), p["scope"]
            )
            with _write_transaction(self.conn, recipes._lock):
                return recipes._insert(
                    name=name,
                    scope=scope,
                    steps=steps,
                    check=check,
                    pitfalls=pitfalls,
                    source=source,
                    provenance={},
                    caller=self.caller,
                    device_id="cloud-sync",
                    supersedes_id=old,
                )
        # record() deduplicates on name/order and intentionally ignores rationale.
        # A remote revision needs its explicit predecessor even when its name or
        # scope changes, so append through the ledger's validated writer here.
        store = SQLiteWorkflowStore(self.conn)
        name, scope, domain, rationale, source = store._validated(
            p["name"], p["scope"], p.get("domain", ""), p.get("rationale", ""), source
        )
        steps = parse_steps(p["steps"])
        with _write_transaction(self.conn, store._lock):
            return store._insert(
                name=name,
                scope=scope,
                domain=domain,
                steps=steps,
                rationale=rationale,
                basis="stated",
                source=source,
                provenance={},
                supersedes_id=old,
                caller=self.caller,
                device_id="cloud-sync",
            )

    def _retract(self, kind: str, local_id: str) -> None:
        stores = {
            "rule": SQLitePrincipleStore,
            "answer": SQLiteKnowledgeStore,
            "workflow": SQLiteWorkflowStore,
            "recipe": SQLiteRecipeStore,
        }
        store = stores[kind](self.conn)
        try:
            store.retract(
                local_id,
                caller=self.caller,
                device_id="cloud-sync",
                reason="Withdrawn in Varyz Cloud",
            )
        except ValueError as exc:
            if "already" not in str(exc):
                raise

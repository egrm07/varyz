"""Explicit per-record approval, offline previews and resumable one-way archive sync."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from varyz.cloud.archive import MODEL, Chunk, Document
from varyz.cloud.local_sync import _sync_lock, save_journal
from varyz.scopes import normalize_scope


def read_archive_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "enabled": False,
            "selected": {},
            "projects": {},
            "records": {},
            "excluded": [],
            "account_id": None,
            "url": None,
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("selected"), dict)
        or not isinstance(value.get("records"), dict)
    ):
        raise ValueError("Archive sync journal is invalid; restore its backup")
    value.setdefault("projects", {})
    value.setdefault("excluded", [])
    return value


def document(conn: sqlite3.Connection, identity: str, project: str) -> Document:
    from varyz.connectors.ai_conversations import is_machine_entry

    row = conn.execute("SELECT * FROM entries WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ValueError("Archive entry unavailable")
    text = row["body"]
    return Document(
        id=row["id"],
        project=project,
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"] or "",
        text=text,
        occurred_at=row["occurred_start"] or row["ingested_at"],
        sensitivity=row["sensitivity"],
        thread_id=row["thread_id"],
        authored=bool(row["authored_by_user"]) and not is_machine_entry(row["source"], text),
        chunks=[Chunk(text=text[start : start + 2000]) for start in range(0, len(text), 2000)],
    )


def fingerprint(payload: dict[str, Any] | None) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def allow_project(
    path: Path, project: str, root: Path | None = None, repository: str | None = None
) -> None:
    """Approve existing and future eligible records tied to this exact project."""
    project = normalize_scope(project)
    if project == "*":
        root = None  # General means uncategorized records, never a wildcard over named projects.
        repository = None
    if repository is not None:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Use an exact GitHub owner/repository")
    with _sync_lock(path.with_suffix(".lock")):
        journal = read_archive_journal(path)
        journal["projects"][project] = {
            "root": str(root.resolve()) if root else None,
            "repository": repository.casefold() if repository else None,
        }
        journal["enabled"] = True
        journal.setdefault("pending_permits", []).append(project)
        save_journal(path, journal)


def entry_project(
    row: Any, projects: dict[str, Any], *, resolved_roots: dict[str, Path] | None = None
) -> str | None:
    """Use connector provenance, never topic matching or a guess from message wording."""
    source = row["source"]
    if source == "ai_conversations":
        header = row["body"].split("\n\n", 1)[0].splitlines()
        if len(header) == 2 and header[1].startswith("Conversation: ") and " — " in header[1]:
            project = normalize_scope(header[1].rsplit(" — ", 1)[1])
            return project
    elif source in {"git", "documents"}:
        external = row["external_id"].rsplit("@", 1)[0] if source == "git" else row["external_id"]
        file = Path(external)
        if not file.is_absolute():
            return "*"
        file = file.resolve()
        matches = []
        for project, approval in projects.items():
            if not approval.get("root"):
                continue
            root = (
                resolved_roots[project] if resolved_roots is not None
                else Path(approval["root"]).resolve()
            )
            if source == "git" and file == root:
                matches.append((len(str(root)), project))
            elif source == "documents" and file.is_relative_to(root):
                # A nested repository has its own approval, rather than inheriting the parent.
                parent = file.parent
                nested = False
                while parent != root and parent.is_relative_to(root):
                    if (parent / ".git").exists():
                        nested = True
                        break
                    parent = parent.parent
                if not nested:
                    matches.append((len(str(root)), project))
        if matches:
            matches.sort(reverse=True)
            if len(matches) > 1 and matches[0][0] == matches[1][0]:
                return "*"
            return matches[0][1]
        if source == "git":
            name = normalize_scope(file.name)
            return "unapproved:" + name if projects.get(name, {}).get("root") else name
        parent = file.parent
        while parent != parent.parent:
            if (parent / ".git").exists():
                name = normalize_scope(parent.name)
                return "unapproved:" + name if projects.get(name, {}).get("root") else name
            parent = parent.parent
    elif source == "github":
        repo = (row["thread_id"] or "").removeprefix("gh:").split("#", 1)[0].casefold()
        repo_matches = [
            project for project, approval in projects.items() if approval.get("repository") == repo
        ]
        if len(repo_matches) == 1:
            return repo_matches[0]
        if repo and "/" in repo:
            name = normalize_scope(repo.rsplit("/", 1)[1])
            return "unapproved:" + name if name in projects else name
    return "*"


def approve(
    conn: sqlite3.Connection, path: Path, identities: list[str], project: str
) -> dict[str, Any]:
    if not identities:
        raise ValueError("Select explicit entry IDs; no archive-wide approval exists")
    with _sync_lock(path.with_suffix(".lock")):
        journal = read_archive_journal(path)
        approved = {identity: document(conn, identity, project) for identity in identities}
        for identity, doc in approved.items():
            if identity in journal["excluded"]:
                journal["excluded"].remove(identity)
            journal["selected"][identity] = {
                "project": doc.project,
                "hash": fingerprint(doc.model_dump()),
            }
        journal["enabled"] = True
        save_journal(path, journal)
    return {"approved": len(approved), "sends_text": False}


class ArchiveSync:
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path

    def plan(
        self, journal: dict[str, Any] | None = None, *, semantic: bool = False
    ) -> dict[str, Any]:
        journal = journal or read_archive_journal(self.path)
        from varyz.cloud.project_policy import journal_exclusions

        excluded = journal_exclusions(self.path)
        changes = []
        blocked = []
        valid = set()
        selections = {
            key: approval for key, approval in journal["selected"].items()
            if normalize_scope(approval["project"]) not in excluded
        }
        for row in self.conn.execute(
            "SELECT * FROM entries WHERE source IN ('ai_conversations','documents','git','github')"
        ):
            project = entry_project(row, journal["projects"])
            if (project in journal["projects"] and project not in excluded
                    and row["id"] not in journal["excluded"]):
                selections[row["id"]] = {"project": project, "hash": None}
        for identity, approval in selections.items():
            try:
                doc = document(self.conn, identity, approval["project"])
                payload = doc.model_dump()
                if approval["hash"] is not None and fingerprint(payload) != approval["hash"]:
                    raise ValueError("Approval expired after content or metadata changed")
                valid.add(identity)
                saved = journal["records"].get(identity, {})
                if (
                    saved.get("approval_hash") != fingerprint(payload)
                    or saved.get("deleted")
                    or semantic
                    and not saved.get("semantic")
                ):
                    changes.append(
                        {
                            "id": identity,
                            "base_version": saved.get("version", 0),
                            "payload": payload,
                        }
                    )
            except ValueError:
                blocked.append(identity)
        for identity, saved in journal["records"].items():
            if identity not in valid and not saved.get("deleted"):
                changes.append({"id": identity, "base_version": saved["version"], "payload": None})
        # Approved removals free storage before new/updated documents consume it.
        # Otherwise a valid replacement selection can fail at the first batch
        # even though its final projection fits the account's quota.
        changes.sort(key=lambda change: change["payload"] is not None)
        return {"changes": changes, "blocked_ids": blocked}

    def preview(self) -> dict[str, Any]:
        journal = read_archive_journal(self.path)
        plan = self.plan(journal)
        return {
            "enabled": bool(journal["enabled"]),
            "approved": len(journal["selected"]),
            "projects": sorted(journal["projects"]),
            "uploads": sum(c["payload"] is not None for c in plan["changes"]),
            "deletions": sum(c["payload"] is None for c in plan["changes"]),
            "blocked_ids": plan["blocked_ids"],
            "sends_text": False,
            "payload_bytes": sum(
                len(json.dumps(c, ensure_ascii=False).encode()) for c in plan["changes"]
            ),
        }

    def control(
        self, action: str, identities: list[str] | None = None, project: str | None = None
    ) -> dict[str, Any]:
        with _sync_lock(self.path.with_suffix(".lock")):
            journal = read_archive_journal(self.path)
            if action == "pause":
                journal["enabled"] = False
            elif action == "resume":
                journal["enabled"] = True
            elif action == "revoke":
                if project:
                    scope = normalize_scope(project)
                    journal["projects"].pop(scope, None)
                    journal["selected"] = {
                        key: row
                        for key, row in journal["selected"].items()
                        if row["project"] != scope
                    }
                for identity in identities or []:
                    journal["selected"].pop(identity, None)
                    if identity not in journal["excluded"]:
                        journal["excluded"].append(identity)
            else:
                raise ValueError("Choose pause, resume or revoke")
            save_journal(self.path, journal)
        return self.preview()

    def sync(
        self, client: Any, account_id: str, *, embedder: Any = None, batch_size: int = 25
    ) -> dict[str, int]:
        from varyz.cloud.client import CloudStorageLimit

        if not 1 <= batch_size <= 25:
            raise ValueError("Archive batch size must be between 1 and 25")
        uploaded = deleted = 0
        with _sync_lock(self.path.with_suffix(".lock")):
            journal = read_archive_journal(self.path)
            if not journal["enabled"]:
                raise ValueError("Archive sync is paused or unapproved")
            if journal.get("account_id") not in (None, account_id) or journal.get("url") not in (
                None,
                client.url,
            ):
                raise ValueError("Archive journal belongs to another Cloud account or origin")
            journal.update(account_id=account_id, url=client.url)
            save_journal(self.path, journal)
            if embedder is not None:
                journal["semantic"] = True
                journal["model_cache"] = (
                    embedder.cache_dir if hasattr(embedder, "cache_dir") else embedder.cache
                )
            for project in journal.get("pending_permits", []):
                client.request("POST", "/api/archive/permit", json={"project": project})
            journal["pending_permits"] = []
            remote = client.request("GET", "/api/archive/status")
            revoked = {p["project"] for p in remote.get("projects", []) if p["revoked"]}
            for project in revoked:
                journal["projects"].pop(project, None)
                journal["selected"] = {
                    k: v for k, v in journal["selected"].items() if v["project"] != project
                }
            if revoked:
                manifest = client.request("GET", "/api/archive/manifest")
                for row in manifest.get("records", []):
                    if row["id"] in journal["records"]:
                        journal["records"][row["id"]].update(
                            version=row["version"], fingerprint=row["fingerprint"]
                        )
            save_journal(self.path, journal)
            plan = self.plan(journal, semantic=embedder is not None)
            paused = {normalize_scope(p) for p in remote.get("paused_projects", [])}
            plan["changes"] = [
                c for c in plan["changes"]
                if c["payload"] is None or normalize_scope(c["payload"]["project"]) not in paused
            ]
            batch: list[dict[str, Any]] = []
            hashes: dict[str, str | None] = {}

            def flush() -> bool:
                nonlocal uploaded, deleted
                try:
                    response = client.request("POST", "/api/archive/sync", json={"records": batch})
                except CloudStorageLimit:
                    journal["storage_full"] = True
                    save_journal(self.path, journal)
                    return False
                rows = response.get("records", [])
                expected = {c["id"]: fingerprint(c["payload"]) for c in batch}
                received = {r.get("id"): r.get("fingerprint") for r in rows}
                if len(rows) != len(batch) or received != expected:
                    raise ValueError("Cloud archive acknowledgement mismatch")
                by_id = {row["id"]: row for row in rows}
                for change in batch:
                    identity = change["id"]
                    journal["records"][identity] = {
                        "version": by_id[identity]["version"],
                        "fingerprint": by_id[identity]["fingerprint"],
                        "approval_hash": hashes[identity],
                        "deleted": change["payload"] is None,
                        "semantic": embedder is not None,
                    }
                    uploaded += change["payload"] is not None
                    deleted += change["payload"] is None
                save_journal(self.path, journal)
                batch.clear()
                hashes.clear()
                return True

            for change in plan["changes"]:
                approval_hash = fingerprint(change["payload"]) if change["payload"] else None
                if embedder is not None and change["payload"] is not None:
                    payload = change["payload"]
                    if embedder.model_name != MODEL:
                        raise ValueError(
                            "Cloud semantic retrieval needs the supported embedding model"
                        )
                    vectors = embedder.embed_documents([c["text"] for c in payload["chunks"]])
                    if len(vectors) != len(payload["chunks"]):
                        raise ValueError("Embedding result did not cover every source chunk")
                    for chunk, vector in zip(payload["chunks"], vectors, strict=True):
                        # Bound JSON transfer size without materially changing cosine ranking.
                        chunk["vector"] = [round(float(value), 6) for value in vector]
                    payload["model"] = MODEL
                    Document.model_validate(payload)
                # Bound each batch by both the API record and byte limits.
                if len(json.dumps({"records": [change]}, ensure_ascii=False).encode()) > 250000:
                    raise ValueError(
                        "Selected record exceeds the Cloud request limit; select smaller records"
                    )
                if (
                    batch
                    and (
                        (batch[-1]["payload"] is None and change["payload"] is not None)
                        or
                        len(batch) >= batch_size
                        or len(
                            json.dumps({"records": [*batch, change]}, ensure_ascii=False).encode()
                        )
                        > 250000
                    )
                    and not flush()
                ):
                    return dict(
                        uploaded=uploaded,
                        deleted=deleted,
                        blocked=len(plan["blocked_ids"]),
                        storage_full=True,
                    )
                batch.append(change)
                hashes[change["id"]] = approval_hash
            if batch and not flush():
                return dict(
                    uploaded=uploaded,
                    deleted=deleted,
                    blocked=len(plan["blocked_ids"]),
                    storage_full=True,
                )
            if journal.pop("storage_full", False):
                save_journal(self.path, journal)
        return {"uploaded": uploaded, "deleted": deleted, "blocked": len(plan["blocked_ids"])}

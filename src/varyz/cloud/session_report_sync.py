"""Separate project consent and resumable report-only egress through the existing client."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from varyz.cloud.local_sync import _sync_lock, save_journal
from varyz.cloud.session_reports import Report, fingerprint, project_name
from varyz.scopes import normalize_scope


def journal_path(config: Path) -> Path:
    return config.with_name(config.stem + ".cloud-reports.json")


def read_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(
            enabled=False,
            projects={},
            records={},
            pending_permits=[],
            pending_revokes=[],
            account_id=None,
            url=None,
            since=None,
        )
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("projects"), dict)
        or not isinstance(value.get("records"), dict)
    ):
        raise ValueError("Invalid report sync journal")
    value.setdefault("pending_permits", [])
    value.setdefault("pending_revokes", [])
    value.setdefault("since", None)
    if value["since"] is not None:
        date.fromisoformat(value["since"])
    return value


class ReportSync:
    def __init__(self, conn, config: Path):
        self.conn = conn
        self.config = config
        self.path = journal_path(config)

    def projects(self):
        from varyz.cloud.project_selection import confirmed_scopes

        counts = {project_name(p): 0 for p in read_journal(self.path)["projects"]}
        for scope in confirmed_scopes(self.conn):
            if len(scope) <= 128 and not scope.startswith(("org:", "published:")):
                counts.setdefault(project_name(scope), 0)
        for row in self.conn.execute(
            "SELECT project,COUNT(*) AS n FROM session_progress GROUP BY project"
        ):
            counts[project_name(row["project"])] = row["n"]
        return counts

    def plan(self, journal=None):
        from varyz.config import load_config
        from varyz.pipeline.sensitivity import SensitivityGate
        from varyz.session_progress import HistoryQuery, history

        journal = journal if journal is not None else read_journal(self.path)
        cfg = load_config(self.config)
        gate = SensitivityGate(cfg.sensitivity)
        since = (
            cfg.session_reports.cloud_since.isoformat()
            if cfg.session_reports.cloud_since
            else journal.get("since")
        )
        approved = set(journal["projects"])
        from varyz.cloud.project_policy import journal_exclusions

        approved = {p for p in approved if normalize_scope(p) not in journal_exclusions(self.path)}
        valid, blocked = {}, 0
        groups = self.conn.execute(
            "SELECT DISTINCT project,conversation_id,agent_id FROM session_progress"
        ).fetchall()
        excluded = set()
        for group in groups:
            if project_name(group["project"]) not in approved:
                continue
            query = HistoryQuery(
                project=group["project"],
                conversation_id=group["conversation_id"],
                agent=group["agent_id"],
            )
            current = history(self.conn, query, cfg=cfg)
            if current["withheld_sensitive"]:
                # Never make an older report look current when the newest is private.
                excluded.add((group["project"], group["conversation_id"]))
            elif current["reports"]:
                report = current["reports"][0]
                if str(gate.label("ai_conversations", report["task"], json.dumps(report))) not in {
                    "public",
                    "personal",
                }:
                    excluded.add((group["project"], group["conversation_id"]))
        for group in groups:
            project = project_name(group["project"])
            if project not in approved:
                continue
            if (group["project"], group["conversation_id"]) in excluded:
                blocked += 1
                continue
            before = None
            while True:
                query = HistoryQuery(
                    project=group["project"],
                    conversation_id=group["conversation_id"],
                    agent=group["agent_id"],
                    latest=False,
                    limit=20,
                    before_seq=before,
                )
                page = history(self.conn, query, cfg=cfg, max_chars=120_000)
                blocked += page["withheld_sensitive"]
                for report in page["reports"]:
                    if since and report["occurred_at"][:10] < since:
                        continue
                    if str(
                        gate.label("ai_conversations", report["task"], json.dumps(report))
                    ) not in {"public", "personal"}:
                        blocked += 1
                        continue
                    try:
                        payload = Report.model_validate(report).model_dump(exclude_none=True)
                    except ValueError:
                        blocked += 1
                        continue
                    valid[payload["reference"].split(":", 1)[1]] = payload
                next_page = page.get("next_before_seq")
                if not next_page:
                    break
                if before and next_page >= before:
                    raise ValueError("Report cursor did not advance")
                before = next_page
        changes = []
        for identity, payload in valid.items():
            saved = journal["records"].get(identity, {})
            if saved.get("fingerprint") != fingerprint(payload) or saved.get("deleted"):
                changes.append(
                    dict(id=identity, base_version=saved.get("version", 0), payload=payload)
                )
        for identity, saved in journal["records"].items():
            if identity not in valid and not saved.get("deleted"):
                changes.append(dict(id=identity, base_version=saved["version"], payload=None))
        # Remove no-longer-approved/private data before new uploads.
        changes.sort(
            key=lambda c: (
                c["payload"] is not None,
                c["payload"]["seq"] if c["payload"] else 0,
                c["id"],
            )
        )
        return dict(changes=changes, blocked=blocked)

    def draft(self, projects, *, project_scopes=None):
        if (
            not isinstance(projects, list)
            or len(projects) > 1000
            or any(not isinstance(p, str) for p in projects)
        ):
            raise ValueError("Choose report projects")
        selected = {project_name(p) for p in projects}
        if not selected <= self.projects().keys():
            raise ValueError("Review available report projects")
        if project_scopes is not None and (
            not isinstance(project_scopes, list)
            or len(project_scopes) > 1000
            or any(not isinstance(p, str) for p in project_scopes)
            or not set(project_scopes) <= {normalize_scope(p) for p in self.projects()}
        ):
            raise ValueError("Review available report project scopes")
        journal = read_journal(self.path)
        draft = {
            **journal,
            "projects": {p: {} for p in sorted(selected)},
            "project_scopes": project_scopes,
        }
        plan = self.plan(draft)
        signature = fingerprint(dict(journal=draft, plan=plan))
        return draft, plan, signature

    def select(self, projects, expected_signature, account_id, url, *, project_scopes=None):
        with _sync_lock(self.path.with_suffix(".lock")):
            draft, _, signature = self.draft(projects, project_scopes=project_scopes)
            if signature != expected_signature:
                raise ValueError("Reports changed; review a fresh preview")
            old = read_journal(self.path)
            if old.get("account_id") not in (None, account_id) or old.get("url") not in (None, url):
                raise ValueError("Report approval belongs to another account")
            removed = set(old["projects"]) - set(draft["projects"])
            draft.update(
                enabled=True,
                account_id=account_id,
                url=url,
                # Only this reviewed owner action can renew a remote revocation.
                pending_permits=sorted(draft["projects"]),
                pending_revokes=sorted(set(old["pending_revokes"]) | removed),
            )
            save_journal(self.path, draft)

    def preview(self):
        journal = read_journal(self.path)
        plan = self.plan(journal)
        return dict(
            enabled=journal["enabled"],
            projects=sorted(journal["projects"]),
            uploads=sum(c["payload"] is not None for c in plan["changes"]),
            deletions=sum(c["payload"] is None for c in plan["changes"]),
            blocked=plan["blocked"],
            sends_text=False,
        )

    def sync(self, client, account_id, batch_size=25):
        from varyz.cloud.client import CloudProjectPaused, CloudStorageLimit

        if type(batch_size) is not int or not 1 <= batch_size <= 25:
            raise ValueError("Use report batches of 1-25")
        uploaded = deleted = 0
        if not read_journal(self.path)["enabled"]:
            return dict(uploaded=0, deleted=0, paused=True)
        owner = client.request("GET", "/api/account")
        if owner.get("id", owner.get("account_id")) != account_id:
            raise ValueError("Cloud credentials belong to another account")
        while True:
            # One batch per lock, so the owner can revoke while a backfill runs.
            with _sync_lock(self.path.with_suffix(".lock")):
                journal = read_journal(self.path)
                if not journal["enabled"]:
                    return dict(uploaded=uploaded, deleted=deleted, paused=True)
                if journal.get("account_id") != account_id or journal.get("url") != client.url:
                    raise ValueError("Report journal belongs to another account or origin")
                remote = client.request("GET", "/api/reports/status")
                previously_revoked = {p["project"] for p in remote["projects"] if p["revoked"]}
                # Only a fresh reviewed selection may renew a remote revocation.
                # Unrevoked logical projects include their future versioned folders;
                # older category approvals remain exact-name only.
                scopes = set(journal.get("project_scopes") or [])
                revoked_scopes = {
                    normalize_scope(p)
                    for p in previously_revoked
                    if p not in journal["pending_permits"]
                }
                scopes -= revoked_scopes
                if journal.get("project_scopes") is not None:
                    journal["project_scopes"] = sorted(scopes)
                added = {p for p in self.projects() if normalize_scope(p) in scopes}
                added -= journal["projects"].keys() | previously_revoked
                if added:
                    journal["projects"].update({p: {} for p in added})
                    journal["pending_permits"] = sorted(set(journal["pending_permits"]) | added)
                updating_permissions = bool(
                    journal["pending_revokes"] or journal["pending_permits"]
                )
                for project in journal["pending_revokes"]:
                    removed = client.request(
                        "POST", "/api/reports/revoke", json={"project": project}
                    )
                    deleted += removed["removed"]
                journal["pending_revokes"] = []
                for project in journal["pending_permits"]:
                    client.request("POST", "/api/reports/permit", json={"project": project})
                journal["pending_permits"] = []
                if updating_permissions:
                    remote = client.request("GET", "/api/reports/status")
                revoked = {p["project"] for p in remote["projects"] if p["revoked"]}
                for project in revoked:
                    journal["projects"].pop(project, None)
                if revoked or previously_revoked:
                    manifest = client.request("GET", "/api/reports/manifest")
                    for row in manifest["records"]:
                        if row["id"] in journal["records"]:
                            journal["records"][row["id"]].update(
                                version=row["version"],
                                fingerprint=row["fingerprint"],
                                deleted=row["fingerprint"] == fingerprint(None),
                            )
                save_journal(self.path, journal)
                plan = self.plan(journal)
                paused = {normalize_scope(p) for p in remote.get("paused_projects", [])}
                plan["changes"] = [
                    c for c in plan["changes"]
                    if c["payload"] is None
                    or normalize_scope(c["payload"]["project"]) not in paused
                ]
                batch = plan["changes"][:batch_size]
                if batch and batch[0]["payload"] is None:
                    batch = [change for change in batch if change["payload"] is None]
                while (
                    batch
                    and len(json.dumps({"records": batch}, ensure_ascii=False).encode()) > 240000
                ):
                    batch.pop()
                if not batch:
                    return dict(uploaded=uploaded, deleted=deleted, blocked=plan["blocked"])
                try:
                    response = client.request("POST", "/api/reports/sync", json={"records": batch})
                except CloudProjectPaused:
                    continue  # Refresh remote pause state without acknowledging this batch.
                except CloudStorageLimit:
                    journal["storage_full"] = True
                    save_journal(self.path, journal)
                    return dict(
                        uploaded=uploaded,
                        deleted=deleted,
                        blocked=plan["blocked"],
                        storage_full=True,
                    )
                journal.pop("storage_full", None)
                rows = response.get("records", [])
                expected = {c["id"]: fingerprint(c["payload"]) for c in batch}
                received = {r.get("id"): r.get("fingerprint") for r in rows}
                if (
                    len(rows) != len(batch)
                    or received != expected
                    or any(type(r.get("version")) is not int or r["version"] < 1 for r in rows)
                ):
                    raise ValueError("Cloud report acknowledgement mismatch")
                for row in rows:
                    journal["records"][row["id"]] = dict(
                        version=row["version"],
                        fingerprint=row["fingerprint"],
                        deleted=expected[row["id"]] == fingerprint(None),
                    )
                uploaded += sum(c["payload"] is not None for c in batch)
                deleted += sum(c["payload"] is None for c in batch)
                save_journal(self.path, journal)

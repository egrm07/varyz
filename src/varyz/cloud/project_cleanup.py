"""Preview and persist exact project removals without deleting local evidence."""

from __future__ import annotations

from pathlib import Path

from varyz.cloud.archive_sync import read_archive_journal
from varyz.cloud.local_sync import _sync_lock, read_journal, save_journal
from varyz.cloud.project_policy import exclude
from varyz.cloud.session_report_sync import read_journal as read_reports
from varyz.scopes import normalize_scope


def remove_projects(config: Path, projects: list[str], *, apply: bool = False) -> dict:
    names = {normalize_scope(p) for p in projects}
    if not names or "*" in names:
        raise ValueError("Choose exact named projects, not General")
    paths = {
        lane: config.with_name(config.stem + suffix)
        for lane, suffix in (
            ("preferences", ".cloud-sync.json"),
            ("history", ".cloud-archive.json"),
            ("reports", ".cloud-reports.json"),
        )
    }
    readers = {
        "preferences": read_journal,
        "history": read_archive_journal,
        "reports": read_reports,
    }
    removed = {}
    # Publish the veto first. Every updated engine checks it, including legacy
    # all-project approvals and individually selected archive records.
    if apply:
        exclude(config, projects)
    for lane, path in paths.items():
        if not path.exists():
            removed[lane] = []
            continue
        with _sync_lock(path.with_suffix(".lock")):
            journal = readers[lane](path)
            previous = journal.get("projects", {})
            removed[lane] = sorted(p for p in previous if normalize_scope(p) in names)
            if not apply:
                continue
            backup = path.with_suffix(path.suffix + ".before-project-cleanup")
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
            if lane == "preferences":
                # Empty means all in the legacy engine; '*' means General only.
                if previous:
                    journal["projects"] = [
                        p for p in previous if normalize_scope(p) not in names
                    ] or ["*"]
            else:
                journal["projects"] = {
                    p: approval
                    for p, approval in previous.items()
                    if normalize_scope(p) not in names
                }
                journal["pending_permits"] = [
                    p for p in journal.get("pending_permits", []) if normalize_scope(p) not in names
                ]
                if lane == "history":
                    journal["selected"] = {
                        key: approval
                        for key, approval in journal["selected"].items()
                        if normalize_scope(approval["project"]) not in names
                    }
                else:
                    journal["project_scopes"] = [
                        p
                        for p in journal.get("project_scopes") or []
                        if normalize_scope(p) not in names
                    ]
                    journal["pending_revokes"] = sorted(
                        set(journal.get("pending_revokes", [])) | set(removed[lane])
                    )
            save_journal(path, journal)
    return dict(
        excluded=sorted(names),
        removed_approvals=removed,
        applied=apply,
        sends_text=False,
        local_evidence_deleted=False,
        cloud_removal_pending=apply,
    )

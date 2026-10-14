"""Local-only setup preview and explicit approval of a bound Cloud projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from varyz.cloud.activation import verify_rule
from varyz.cloud.archive_sync import ArchiveSync, entry_project, read_archive_journal
from varyz.cloud.client import CloudClient, CloudStorageLimit, credential_name, load_tokens
from varyz.cloud.computer import CLOUD_ORIGIN
from varyz.cloud.local_sync import LocalSync, read_journal, selected_payloads
from varyz.cloud.project_policy import exclusions
from varyz.cloud.project_selection import confirmed_scopes
from varyz.cloud.session_report_sync import ReportSync
from varyz.cloud.session_report_sync import read_journal as read_report_journal
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution.knowledge import normalize_scope


class PreviewChanged(ValueError):
    """The owner must review a fresh preview before anything is enabled."""


class GuidedSetup:
    def __init__(self, config: Path, account_id: str) -> None:
        self.config = config
        self.account_id = account_id
        self.path = config.with_name(config.stem + ".cloud-sync.json")

    def _journal(self) -> dict[str, Any]:
        journal = read_journal(self.path)
        if journal.get("url") != CLOUD_ORIGIN or journal.get("account_id") != self.account_id:
            raise ValueError("Reconnect this computer to the account shown on this page.")
        return journal

    def _db(self) -> sqlite3.Connection:
        return open_db(load_config(self.config).db_path)

    @property
    def archive_path(self):
        return self.config.with_name(self.config.stem + ".cloud-archive.json")

    def history_projects(self, conn):
        journal = read_archive_journal(self.archive_path)
        mappings = dict(journal["projects"])
        cfg = load_config(self.config)
        for root in cfg.connectors.get("git", {}).get("paths", []):
            path = Path(root).expanduser()
            if not path.is_absolute():
                path = cfg.base_dir / path
            mappings.setdefault(
                normalize_scope(path.name), {"root": str(path.resolve()), "repository": None}
            )
        for repo in cfg.connectors.get("github", {}).get("repos", []):
            mappings.setdefault(
                normalize_scope(repo.rsplit("/", 1)[-1]), {"root": None, "repository": repo}
            )
        counts = {"*": 0}
        # Discovery is not ownership. Cloned repos can contain thousands of
        # other people's commits; propose only scopes with evidence of own work.
        proposed = {"*", *journal["projects"], *confirmed_scopes(conn)}
        proposed.update(normalize_scope(p) for p in ReportSync(conn, self.config).projects())
        proposed.update(normalize_scope(a["project"]) for a in journal["selected"].values())
        roots = {
            p: Path(approval["root"]).resolve()
            for p, approval in mappings.items() if approval.get("root")
        }
        for row in conn.execute(
            "SELECT source,external_id,thread_id,authored_by_user,"
            "substr(body,1,instr(body,char(10)||char(10))-1) AS body "
            "FROM entries WHERE source IN ('ai_conversations','documents','git','github')"
        ):
            project = entry_project(row, mappings, resolved_roots=roots) or "*"
            if project.startswith("unapproved:"):
                continue
            counts[project] = counts.get(project, 0) + 1
            if row["authored_by_user"]:
                proposed.add(project)
            mappings.setdefault(project, {"root": None, "repository": None})
        # Keep existing approvals visible even when their source entries disappeared.
        return mappings, {project: counts.get(project, 0) for project in proposed}

    def history_draft(self, conn, projects):
        if (
            not isinstance(projects, list)
            or len(projects) > 1000
            or any(not isinstance(p, str) for p in projects)
        ):
            raise ValueError("Choose history projects")
        mappings, counts = self.history_projects(conn)
        selected = {normalize_scope(p) for p in projects}
        if not selected <= counts.keys():
            raise ValueError("Review available history projects")
        journal = read_archive_journal(self.archive_path)
        journal = {
            **journal,
            "enabled": bool(selected),
            "projects": {p: mappings.get(p, {"root": None, "repository": None}) for p in selected},
            # The project picker is authoritative, including older per-entry approvals.
            "selected": {
                identity: approval
                for identity, approval in journal["selected"].items()
                if normalize_scope(approval["project"]) in selected
            },
        }
        plan = ArchiveSync(conn, self.archive_path).plan(journal)
        signature = hashlib.sha256(
            json.dumps({"journal": journal, "plan": plan}, sort_keys=True).encode()
        ).hexdigest()
        return journal, plan, signature

    def choices(self) -> dict[str, Any]:
        journal = self._journal()
        conn = self._db()
        try:
            # Source locations and counts explain project identity; never message text.
            scopes = confirmed_scopes(conn)
            counts: dict[str, int] = {}
            for scope in scopes:
                scope = normalize_scope(scope)
                counts[scope] = counts.get(scope, 0) + 1
            for scope in journal.get("projects", []):
                counts.setdefault(normalize_scope(scope), 0)
            mappings, history_counts = self.history_projects(conn)
            report_sync = ReportSync(conn, self.config)
            report_counts = report_sync.projects()
            history_selected = list(read_archive_journal(self.archive_path)["projects"])
            report_selected = sorted(read_report_journal(report_sync.path)["projects"])
            grouped_reports: dict[str, int] = {}
            for name, count in report_counts.items():
                scope = normalize_scope(name)
                grouped_reports[scope] = grouped_reports.get(scope, 0) + count
            names = (counts.keys() | history_counts.keys() | grouped_reports.keys()) - {"*"}
            excluded = exclusions(self.config)
            evidence: dict[str, dict[str, Any]] = {}
            roots = {
                p: Path(approval["root"]).resolve()
                for p, approval in mappings.items() if approval.get("root")
            }
            # Inspect source addresses and headers, never classify message topics.
            for row in conn.execute(
                "SELECT source, external_id, thread_id, authored_by_user, "
                "substr(body,1,instr(body,char(10)||char(10))-1) AS body, "
                "occurred_start, conversation_metadata_json FROM entries WHERE source IN "
                "('ai_conversations','documents','git','github')"
            ):
                name = entry_project(row, mappings, resolved_roots=roots) or "*"
                item = evidence.setdefault(
                    name,
                    dict(sources=set(), sessions=set(), own_commits=0, paths=set(), latest=None),
                )
                item["sources"].add(row["source"])
                metadata = json.loads(row["conversation_metadata_json"] or "{}")
                if metadata.get("working_directory"):
                    item["paths"].add(metadata["working_directory"])
                if row["occurred_start"]:
                    item["latest"] = max(item["latest"] or "", row["occurred_start"])
                if row["source"] == "ai_conversations" and row["authored_by_user"]:
                    item["sessions"].add(row["thread_id"] or row["external_id"])
                if row["source"] in {"git", "github"} and row["authored_by_user"]:
                    item["own_commits"] += 1

            def details(name):
                mapping = mappings.get(name, {})
                item = evidence.get(name, {})
                own = item.get("own_commits", 0)
                sessions = len(item.get("sessions", set()))
                root = mapping.get("root")
                # A conversation proves activity, not ownership or a desire to sync.
                identified = bool(root or mapping.get("repository"))
                review = not own and not (identified and counts.get(name, 0))
                generic = name in {
                    "codex",
                    "session-workspace",
                    "site",
                    "website",
                    "projects",
                    "documents",
                    "cowork-recents",
                    "tmp",
                    "temp",
                    "demo",
                }
                if generic and not own:
                    review = True
                reason = (
                    "Your authored repository activity"
                    if own
                    else "Project location and confirmed preferences"
                    if not review
                    else "Folder name only; confirm what this project is"
                    if not identified
                    else "Conversations found; confirm this is a project you want synced"
                    if sessions
                    else "Imported repository; confirm this is your work"
                )
                return dict(
                    root=root,
                    conversation_locations=sorted(item.get("paths", set()))[:5],
                    last_activity=item.get("latest"),
                    repository=mapping.get("repository"),
                    sources=sorted(item.get("sources", set())),
                    sessions=sessions,
                    authored_repository_records=own,
                    needs_review=review,
                    reason=reason,
                )

            project_choices = [
                dict(
                    name=name,
                    count=counts.get(name, 0),
                    history=history_counts.get(name, 0),
                    reports=grouped_reports.get(name, 0),
                    **details(name),
                )
                for name in sorted(names - excluded)
            ]
            return {
                "setup_complete": bool(journal.get("enabled")),
                "sync_projects": project_choices,
                "excluded_projects": sorted(excluded),
                "sync_selected": sorted(
                    (
                        {normalize_scope(p) for p in journal.get("projects", [])}
                        if journal.get("enabled")
                        else set()
                    ).union(history_selected, {normalize_scope(p) for p in report_selected})
                    - excluded
                ),
                "report_selected": sorted(read_report_journal(report_sync.path)["projects"]),
                "report_projects": [
                    dict(name=name, count=count)
                    for name, count in sorted(report_sync.projects().items())
                ],
                "history_selected": list(read_archive_journal(self.archive_path)["projects"]),
                "history_projects": [
                    {"name": name, "count": count} for name, count in sorted(history_counts.items())
                ],
                "projects": [
                    {"name": name, "count": counts[name]} for name in sorted(counts) if name != "*"
                ],
                "general": counts.get("*", 0),
                "selected": sorted(
                    {normalize_scope(p) for p in journal.get("projects", [])} - {"*"}
                )
                if journal.get("enabled")
                else [],
                "all_projects": bool(journal.get("enabled") and not journal.get("projects")),
                "selection_locked": False,
            }
        finally:
            conn.close()

    @staticmethod
    def scopes(projects: Any, all_projects: Any) -> list[str]:
        if not isinstance(all_projects, bool) or not isinstance(projects, list):
            raise ValueError("Choose which projects to include.")
        if len(projects) > 1000 or any(
            not isinstance(p, str) or not p.strip() or len(p) > 200 for p in projects
        ):
            raise ValueError("Choose valid projects.")
        if all_projects:
            if projects:
                raise ValueError("Choose all projects or individual projects.")
            return []
        # ['*'] expresses general-only; an empty list in the sync engine means all.
        return sorted({normalize_scope(p) for p in projects} - {"*"}) or ["*"]

    def _draft(
        self,
        conn: sqlite3.Connection,
        projects: list[str],
        journal: dict[str, Any],
    ) -> tuple[LocalSync, str]:
        sync = LocalSync(conn, self.path)
        sync.journal = {**journal, "enabled": True, "projects": projects}
        snapshot = {
            "journal": journal,
            "selected": selected_payloads(conn, projects),
            "changes": sync.plan(),
        }
        signature = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return sync, signature

    def preview_projects(self, projects: Any) -> dict[str, Any]:
        """One reviewed project selection authorizes all eligible context in that scope.

        General preferences are always included; general history requires choosing '*'.
        No discovery or automatic reconnection may invoke finish or create approvals.
        """
        scopes = self.scopes(projects, False)
        choices = self.choices()
        names = {p["name"] for p in choices["sync_projects"]} | {"*"}
        selected = {normalize_scope(p) for p in projects}
        if not selected <= names:
            raise ValueError("Review available projects before syncing.")
        history = selected & {p["name"] for p in choices["history_projects"]}
        reports = {
            p["name"] for p in choices["report_projects"] if normalize_scope(p["name"]) in selected
        }
        report_scopes = selected & {normalize_scope(p["name"]) for p in choices["report_projects"]}
        return self.preview(
            scopes, sorted(history), sorted(reports), report_scopes=sorted(report_scopes)
        )

    def preview(
        self,
        projects: list[str],
        history_projects: list[str] | None = None,
        report_projects: list[str] | None = None,
        *,
        report_scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        journal = self._journal()
        conn = self._db()
        try:
            sync, signature = self._draft(conn, projects, journal)
            history_projects = history_projects or []
            _, history_plan, history_signature = self.history_draft(conn, history_projects)
            report_projects = report_projects or []
            _, report_plan, report_signature = ReportSync(conn, self.config).draft(
                report_projects, project_scopes=report_scopes
            )
            return {
                "report_projects": report_projects,
                "report_scopes": report_scopes,
                "report_signature": report_signature,
                "report_uploads": sum(c["payload"] is not None for c in report_plan["changes"]),
                "report_deletions": sum(c["payload"] is None for c in report_plan["changes"]),
                "report_blocked": report_plan["blocked"],
                **sync.preview(),
                "signature": signature,
                "projects": projects,
                "history_projects": history_projects,
                "history_signature": history_signature,
                "history_uploads": sum(c["payload"] is not None for c in history_plan["changes"]),
                "history_deletions": sum(c["payload"] is None for c in history_plan["changes"]),
                "history_blocked": len(history_plan["blocked_ids"]),
            }
        finally:
            conn.close()

    def finish(self, preview: dict[str, Any]) -> dict[str, Any]:
        journal = self._journal()
        name = credential_name(self.config, CLOUD_ORIGIN)
        client = CloudClient(CLOUD_ORIGIN, load_tokens(name), credential=name)
        conn = self._db()
        try:
            account = client.request("GET", "/api/account")
            if account.get("id", account.get("account_id")) != self.account_id:
                raise ValueError("Reconnect to the same Cloud account.")
            if account.get("entitlement") is not True:
                raise ValueError("Check your Cloud access before finishing setup.")

            def prepare(current: dict[str, Any]) -> dict[str, Any]:
                if current != journal:
                    raise PreviewChanged("Your preferences changed. Review an updated preview.")
                draft, signature = self._draft(conn, preview["projects"], current)
                if signature != preview["signature"]:
                    raise PreviewChanged("Your preferences changed. Review an updated preview.")
                return draft.journal

            history_journal, history_plan, history_signature = self.history_draft(
                conn, preview.get("history_projects", [])
            )
            if history_signature != preview.get("history_signature"):
                raise PreviewChanged("Your history changed. Review an updated preview.")
            report_sync = ReportSync(conn, self.config)
            _, report_plan, report_signature = report_sync.draft(
                preview.get("report_projects", []), project_scopes=preview.get("report_scopes")
            )
            if report_signature != preview.get("report_signature"):
                raise PreviewChanged("Your reports changed. Review an updated preview.")
            storage_full = False
            result: dict[str, Any] = {}
            try:
                result.update(LocalSync(conn, self.path).sync(client, prepare=prepare))
            except CloudStorageLimit:
                result.update(uploaded=0, downloaded=0)
                storage_full = True
            if (
                preview.get("history_projects")
                or history_plan["changes"]
                or read_archive_journal(self.archive_path)["projects"]
                or read_archive_journal(self.archive_path)["selected"]
            ):
                from varyz.cloud.local_sync import _sync_lock, save_journal

                with _sync_lock(self.archive_path.with_suffix(".lock")):
                    _, _, current_signature = self.history_draft(conn, preview["history_projects"])
                    if current_signature != history_signature:
                        raise PreviewChanged("Your history changed. Review an updated preview.")
                    removed = set(read_archive_journal(self.archive_path)["projects"]) - set(
                        history_journal["projects"]
                    )
                    history_journal["enabled"] = True
                    history_journal["pending_permits"] = preview["history_projects"]
                    save_journal(self.archive_path, history_journal)
                for project in removed:
                    client.request("POST", "/api/archive/revoke", json={"project": project})
                from varyz.cloud.archive import CachedEmbedder

                cache = load_config(self.config).data_dir / "models"
                embedder = (
                    CachedEmbedder(str(cache)) if any(cache.glob("models--*MiniLM*")) else None
                )
                result["history"] = ArchiveSync(conn, self.archive_path).sync(
                    client, self.account_id, embedder=embedder
                )
                storage_full = storage_full or bool(result["history"].get("storage_full"))
            report_verified = False
            if (
                preview.get("report_projects")
                or report_plan["changes"]
                or read_report_journal(report_sync.path)["projects"]
            ):
                try:
                    report_sync.select(
                        preview.get("report_projects", []),
                        report_signature,
                        self.account_id,
                        CLOUD_ORIGIN,
                        project_scopes=preview.get("report_scopes"),
                    )
                except ValueError:
                    raise PreviewChanged(
                        "Your reports changed. Review an updated preview."
                    ) from None
                result["reports"] = report_sync.sync(client, self.account_id)
                storage_full = storage_full or bool(result["reports"].get("storage_full"))
                candidates = [c for c in report_plan["changes"] if c["payload"]]
                if not candidates:
                    from varyz.session_progress import HistoryQuery, history

                    synced = {
                        identity
                        for identity, receipt in read_report_journal(report_sync.path)[
                            "records"
                        ].items()
                        if not receipt.get("deleted")
                    }
                    for project in preview.get("report_projects", []):
                        current = history(
                            conn, HistoryQuery(project=project), cfg=load_config(self.config)
                        )
                        if current["reports"]:
                            payload = dict(current["reports"][0])
                            payload.pop("earlier_session_outcomes", None)
                            identity = payload["reference"].split(":", 1)[1]
                            if identity not in synced:
                                continue
                            candidates = [{"id": identity, "payload": payload}]
                            break
                if candidates and not result["reports"].get("storage_full"):
                    payload = candidates[-1]["payload"]
                    response = client.request(
                        "POST",
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "session_history",
                                "arguments": {
                                    "project": payload["project"],
                                    "conversation_id": payload["conversation_id"],
                                    "agent": payload["agent"],
                                    "latest": True,
                                },
                            },
                        },
                    )["result"]
                    fetched = client.request("GET", "/reports/" + candidates[-1]["id"])
                    fetched.pop("synced_at", None)
                    if (
                        response.get("isError")
                        or fetched != payload
                        or not json.loads(response["content"][0]["text"]).get("found")
                    ):
                        return {**result, "state": "verification_failed"}
                    report_verified = True
            if storage_full:
                return {**result, "state": "complete_limited", "storage_full": True}
            # Failure here must be reported as synced but not verified, never complete.
            try:
                verified = self._verify(conn, read_journal(self.path), client)
            except Exception:
                return {**result, "state": "verification_failed"}
            state = "complete" if verified or report_verified else "complete_empty"
            return {**result, "state": state}
        finally:
            client.close()
            conn.close()

    def _verify(
        self,
        conn: sqlite3.Connection,
        journal: dict[str, Any],
        client: CloudClient,
    ) -> bool:
        selected = selected_payloads(conn, journal.get("projects", []))
        if not selected:
            # Account identity/access was checked; no rule exists to claim retrieval of.
            return False
        key = next((k for k in selected if k.startswith("rule:")), next(iter(selected)))
        kind, identity = key.split(":", 1)
        payload = selected[key]
        project = payload["scope"]
        if kind == "rule":
            verify_rule(conn, journal, client, identity, project)
            return True
        arguments = {"project": project}
        tool = "start_task"
        if kind == "answer":
            tool = "ask"
            arguments["question"] = payload["question"]
        response = client.request(
            "POST",
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )["result"]
        if response.get("isError"):
            raise ValueError("Cloud retrieval did not complete.")
        value = json.loads(response["content"][0]["text"])
        if kind == "answer":
            if value.get("status") != "answered" or value.get("answer") != payload["answer"]:
                raise ValueError("Cloud did not return this preference.")
        else:
            from varyz.cloud.local_sync import fingerprint

            remote_id = next(
                (
                    k.split(":", 1)[1]
                    for k, r in journal["records"].items()
                    if k.startswith("workflow:") and r.get("local_id") == identity
                ),
                identity,
            )
            if not any(
                r["id"] == remote_id and fingerprint(r["payload"]) == fingerprint(payload)
                for r in value["workflows"]
            ):
                raise ValueError("Cloud did not return this preference.")
        return True

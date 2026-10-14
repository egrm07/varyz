"""Privacy integrity, receipt verification, and provider-bypass diagnostics."""

from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class PrivacyIssue:
    code: str
    message: str
    count: int
    severity: str = "error"


APPROVED_PROVIDER_MODULES = {
    "varyz/llm/claude.py",
    "varyz/llm/openai_compat.py",
    "varyz/llm/openai_oauth.py",
    "varyz/llm/ollama_local.py",
    "varyz/llm/providers.py",
    "varyz/destinations/llm.py",
}
PROVIDER_IMPORT_ROOTS = {"anthropic", "openai"}


def provider_bypasses(source_root: Path) -> list[str]:
    findings: list[str] = []
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root.parent).as_posix()
        if relative in APPROVED_PROVIDER_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError):
            findings.append(f"{relative}:unparseable")
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", 1)[0] in PROVIDER_IMPORT_ROOTS:
                    findings.append(f"{relative}:{node.lineno}:{name}")
    return findings


def diagnose(conn: sqlite3.Connection, *, source_root: Path | None = None) -> list[PrivacyIssue]:
    issues: list[PrivacyIssue] = []
    unlabeled_entries = conn.execute(
        """SELECT COUNT(*) FROM entries
           WHERE sensitivity IS NULL OR sensitivity NOT IN
             ('public','personal','sensitive','unclassified')"""
    ).fetchone()[0]
    if unlabeled_entries:
        issues.append(
            PrivacyIssue(
                "unlabeled_entry",
                "Entries have no recognized fail-closed sensitivity.",
                unlabeled_entries,
            )
        )

    unlabeled_entities = conn.execute(
        """SELECT COUNT(*) FROM entities e
           WHERE e.status='active' AND NOT EXISTS (
             SELECT 1 FROM policy_labels l
              WHERE l.resource_type='entity' AND l.resource_id=e.id AND l.active=1
           )"""
    ).fetchone()[0]
    if unlabeled_entities:
        issues.append(
            PrivacyIssue(
                "unlabeled_entity",
                "Active entities have no inherited or explicit policy label.",
                unlabeled_entities,
            )
        )

    unlabeled_graph = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT 'alias' AS resource_type, a.id AS resource_id
               FROM entity_aliases a
               JOIN graph_runs g ON g.id=a.graph_generation_id
              WHERE g.state='active'
             UNION ALL
             SELECT 'claim', c.id
               FROM claims c
               JOIN graph_runs g ON g.id=c.graph_generation_id
              WHERE g.state='active'
             UNION ALL
             SELECT 'evidence_quote', e.id
               FROM claim_evidence e
               JOIN graph_runs g ON g.id=e.graph_generation_id
              WHERE g.state='active'
           ) resource
           WHERE NOT EXISTS (
             SELECT 1 FROM policy_labels l
              WHERE l.resource_type=resource.resource_type
                AND l.resource_id=resource.resource_id AND l.active=1
           )"""
    ).fetchone()[0]
    if unlabeled_graph:
        issues.append(
            PrivacyIssue(
                "unlabeled_graph_resource",
                "Active aliases, claims, or evidence quotes lack a policy label.",
                unlabeled_graph,
            )
        )

    expired_active = conn.execute(
        """SELECT COUNT(*) FROM consent_grants
           WHERE revoked_at IS NULL AND valid_until IS NOT NULL AND valid_until < ?""",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    ).fetchone()[0]
    if expired_active:
        issues.append(
            PrivacyIssue(
                "expired_consent",
                "Expired grants remain stored; they are denied but can be reviewed.",
                expired_active,
                "warning",
            )
        )

    invalid_grants = conn.execute(
        """SELECT COUNT(*) FROM consent_grants
           WHERE (valid_from IS NOT NULL AND valid_until IS NOT NULL
                  AND valid_until < valid_from)
              OR (scope_valid_from IS NOT NULL AND scope_valid_to IS NOT NULL
                  AND scope_valid_to < scope_valid_from)
              OR (resource_id IS NOT NULL AND resource_type IS NULL)
              OR max_items < 0 OR max_chars < 0
              OR used_items < 0 OR used_chars < 0"""
    ).fetchone()[0]
    if invalid_grants:
        issues.append(
            PrivacyIssue(
                "invalid_consent_grant",
                "Consent grants contain an invalid scope, interval, or budget.",
                invalid_grants,
            )
        )

    invalid_rules = conn.execute(
        """SELECT COUNT(*) FROM policy_rules r
           WHERE active=1 AND (
             (effect='transform' AND (
               transformation IS NULL OR NOT EXISTS (
                 SELECT 1 FROM transformation_registry t
                  WHERE t.name=r.transformation AND t.active=1
               )
             ))
             OR (destination IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM destination_registry d
                WHERE d.destination=r.destination AND d.active=1
             ))
           )"""
    ).fetchone()[0]
    if invalid_rules:
        issues.append(
            PrivacyIssue(
                "invalid_policy_rule",
                "Active policy rules reference an unknown destination or transformation.",
                invalid_rules,
            )
        )

    unverifiable = conn.execute(
        """SELECT COUNT(*) FROM egress_receipts r
           LEFT JOIN context_requests q ON q.id=r.request_id
           WHERE q.id IS NULL OR length(r.payload_hash)<>64"""
    ).fetchone()[0]
    if unverifiable:
        issues.append(
            PrivacyIssue(
                "unverifiable_receipt",
                "Receipts lack a request or a valid SHA-256 payload hash.",
                unverifiable,
            )
        )

    corrupt_receipts = 0
    for row in conn.execute(
        """SELECT r.request_id, r.payload_item_count, r.payload_char_count,
                  r.considered_ids_json,
                  decision_summary_json, transformation_versions_json,
                  receipt_version,
                  (SELECT COUNT(*) FROM context_decisions d
                    WHERE d.request_id=r.request_id) AS decision_count
             FROM egress_receipts r"""
    ):
        try:
            considered = json.loads(row["considered_ids_json"])
            summary = json.loads(row["decision_summary_json"])
            versions = json.loads(row["transformation_versions_json"])
            valid = (
                isinstance(considered, list)
                and isinstance(summary, dict)
                and isinstance(versions, dict)
                and len(considered) == row["decision_count"]
                and sum(summary.values()) == row["decision_count"]
                and all(
                    isinstance(key, str)
                    and isinstance(value, int)
                    and value >= 0
                    for key, value in summary.items()
                )
                and row["payload_item_count"] >= 0
                and row["payload_char_count"] >= 0
                and row["receipt_version"] == "1"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            valid = False
        corrupt_receipts += int(not valid)
    if corrupt_receipts:
        issues.append(
            PrivacyIssue(
                "corrupt_receipt",
                "Receipt fields are corrupt, partial, or use an unknown version.",
                corrupt_receipts,
            )
        )

    corrupt_executions = 0
    for row in conn.execute(
        """SELECT state, response_metadata_json, approval_scope_json,
                  started_at, completed_at FROM egress_executions"""
    ):
        try:
            metadata = json.loads(row["response_metadata_json"])
            approval = json.loads(row["approval_scope_json"])
            valid = isinstance(metadata, dict) and isinstance(approval, dict)
            if row["state"] == "started":
                valid = valid and row["completed_at"] is None
            else:
                valid = valid and row["completed_at"] is not None
            if row["completed_at"] is not None:
                valid = valid and row["completed_at"] >= row["started_at"]
        except (TypeError, ValueError, json.JSONDecodeError):
            valid = False
        corrupt_executions += int(not valid)
    if corrupt_executions:
        issues.append(
            PrivacyIssue(
                "corrupt_execution_receipt",
                "Execution audit rows are malformed or have impossible timestamps.",
                corrupt_executions,
            )
        )

    incomplete_executions = conn.execute(
        "SELECT COUNT(*) FROM egress_executions WHERE state='started'"
    ).fetchone()[0]
    if incomplete_executions:
        issues.append(
            PrivacyIssue(
                "incomplete_execution",
                "Started disclosures need reconciliation; they are never assumed complete.",
                incomplete_executions,
                "warning",
            )
        )

    missing_transform_versions = 0
    for row in conn.execute(
        "SELECT transformation_versions_json FROM egress_receipts"
    ):
        try:
            versions = json.loads(row["transformation_versions_json"])
        except (AttributeError, TypeError, json.JSONDecodeError):
            missing_transform_versions += 1
            continue
        for name, version in versions.items():
            exists = conn.execute(
                """SELECT 1 FROM transformation_registry
                   WHERE name=? AND version=?""",
                (name, version),
            ).fetchone()
            missing_transform_versions += int(exists is None)
    if missing_transform_versions:
        issues.append(
            PrivacyIssue(
                "missing_transformation_version",
                "Receipts reference unavailable transformation implementations.",
                missing_transform_versions,
            )
        )

    if source_root:
        bypasses = provider_bypasses(source_root)
        if bypasses:
            issues.append(
                PrivacyIssue(
                    "provider_bypass",
                    "Provider SDK imports exist outside approved destination adapters: "
                    + ", ".join(bypasses[:5]),
                    len(bypasses),
                )
            )
    return issues

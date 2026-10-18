"""Conservative identity resolution: exact identifiers link; names queue."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

from varyz.domain import iso, new_id, utc_now
from varyz.graph.models import AliasVerification, EntityType

SAFE_NAMESPACES = {
    "system",
    "email",
    "phone",
    "whatsapp_jid",
    "telegram_id",
    "source_stable_id",
    "connector_link",
}


def normalize_alias(namespace: str, value: str) -> str:
    value = value.strip()
    if namespace == "email":
        return value.casefold()
    if namespace == "phone":
        digits = re.sub(r"\D", "", value)
        return f"+{digits}" if digits else ""
    if namespace in {"whatsapp_jid", "telegram_id", "source_stable_id", "system"}:
        return value.casefold()
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class Resolution:
    entity_id: str
    explanation: str
    automatic: bool


class ConservativeIdentityResolver:
    version = "1"

    def __init__(
        self,
        conn: sqlite3.Connection,
        generation_id: str,
        generation_version: str,
        *,
        candidate_threshold: float = 0.55,
    ):
        self.conn = conn
        self.generation_id = generation_id
        self.generation_version = generation_version
        self.candidate_threshold = candidate_threshold

    def resolve(self, alias: dict) -> Resolution:
        namespace = str(alias["namespace"])
        value = str(alias["value"])
        normalized = normalize_alias(namespace, value)
        verification = AliasVerification(alias.get("verification_state", "candidate"))
        source_entry_id = alias.get("source_entry_id")
        if source_entry_id:
            prior_source = self.conn.execute(
                """SELECT COALESCE(e.merged_into_id, e.id) AS id
                   FROM entity_aliases a JOIN entities e ON e.id=a.entity_id
                   WHERE a.source_entry_id=? AND a.namespace=?
                     AND a.normalized_value=?
                   ORDER BY a.created_at LIMIT 1""",
                (source_entry_id, namespace, normalized),
            ).fetchone()
            if prior_source:
                entity_id = prior_source["id"]
                self._reactivate(entity_id, str(alias["label"]))
                self._insert_alias(entity_id, alias, normalized)
                return Resolution(entity_id, "same source representation", True)
        safe = namespace in SAFE_NAMESPACES and verification in {
            AliasVerification.VERIFIED,
            AliasVerification.AUTOMATIC,
        }
        if safe:
            existing = self.conn.execute(
                """SELECT COALESCE(e.merged_into_id, e.id) AS id
                   FROM entity_aliases a JOIN entities e ON e.id=a.entity_id
                   WHERE a.namespace=? AND a.normalized_value=?
                     AND a.verification_state IN ('verified','automatic')
                   ORDER BY a.created_at LIMIT 1""",
                (namespace, normalized),
            ).fetchone()
            if existing:
                entity_id = existing["id"]
                self._reactivate(entity_id, str(alias["label"]))
                self._insert_alias(entity_id, alias, normalized)
                return Resolution(entity_id, f"exact {namespace} match", True)

        entity_id = new_id()
        now = iso(utc_now())
        self.conn.execute(
            """INSERT INTO entities
               (id, entity_type, canonical_label, status, created_at, updated_at,
                graph_generation_id, generation_version)
               VALUES (?,?,?,'active',?,?,?,?)""",
            (
                entity_id,
                EntityType(alias["entity_type"]),
                str(alias["label"]).strip() or value,
                now,
                now,
                self.generation_id,
                self.generation_version,
            ),
        )
        self._insert_alias(entity_id, alias, normalized)
        if not safe:
            self._candidate_same_names(entity_id, namespace, normalized)
        return Resolution(
            entity_id,
            "new identity; display names are never auto-merged"
            if not safe
            else f"new verified {namespace}",
            False,
        )

    def _reactivate(self, entity_id: str, label: str) -> None:
        overridden = self.conn.execute(
            """SELECT 1 FROM graph_overrides
               WHERE (target_type='entity' AND target_key=? AND action='change_label')
                  OR (target_type='entity' AND action='merge'
                      AND json_extract(payload_json, '$.merged_into_id')=?)
               LIMIT 1""",
            (entity_id, entity_id),
        ).fetchone()
        if overridden:
            self.conn.execute(
                """UPDATE entities SET status='active', merged_into_id=NULL,
                   graph_generation_id=?, generation_version=?, updated_at=? WHERE id=?""",
                (self.generation_id, self.generation_version, iso(utc_now()), entity_id),
            )
        else:
            self.conn.execute(
                """UPDATE entities SET canonical_label=?, status='active',
                   merged_into_id=NULL, graph_generation_id=?, generation_version=?,
                   updated_at=? WHERE id=?""",
                (
                    label.strip(),
                    self.generation_id,
                    self.generation_version,
                    iso(utc_now()),
                    entity_id,
                ),
            )

    def _insert_alias(self, entity_id: str, alias: dict, normalized: str) -> None:
        fingerprint = hashlib.sha256(
            f"{entity_id}\0{alias['namespace']}\0{normalized}\0"
            f"{alias.get('source_entry_id') or ''}".encode()
        ).hexdigest()
        self.conn.execute(
            """INSERT OR IGNORE INTO entity_aliases
               (id, entity_id, namespace, alias_value, normalized_value,
                source_entry_id, confidence, verification_state,
                graph_generation_id, fingerprint, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id(),
                entity_id,
                alias["namespace"],
                alias["value"],
                normalized,
                alias.get("source_entry_id"),
                float(alias.get("confidence", 1.0)),
                alias.get("verification_state", "candidate"),
                self.generation_id,
                fingerprint,
                iso(utc_now()),
            ),
        )

    def _candidate_same_names(
        self, entity_id: str, namespace: str, normalized: str
    ) -> None:
        if namespace not in {"display_name", "place_spelling", "project_nickname"}:
            return
        rows = self.conn.execute(
            """SELECT DISTINCT entity_id FROM entity_aliases
               WHERE namespace=? AND normalized_value=? AND entity_id<>?""",
            (namespace, normalized, entity_id),
        ).fetchall()
        for row in rows:
            left, right = sorted((entity_id, row["entity_id"]))
            fingerprint = hashlib.sha256(
                f"{left}\0{right}\0same_{namespace}".encode()
            ).hexdigest()
            self.conn.execute(
                """INSERT OR IGNORE INTO entity_candidates
                   (id, left_entity_id, right_entity_id, reason_code, score,
                    explanation, state, graph_generation_id, fingerprint, created_at)
                   VALUES (?,?,?,?,?,?,'pending',?,?,?)""",
                (
                    new_id(),
                    left,
                    right,
                    f"same_{namespace}",
                    self.candidate_threshold,
                    f"Both identities use {normalized!r}; no stable identifier links them.",
                    self.generation_id,
                    fingerprint,
                    iso(utc_now()),
                ),
            )


def self_entity_id(conn: sqlite3.Connection) -> str | None:
    """Return the canonical graph entity representing the archive owner."""
    row = conn.execute(
        """SELECT COALESCE(e.merged_into_id, e.id) AS id
           FROM entity_aliases a JOIN entities e ON e.id=a.entity_id
           WHERE a.namespace='system' AND a.normalized_value='self'
           ORDER BY a.created_at LIMIT 1"""
    ).fetchone()
    return str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) if row else None

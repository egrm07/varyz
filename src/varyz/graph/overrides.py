"""Append-only human corrections that replay over regenerated projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from varyz.domain import Sensitivity, TimePrecision, iso, new_id, utc_now
from varyz.events import emit_event
from varyz.graph.temporal import parse_time_expression, parse_time_interval
from varyz.privacy.labels import label_resource
from varyz.privacy.models import LabelSource


def append_override(
    conn: sqlite3.Connection,
    target_type: str,
    target_key: str,
    action: str,
    payload: dict | None = None,
    *,
    reason: str | None = None,
    reverses_id: str | None = None,
) -> str:
    override_id = new_id()
    with conn:
        conn.execute(
            """INSERT INTO graph_overrides
               (id, target_type, target_key, action, payload_json,
                actor, reason, reverses_id, created_at)
               VALUES (?,?,?,?,?,'local_user',?,?,?)""",
            (
                override_id,
                target_type,
                target_key,
                action,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                reason,
                reverses_id,
                iso(utc_now()),
            ),
        )
        run = conn.execute(
            "SELECT id FROM graph_runs WHERE state='active'"
        ).fetchone()
        if run:
            _apply_one(conn, run["id"], target_type, target_key, action, payload or {})
        emit_event(
            conn,
            "entity_overridden" if target_type == "entity" else "claim_overridden",
            target_type,
            target_key,
            {"override_id": override_id, "action": action},
        )
    return override_id


def apply_overrides(conn: sqlite3.Connection, run_id: str) -> None:
    rows = conn.execute(
        "SELECT * FROM graph_overrides ORDER BY created_at, id"
    ).fetchall()
    for row in rows:
        _apply_one(
            conn,
            run_id,
            row["target_type"],
            row["target_key"],
            row["action"],
            json.loads(row["payload_json"]),
        )


def _apply_one(
    conn: sqlite3.Connection,
    run_id: str,
    target_type: str,
    target_key: str,
    action: str,
    payload: dict,
) -> None:
    now = iso(utc_now())
    if target_type == "entity":
        if action == "merge":
            into = payload.get("merged_into_id")
            # Every override is replayed onto every rebuild, and the entity a
            # merge survives into need not exist in the generation being
            # replayed onto -- it may be one a later run produces, or one an
            # entry deleted since took with it. merged_into_id is a foreign
            # key, so writing it anyway raised IntegrityError: it turned
            # `graph merge <real> <typo>` into a traceback, and any rebuild
            # after the surviving entity stopped being produced into a
            # rebuild that could never finish again.
            survives = (
                conn.execute("SELECT 1 FROM entities WHERE id=?", (into,)).fetchone()
                if into
                else None
            )
            if into and into != target_key and survives is not None:
                conn.execute(
                    """UPDATE entities SET status='merged', merged_into_id=?, updated_at=?
                       WHERE id=?""",
                    (into, now, target_key),
                )
        elif action == "split":
            conn.execute(
                """UPDATE entities SET status='active', merged_into_id=NULL, updated_at=?
                   WHERE id=?""",
                (now, target_key),
            )
        elif action == "change_label":
            label = str(payload.get("label", "")).strip()
            if label:
                conn.execute(
                    "UPDATE entities SET canonical_label=?, updated_at=? WHERE id=?",
                    (label, now, target_key),
                )
        elif action == "change_sensitivity":
            _label_user_override(conn, "entity", target_key, payload["sensitivity"])
        return

    if target_type == "alias" and action == "change_sensitivity":
        row = conn.execute(
            """SELECT id FROM entity_aliases
               WHERE graph_generation_id=? AND (id=? OR fingerprint=?)""",
            (run_id, target_key, target_key),
        ).fetchone()
        if row:
            _label_user_override(conn, "alias", row["id"], payload["sensitivity"])
        return

    if target_type == "candidate" and action in {"merge", "keep_separate"}:
        state = "merged" if action == "merge" else "separate"
        conn.execute(
            """UPDATE entity_candidates SET state=?, reviewed_at=?
               WHERE graph_generation_id=? AND (id=? OR fingerprint=?)""",
            (state, now, run_id, target_key, target_key),
        )
        if action == "merge":
            row = conn.execute(
                """SELECT left_entity_id, right_entity_id FROM entity_candidates
                   WHERE graph_generation_id=? AND (id=? OR fingerprint=?)""",
                (run_id, target_key, target_key),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE entities SET status='merged', merged_into_id=?, updated_at=?
                       WHERE id=?""",
                    (row["left_entity_id"], now, row["right_entity_id"]),
                )
        return

    if target_type == "claim":
        where = "graph_generation_id=? AND fingerprint=?"
        params = (run_id, target_key)
        if action == "accept":
            conn.execute(
                f"UPDATE claims SET lifecycle_state='active', updated_at=? WHERE {where}",
                (now, *params),
            )
        elif action == "reject":
            conn.execute(
                f"UPDATE claims SET lifecycle_state='rejected', updated_at=? WHERE {where}",
                (now, *params),
            )
        elif action == "correct_time":
            valid_from = _override_time(payload.get("valid_from"), end=False)
            valid_to = _override_time(payload.get("valid_to"), end=True)
            if valid_from and valid_to and valid_to < valid_from:
                raise ValueError("corrected valid_to precedes valid_from")
            precision = TimePrecision(payload.get("temporal_precision", "unknown"))
            conn.execute(
                f"""UPDATE claims SET valid_from=?, valid_to=?,
                    temporal_precision=?, temporal_inferred=0, updated_at=?
                    WHERE {where}""",
                (
                    valid_from,
                    valid_to,
                    precision,
                    now,
                    *params,
                ),
            )
        elif action == "change_sensitivity":
            conn.execute(
                f"UPDATE claims SET sensitivity=?, updated_at=? WHERE {where}",
                (payload["sensitivity"], now, *params),
            )
            for row in conn.execute(
                f"SELECT id FROM claims WHERE {where}", params
            ):
                _label_user_override(
                    conn, "claim", row["id"], payload["sensitivity"]
                )
        elif action == "user_assertion":
            _insert_user_assertion(conn, run_id, payload)
        return

    if target_type == "evidence" and action == "mark_speaker":
        conn.execute(
            """UPDATE claim_evidence SET speaker_entity_id=? WHERE fingerprint=?
               AND graph_generation_id=?""",
            (payload.get("speaker_entity_id"), target_key, run_id),
        )
    elif target_type == "evidence" and action == "change_sensitivity":
        conn.execute(
            """UPDATE claim_evidence SET sensitivity=? WHERE fingerprint=?
               AND graph_generation_id=?""",
            (payload["sensitivity"], target_key, run_id),
        )
        for row in conn.execute(
            """SELECT id FROM claim_evidence
               WHERE fingerprint=? AND graph_generation_id=?""",
            (target_key, run_id),
        ):
            _label_user_override(
                conn, "evidence_quote", row["id"], payload["sensitivity"]
            )


def _label_user_override(
    conn: sqlite3.Connection,
    resource_type: str,
    resource_id: str,
    sensitivity: str,
) -> None:
    label_resource(
        conn,
        resource_type,
        resource_id,
        Sensitivity(sensitivity),
        LabelSource.USER_OVERRIDE,
        ["graph_override"],
    )


def _override_time(value: str | None, *, end: bool) -> str | None:
    if not value:
        return None
    point = parse_time_expression(value, None)
    if end and point.precision == TimePrecision.EXACT:
        return iso(point.value)
    parsed = parse_time_interval(f"until {value}" if end else value, None)
    normalized = parsed.valid_to if end else parsed.valid_from
    if normalized is None:
        raise ValueError(f"invalid ISO or calendar time: {value}")
    return iso(normalized)


def _insert_user_assertion(conn: sqlite3.Connection, run_id: str, payload: dict) -> None:
    required = {"subject_entity_id", "predicate", "object_type", "object_value"}
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"user assertion missing: {', '.join(sorted(missing))}")
    registered = conn.execute(
        """SELECT 1 FROM predicate_registry
           WHERE predicate=? AND version=? AND active=1""",
        (payload["predicate"], payload.get("predicate_version", "1")),
    ).fetchone()
    if not registered:
        raise ValueError("user assertion predicate is not registered")
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    now = iso(utc_now())
    valid_from = _override_time(payload.get("valid_from"), end=False)
    valid_to = _override_time(payload.get("valid_to"), end=True)
    if valid_from and valid_to and valid_to < valid_from:
        raise ValueError("user assertion valid_to precedes valid_from")
    asserted_at = _override_time(payload.get("asserted_at"), end=False)
    precision = TimePrecision(payload.get("temporal_precision", "unknown"))
    claim_id = new_id()
    conn.execute(
        """INSERT OR IGNORE INTO claims
           (id, subject_entity_id, predicate, predicate_version, object_type,
            object_value_json, object_surface, object_entity_id, polarity, modality,
            asserted_by_entity_id, observed_at, asserted_at, valid_from, valid_to,
            temporal_precision, temporal_inferred, confidence, sensitivity,
            extractor_name, graph_generation_id, fingerprint, lifecycle_state,
            origin, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active','user',?,?)""",
        (
            claim_id,
            payload["subject_entity_id"],
            payload["predicate"],
            payload.get("predicate_version", "1"),
            payload["object_type"],
            json.dumps(payload["object_value"], ensure_ascii=False, sort_keys=True),
            str(payload.get("object_surface", payload["object_value"])),
            payload.get("object_entity_id"),
            payload.get("polarity", "positive"),
            payload.get("modality", "fact"),
            payload.get("asserted_by_entity_id"),
            now,
            asserted_at,
            valid_from,
            valid_to,
            precision,
            0,
            float(payload.get("confidence", 1.0)),
            payload.get("sensitivity", "personal"),
            "user",
            run_id,
            fingerprint,
            now,
            now,
        ),
    )
    stored = conn.execute(
        """SELECT id FROM claims
           WHERE graph_generation_id=? AND fingerprint=?""",
        (run_id, fingerprint),
    ).fetchone()
    if stored is None:
        # INSERT OR IGNORE is here so replaying this override onto the next
        # rebuild is idempotent, and it swallowed constraint failures with the
        # same silence: `graph assert ... --sensitivity nope` violated the
        # CHECK, wrote nothing, and reported "User assertion recorded." The
        # fingerprint lookup tells the two apart -- a replay finds the row.
        raise ValueError(
            "user assertion was rejected by the claim constraints; "
            "check --sensitivity and --object-type"
        )
    _label_user_override(
        conn,
        "claim",
        stored["id"],
        payload.get("sensitivity", "personal"),
    )

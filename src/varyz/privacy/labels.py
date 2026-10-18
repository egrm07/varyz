"""Sensitivity label inheritance with sticky escalation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from varyz.domain import Sensitivity, iso, new_id, utc_now
from varyz.privacy.models import LabelSource

RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.UNCLASSIFIED: 3,
}


def effective_label(
    labels: Iterable[Sensitivity], *, user_override: Sensitivity | None = None
) -> Sensitivity:
    """Higher sensitivity is sticky unless an explicit user override exists."""
    if user_override is not None:
        return user_override
    values = list(labels)
    return max(values, key=RANK.__getitem__) if values else Sensitivity.UNCLASSIFIED


def label_resource(
    conn: sqlite3.Connection,
    resource_type: str,
    resource_id: str,
    label: Sensitivity,
    source: LabelSource,
    reasons: list[str],
    *,
    policy_version: str = "1",
) -> str:
    label_id = new_id()
    if source == LabelSource.USER_OVERRIDE:
        conn.execute(
            """UPDATE policy_labels SET active=0
               WHERE resource_type=? AND resource_id=? AND label_source='user_override'""",
            (resource_type, resource_id),
        )
    conn.execute(
        """INSERT INTO policy_labels
           (id, resource_type, resource_id, label, label_source, reasons_json,
            policy_version, active, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            label_id,
            resource_type,
            resource_id,
            label,
            source,
            json.dumps(reasons, sort_keys=True),
            policy_version,
            1,
            iso(utc_now()),
        ),
    )
    return label_id


def resource_label(
    conn: sqlite3.Connection, resource_type: str, resource_id: str
) -> tuple[Sensitivity, list[str]]:
    rows = conn.execute(
        """SELECT label, label_source, reasons_json FROM policy_labels
           WHERE resource_type=? AND resource_id=? AND active=1 ORDER BY created_at""",
        (resource_type, resource_id),
    ).fetchall()
    overrides = [
        Sensitivity(row["label"])
        for row in rows
        if row["label_source"] == LabelSource.USER_OVERRIDE
    ]
    labels = [Sensitivity(row["label"]) for row in rows]
    reasons = [
        reason
        for row in rows
        for reason in json.loads(row["reasons_json"] or "[]")
    ]
    return effective_label(labels, user_override=overrides[-1] if overrides else None), reasons

"""Rule-based sensitivity gate (Phase 1 layers: source defaults + config rules).

Classification never uses cloud models — classifying content remotely would
itself leak it. Anything the rules escalate is local-only forever after,
unless the user explicitly overrides: `release` is that override, run by the
owner after they change the rules.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from varyz.config import SensitivityConfig
from varyz.domain import NormalizedEntry, Sensitivity
from varyz.events import emit_event


class SensitivityGate:
    def __init__(self, cfg: SensitivityConfig):
        self.cfg = cfg
        self._patterns = [re.compile(p, re.IGNORECASE) for p in cfg.rules.patterns]

    def classify(self, source: str, norm: NormalizedEntry) -> Sensitivity:
        return self.label(source, norm.title, norm.body)

    def label(self, source: str, title: str | None, body: str) -> Sensitivity:
        base = self.cfg.source_defaults.get(source, self.cfg.default)
        if base == Sensitivity.SENSITIVE:
            return base
        text = f"{title or ''}\n{body}"
        for pattern in self._patterns:
            if pattern.search(text):
                return Sensitivity.SENSITIVE
        return base


@dataclass
class Release:
    """What `release` found: per source, entries held back and entries let go."""

    held: dict[str, int] = field(default_factory=dict)
    released: dict[str, int] = field(default_factory=dict)


def release(
    conn: sqlite3.Connection,
    gate: SensitivityGate,
    *,
    sources: tuple[str, ...] = (),
    apply: bool = False,
) -> Release:
    """Lower 'sensitive' entries that today's rules no longer hold back.

    'sensitive' is sticky on sync, so a source default or a pattern the owner
    has since dropped keeps every entry it ever caught. Only entries the
    current rules would label otherwise are lowered; nothing is raised or
    touched beyond its label, and each release is recorded as an event.
    """
    result = Release()
    query = "SELECT id, source, title, body FROM entries WHERE sensitivity = 'sensitive'"
    params: tuple[str, ...] = ()
    if sources:
        query += f" AND source IN ({','.join('?' * len(sources))})"
        params = sources
    changes: list[tuple[str, str, Sensitivity]] = []
    for row in conn.execute(query, params).fetchall():
        entry_id, source, title, body = row[0], str(row[1]), row[2], str(row[3] or "")
        level = gate.label(source, title, body)
        if level == Sensitivity.SENSITIVE:
            result.held[source] = result.held.get(source, 0) + 1
            continue
        result.released[source] = result.released.get(source, 0) + 1
        changes.append((entry_id, source, level))
    if apply and changes:
        with conn:
            for entry_id, source, level in changes:
                conn.execute(
                    "UPDATE entries SET sensitivity = ? WHERE id = ?", (level, entry_id)
                )
                emit_event(
                    conn, "entry_sensitivity_released", "entry", entry_id,
                    {"source": source, "from": "sensitive", "to": str(level)},
                )
    return result

"""Which data sources feed this archive: one answer every command shares.

Onboarding used to leave this unsaid. `init` wrote a config with every
connector commented out, `install` wired the agents, and nothing afterwards
said whether the archive was reading anything at all -- an owner with no
source connected and one with five saw the same GO from `varyz hooks doctor`.
`varyz setup`, `varyz sources`, the doctor and the trust centre now all read
the state from here, so they cannot disagree about it.

Only the alpha sources are listed (docs/SHIPPING.md). A held-back or retired
connector that a config still names is reported by the trust centre, which
shows every section; this list is about what an owner can connect today.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from varyz.config import Config


@dataclass(frozen=True)
class SourceInfo:
    connector_id: str
    label: str
    reads: str
    connect: str


# In the order onboarding offers them: the owner's own words first, then the
# history of what they built, then what needs an account.
ALPHA_SOURCES: tuple[SourceInfo, ...] = (
    SourceInfo(
        "ai_conversations",
        "AI conversations",
        "your own turns in local Claude Code, Codex and other agent histories",
        "varyz connect ai-conversations",
    ),
    SourceInfo(
        "git",
        "Git history",
        "commit history of the local repositories you approve",
        "varyz setup --source git",
    ),
    SourceInfo(
        "github",
        "GitHub",
        "your repositories, issues and pull-request threads",
        "varyz connect github",
    ),
    SourceInfo(
        "documents",
        "Project files",
        "docs and notes in the project folders you approve",
        "varyz setup --source documents",
    ),
    SourceInfo(
        "manual",
        "Notes",
        "what you capture yourself",
        'varyz add "..."',
    ),
)


@dataclass(frozen=True)
class SourceStatus:
    info: SourceInfo
    # "connected", "paused", "not approved" or "not connected".
    state: str
    items: int
    last_sync: str | None
    last_status: str | None
    scope: str

    @property
    def connected(self) -> bool:
        return self.state == "connected"


def _scope(connector_id: str, options: dict) -> str:
    """What was approved, in words that never echo a secret."""
    if not options:
        return ""
    if connector_id == "ai_conversations":
        paths = options.get("paths") or []
        if paths:
            return f"{len(paths)} approved folder(s)"
        adapters = options.get("adapters") or []
        return ", ".join(str(a) for a in adapters) if adapters else "every detected agent"
    if connector_id == "github":
        return str(options.get("account") or "configured account")
    paths = options.get("paths") or []
    if paths:
        return f"{len(paths)} approved path(s)"
    return ""


def _state(connector_id: str, options: dict) -> str:
    if not options:
        return "not connected"
    if options.get("enabled", True) is False:
        return "paused"
    if connector_id == "github":
        # The account's consent is the token it was linked with; there is no
        # separate `approved` key to read.
        return "connected" if options.get("account") else "not connected"
    if options.get("approved") is not True:
        return "not approved"
    if connector_id in {"git", "documents"} and not options.get("paths"):
        return "not connected"
    return "connected"


def source_statuses(cfg: Config, conn: sqlite3.Connection | None) -> list[SourceStatus]:
    """Every alpha source with its state, item count and last sync.

    ``conn`` may be None -- a config whose database was never created still
    has an answer: nothing indexed yet.
    """
    counts: dict[str, int] = {}
    synced: dict[str, tuple[str | None, str | None]] = {}
    if conn is not None:
        counts = {
            str(row[0]): int(row[1])
            for row in conn.execute("SELECT source, COUNT(*) FROM entries GROUP BY source")
        }
        synced = {
            str(row[0]): (row[1], row[2])
            for row in conn.execute(
                """SELECT connector_id, MAX(last_run_at), MAX(last_status)
                   FROM sync_state GROUP BY connector_id"""
            )
        }
    result = []
    for info in ALPHA_SOURCES:
        items = counts.get(info.connector_id, 0)
        if info.connector_id == "manual":
            # Notes need no section: `varyz add` is always available, and the
            # source counts as connected once it has been used.
            state = "connected" if items else "not connected"
            options: dict = {}
        else:
            options = cfg.connector_options(info.connector_id)
            state = _state(info.connector_id, options)
        last_run, last_status = synced.get(info.connector_id, (None, None))
        result.append(
            SourceStatus(
                info=info,
                state=state,
                items=items,
                last_sync=last_run,
                last_status=last_status,
                scope=_scope(info.connector_id, options),
            )
        )
    return result


def connected_summary(statuses: list[SourceStatus]) -> str:
    """One line for the doctor: which sources, and how much each holds."""
    connected = [s for s in statuses if s.connected]
    if not connected:
        return "none connected; run `varyz sources` to see what you can connect"
    parts = []
    for status in connected:
        never = not status.last_sync and status.info.connector_id != "manual"
        synced = ", never synced" if never else ""
        parts.append(f"{status.info.label} ({status.items:,} items{synced})")
    return ", ".join(parts)


__all__ = [
    "ALPHA_SOURCES",
    "SourceInfo",
    "SourceStatus",
    "connected_summary",
    "source_statuses",
]

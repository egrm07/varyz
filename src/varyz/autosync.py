"""Hands-off source refresh: background daemon plus stale-on-use trigger.

The daemon and MCP trigger both call the same idempotent run. A fixed claim
file prevents duplicate launches; the existing Varyz write lock prevents this
worker from interleaving with any other writer. Reads remain available through
SQLite WAL while a refresh is in progress.
"""

from __future__ import annotations

import configparser
import contextlib
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from varyz import connectors as connector_registry
from varyz import mac_app
from varyz.config import Config, load_config
from varyz.connectors.base import get_connector
from varyz.db.connection import LockHeldError, WriteLock, open_db
from varyz.domain import iso, utc_now
from varyz.legacy import LEGACY_DAEMON_PREFIX, LEGACY_NAME, LEGACY_REGISTRATION_MARKER
from varyz.pipeline.embed import Embedder, embed_pending
from varyz.pipeline.ingest import sync_connector
from varyz.pipeline.windows import build_windows
from varyz.vault import Vault

CLAIM_MAX_AGE = timedelta(hours=4)
DAEMON_PREFIX = "VaryzAutoSync"
DAEMON_HEARTBEAT_MAX_AGE = timedelta(seconds=30)
REGISTRATION_MARKER = "Managed by Varyz autosync"


@dataclass(frozen=True)
class ConnectorRun:
    connector: str
    status: str
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    excluded: int = 0
    deleted: int = 0
    errors: int = 0
    detail: str = ""


@dataclass
class AutoSyncRun:
    trigger: str
    status: str
    started_at: str
    finished_at: str
    connectors: list[ConnectorRun] = field(default_factory=list)
    embedded: int = 0
    graph_status: str = "not_run"
    graph_run_id: str | None = None
    graph_entries: int = 0
    graph_reused: int = 0
    graph_claims: int = 0
    graph_pruned: int = 0
    warnings: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def new(self) -> int:
        return sum(value.new for value in self.connectors)

    @property
    def updated(self) -> int:
        return sum(value.updated for value in self.connectors)

    @property
    def unchanged(self) -> int:
        return sum(value.unchanged for value in self.connectors)

    @property
    def excluded(self) -> int:
        return sum(value.excluded for value in self.connectors)

    @property
    def deleted(self) -> int:
        return sum(value.deleted for value in self.connectors)

    @property
    def errors(self) -> int:
        return sum(value.errors for value in self.connectors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "excluded": self.excluded,
            "deleted": self.deleted,
            "errors": self.errors,
            "embedded": self.embedded,
            "graph_status": self.graph_status,
            "graph_run_id": self.graph_run_id,
            "graph_entries": self.graph_entries,
            "graph_reused": self.graph_reused,
            "graph_claims": self.graph_claims,
            "graph_pruned": self.graph_pruned,
            "warnings": self.warnings,
            "detail": self.detail,
            "connectors": [asdict(value) for value in self.connectors],
        }


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DaemonStatus:
    registered: bool
    running: bool
    detail: str


def autosync_dir(cfg: Config) -> Path:
    return cfg.data_dir / "autosync"


def enabled_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "ENABLED"


def paused_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "PAUSED"


def claim_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "RUNNING"


def state_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "state.json"


def log_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "runs.jsonl"


def report_path(cfg: Config) -> Path:
    return cfg.data_dir / "reports" / "AUTOSYNC_LATEST.md"


def daemon_heartbeat_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "daemon.json"


def daemon_lock_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "DAEMON.lock"


def configured_connector_ids(cfg: Config) -> tuple[str, ...]:
    known = set(connector_registry.all_connector_ids())
    return tuple(
        connector_id
        for connector_id in sorted(cfg.connectors)
        if connector_id in known
        and cfg.connector_options(connector_id)
        and cfg.connector_options(connector_id).get("enabled", True) is not False
    )


def unknown_connector_ids(cfg: Config) -> tuple[str, ...]:
    known = set(connector_registry.all_connector_ids())
    return tuple(
        connector_id
        for connector_id in sorted(cfg.connectors)
        if connector_id not in known
        and cfg.connector_options(connector_id).get("enabled", True) is not False
    )


def enable(cfg: Config, config_path: Path, *, schedule_minutes: int | None = None) -> None:
    target = enabled_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "enabled_at": iso(utc_now()),
                "config": str(config_path.resolve()),
                "schedule_minutes": schedule_minutes or cfg.autosync.schedule_minutes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def disable(cfg: Config) -> None:
    enabled_path(cfg).unlink(missing_ok=True)


def pause(cfg: Config) -> None:
    target = paused_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"paused_at = {iso(utc_now())}\n", encoding="utf-8")


def resume(cfg: Config) -> None:
    paused_path(cfg).unlink(missing_ok=True)


def is_enabled(cfg: Config) -> bool:
    return cfg.autosync.enabled and enabled_path(cfg).is_file()


def is_paused(cfg: Config) -> bool:
    return paused_path(cfg).is_file()


def load_state(cfg: Config) -> dict[str, Any]:
    try:
        value = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_due(cfg: Config, *, now: datetime | None = None) -> bool:
    if not is_enabled(cfg) or is_paused(cfg) or not cfg.autosync.on_use:
        return False
    state = load_state(cfg)
    finished = _parse_time(state.get("finished_at"))
    if finished is None:
        return True
    current = now or utc_now()
    return current - finished >= timedelta(minutes=cfg.autosync.freshness_minutes)


def is_schedule_due(cfg: Config, *, now: datetime | None = None) -> bool:
    """Return whether the daemon recurrence is due, independent of on-use settings."""
    if not is_enabled(cfg) or is_paused(cfg):
        return False
    finished = _parse_time(load_state(cfg).get("finished_at"))
    if finished is None:
        return True
    current = now or utc_now()
    marker = _read_json(enabled_path(cfg))
    interval = marker.get("schedule_minutes", cfg.autosync.schedule_minutes)
    if not isinstance(interval, int) or not 5 <= interval <= 1440:
        interval = cfg.autosync.schedule_minutes
    return current - finished >= timedelta(minutes=interval)


def maybe_trigger_on_use(cfg: Config, config_path: Path | None) -> str:
    """Start a detached refresh when stale; never delay the current read."""
    if not is_due(cfg):
        return "fresh_or_disabled"
    if config_path is None or not config_path.is_file():
        return "no_config"
    if not _claim(cfg):
        return "already_running"
    command = [
        sys.executable,
        "-m",
        "varyz.autosync_worker",
        "--trigger",
        "on-use",
        "--claimed",
        "--config",
        str(config_path.resolve()),
    ]
    kwargs: dict[str, Any] = {
        "cwd": str(config_path.resolve().parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # A hidden console, not DETACHED_PROCESS: a process with no console at
        # all makes every git or gh it runs open a terminal window of its own.
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError:
        _release_claim(cfg)
        return "launch_failed"
    return "started"


def run_once(
    config_path: Path,
    *,
    trigger: str,
    dry_run: bool = False,
    force: bool = False,
    claimed: bool = False,
) -> AutoSyncRun:
    """Refresh every configured connector, embed, and update the graph once."""
    cfg = load_config(config_path)
    started = utc_now()
    ids = configured_connector_ids(cfg)
    unknown = unknown_connector_ids(cfg)
    if dry_run:
        connectors = [ConnectorRun(value, "planned") for value in ids]
        connectors.extend(
            ConnectorRun(value, "failed", errors=1, detail="unknown connector") for value in unknown
        )
        return AutoSyncRun(
            trigger=trigger,
            status="dry_run" if ids and not unknown else "failed",
            started_at=iso(started) or "",
            finished_at=iso(utc_now()) or "",
            connectors=connectors,
            detail="No sources, archive rows, vectors, logs, or schedule were changed.",
        )

    if not force and not is_enabled(cfg):
        return _simple_run(trigger, "disabled", started, "Run: varyz autosync install")

    owns_claim = claimed
    if not claimed:
        owns_claim = _claim(cfg)
        if not owns_claim:
            return _simple_run(trigger, "busy", started, "Another autosync is running")

    try:
        if is_paused(cfg):
            result = _simple_run(trigger, "paused", started, "Kill switch is engaged")
            _persist(cfg, result)
            return result
        if not cfg.db_path.is_file():
            result = _simple_run(trigger, "failed", started, "No Varyz database; run varyz init")
            _persist(cfg, result)
            _notify_failure(cfg, result)
            return result
        if not ids and not unknown:
            result = _simple_run(trigger, "failed", started, "No connectors configured")
            _persist(cfg, result)
            _notify_failure(cfg, result)
            return result

        outcomes = [
            ConnectorRun(value, "failed", errors=1, detail="unknown connector") for value in unknown
        ]
        embedded = 0
        stopped = False
        expired = False
        deadline = (
            started + timedelta(minutes=cfg.autosync.cycle_deadline_minutes)
            if cfg.autosync.cycle_deadline_minutes
            else None
        )

        def out_of_time() -> bool:
            return deadline is not None and utc_now() >= deadline

        details: list[str] = []
        # `details` contains failures; warnings and informational notes do not
        # turn an otherwise successful ingestion/graph cycle red.
        warnings: list[str] = []
        notes: list[str] = []
        if sys.platform == "darwin":
            blocked = record_mac_privacy(cfg, config_path)
            if blocked:
                warnings.append(mac_privacy_advice(blocked, config_path))
        graph_status = "not_run"
        graph_run_id = None
        graph_entries = 0
        graph_reused = 0
        graph_claims = 0
        graph_pruned = 0
        conn = open_db(cfg.db_path)
        try:
            try:
                with WriteLock(cfg.data_dir):
                    vault = Vault(cfg.vault_dir)
                    for connector_id in ids:
                        # This check stays inside the ingestion session so a pause
                        # can stop the current cycle between connectors.
                        if is_paused(cfg) or out_of_time():
                            reason = (
                                "kill switch engaged"
                                if is_paused(cfg)
                                else "cycle deadline reached"
                            )
                            if is_paused(cfg):
                                stopped = True
                            else:
                                expired = True
                            outcomes.extend(
                                ConnectorRun(value, "skipped", detail=reason)
                                for value in ids[ids.index(connector_id) :]
                            )
                            break
                        try:
                            stats = sync_connector(
                                conn,
                                vault,
                                get_connector(connector_id),
                                cfg,
                            )
                        except Exception as exc:  # one source never stops the rest
                            outcomes.append(
                                ConnectorRun(
                                    connector_id,
                                    "failed",
                                    errors=1,
                                    detail=_safe_error(exc),
                                )
                            )
                            continue
                        outcomes.append(
                            ConnectorRun(
                                connector_id,
                                "ok" if stats.errors == 0 else "partial",
                                new=stats.new,
                                updated=stats.updated,
                                unchanged=stats.unchanged,
                                excluded=stats.excluded,
                                deleted=stats.deleted,
                                errors=stats.errors,
                            )
                        )
                    try:
                        embedded = embed_pending(conn, cfg, Embedder(cfg))
                    except Exception as exc:
                        details.append(f"Embedding failed: {_safe_error(exc)}")
                    # Re-cut the conversation partition and both its
                    # indexes. This costs what embedding costs -- the same
                    # small local model, seconds over thousands of windows --
                    # so it belongs here and not in the GPU budget below,
                    # where having the conversations *read* lives. What this
                    # keeps true is that a window and its indexes never
                    # disagree about what is in it.
                    try:
                        partitioned = build_windows(
                            conn, sources=tuple(cfg.conversations.sources)
                        )
                        if partitioned["added"] or partitioned["removed"]:
                            notes.append(
                                f"Conversations: +{partitioned['added']} "
                                f"-{partitioned['removed']}"
                            )
                        # Cutting a conversation invalidates its vector, so
                        # the two run together or the index quietly describes
                        # messages that have since changed.
                        from varyz.pipeline.window_vectors import embed_windows

                        embed_windows(conn, cfg, Embedder(cfg))
                    except Exception as exc:
                        details.append(f"Conversation partition failed: {_safe_error(exc)}")
            except LockHeldError:
                result = _simple_run(
                    trigger,
                    "busy",
                    started,
                    "Another Varyz writer is active; the next recurrence will retry",
                )
                _persist(cfg, result)
                return result

            # Graph extraction has its own claim and takes the writer lock only
            # for staging and activation, so model latency cannot stop ingestion.
            if any(value.status in {"ok", "partial"} for value in outcomes):
                try:
                    from varyz.graph.cadence import rebuild_due
                    from varyz.graph.service import rebuild_graph

                    decision = rebuild_due(
                        conn,
                        min_new_entries=cfg.graph.rebuild_min_new_entries,
                        max_age_minutes=cfg.graph.rebuild_max_age_minutes,
                        excluded_sources=tuple(cfg.graph.excluded_sources),
                    )
                    if not decision.due:
                        graph_status = "throttled"
                        notes.append(f"Graph update deferred: {decision.reason}")
                    elif out_of_time():
                        expired = True
                        graph_status = "deadline"
                    elif (
                        cfg.graph.extractor.casefold().strip() != "deterministic"
                        and not cfg.autosync.semantic_graph
                    ):
                        graph_status = "deferred"
                        notes.append(
                            "Semantic graph update deferred: background local inference "
                            "is disabled; run `varyz graph rebuild` explicitly or set "
                            "autosync.semantic_graph = true"
                        )
                    else:
                        # Only the branch that acquires the managed model owns
                        # its cleanup. A deferred background refresh must never
                        # stop a model that an explicit compile is using.
                        try:
                            graph = rebuild_graph(
                                conn,
                                cfg,
                                should_stop=lambda: is_paused(cfg) or out_of_time(),
                            )
                            if graph.stopped and is_paused(cfg):
                                stopped = True
                                graph_status = "stopped"
                            elif graph.stopped:
                                expired = True
                                graph_status = "deadline"
                            else:
                                graph_status = "unchanged" if graph.no_op else "updated"
                            graph_run_id = graph.run_id
                            graph_entries = graph.entries_processed
                            graph_reused = graph.entries_reused
                            graph_claims = graph.claims
                            if graph.entries_failed:
                                notes.append(
                                    f"Graph quarantined {graph.entries_failed} "
                                    "entries the extractor could not read"
                                )
                            if not graph.stopped and cfg.graph.prune_on_sync:
                                with WriteLock(cfg.data_dir):
                                    graph_pruned = _prune_graph(cfg, conn, warnings)
                        finally:
                            # A rebuild is the only thing in a cycle that wants
                            # the model, and the next one is minutes away.
                            _release_local_model()
                except Exception as exc:
                    graph_status = "failed"
                    details.append(f"Graph update failed: {_safe_error(exc)}")
            else:
                graph_status = "skipped"
                details.append("Graph update skipped: no connector completed ingestion")

            # `lookup` and `interpret` answer from the personal model, which is
            # compiled from the graph. A build that holds the managed model back
            # can only compile it literally, in seconds, so each new graph is
            # compiled here. A build with the model leaves compiling to the owner,
            # whose last compile may be a semantic one this would replace.
            if graph_status == "updated":
                from varyz.llm.local_runtime import installer_is_shipped

                if not installer_is_shipped():
                    try:
                        from varyz.personal_model.compiler import build_personal_model_compiler

                        compiled = build_personal_model_compiler(conn, cfg).compile()
                        if compiled.failed or not compiled.activated:
                            details.append(
                                f"Personal model not recompiled: compile {compiled.status}"
                            )
                    except Exception as exc:
                        details.append(f"Personal model compile failed: {_safe_error(exc)}")

            # Read the conversations nobody has read yet. This is the only
            # stage whose cost scales with how long the owner was away, so it
            # is capped per cycle rather than run to completion: a week of
            # backlog is caught up over a week of cycles instead of holding
            # the card for an hour tonight. The pass commits one conversation
            # at a time and skips what it has read, so a cap costs nothing but
            # time, and the kill switch and the deadline reach inside the loop.
            #
            # It starts the same managed model the semantic graph does, so it
            # takes the same opt-in. One switch for "this daemon may use the
            # GPU in the background", not two that a user has to find.
            unread = _unread_conversations(conn)
            if cfg.conversations.read_on_sync and unread and not is_paused(cfg):
                if not cfg.autosync.semantic_graph:
                    notes.append(
                        f"{unread} conversations unread: background local inference "
                        "is disabled; run `varyz conversations read` or set "
                        "autosync.semantic_graph = true"
                    )
                elif out_of_time():
                    expired = True
                else:
                    try:
                        from varyz.pipeline.conclusions import reader, run_pass

                        author, author_model = reader(cfg)
                        try:
                            read = run_pass(
                                conn,
                                author=author,
                                author_model=author_model,
                                limit=cfg.conversations.max_per_sync,
                                should_stop=lambda: is_paused(cfg) or out_of_time(),
                            )
                        finally:
                            _release_local_model()
                        if read["read"]:
                            notes.append(
                                f"Conversations read: {read['read']} "
                                f"({read['personal']} quotable, "
                                f"{read['sensitive']} kept shut)"
                            )
                        if read["failed"]:
                            notes.append(
                                f"{read['failed']} conversations the model could not read"
                            )
                        if read["stopped"]:
                            if is_paused(cfg):
                                stopped = True
                            else:
                                expired = True
                    except Exception as exc:
                        details.append(f"Conversation reading failed: {_safe_error(exc)}")

            # Learn the order the owner guides agents in (website: colour, then
            # logo, then the page) from the turns that just arrived. Lexical and
            # quick; it reads only the owner's own non-sensitive turns and writes
            # an order only when it changed. A failure here is noted, not a
            # failed sync: the archive itself is up to date.
            fresh_turns = any(
                value.connector == "ai_conversations" and (value.new or value.updated)
                for value in outcomes
            )
            if (
                cfg.autosync.learn_workflows
                and fresh_turns
                and not is_paused(cfg)
                and not out_of_time()
            ):
                try:
                    written = _learn_workflows(cfg, conn)
                    if written:
                        notes.append(f"Workflows learned or updated: {written}")
                except Exception as exc:
                    notes.append(f"Workflow learning skipped: {_safe_error(exc)}")

            # Record the owner's picks in agents' questions as decisions. Reads
            # only the new lines of each transcript, so it runs every cycle: a
            # pick is often the last line of a session that is still open.
            if cfg.autosync.capture_decisions and not is_paused(cfg) and not out_of_time():
                try:
                    captured = capture_decisions(cfg, conn, apply=True)
                    if captured.recorded:
                        notes.append(
                            f"Decisions recorded from agents' questions: {captured.recorded}"
                            + (f" ({captured.superseded} replaced older answers)"
                               if captured.superseded else "")
                        )
                except Exception as exc:
                    notes.append(f"Decision capture skipped: {_safe_error(exc)}")

            # Cloud is off by default. Only an explicit cloud selection and
            # connected keyring credential enable this projection; never read
            # retrieval tables or expand the upload scope in the background.
            if not is_paused(cfg) and not out_of_time():
                from varyz.cloud.background import sync_if_selected

                cloud_note = sync_if_selected(config_path, conn)
                if cloud_note:
                    notes.append(cloud_note)
        finally:
            conn.close()

        # Judge failure before the stop note is added: being paused is not an error.
        failed = bool(details) or any(value.errors for value in outcomes)
        if stopped:
            notes.append("Stopped by the kill switch; the next run resumes from here")
        if expired:
            notes.append(
                f"Stopped at the {cfg.autosync.cycle_deadline_minutes}-minute cycle "
                "deadline; the next run resumes from here"
            )
        detail = "; ".join([*details, *notes])
        result = AutoSyncRun(
            trigger=trigger,
            status=(
                "failed" if failed else "paused" if stopped else "partial" if expired else "ok"
            ),
            started_at=iso(started) or "",
            finished_at=iso(utc_now()) or "",
            connectors=outcomes,
            embedded=embedded,
            graph_status=graph_status,
            graph_run_id=graph_run_id,
            graph_entries=graph_entries,
            graph_reused=graph_reused,
            graph_claims=graph_claims,
            graph_pruned=graph_pruned,
            warnings=warnings,
            detail=detail,
        )
        _persist(cfg, result)
        if failed:
            _notify_failure(cfg, result)
        return result
    finally:
        if owns_claim:
            _release_claim(cfg)


def _prune_graph(cfg: Config, conn, warnings: list[str]) -> int:
    """Delete the generations this rebuild just made redundant.

    The caller holds the writer lock. A failure here is reported but does not
    fail the cycle: stale rows are wasted space, not a wrong answer.
    """
    try:
        from varyz.graph.prune import prune_graph

        result = prune_graph(
            conn,
            keep=cfg.graph.keep_generations,
            keep_job_days=cfg.graph.keep_job_days,
        )
        return result.rows
    except Exception as exc:
        warnings.append(f"Graph prune failed: {_safe_error(exc)}")
        return 0


def _learn_workflows(cfg: Config, conn: sqlite3.Connection) -> int:
    """Hand agents the orders repeated across the owner's sessions; how many changed."""
    from functools import partial

    from varyz.resolution.models import Caller
    from varyz.resolution.workflow_miner import learn_workflows, record_learned
    from varyz.resolution.workflows import SQLiteWorkflowStore

    learned, _stats = learn_workflows(conn)
    if not learned:
        return 0
    caller = Caller(
        agent_id="varyz:autosync",
        display_name="Varyz autosync",
        purpose="learn_workflows",
        device_id=cfg.resolution.device_id,
        transport="local",
    )
    record = partial(
        SQLiteWorkflowStore(conn).learn, caller=caller, device_id=cfg.resolution.device_id
    )
    with WriteLock(cfg.data_dir):
        written, _unchanged = record_learned(record, learned)
    return written


def decision_offsets_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "decision_capture.json"


def capture_decisions(cfg: Config, conn: sqlite3.Connection, *, apply: bool):
    """Record the owner's picks in Claude Code questions; ``apply`` False is a dry run."""
    from varyz.resolution.decision_capture import capture
    from varyz.resolution.models import Caller

    caller = Caller(
        agent_id="varyz:autosync",
        display_name="Varyz autosync",
        purpose="capture_decisions",
        device_id=cfg.resolution.device_id,
        transport="local",
    )

    def run():
        return capture(
            conn,
            cfg.connector_options("ai_conversations"),
            offsets_path=decision_offsets_path(cfg),
            caller=caller,
            device_id=cfg.resolution.device_id,
            apply=apply,
        )

    if not apply:
        return run()
    with WriteLock(cfg.data_dir):
        return run()


def _unread_conversations(conn: sqlite3.Connection) -> int:
    """Conversations the local model has never read, as they stand today.

    Joined on the content hash, so a conversation that has since grown counts
    as unread again: the reading was of messages that are no longer all of it.
    Cheap, and model-free, which is why it is asked before anything decides to
    start a model.

    Counts only what the pass would actually offer a model. Without the size
    floor a sync would report thousands of two-word exchanges as waiting to be
    read, and no amount of reading would ever bring that number down.
    """
    from varyz.pipeline.conclusions import MIN_CHARACTERS, MIN_TURNS

    try:
        row = conn.execute(
            "SELECT count(*) FROM conversation_windows w"
            " LEFT JOIN conversation_window_labels l"
            "   ON l.window_id = w.id AND l.content_hash = w.content_hash"
            " WHERE l.window_id IS NULL"
            "   AND w.turns >= ? AND w.characters >= ?",
            (MIN_TURNS, MIN_CHARACTERS),
        ).fetchone()
    except sqlite3.OperationalError:
        # An archive older than the conversation migration.
        return 0
    return int(row[0])


def _release_local_model() -> None:
    """Unload managed weights from VRAM; harmless when nothing is running."""
    with contextlib.suppress(Exception):
        from varyz.llm.local_runtime import stop_managed_servers

        stop_managed_servers()


def doctor(config_path: Path) -> list[DoctorCheck]:
    cfg = load_config(config_path)
    ids = configured_connector_ids(cfg)
    checks = [
        DoctorCheck("config", config_path.is_file(), str(config_path.resolve())),
        DoctorCheck("database", cfg.db_path.is_file(), str(cfg.db_path)),
        DoctorCheck(
            "connectors",
            bool(ids),
            ", ".join(ids) if ids else "none configured",
        ),
        DoctorCheck(
            "registry",
            not unknown_connector_ids(cfg),
            (
                "all configured sources are registered"
                if not unknown_connector_ids(cfg)
                else "unknown: " + ", ".join(unknown_connector_ids(cfg))
            ),
        ),
        DoctorCheck(
            "python",
            importlib.util.find_spec("varyz") is not None,
            sys.executable,
        ),
    ]
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    checks.append(
        DoctorCheck(
            "data directory",
            os.access(cfg.data_dir, os.W_OK),
            str(cfg.data_dir),
        )
    )
    checks.append(_login_startup_check(config_path))
    if sys.platform == "darwin":
        checks.append(_mac_privacy_check(cfg, config_path))
    from varyz.update import doctor_detail

    update_ok, update_detail = doctor_detail(cfg)
    checks.append(DoctorCheck("update", update_ok, update_detail))
    for connector_id in ids:
        checks.append(_connector_check(cfg, connector_id))
    return checks


def daemon_name(config_path: Path) -> str:
    suffix = hashlib.sha256(str(config_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{DAEMON_PREFIX}-{suffix}"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"


def startup_path(config_path: Path) -> Path:
    return startup_dir() / f"{daemon_name(config_path)}.vbs"


def launch_agent_path(config_path: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_posix_daemon_name(config_path)}.plist"


def systemd_service_path(config_path: Path) -> Path:
    return _xdg_config_home() / "systemd" / "user" / f"{_linux_daemon_name(config_path)}.service"


def xdg_autostart_path(config_path: Path) -> Path:
    return _xdg_config_home() / "autostart" / f"{_linux_daemon_name(config_path)}.desktop"


def _posix_daemon_name(config_path: Path) -> str:
    return f"com.varyz.autosync.{daemon_name(config_path).rsplit('-', 1)[-1].lower()}"


def _linux_daemon_name(config_path: Path) -> str:
    return f"varyz-autosync-{daemon_name(config_path).rsplit('-', 1)[-1].lower()}"


def legacy_registrations(config_path: Path) -> list[Path]:
    """Login registrations the pre-rename version wrote for this archive.

    They carry the same per-archive suffix under the old names, and start
    `-m varys.autosync_daemon`, which no longer exists. Only files that version
    recognisably wrote are returned; another archive's are never touched.
    """
    suffix = daemon_name(config_path).rsplit("-", 1)[-1]
    if sys.platform == "win32":
        candidates = [startup_dir() / f"{LEGACY_DAEMON_PREFIX}-{suffix}.vbs"]
    elif sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        candidates = [agents / f"com.{LEGACY_NAME}.autosync.{suffix.lower()}.plist"]
    elif sys.platform.startswith("linux"):
        name = f"{LEGACY_NAME}-autosync-{suffix.lower()}"
        candidates = [
            _xdg_config_home() / "systemd" / "user" / f"{name}.service",
            _xdg_config_home() / "autostart" / f"{name}.desktop",
        ]
    else:
        candidates = []
    return [path for path in candidates if _registration_owned(path)]


def _remove_legacy_registrations(config_path: Path) -> list[Path]:
    removed = []
    for path in legacy_registrations(config_path):
        if path.suffix == ".service":
            systemctl = _systemd_user_manager()
            if systemctl is not None:
                _disable_systemd_unit(systemctl, path)
        elif path.suffix == ".plist":
            _launchd_stop(path)
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def _xdg_config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".config"


def _systemd_user_manager() -> str | None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None
    try:
        completed = subprocess.run(
            [systemctl, "--user", "show-environment"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return systemctl if completed.returncode == 0 else None


def _xdg_session_available() -> bool:
    return any(
        os.environ.get(name)
        for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY")
    )


def _launchd_plist(config_path: Path, cfg: Config) -> str:
    payload = plistlib.dumps(
        {
            "Label": _posix_daemon_name(config_path),
            "ProgramArguments": daemon_command(config_path, wait=True),
            "WorkingDirectory": str(config_path.parent),
            "RunAtLoad": True,
            # Background: macOS schedules it below anything the owner is using.
            "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": _launchd_path()},
            "StandardOutPath": str(daemon_launch_log_path(cfg)),
            "StandardErrorPath": str(daemon_launch_log_path(cfg)),
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    ).decode("utf-8")
    declaration = '<?xml version="1.0" encoding="UTF-8"?>\n'
    return payload.replace(declaration, f"{declaration}<!-- {REGISTRATION_MARKER} -->\n", 1)


# launchd starts an agent with only the system folders on PATH, so git, npm and
# uv installed through Homebrew (or into ~/.local/bin) would not be found.
_MAC_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _launchd_path() -> str:
    """The PATH the agent runs with: the installing shell's, then the usual folders."""
    entries: list[str] = []
    for value in [*os.environ.get("PATH", "").split(os.pathsep), *_MAC_TOOL_DIRS]:
        if value.startswith("/") and value not in entries:
            entries.append(value)
    return ":".join(entries)


def _launchd_domain() -> str:
    """This login session's launchd domain."""
    return f"gui/{os.getuid()}"  # type: ignore[attr-defined,unused-ignore]


def _launchctl(*args: str) -> bool:
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return False
    try:
        completed = subprocess.run(
            [launchctl, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _launchd_start(target: Path) -> bool:
    """Load the agent into this session and start it now, not at the next login.

    Already loaded (a reinstall) means `kickstart`, which starts it only when
    it is not running: a sync in progress is never killed to be replaced by an
    identical one. Its definition then refreshes at the next login.
    """
    if shutil.which("launchctl") is None:
        return False
    domain = _launchd_domain()
    if _launchctl("print", f"{domain}/{target.stem}"):
        return _launchctl("kickstart", f"{domain}/{target.stem}")
    return _launchctl("bootstrap", domain, str(target))


def _launchd_stop(target: Path) -> None:
    """Unload an agent, so it does not linger in this session after uninstall."""
    if shutil.which("launchctl") is not None:
        _launchctl("bootout", f"{_launchd_domain()}/{target.stem}")


def _systemd_unit(config_path: Path, cfg: Config) -> str:
    command = " ".join(_systemd_quote(value) for value in worker_command(config_path, daemon=True))
    working_directory = _systemd_quote(str(config_path.parent))
    output = _systemd_quote(f"append:{daemon_launch_log_path(cfg)}")
    return (
        f"# {REGISTRATION_MARKER}\n"
        "[Unit]\n"
        "Description=Varyz autosync daemon\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        f"WorkingDirectory={working_directory}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        f"StandardOutput={output}\n"
        f"StandardError={output}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _xdg_desktop_entry(config_path: Path) -> str:
    command = " ".join(
        _desktop_exec_quote(value) for value in worker_command(config_path, daemon=True)
    )
    return (
        f"# {REGISTRATION_MARKER}\n"
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Varyz Autosync\n"
        "Comment=Keep the local Varyz archive current\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _desktop_exec_quote(value: str) -> str:
    escaped = (
        value.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def _registration_owned(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if REGISTRATION_MARKER in text or LEGACY_REGISTRATION_MARKER in text:
        return True
    # Recognize registrations emitted before ownership markers were added, under
    # the current name or the pre-rename one.
    names = ("varyz", LEGACY_NAME)
    if path.suffix == ".vbs":
        return "CreateObject(\"WScript.Shell\")" in text and any(
            f"{name}Shell.Run" in text for name in names
        )
    if path.name == "daemon_runner.cmd":
        return any(
            f"{name}.autosync_daemon" in text and f"{name}_code" in text for name in names
        )
    return False


def _refuse_foreign_registration(path: Path) -> None:
    if path.exists() and not _registration_owned(path):
        raise ValueError(f"login startup path contains a non-Varyz file: {path}")


def _remove_owned_file(path: Path) -> bool | None:
    if not path.exists():
        return False
    if not _registration_owned(path):
        return None
    path.unlink()
    return True


def _registration_file_valid(path: Path, config_path: Path) -> bool:
    if not _registration_owned(path):
        return False
    try:
        if path.suffix == ".plist":
            value = plistlib.loads(path.read_bytes())
            arguments = value.get("ProgramArguments", [])
            if arguments[:1] == [mac_app.OPEN]:
                # Through the Varyz app: the launcher beside it names the daemon,
                # its arguments quoted for the shell.
                directory = autosync_dir(load_config(config_path))
                app = mac_app.ready(directory)
                runs = app is not None and str(app) in arguments and mac_app.launcher_runs(
                    directory, "varyz.autosync_daemon", shlex.quote(str(config_path))
                )
            else:
                runs = "varyz.autosync_daemon" in arguments and str(config_path) in arguments
            return (
                value.get("Label") == _posix_daemon_name(config_path)
                and value.get("RunAtLoad") is True
                and runs
            )
        if path.suffix in {".service", ".desktop"}:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(path, encoding="utf-8")
            section = "Service" if path.suffix == ".service" else "Desktop Entry"
            command = parser.get(section, "ExecStart" if section == "Service" else "Exec")
            unescaped = command.replace("\\\\", "\\").replace("%%", "%")
            return "varyz.autosync_daemon" in unescaped and str(config_path) in unescaped
    except (
        OSError,
        UnicodeError,
        ValueError,
        configparser.Error,
        plistlib.InvalidFileException,
    ):
        return False
    return True


def _windows_registration_valid(config_path: Path) -> bool:
    startup = startup_path(config_path)
    runner = daemon_runner_path(load_config(config_path))
    if not _registration_owned(startup) or not _registration_owned(runner):
        return False
    try:
        startup_text = startup.read_text(encoding="utf-8")
        runner_text = runner.read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        str(runner) in startup_text
        and "varyz.autosync_daemon" in runner_text
        and str(config_path) in runner_text
    )


def _systemd_unit_enabled(systemctl: str, target: Path) -> bool:
    try:
        completed = subprocess.run(
            [systemctl, "--user", "is-enabled", target.name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _disable_systemd_unit(systemctl: str, target: Path) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [systemctl, "--user", "disable", "--now", target.name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        subprocess.run(
            [systemctl, "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )


def _registration_is_active(config_path: Path) -> bool:
    if sys.platform == "win32":
        return shutil.which("wscript") is not None and _windows_registration_valid(config_path)
    if sys.platform == "darwin":
        return _registration_file_valid(launch_agent_path(config_path), config_path)
    if sys.platform.startswith("linux"):
        service = systemd_service_path(config_path)
        systemctl = _systemd_user_manager()
        if (
            systemctl is not None
            and _registration_file_valid(service, config_path)
            and _systemd_unit_enabled(systemctl, service)
        ):
            return True
        desktop = xdg_autostart_path(config_path)
        return _xdg_session_available() and _registration_file_valid(desktop, config_path)
    return False


def _login_startup_check(config_path: Path) -> DoctorCheck:
    config_path = config_path.resolve()
    if sys.platform == "win32":
        target = startup_path(config_path)
        available = bool(os.environ.get("APPDATA")) and shutil.which("wscript") is not None
        mechanism = "Windows login startup"
    elif sys.platform == "darwin":
        target = launch_agent_path(config_path)
        available = Path.home().is_dir()
        mechanism = "macOS launchd user agent"
    elif sys.platform.startswith("linux"):
        service = systemd_service_path(config_path)
        desktop = xdg_autostart_path(config_path)
        systemctl = _systemd_user_manager()
        if service.exists():
            target = service
            available = True
            mechanism = "systemd user service"
        elif desktop.exists():
            target = desktop
            available = _xdg_session_available()
            mechanism = "XDG desktop autostart"
        elif systemctl is not None:
            target = service
            available = True
            mechanism = "systemd user service"
        else:
            target = desktop
            available = _xdg_session_available()
            mechanism = "XDG desktop autostart"
    else:
        return DoctorCheck("login startup", False, f"unsupported platform: {sys.platform}")

    if target.exists() and not _registration_owned(target):
        return DoctorCheck(
            "login startup", False, f"non-Varyz file blocks {mechanism} at {target}"
        )
    legacy = legacy_registrations(config_path)
    if legacy:
        return DoctorCheck(
            "login startup",
            False,
            f"pre-rename registration at {legacy[0]} starts a program that no longer "
            "exists; run: varyz autosync install (or varyz autosync uninstall)",
        )
    if target.exists():
        active = _registration_is_active(config_path)
        state = "will run at login" if active else "will not run at login"
        return DoctorCheck(
            "login startup", active, f"registered via {mechanism} at {target}; {state}"
        )
    if available:
        return DoctorCheck(
            "login startup", True, f"not registered; {mechanism} is available at {target}"
        )
    return DoctorCheck("login startup", False, f"{mechanism} is unavailable; path: {target}")


def daemon_runner_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "daemon_runner.cmd"


def daemon_launch_log_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "daemon-launch.log"


def worker_command(config_path: Path, *, daemon: bool = False) -> list[str]:
    config_path = config_path.resolve()
    worker = [
        "-m",
        "varyz.autosync_daemon" if daemon else "varyz.autosync_worker",
        "--config",
        str(config_path),
    ]
    if not daemon:
        worker[2:2] = ["--trigger", "scheduled"]
    uv = shutil.which("uv")
    pyvenv = Path(sys.prefix) / "pyvenv.cfg"
    try:
        uv_managed = "\nuv =" in ("\n" + pyvenv.read_text(encoding="utf-8"))
    except OSError:
        uv_managed = False
    # The project that owns this environment, if it is a source checkout's
    # `.venv`. A `uv tool install` is uv-managed too but belongs to no project:
    # `uv run` in the config's folder then found a bare Python without Varyz,
    # and the login job died with "No module named 'varyz'".
    project = Path(sys.prefix).parent
    if uv and uv_managed and (project / "pyproject.toml").is_file():
        # `uv run` reliably selects the managed environment even when Windows
        # starts this process at login with an unrelated working directory.
        command = [uv, "run", "--directory", str(project), "python", *worker]
    else:
        command = [sys.executable, *worker]
    return command


def daemon_command(config_path: Path, *, wait: bool) -> list[str]:
    """How the daemon is started: on macOS through the Varyz app once it is built."""
    if sys.platform == "darwin":
        app = mac_app.ready(autosync_dir(load_config(config_path)))
        if app is not None:
            return mac_app.open_command(app, wait=wait)
    return worker_command(config_path, daemon=True)


def daemon_program(config_path: Path) -> str:
    """What macOS asks about: the Varyz app, or the Python it could not be built for."""
    if sys.platform == "darwin":
        app = mac_app.ready(autosync_dir(load_config(config_path)))
        if app is not None:
            return str(app)
    return worker_command(config_path, daemon=True)[0]


def _prepare_mac_app(config_path: Path, cfg: Config) -> None:
    """Build the Varyz app and point its launcher at this install's daemon.

    Where the app cannot be built, the agent runs Python directly as before,
    and only Full Disk Access can open a protected folder to it.
    """
    directory = autosync_dir(cfg)
    if mac_app.build_app(directory, _posix_daemon_name(config_path)) is None:
        return
    mac_app.write_launcher(
        directory,
        worker_command(config_path, daemon=True),
        working_directory=config_path.parent,
        path=_launchd_path(),
        log=daemon_launch_log_path(cfg),
        marker=REGISTRATION_MARKER,
    )


def code_fingerprint() -> str:
    """Identify the installed Varyz source, so a daemon can notice it changed.

    A daemon started days ago holds whatever code it imported at launch. That
    is how an old extractor default came back and tried to overwrite a newer
    graph. Size and mtime are enough to detect an edit or a reinstall.

    Migrations count as installed source. They are not imported, so a new one
    would otherwise leave the daemon running happily against a schema it is a
    version behind, applying it only whenever the machine next rebooted.
    """
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    sources = (path for pattern in ("*.py", "*.sql") for path in root.rglob(pattern))
    for path in sorted(sources):
        with contextlib.suppress(OSError):
            info = path.stat()
            digest.update(
                f"{path.relative_to(root)}:{info.st_size}:{int(info.st_mtime)}\n".encode()
            )
    return digest.hexdigest()


def spawn_daemon(config_path: Path) -> bool:
    """Start a fresh, console-free daemon process. False if it could not launch."""
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    startup = startup_path(config_path)
    wscript = shutil.which("wscript") if sys.platform == "win32" else None
    if wscript and startup.is_file():
        command: list[str] = [wscript, str(startup)]
    else:
        if sys.platform == "win32":
            # Never rewritten here: the predecessor's cmd.exe is still reading
            # this file by byte offset while it hands off.
            runner = daemon_runner_path(cfg)
            if not runner.is_file():
                write_daemon_runner(config_path)
            command = ["cmd.exe", "/D", "/C", str(runner)]
        else:
            command = daemon_command(config_path, wait=False)
    try:
        subprocess.Popen(
            command,
            cwd=str(config_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        return False
    return True


def write_daemon_runner(config_path: Path) -> Path:
    cfg = load_config(config_path)
    target = daemon_runner_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = worker_command(config_path, daemon=True)
    invocation = " ".join(_cmd_quote(value) for value in command)
    log = _cmd_quote(str(daemon_launch_log_path(cfg)))
    # A daemon's stderr goes to a file of its own and joins the log once it
    # exits: redirecting it into the log would hold the log open for the
    # daemon's lifetime, and the successor's own ">>log" would then fail
    # before it ever started.
    errors = str(daemon_launch_log_path(cfg).with_suffix("")).replace("%", "%%")
    target.write_text(
        f"REM {REGISTRATION_MARKER}\n"
        "@echo off\n"
        f'set "varyz_err={errors}-%RANDOM%%RANDOM%.err"\n'
        f">>{log} echo %DATE% %TIME% START\n"
        f'{invocation} 2>"%varyz_err%"\n'
        'set "varyz_code=%ERRORLEVEL%"\n'
        f">>{log} echo %DATE% %TIME% END exit=%varyz_code%\n"
        f'if exist "%varyz_err%" (type "%varyz_err%" >>{log} & del "%varyz_err%")\n'
        "exit /b %varyz_code%\n",
        encoding="utf-8",
    )
    return target


def install_daemon(config_path: Path, *, every_minutes: int, dry_run: bool = False) -> str:
    if not 5 <= every_minutes <= 1440:
        raise ValueError("autosync interval must be between 5 and 1440 minutes")
    config_path = config_path.resolve()
    if sys.platform == "win32":
        installed = _install_windows_daemon(
            config_path, every_minutes=every_minutes, dry_run=dry_run
        )
    elif sys.platform == "darwin":
        installed = _install_launchd_daemon(
            config_path, every_minutes=every_minutes, dry_run=dry_run
        )
    elif sys.platform.startswith("linux"):
        installed = _install_linux_daemon(
            config_path, every_minutes=every_minutes, dry_run=dry_run
        )
    else:
        raise ValueError(f"automatic daemon installation is unavailable on {sys.platform}")
    legacy = legacy_registrations(config_path)
    if not legacy:
        return installed
    if dry_run:
        return f"{installed}; remove pre-rename {', '.join(str(path) for path in legacy)}"
    removed = _remove_legacy_registrations(config_path)
    return f"{installed}; removed pre-rename {', '.join(str(path) for path in removed)}"


def _install_windows_daemon(
    config_path: Path, *, every_minutes: int, dry_run: bool
) -> str:
    if not os.environ.get("APPDATA") or shutil.which("wscript") is None:
        raise ValueError("Windows login startup support is unavailable: APPDATA or wscript missing")
    cfg = load_config(config_path)
    runner = daemon_runner_path(cfg)
    startup = startup_path(config_path)
    _refuse_foreign_registration(startup)
    _refuse_foreign_registration(runner)
    if dry_run:
        return f"write {startup}; launch {runner}; every {every_minutes} minutes"
    write_daemon_runner(config_path)
    startup.parent.mkdir(parents=True, exist_ok=True)
    launch = f'cmd.exe /D /C ""{runner}""'
    escaped = launch.replace('"', '""')
    startup.write_text(
        f"' {REGISTRATION_MARKER}\n"
        f'Set varyzShell = CreateObject("WScript.Shell")\nvaryzShell.Run "{escaped}", 0, False\n',
        encoding="utf-8",
    )
    enable(cfg, config_path, schedule_minutes=every_minutes)
    try:
        subprocess.Popen(
            [shutil.which("wscript") or "wscript.exe", str(startup)],
            cwd=str(config_path.resolve().parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except OSError as exc:
        disable(cfg)
        startup.unlink(missing_ok=True)
        runner.unlink(missing_ok=True)
        raise ValueError(f"could not start autosync daemon: {_safe_error(exc)}") from exc
    return str(startup)


def _install_launchd_daemon(
    config_path: Path, *, every_minutes: int, dry_run: bool
) -> str:
    home = Path.home()
    if not home.is_dir():
        raise ValueError("macOS launchd user agent support is unavailable: home directory missing")
    cfg = load_config(config_path)
    target = launch_agent_path(config_path)
    _refuse_foreign_registration(target)
    if dry_run:
        return f"write {target}; start it now and at every login; every {every_minutes} minutes"
    try:
        _prepare_mac_app(config_path, cfg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_launchd_plist(config_path, cfg), encoding="utf-8")
        enable(cfg, config_path, schedule_minutes=every_minutes)
    except OSError as exc:
        target.unlink(missing_ok=True)
        disable(cfg)
        raise ValueError(
            f"macOS launchd user agent support is unavailable: {_safe_error(exc)}"
        ) from exc
    # Without launchctl (or if it refuses) the agent still runs from the next
    # login; until then a plain background process keeps the archive current.
    if not _launchd_start(target) and not spawn_daemon(config_path):
        disable(cfg)
        target.unlink(missing_ok=True)
        raise ValueError("could not start autosync daemon")
    return str(target)


def _install_linux_daemon(
    config_path: Path, *, every_minutes: int, dry_run: bool
) -> str:
    cfg = load_config(config_path)
    systemctl = _systemd_user_manager()
    systemd_failure = "systemd user manager missing"
    if systemctl is not None:
        target = systemd_service_path(config_path)
        alternate = xdg_autostart_path(config_path)
        _refuse_foreign_registration(target)
        if dry_run:
            return f"write and enable {target}; every {every_minutes} minutes"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_systemd_unit(config_path, cfg), encoding="utf-8")
            completed = subprocess.run(
                [systemctl, "--user", "daemon-reload"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode == 0:
                completed = subprocess.run(
                    [systemctl, "--user", "enable", "--now", target.name],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            completed = subprocess.CompletedProcess([], 1, stderr=str(exc))
        if completed.returncode == 0:
            try:
                _remove_owned_file(alternate)
                enable(cfg, config_path, schedule_minutes=every_minutes)
            except OSError as exc:
                _disable_systemd_unit(systemctl, target)
                target.unlink(missing_ok=True)
                raise ValueError(
                    f"Linux systemd user service support is unavailable: {_safe_error(exc)}"
                ) from exc
            return str(target)
        failure_detail = getattr(completed, "stderr", "") or getattr(completed, "stdout", "")
        systemd_failure = "systemctl --user could not enable the Varyz service"
        if failure_detail:
            systemd_failure += f": {_safe_text(failure_detail)[:300]}"
        target.unlink(missing_ok=True)

    if not _xdg_session_available():
        raise ValueError(
            f"Linux login startup support is unavailable: {systemd_failure}; "
            "XDG desktop session missing"
        )
    target = xdg_autostart_path(config_path)
    alternate = systemd_service_path(config_path)
    _refuse_foreign_registration(target)
    if dry_run:
        return f"write {target}; launch at next desktop login; every {every_minutes} minutes"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_xdg_desktop_entry(config_path), encoding="utf-8")
        _remove_owned_file(alternate)
        enable(cfg, config_path, schedule_minutes=every_minutes)
    except OSError as exc:
        target.unlink(missing_ok=True)
        disable(cfg)
        raise ValueError(f"Linux XDG autostart support is unavailable: {_safe_error(exc)}") from exc
    return str(target)


def uninstall_daemon(config_path: Path) -> str:
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    disable(cfg)
    if sys.platform == "win32":
        targets = [startup_path(config_path), daemon_runner_path(cfg)]
    elif sys.platform == "darwin":
        agent = launch_agent_path(config_path)
        if _registration_owned(agent):
            _launchd_stop(agent)
        targets = [agent]
    elif sys.platform.startswith("linux"):
        service = systemd_service_path(config_path)
        if _registration_owned(service):
            systemctl = _systemd_user_manager()
            if systemctl is not None:
                _disable_systemd_unit(systemctl, service)
        targets = [service, xdg_autostart_path(config_path)]
    else:
        raise ValueError(f"automatic daemon uninstallation is unavailable on {sys.platform}")
    outcomes = [_remove_owned_file(target) for target in targets]
    removed = [str(target) for target, outcome in zip(targets, outcomes, strict=True) if outcome]
    removed += [str(path) for path in _remove_legacy_registrations(config_path)]
    foreign = [
        str(target)
        for target, outcome in zip(targets, outcomes, strict=True)
        if outcome is None
    ]
    if removed:
        return "removed " + ", ".join(removed)
    if foreign:
        return "left non-Varyz file in place: " + ", ".join(foreign)
    return "nothing installed; no Varyz login registration found"


def daemon_status(config_path: Path, *, now: datetime | None = None) -> DaemonStatus:
    cfg = load_config(config_path)
    registered = _registration_is_active(config_path.resolve())
    heartbeat = _read_json(daemon_heartbeat_path(cfg))
    beat = _parse_time(heartbeat.get("heartbeat_at"))
    current = now or utc_now()
    running = beat is not None and current - beat <= DAEMON_HEARTBEAT_MAX_AGE
    if running:
        detail = f"pid {heartbeat.get('pid', '?')}; {heartbeat.get('status', 'unknown')}"
    elif heartbeat:
        detail = f"last heartbeat {heartbeat.get('heartbeat_at', '?')}"
    else:
        detail = "no heartbeat yet"
    return DaemonStatus(registered=registered, running=running, detail=detail)


def _connector_check(cfg: Config, connector_id: str) -> DoctorCheck:
    options = cfg.connector_options(connector_id)
    if connector_id in {"documents", "git", "personal_archive"}:
        paths = [_option_path(cfg, value) for value in options.get("paths", [])]
        ok = bool(paths) and any(value.exists() for value in paths)
        return DoctorCheck(f"source:{connector_id}", ok, f"{sum(p.exists() for p in paths)} paths")
    return DoctorCheck(f"source:{connector_id}", True, "configured; live auth checked on sync")


# Folders macOS privacy protection (TCC) guards. A background agent gets
# "Operation not permitted" there unless its program has Full Disk Access, and
# macOS never asks: the folder simply reads as unreadable, and a walk that skips
# unreadable folders indexes nothing from it without saying so.
_MAC_PROTECTED = (
    "Desktop",
    "Documents",
    "Downloads",
    "Library/Mobile Documents",  # iCloud Drive
    "Pictures",
    "Movies",
    "Music",
)


def mac_protected_paths(cfg: Config) -> list[Path]:
    """The archive and configured source folders that sit in a protected place."""
    home = Path.home()
    guarded = [home / name for name in _MAC_PROTECTED] + [Path("/Volumes")]
    candidates = [cfg.data_dir]
    for connector_id in configured_connector_ids(cfg):
        values = cfg.connector_options(connector_id).get("paths") or []
        candidates.extend(_option_path(cfg, value) for value in values if str(value).strip())
    found: list[Path] = []
    for path in candidates:
        if path not in found and any(path == root or root in path.parents for root in guarded):
            found.append(path)
    return found


def mac_folder_name(path: Path) -> str:
    """The protected place a path is in, as macOS names it when it asks."""
    home = Path.home()
    for name in _MAC_PROTECTED:
        root = home / name
        if path == root or root in path.parents:
            return "iCloud Drive" if name == "Library/Mobile Documents" else name
    return "external drives"


def mac_protected_sources(cfg: Config) -> dict[str, str]:
    """Each protected folder, named by the source it belongs to."""
    from varyz.sources import ALPHA_SOURCES

    labels = {info.connector_id: info.label for info in ALPHA_SOURCES}
    named = {str(cfg.data_dir): "the Varyz archive"}
    for connector_id in configured_connector_ids(cfg):
        for value in cfg.connector_options(connector_id).get("paths") or []:
            if str(value).strip():
                named.setdefault(
                    str(_option_path(cfg, value)), labels.get(connector_id, connector_id)
                )
    return {path: named.get(path, Path(path).name) for path in map(str, mac_protected_paths(cfg))}


def mac_privacy_path(cfg: Config) -> Path:
    return autosync_dir(cfg) / "macos_privacy.json"


def record_mac_privacy(cfg: Config, config_path: Path) -> list[Path]:
    """Which protected folders this process cannot read, as the agent sees them.

    Only the background agent's own answer counts: a Terminal with Full Disk
    Access reads everything, so `doctor` run by hand would report all clear.
    The result is kept for `doctor` to show.
    """
    blocked: list[Path] = []
    for path in mac_protected_paths(cfg):
        try:
            with os.scandir(path):
                pass
        except PermissionError:
            blocked.append(path)
        except OSError:
            continue  # missing is the source check's business, not privacy's
    with contextlib.suppress(OSError):
        mac_privacy_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            mac_privacy_path(cfg),
            {
                "checked_at": iso(utc_now()),
                "program": daemon_program(config_path),
                "blocked": [str(path) for path in blocked],
            },
        )
    return blocked


def mac_privacy_advice(blocked: list[Path], config_path: Path) -> str:
    cfg = load_config(config_path)
    sources = mac_protected_sources(cfg)
    folders = ", ".join(f"{sources.get(str(path), path.name)} ({path})" for path in blocked)
    yourself = "Or skip it and run `varyz sync` yourself when you want them updated."
    if mac_app.ready(autosync_dir(cfg)) is not None:
        return (
            f"macOS has not let Varyz read {folders}, so background syncing skips them. "
            "If macOS asked, choose Allow; otherwise switch them on under Varyz in "
            f"System Settings > Privacy & Security > Files and Folders. {yourself}"
        )
    program = daemon_program(config_path)
    return (
        f"macOS keeps background programs out of {folders}, so background syncing skips "
        f"them. It has no folder-only switch for {program}; the one it offers is Full Disk "
        "Access (System Settings > Privacy & Security > Full Disk Access), which opens more "
        f"than these folders. {yourself}"
    )


def _mac_privacy_check(cfg: Config, config_path: Path) -> DoctorCheck:
    protected = mac_protected_paths(cfg)
    if not protected:
        return DoctorCheck("macOS privacy", True, "no archive or source folder is protected")
    record = _read_json(mac_privacy_path(cfg))
    blocked = [Path(value) for value in record.get("blocked", [])]
    if blocked:
        return DoctorCheck("macOS privacy", False, mac_privacy_advice(blocked, config_path))
    names = ", ".join(str(path) for path in protected)
    if record:
        return DoctorCheck(
            "macOS privacy",
            True,
            f"the background agent read {names} at {record.get('checked_at', '?')}",
        )
    if mac_app.ready(autosync_dir(cfg)) is not None:
        ask = "macOS asks once whether Varyz may read them"
    else:
        ask = f"the background agent needs Full Disk Access for {daemon_program(config_path)}"
    return DoctorCheck(
        "macOS privacy",
        True,
        f"{names} are protected: {ask}; its first run checks and reports here",
    )


def _option_path(cfg: Config, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (cfg.base_dir / path).resolve()


def process_alive(pid: int) -> bool:
    """Is this pid a live process? Unknown answers count as alive.

    Never use os.kill for this on Windows: any signal other than a console
    control event is implemented as TerminateProcess, so the liveness probe
    would kill what it is asking about.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # Declared explicitly: the default int return truncates a 64-bit handle.
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        query_limited_information = 0x1000
        still_active = 259
        invalid_parameter = 87
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER means no such process; anything else
            # (notably ERROR_ACCESS_DENIED) means it exists but is not ours.
            return kernel32.GetLastError() != invalid_parameter
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _claim_pid(target: Path) -> int | None:
    with contextlib.suppress(OSError, ValueError):
        match = re.search(r"pid=(\d+)", target.read_text(encoding="utf-8"))
        if match:
            return int(match.group(1))
    return None


def _claim(cfg: Config) -> bool:
    target = claim_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        with contextlib.suppress(OSError):
            # A claim outlives its owner whenever the daemon is killed rather
            # than stopped. Without the liveness probe the dead pid blocked
            # every later run until the claim aged out four hours later.
            pid = _claim_pid(target)
            modified = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)
            stale = utc_now() - modified > CLAIM_MAX_AGE
            if stale or (pid is not None and not process_alive(pid)):
                target.unlink()
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, f"pid={os.getpid()} started={iso(utc_now())}\n".encode())
    finally:
        os.close(descriptor)
    return True


def _release_claim(cfg: Config) -> None:
    claim_path(cfg).unlink(missing_ok=True)


def _persist(cfg: Config, result: AutoSyncRun) -> None:
    directory = autosync_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    payload = result.as_dict()
    _atomic_json(state_path(cfg), payload)
    with log_path(cfg).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    report = report_path(cfg)
    report.parent.mkdir(parents=True, exist_ok=True)
    warning_count = len(result.warnings)
    warning_summary = f"{warning_count} warning{'s' if warning_count != 1 else ''}"
    lines = [
        "# Varyz autosync - latest run",
        "",
        f"> **STATUS {result.finished_at[:10]}:** {result.status}; trigger `{result.trigger}`; "
        f"{result.new} new, {result.updated} updated, {result.excluded} excluded, "
        f"{result.deleted} removed, "
        f"{result.errors} errors, {warning_summary}, "
        f"{result.embedded} chunks embedded; "
        f"graph {result.graph_status} ({result.graph_entries} entries, "
        f"{result.graph_reused} reused, {result.graph_claims} claims, "
        f"{result.graph_pruned} rows pruned).",
        "",
        "| Source | Status | New | Updated | Unchanged | Excluded | Removed | Errors | Detail |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for value in result.connectors:
        lines.append(
            f"| {value.connector} | {value.status} | {value.new} | {value.updated} | "
            f"{value.unchanged} | {value.excluded} | {value.deleted} | "
            f"{value.errors} | {value.detail} |"
        )
    if result.warnings:
        lines.extend(("", "## Warnings", ""))
        lines.extend(f"- {warning}" for warning in result.warnings)
    if result.detail:
        lines.extend(("", f"> Note: {result.detail}"))
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _notify_failure(cfg: Config, result: AutoSyncRun) -> None:
    topic = cfg.autosync.notify_topic.strip()
    if not topic:
        return
    url = f"{cfg.autosync.notify_server.rstrip('/')}/{quote(topic, safe='')}"
    body = (
        f"Varyz autosync {result.status}: {result.errors} source errors; "
        f"{result.new} new, {result.embedded} embedded. See {report_path(cfg)}"
    )
    with contextlib.suppress(Exception):
        httpx.post(
            url,
            content=body.encode("utf-8"),
            headers={"Title": "Varyz autosync needs attention", "Priority": "4"},
            timeout=10,
        )


def _simple_run(trigger: str, status: str, started: datetime, detail: str) -> AutoSyncRun:
    return AutoSyncRun(
        trigger=trigger,
        status=status,
        started_at=iso(started) or "",
        finished_at=iso(utc_now()) or "",
        detail=detail,
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {_safe_text(str(exc))}"[:500]


def _safe_text(value: str) -> str:
    # Connector errors are durable logs; bearer URLs and URI credentials never
    # belong there even if a future connector forgets to sanitize its exception.
    value = re.sub(r"(?i)https?://[^\s]+", "[redacted-url]", value)
    value = re.sub(r"(?i)(token|password|secret)=\S+", r"\1=[redacted]", value)
    return value.replace("\r", " ").replace("\n", " ").strip()


def _cmd_quote(value: str) -> str:
    return '"' + value.replace("%", "%%") + '"'


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

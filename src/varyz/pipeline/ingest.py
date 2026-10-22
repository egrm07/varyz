"""Idempotent ingest: discover → vault → normalize → upsert → enqueue enrichment.

One transaction per item, cursor persisted inside it — a crash mid-sync never
duplicates or loses items."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from varyz.config import Config
from varyz.connectors.base import Connector, SkipItem, missing_paths
from varyz.db import dao
from varyz.deletion import forget_entry
from varyz.domain import IngestStats, Kind, RawItem, iso, utc_now
from varyz.events import emit_event
from varyz.jobs.repository import enqueue_in_transaction
from varyz.pipeline import significance
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.privacy.labels import label_resource
from varyz.privacy.models import LabelSource
from varyz.vault import Vault

log = logging.getLogger(__name__)

ENRICHMENT_TASKS = ("embed",)  # Phase 2 adds: classify, extract_entities, extract_todos


def ingest_item(
    conn: sqlite3.Connection,
    vault: Vault,
    connector: Connector,
    gate: SensitivityGate,
    item: RawItem,
    *,
    source_account: str = "",
) -> tuple[str, str]:
    """Ingest one item inside a transaction. Returns (entry_id, status)."""
    if dao.is_entry_excluded(
        conn,
        connector.connector_id,
        source_account,
        item.external_id,
    ):
        return "", "excluded"
    vault_ref = vault.put(item.payload_bytes(), item.payload_ext)
    norm = connector.normalize(item)
    sensitivity = gate.classify(connector.connector_id, norm)
    with conn:
        entry_id, status = dao.upsert_entry(
            conn,
            norm,
            source=connector.connector_id,
            source_account=source_account,
            vault_ref=vault_ref,
            sensitivity=sensitivity,
            significance=(
                norm.significance
                if norm.significance is not None
                else significance.score(norm)
            ),
        )
        if status in ("new", "updated"):
            for task in ENRICHMENT_TASKS:
                dao.enqueue_job(conn, entry_id, task)
            if norm.kind == Kind.MESSAGE:
                dao.enqueue_job(conn, entry_id, "significance")
            stored = dao.get_entry(conn, entry_id)
            if stored is None:
                raise RuntimeError("entry disappeared during ingest")
            if _graph_is_built():
                enqueue_in_transaction(
                    conn,
                    "entry",
                    entry_id,
                    "graph_extract",
                    "deterministic:1",
                    stored.content_hash,
                )
            label_resource(
                conn,
                "entry",
                entry_id,
                stored.sensitivity,
                LabelSource.CONNECTOR_DEFAULT,
                [f"source:{connector.connector_id}", "ingest_sensitivity_gate"],
            )
            if status == "updated":
                conn.execute(
                    """UPDATE claims SET lifecycle_state='withdrawn', updated_at=?
                       WHERE origin<>'user' AND id IN (
                         SELECT claim_id FROM claim_evidence WHERE entry_id=?
                       )""",
                    (iso(utc_now()), entry_id),
                )
                conn.execute("UPDATE summaries SET dirty=1")
            emit_event(
                conn,
                "entry_created" if status == "new" else "entry_updated",
                "entry",
                entry_id,
                {"content_hash": stored.content_hash, "source": stored.source},
            )
    return entry_id, status


def _graph_is_built() -> bool:
    """Whether a graph job could ever run here.

    The alpha holds the builder back, and before this every new entry still
    queued one: 58,937 pending jobs on the owner's archive, none ever run.
    """
    from varyz.graph import builder_is_shipped

    return builder_is_shipped()


def prune_vanished(
    conn: sqlite3.Connection,
    connector: Connector,
    account: str,
    seen: set[str],
) -> int:
    """Drop entries a re-enumerating source no longer offers. Returns the count.

    Without this the index only ever grows. A note deleted because its decision
    was reversed stays answerable forever, and `recall` quotes it as current
    evidence with a `reference` pointing at a file that is not there — the one
    failure an extractive engine cannot let the caller catch, since the caller
    checks the citation and the citation is what is wrong. A renamed file is
    worse than it sounds too: the path is the identity, so the old entry lingers
    beside the new one and the same text comes back twice, spending two of the
    eight capsules an agent asked for on one note.

    Only called when discovery ran to exhaustion, because a scan that stopped
    early has not seen the rest of the corpus and would read it as deleted.

    A scan that found nothing at all is refused. Emptiness here is far more
    often a source that went away — an unmounted drive, a folder moved, a path
    that resolves somewhere different on this machine — than a user who deleted
    every last note, and those two look identical from in here. The cost of
    guessing wrong is the whole corpus re-embedded; the cost of the guard is
    that genuinely emptying a folder leaves entries behind until something else
    is added to it.
    """
    connector_id = connector.connector_id
    if not seen:
        log.warning(
            "%s enumerated nothing; keeping existing entries rather than "
            "treating an empty scan as a deletion",
            connector_id,
        )
        return 0
    # The source gets a second say on anything this run did not yield. The walk
    # skips a file it cannot read, and on Windows "cannot read" routinely means
    # "open in Word right now" — which would otherwise delete the entry and
    # re-embed it the moment the document is closed.
    still_present = getattr(connector, "still_present", None)

    def vanished(external_id: str) -> bool:
        if external_id in seen:
            return False
        if still_present is None:
            return True
        try:
            return not still_present(external_id)
        except Exception:
            # A source that cannot answer does not get to authorise a deletion.
            log.exception("%s could not confirm %s is gone", connector_id, external_id)
            return False

    stale = [
        entry_id
        for entry_id, external_id in dao.external_ids_for_source(conn, connector_id, account)
        if vanished(external_id)
    ]
    if not stale:
        return 0
    with conn:
        for entry_id in stale:
            dao.delete_entry(conn, entry_id)
    log.info("%s: dropped %d entries no longer present at the source", connector_id, len(stale))
    return len(stale)


def sync_connector(
    conn: sqlite3.Connection,
    vault: Vault,
    connector: Connector,
    cfg: Config,
    *,
    on_progress: Callable[[IngestStats], Any] | None = None,
) -> IngestStats:
    gate = SensitivityGate(cfg.sensitivity)
    options = {
        **cfg.connector_options(connector.connector_id),
        "_data_dir": str(cfg.data_dir),  # for connectors that keep session state
        "_base_dir": str(cfg.base_dir),
    }
    account = str(options.get("account", ""))
    cursor = dao.get_cursor(conn, connector.connector_id, account)
    stats = IngestStats(connector=connector.connector_id)
    # `paths` is spelled the same way by every connector that walks folders, so
    # this reads them without asking the connector anything. It is taken before
    # discovery: a path can disappear mid-walk, and one that did was still there
    # to be configured for.
    stats.missing_paths = missing_paths(options)
    last_cursor = cursor

    # These three only matter for connectors that re-enumerate everything they
    # hold on every run; see `prune_vanished`. `exhausted` is tracked rather
    # than inferred from the error count, because the two say different things:
    # one file that failed to ingest is no reason to stop pruning, while a
    # discovery that stopped early is — and only one of them is an error.
    enumerates = bool(getattr(connector, "enumerates_corpus", False))
    seen: set[str] = set()
    exhausted = False
    reported = 0

    # The generator is stepped by hand so a failure *inside* discover is
    # survivable. A plain `for` covers only the body: a file that vanishes
    # mid-walk, a mailbox that drops its connection, a repository that turns
    # unreadable halfway through would end the whole sync and skip the cursor
    # write below, so the next run began again from the last saved position and
    # redid everything already ingested.
    items = connector.discover(options, cursor)
    while True:
        try:
            item, new_cursor = next(items)
        except StopIteration:
            exhausted = True
            break
        except ValueError:
            # How a connector reports a configuration it cannot work with —
            # no approval, no paths, no repositories under them. That is the
            # user's to fix and it must reach them, not be counted as one
            # error among many.
            raise
        except Exception:
            log.exception("%s discovery failed part-way through", connector.connector_id)
            stats.errors += 1
            break
        # Recorded before the ingest, not after: an item that failed to ingest
        # was still offered by the source, and pruning it would delete the
        # previous good copy because this run happened to stumble on it.
        if enumerates:
            seen.add(item.external_id)
        try:
            _, status = ingest_item(
                conn, vault, connector, gate, item, source_account=account
            )
            setattr(stats, status, getattr(stats, status) + 1)
            if new_cursor is not None:
                with conn:
                    dao.set_cursor(conn, connector.connector_id, new_cursor, account)
                last_cursor = new_cursor
        except SkipItem:
            # Withdrawn, not failed — so it leaves `seen` and the prune below
            # treats it as the source no longer offering it, which removes the
            # entry an earlier run made before this check existed.
            seen.discard(item.external_id)
        except Exception:
            log.exception("failed to ingest %s item %s", connector.connector_id, item.external_id)
            stats.errors += 1
        # Only when the count has reached a new hundred. A skipped item leaves
        # the count where it was, so testing the count alone announced "0
        # items" before anything was read and repeated the last hundred once
        # per skip after it.
        if on_progress and stats.total and stats.total % 100 == 0 and stats.total != reported:
            reported = stats.total
            on_progress(stats)

    if enumerates and exhausted:
        stats.deleted = prune_vanished(conn, connector, account, seen)

    # Entries the connector now judges it should never have made - turns filed
    # as the owner's words under rules that have since been tightened. Only
    # after a complete discovery, since the judgement may read the source.
    withdrawn = getattr(connector, "withdrawn", None)
    if exhausted and stats.errors == 0 and withdrawn is not None:
        for entry_id in withdrawn(conn):
            if forget_entry(conn, vault, entry_id):
                stats.deleted += 1

    with conn:
        final_cursor = (
            getattr(connector, "checkpoint", last_cursor)
            if stats.errors == 0
            else last_cursor
        )
        dao.set_cursor(
            conn,
            connector.connector_id,
            final_cursor,
            account,
            status="ok" if stats.errors == 0 else f"{stats.errors} errors",
        )
    return stats

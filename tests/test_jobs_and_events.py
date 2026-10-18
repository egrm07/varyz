"""Durable job leases and transactional invalidation events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from varyz.connectors.manual import ManualConnector
from varyz.events import pending_events
from varyz.jobs.repository import JobRepository
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate


def test_job_enqueue_is_idempotent_for_same_input(conn):
    repo = JobRepository(conn)
    first = repo.enqueue("entry", "e1", "graph_extract", "v1", "hash")
    second = repo.enqueue("entry", "e1", "graph_extract", "v1", "hash")
    assert first.id == second.id
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_expired_lease_is_reclaimed_and_attempts_are_bounded(conn):
    repo = JobRepository(conn)
    job = repo.enqueue(
        "entry", "e1", "graph_extract", "v1", "hash", max_attempts=2
    )
    now = datetime.now(UTC)
    first = repo.claim("one", now=now)[0]
    assert first.id == job.id and first.attempts == 1
    # Before expiry another worker cannot steal it.
    assert repo.claim("two", now=now + timedelta(seconds=30)) == []
    reclaimed = repo.claim("two", now=now + timedelta(minutes=3))[0]
    failed = repo.fail(reclaimed.id, "two", "fixture", "boom")
    assert failed.state == "failed"
    assert repo.claim("three", now=now + timedelta(minutes=4)) == []


def test_ingest_writes_event_label_and_graph_job_in_one_transaction(conn, vault, cfg):
    entry_id, status = ingest_item(
        conn,
        vault,
        ManualConnector(),
        SensitivityGate(cfg.sensitivity),
        ManualConnector.make_item("I moved to Berlin."),
    )
    assert status == "new"
    event = next(e for e in pending_events(conn) if e["aggregate_id"] == entry_id)
    assert event["event_type"] == "entry_created"
    assert conn.execute(
        """SELECT COUNT(*) FROM jobs
           WHERE resource_id=? AND task='graph_extract' AND state='pending'""",
        (entry_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        """SELECT label FROM policy_labels
           WHERE resource_type='entry' AND resource_id=? AND active=1""",
        (entry_id,),
    ).fetchone()[0] == "personal"

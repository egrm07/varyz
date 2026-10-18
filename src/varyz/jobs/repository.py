"""Transactional leases, retries, checkpoints, and idempotent completion."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from varyz.domain import iso, new_id, utc_now
from varyz.jobs.models import Job, JobState, job_key


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        job_key=row["job_key"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        task=row["task"],
        version=row["version"],
        state=row["state"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        checkpoint=json.loads(row["checkpoint_json"] or "{}"),
        error_category=row["error_category"],
        error_detail=row["error_detail"],
        next_retry_at=row["next_retry_at"],
        input_content_hash=row["input_content_hash"],
        output_generation_id=row["output_generation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def enqueue_in_transaction(
    conn: sqlite3.Connection,
    resource_type: str,
    resource_id: str,
    task: str,
    version: str,
    input_content_hash: str,
    *,
    max_attempts: int = 5,
) -> str:
    """Queue a job without committing the caller's surrounding transaction."""
    key = job_key(resource_type, resource_id, task, version)
    existing = conn.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    now = iso(utc_now())
    if existing:
        if (
            existing["state"] == JobState.DONE
            and existing["input_content_hash"] == input_content_hash
        ):
            return existing["id"]
        if existing["input_content_hash"] != input_content_hash:
            conn.execute(
                """UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                   attempts=0, checkpoint_json='{}', error_category=NULL,
                   error_detail=NULL, next_retry_at=NULL, input_content_hash=?,
                   output_generation_id=NULL, updated_at=?, completed_at=NULL
                   WHERE id=?""",
                (input_content_hash, now, existing["id"]),
            )
        return existing["id"]
    job_id = new_id()
    conn.execute(
        """INSERT INTO jobs
           (id, job_key, resource_type, resource_id, task, version, state,
            attempts, max_attempts, checkpoint_json, input_content_hash,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,'pending',0,?,'{}',?,?,?)""",
        (
            job_id,
            key,
            resource_type,
            resource_id,
            task,
            version,
            max_attempts,
            input_content_hash,
            now,
            now,
        ),
    )
    return job_id


class JobRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def enqueue(
        self,
        resource_type: str,
        resource_id: str,
        task: str,
        version: str,
        input_content_hash: str,
        *,
        max_attempts: int = 5,
    ) -> Job:
        key = job_key(resource_type, resource_id, task, version)
        now = iso(utc_now())
        with self.conn:
            existing = self.conn.execute(
                "SELECT * FROM jobs WHERE job_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["state"] == JobState.DONE
                    and existing["input_content_hash"] == input_content_hash
                ):
                    return _row_to_job(existing)
                if existing["input_content_hash"] != input_content_hash:
                    self.conn.execute(
                        """UPDATE jobs
                           SET state='pending', lease_owner=NULL, lease_expires_at=NULL,
                               attempts=0, checkpoint_json='{}', error_category=NULL,
                               error_detail=NULL, next_retry_at=NULL,
                               input_content_hash=?, output_generation_id=NULL,
                               updated_at=?, completed_at=NULL
                           WHERE id=?""",
                        (input_content_hash, now, existing["id"]),
                    )
                return _row_to_job(
                    self.conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (existing["id"],)
                    ).fetchone()
                )
            job_id = new_id()
            self.conn.execute(
                """INSERT INTO jobs
                   (id, job_key, resource_type, resource_id, task, version, state,
                    attempts, max_attempts, checkpoint_json, input_content_hash,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,'pending',0,?,'{}',?,?,?)""",
                (
                    job_id,
                    key,
                    resource_type,
                    resource_id,
                    task,
                    version,
                    max_attempts,
                    input_content_hash,
                    now,
                    now,
                ),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _row_to_job(row)

    def claim(
        self,
        owner: str,
        *,
        tasks: tuple[str, ...] = (),
        limit: int = 25,
        lease_seconds: int = 120,
        now: datetime | None = None,
    ) -> list[Job]:
        moment = now or utc_now()
        now_s = iso(moment)
        expiry = iso(moment + timedelta(seconds=lease_seconds))
        task_where = ""
        params: list = [now_s, now_s]
        if tasks:
            task_where = f"AND task IN ({','.join('?' for _ in tasks)})"
            params.extend(tasks)
        params.append(limit)
        with self.conn:
            # An expired lease is claimable directly; no separate recovery race.
            rows = self.conn.execute(
                f"""SELECT id FROM jobs
                    WHERE attempts < max_attempts
                      AND (
                        (state='pending' AND (next_retry_at IS NULL OR next_retry_at <= ?))
                        OR (state='leased' AND lease_expires_at <= ?)
                      )
                      {task_where}
                    ORDER BY created_at, id LIMIT ?""",
                params,
            ).fetchall()
            ids = [row["id"] for row in rows]
            for job_id in ids:
                self.conn.execute(
                    """UPDATE jobs SET state='leased', lease_owner=?,
                       lease_expires_at=?, attempts=attempts+1, updated_at=?
                       WHERE id=?""",
                    (owner, expiry, now_s, job_id),
                )
        return [self.get(job_id) for job_id in ids]

    def checkpoint(self, job_id: str, owner: str, value: dict) -> None:
        with self.conn:
            cur = self.conn.execute(
                """UPDATE jobs SET checkpoint_json=?, updated_at=?
                   WHERE id=? AND state='leased' AND lease_owner=?""",
                (json.dumps(value, sort_keys=True), iso(utc_now()), job_id, owner),
            )
        if cur.rowcount != 1:
            raise RuntimeError("job lease is not owned by this worker")

    def complete(self, job_id: str, owner: str, output_generation_id: str | None = None) -> None:
        now = iso(utc_now())
        with self.conn:
            cur = self.conn.execute(
                """UPDATE jobs SET state='done', lease_owner=NULL, lease_expires_at=NULL,
                       output_generation_id=?, updated_at=?, completed_at=?
                   WHERE id=? AND state='leased' AND lease_owner=?""",
                (output_generation_id, now, now, job_id, owner),
            )
        if cur.rowcount != 1:
            raise RuntimeError("job lease is not owned by this worker")

    def fail(
        self,
        job_id: str,
        owner: str,
        category: str,
        detail: str,
        *,
        retry_after_seconds: int = 0,
    ) -> Job:
        row = self.conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        terminal = row["attempts"] >= row["max_attempts"]
        state = JobState.FAILED if terminal else JobState.PENDING
        next_retry = iso(utc_now() + timedelta(seconds=retry_after_seconds))
        with self.conn:
            cur = self.conn.execute(
                """UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL,
                       error_category=?, error_detail=?, next_retry_at=?,
                       updated_at=?, completed_at=CASE WHEN ?='failed' THEN ? ELSE NULL END
                   WHERE id=? AND state='leased' AND lease_owner=?""",
                (
                    state,
                    category,
                    detail,
                    next_retry,
                    iso(utc_now()),
                    state,
                    iso(utc_now()),
                    job_id,
                    owner,
                ),
            )
        if cur.rowcount != 1:
            raise RuntimeError("job lease is not owned by this worker")
        return self.get(job_id)

    def supersede(self, job_id: str) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE jobs SET state='superseded', lease_owner=NULL,
                   lease_expires_at=NULL, updated_at=?, completed_at=?
                   WHERE id=? AND state NOT IN ('done','superseded')""",
                (iso(utc_now()), iso(utc_now()), job_id),
            )

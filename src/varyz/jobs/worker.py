"""Small bounded worker loop over registered task handlers."""

from __future__ import annotations

from collections.abc import Callable

from varyz.jobs.models import Job
from varyz.jobs.repository import JobRepository

JobHandler = Callable[[Job, Callable[[dict], None]], str | None]


class Worker:
    def __init__(
        self,
        repository: JobRepository,
        owner: str,
        handlers: dict[str, JobHandler],
    ):
        self.repository = repository
        self.owner = owner
        self.handlers = handlers

    def run_once(self, *, limit: int = 25) -> tuple[int, int]:
        jobs = self.repository.claim(
            self.owner, tasks=tuple(sorted(self.handlers)), limit=limit
        )
        done = 0
        failed = 0
        for job in jobs:
            try:
                output = self.handlers[job.task](
                    job,
                    lambda value, job_id=job.id: self.repository.checkpoint(
                        job_id, self.owner, value
                    ),
                )
                self.repository.complete(job.id, self.owner, output)
                done += 1
            except Exception as exc:
                self.repository.fail(
                    job.id,
                    self.owner,
                    type(exc).__name__,
                    str(exc)[:2000],
                )
                failed += 1
        return done, failed

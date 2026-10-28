"""Persistent job queue, bounded dispatch, workers, retries, and shutdown."""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from smartfs import db
from smartfs.analysis import AnalysisService
from smartfs.config import Settings
from smartfs.embeddings import EmbeddingService
from smartfs.models import FileStatus, JobRecord, JobState, JobType
from smartfs.scanner import (
    PipelineCancelled,
    index_file,
    is_partial_name,
    normalize_root,
    wait_until_stable,
)

LOGGER = logging.getLogger("smartfs.jobs")
_SENTINEL = object()


class RetryableJobError(RuntimeError):
    """A transient failure that should be retried with backoff."""


class PermanentJobError(RuntimeError):
    """A failure that retrying cannot fix."""


class JobQueue:
    """Thread-safe convenience API that opens one connection per operation."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def enqueue(
        self,
        path: str,
        job_type: JobType | str,
        *,
        file_id: int | None = None,
        max_attempts: int = 3,
    ) -> int:
        """Add a job, coalescing active work for the same path and type."""
        normalized = normalize_root(path)
        connection = db.connect(self.db_path)
        try:
            return db.enqueue_job(
                connection,
                normalized,
                job_type,
                file_id=file_id,
                max_attempts=max_attempts,
            )
        finally:
            connection.close()

    def cancel(self, job_id: int) -> bool:
        """Cancel a job or request cancellation of its running worker."""
        connection = db.connect(self.db_path)
        try:
            return db.request_job_cancel(connection, job_id)
        finally:
            connection.close()

    def stats(self) -> dict[str, int]:
        """Return queue counts by state."""
        connection = db.connect(self.db_path)
        try:
            return db.job_stats(connection)
        finally:
            connection.close()


class PipelineProcessor:
    """Execute one claimed job using the incremental indexing pipeline."""

    def __init__(
        self,
        settings: Settings,
        *,
        analysis_service: AnalysisService | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.settings = settings
        self.analysis_service = analysis_service
        self.embedding_service = embedding_service

    def process(
        self,
        job: JobRecord,
        connection: sqlite3.Connection,
        cancel_check: Callable[[], bool],
    ) -> str:
        """Process a job and return a short result label."""
        if cancel_check():
            raise PipelineCancelled("job cancelled")
        if job.type == JobType.DELETE:
            return self._delete(job.path, connection, cancel_check)
        if job.type == JobType.INDEX:
            return self._index(job, connection, cancel_check)
        raise PermanentJobError(f"unsupported job type: {job.type}")

    def _index(
        self,
        job: JobRecord,
        connection: sqlite3.Connection,
        cancel_check: Callable[[], bool],
    ) -> str:
        path = job.path
        if not os.path.lexists(path):
            return self._delete(path, connection, cancel_check)
        if os.path.isdir(path):
            return "skipped-directory"
        if is_partial_name(os.path.basename(path)):
            return "skipped-partial"
        stability = wait_until_stable(
            path,
            self.settings.stability_seconds,
            timeout_seconds=self.settings.stability_timeout_seconds,
            cancel_check=cancel_check,
        )
        if not stability.ready:
            raise RetryableJobError(stability.reason or "file did not stabilize")
        if cancel_check():
            raise PipelineCancelled("job cancelled")
        stats = index_file(
            connection,
            path,
            self.settings,
            cancel_check=cancel_check,
            analysis_service=self.analysis_service,
            embedding_service=self.embedding_service,
        )
        record = db.get_file_by_path(connection, path)
        if record is not None and record.id is not None and job.file_id is None and int(job.id) > 0:
            db.set_job_file_id(connection, int(job.id), int(record.id))
        return (
            f"indexed added={stats.added} updated={stats.updated} "
            f"errors={stats.errors} unsupported={stats.unsupported}"
        )

    def _delete(
        self,
        path: str,
        connection: sqlite3.Connection,
        cancel_check: Callable[[], bool],
    ) -> str:
        if cancel_check():
            raise PipelineCancelled("job cancelled")
        if os.path.lexists(path):
            if os.path.isdir(path):
                return "reappeared-directory"
            synthetic = JobRecord(
                id=0,
                path=path,
                type=JobType.INDEX,
                state=JobState.RUNNING,
                next_run_at=db.utc_now(),
                created_at=db.utc_now(),
                updated_at=db.utc_now(),
            )
            return self._index(synthetic, connection, cancel_check)
        rows = db.list_files_under(connection, path)
        missing_ids = [
            int(record.id)
            for record in rows
            if record.id is not None
            and record.status != FileStatus.MISSING
            and not os.path.lexists(record.path)
        ]
        if missing_ids:
            with db.transaction(connection):
                db.mark_missing(connection, missing_ids)
        return f"marked-missing={len(missing_ids)}"


class WorkerPool:
    """Bounded in-memory dispatcher backed by the persistent SQLite queue."""

    def __init__(
        self,
        db_path: str | Path,
        settings: Settings,
        *,
        worker_count: int | None = None,
        queue_max: int | None = None,
        processor: PipelineProcessor | None = None,
        analysis_service: AnalysisService | None = None,
        embedding_service: EmbeddingService | None = None,
        poll_interval: float = 0.25,
        retry_base_seconds: float = 2.0,
        retry_jitter_seconds: float = 1.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.settings = settings
        self.worker_count = worker_count or settings.worker_count
        self.queue_max = queue_max or settings.job_queue_max
        if self.worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if self.queue_max < 1:
            raise ValueError("queue_max must be at least 1")
        self.processor = processor or PipelineProcessor(
            settings,
            analysis_service=analysis_service,
            embedding_service=embedding_service,
        )
        self.poll_interval = poll_interval
        self.retry_base_seconds = retry_base_seconds
        self.retry_jitter_seconds = retry_jitter_seconds
        self._queue: queue.Queue[JobRecord | object] = queue.Queue(maxsize=self.queue_max)
        self._stop_event = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._dispatcher: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._started and not self._stop_event.is_set()

    @property
    def queued_in_memory(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        """Recover interrupted jobs and launch dispatcher and worker threads."""
        with self._lock:
            if self._started:
                raise RuntimeError("WorkerPool is already started")
            connection = db.connect(self.db_path)
            try:
                recovered = db.reset_running_jobs(connection)
            finally:
                connection.close()
            if recovered:
                LOGGER.info(
                    "Recovered interrupted jobs",
                    extra={"event": "jobs.recovered", "count": recovered},
                )
            self._stop_event.clear()
            self._started = True
            self._threads = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"smartfs-worker-{index}",
                    daemon=True,
                )
                for index in range(self.worker_count)
            ]
            for thread in self._threads:
                thread.start()
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="smartfs-dispatcher",
                daemon=True,
            )
            self._dispatcher.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Stop claiming work, settle active jobs, and requeue in-memory work."""
        with self._lock:
            if not self._started:
                return
            self._stop_event.set()
            dispatcher = self._dispatcher
            self._dispatcher = None
        if dispatcher is not None:
            dispatcher.join(timeout=timeout)

        connection = db.connect(self.db_path)
        try:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    if item is not _SENTINEL:
                        db.requeue_job(connection, int(item.id))
                    self._queue.task_done()
            for _ in self._threads:
                try:
                    self._queue.put(_SENTINEL, timeout=timeout)
                except queue.Full:
                    break
        finally:
            connection.close()

        for thread in self._threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                LOGGER.warning(
                    "Worker did not stop before timeout",
                    extra={"event": "jobs.worker_timeout", "thread": thread.name},
                )
        with self._lock:
            self._started = False
            self._threads = []
        connection = db.connect(self.db_path)
        try:
            left = db.reset_running_jobs(connection)
        finally:
            connection.close()
        if left:
            LOGGER.info(
                "Requeued jobs after shutdown",
                extra={"event": "jobs.shutdown_requeue", "count": left},
            )

    def enqueue(
        self,
        path: str,
        job_type: JobType | str,
        *,
        file_id: int | None = None,
    ) -> int:
        """Add work to the persistent queue."""
        return JobQueue(self.db_path).enqueue(
            path,
            job_type,
            file_id=file_id,
            max_attempts=self.settings.job_max_attempts,
        )

    def _dispatch_loop(self) -> None:
        connection = db.connect(self.db_path)
        try:
            while not self._stop_event.is_set():
                if self._queue.full():
                    self._stop_event.wait(self.poll_interval)
                    continue
                try:
                    job = db.claim_job(connection)
                except sqlite3.Error as exc:
                    LOGGER.error(
                        "Job claim failed",
                        extra={"event": "jobs.claim_failed", "error": str(exc)},
                    )
                    self._stop_event.wait(self.poll_interval)
                    continue
                if job is None:
                    self._stop_event.wait(self.poll_interval)
                    continue
                try:
                    self._queue.put(job, timeout=0.5)
                except queue.Full:
                    db.requeue_job(connection, int(job.id))
                    self._stop_event.wait(self.poll_interval)
        finally:
            connection.close()

    def _worker_loop(self) -> None:
        connection = db.connect(self.db_path)
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is _SENTINEL:
                        break
                    job = item
                    try:
                        self._process_job(connection, job)
                    except Exception:
                        LOGGER.exception(
                            "Worker could not settle job",
                            extra={
                                "event": "jobs.worker_settle_failed",
                                "job_id": job.id,
                                "path": job.path,
                            },
                        )
                        try:
                            db.requeue_job(connection, int(job.id))
                        except sqlite3.Error:
                            LOGGER.exception(
                                "Could not requeue unsettled job",
                                extra={
                                    "event": "jobs.worker_requeue_failed",
                                    "job_id": job.id,
                                },
                            )
                finally:
                    self._queue.task_done()
        finally:
            connection.close()

    def _process_job(self, connection: sqlite3.Connection, job: JobRecord) -> None:
        def cancel_check() -> bool:
            if self._stop_event.is_set():
                return True
            return db.is_job_cancelled(connection, int(job.id))

        try:
            result = self.processor.process(job, connection, cancel_check)
        except PipelineCancelled as exc:
            if self._stop_event.is_set():
                db.requeue_job(connection, int(job.id))
                outcome = "requeued"
            else:
                db.complete_job(connection, int(job.id))
                outcome = "cancelled"
            LOGGER.info(
                "Job cancelled",
                extra={
                    "event": "jobs.cancelled",
                    "job_id": job.id,
                    "path": job.path,
                    "outcome": outcome,
                    "error": str(exc),
                },
            )
        except RetryableJobError as exc:
            if self._stop_event.is_set():
                db.requeue_job(connection, int(job.id))
                outcome = "requeued"
            else:
                updated = db.fail_job(
                    connection,
                    int(job.id),
                    str(exc),
                    retryable=True,
                    base_delay_seconds=self.retry_base_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                outcome = updated.state.value if updated else "missing"
            LOGGER.warning(
                "Retryable job failure",
                extra={
                    "event": "jobs.retryable_failure",
                    "job_id": job.id,
                    "path": job.path,
                    "outcome": outcome,
                    "error": str(exc),
                },
            )
        except PermanentJobError as exc:
            updated = db.fail_job(connection, int(job.id), str(exc), retryable=False)
            LOGGER.error(
                "Permanent job failure",
                extra={
                    "event": "jobs.permanent_failure",
                    "job_id": job.id,
                    "path": job.path,
                    "outcome": updated.state.value if updated else "missing",
                    "error": str(exc),
                },
            )
        except Exception as exc:
            if self._stop_event.is_set():
                db.requeue_job(connection, int(job.id))
                outcome = "requeued"
            else:
                updated = db.fail_job(
                    connection,
                    int(job.id),
                    f"{type(exc).__name__}: {exc}",
                    retryable=True,
                    base_delay_seconds=self.retry_base_seconds,
                    jitter_seconds=self.retry_jitter_seconds,
                )
                outcome = updated.state.value if updated else "missing"
            LOGGER.exception(
                "Unexpected job failure",
                extra={
                    "event": "jobs.unexpected_failure",
                    "job_id": job.id,
                    "path": job.path,
                    "outcome": outcome,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            fresh = db.get_job(connection, int(job.id))
            requeue = fresh is not None and fresh.rerun_requested
            db.complete_job(connection, int(job.id), requeue=requeue)
            LOGGER.info(
                "Job completed",
                extra={
                    "event": "jobs.completed",
                    "job_id": job.id,
                    "path": job.path,
                    "result": result,
                    "requeued": requeue,
                },
            )


def wait_for_idle(
    queue: JobQueue,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.1,
) -> bool:
    """Wait until no queued, retry-scheduled, or running jobs remain."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = queue.stats()
        if (
            stats.get(JobState.QUEUED.value, 0) == 0
            and stats.get(JobState.RUNNING.value, 0) == 0
            and stats.get(JobState.RETRY_SCHEDULED.value, 0) == 0
        ):
            return True
        time.sleep(poll_interval)
    return False

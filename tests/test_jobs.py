from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from smartfs import db
from smartfs.config import AppPaths, Settings
from smartfs.jobs import (
    JobQueue,
    RetryableJobError,
    WorkerPool,
    wait_for_idle,
)
from smartfs.models import FileStatus, JobState, JobType
from smartfs.scanner import PipelineCancelled


def test_enqueue_coalesces_and_rerun_marks_running(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    path = str(tmp_path / "note.txt")
    first = db.enqueue_job(connection, path, JobType.INDEX)
    second = db.enqueue_job(connection, path, JobType.INDEX)
    assert first == second

    job = db.claim_job(connection)
    assert job is not None
    assert job.id == first
    assert job.state == JobState.RUNNING
    assert job.attempts == 1

    assert db.enqueue_job(connection, path, JobType.INDEX) == first
    running = db.get_job(connection, first)
    assert running is not None
    assert running.rerun_requested is True
    assert db.complete_job(connection, first, requeue=True) is True

    requeued = db.claim_job(connection)
    assert requeued is not None
    assert requeued.id == first
    assert requeued.attempts == 1
    assert db.complete_job(connection, first) is True
    done = db.get_job(connection, first)
    assert done is not None
    assert done.state == JobState.DONE

    third = db.enqueue_job(connection, path, JobType.INDEX)
    assert third != first


def test_retry_backoff_and_terminal_failure(tmp_path: Path, connection: sqlite3.Connection) -> None:
    path = str(tmp_path / "slow.txt")
    job_id = db.enqueue_job(connection, path, JobType.INDEX, max_attempts=3)
    claimed = db.claim_job(connection)
    assert claimed is not None

    retry = db.fail_job(
        connection,
        job_id,
        "still growing",
        retryable=True,
        base_delay_seconds=30,
        jitter_seconds=0,
    )
    assert retry is not None
    assert retry.state == JobState.RETRY_SCHEDULED
    assert retry.last_error == "still growing"

    claimed_again = db.claim_job(connection, now=db.utc_after(120))
    assert claimed_again is not None
    assert claimed_again.attempts == 2

    failed = db.fail_job(
        connection,
        job_id,
        "corrupt",
        retryable=False,
        base_delay_seconds=1,
        jitter_seconds=0,
    )
    assert failed is not None
    assert failed.state == JobState.FAILED
    assert db.job_stats(connection)["failed"] == 1


def test_cancel_reset_and_stats(tmp_path: Path, connection: sqlite3.Connection) -> None:
    queued = db.enqueue_job(connection, str(tmp_path / "queued.txt"), "index")
    assert db.request_job_cancel(connection, queued) is True
    cancelled = db.get_job(connection, queued)
    assert cancelled is not None and cancelled.state == JobState.CANCELLED

    interrupted = db.enqueue_job(connection, str(tmp_path / "interrupted.txt"), "index")
    assert db.claim_job(connection) is not None
    assert db.reset_running_jobs(connection) == 1
    recovered = db.get_job(connection, interrupted)
    assert recovered is not None and recovered.state == JobState.QUEUED

    running = db.claim_job(connection)
    assert running is not None
    assert db.request_job_cancel(connection, int(running.id)) is True
    assert db.complete_job(connection, int(running.id)) is True
    finished = db.get_job(connection, int(running.id))
    assert finished is not None and finished.state == JobState.CANCELLED

    stats = db.job_stats(connection)
    assert stats["cancelled"] == 2
    assert stats["queued"] == 0
    assert stats["total"] == 2
    assert len(db.list_jobs(connection, state=JobState.CANCELLED)) == 2


def test_worker_indexes_and_deletes(
    tmp_path: Path,
    paths: AppPaths,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "worker.txt"
    note.write_text("Worker integration content", encoding="utf-8")
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    queue = JobQueue(paths.db_path)
    pool = WorkerPool(
        paths.db_path,
        settings,
        worker_count=1,
        queue_max=10,
        poll_interval=0.05,
    )
    pool.start()
    try:
        pool.enqueue(str(note), JobType.INDEX)
        assert wait_for_idle(queue, timeout=8, poll_interval=0.05)
        connection = db.connect(paths.db_path)
        try:
            record = db.get_file_by_path(connection, str(note))
            assert record is not None
            assert record.status == FileStatus.DONE
            assert record.text_excerpt == "Worker integration content"
            assert db.job_stats(connection)["done"] == 1
        finally:
            connection.close()

        note.unlink()
        pool.enqueue(str(note), JobType.DELETE)
        assert wait_for_idle(queue, timeout=8, poll_interval=0.05)
        connection = db.connect(paths.db_path)
        try:
            deleted = db.get_file_by_path(connection, str(note))
            assert deleted is not None
            assert deleted.status == FileStatus.MISSING
        finally:
            connection.close()
    finally:
        pool.stop(timeout=5)


class FlakyProcessor:
    def __init__(self) -> None:
        self.calls = 0

    def process(self, job, connection, cancel_check) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RetryableJobError("temporary")
        return "recovered"


def test_worker_retries_transient_failure(
    tmp_path: Path,
    paths: AppPaths,
    settings: Settings,
) -> None:
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    queue = JobQueue(paths.db_path)
    processor = FlakyProcessor()
    pool = WorkerPool(
        paths.db_path,
        settings,
        worker_count=1,
        processor=processor,
        poll_interval=0.05,
        retry_base_seconds=0.01,
        retry_jitter_seconds=0,
    )
    pool.start()
    try:
        job_id = pool.enqueue(str(tmp_path / "retry.txt"), JobType.INDEX)
        assert wait_for_idle(queue, timeout=5, poll_interval=0.05)
        connection = db.connect(paths.db_path)
        try:
            job = db.get_job(connection, job_id)
            assert job is not None
            assert job.state == JobState.DONE
            assert job.attempts == 2
        finally:
            connection.close()
        assert processor.calls == 2
    finally:
        pool.stop(timeout=5)


class BlockingProcessor:
    def __init__(self) -> None:
        self.started = threading.Event()

    def process(self, job, connection, cancel_check) -> str:
        self.started.set()
        while not cancel_check():
            time.sleep(0.01)
        raise PipelineCancelled("cancelled by test")


def test_worker_cancels_running_job(
    tmp_path: Path,
    paths: AppPaths,
    settings: Settings,
) -> None:
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    processor = BlockingProcessor()
    queue = JobQueue(paths.db_path)
    pool = WorkerPool(
        paths.db_path,
        settings,
        worker_count=1,
        processor=processor,
        poll_interval=0.05,
    )
    pool.start()
    try:
        job_id = pool.enqueue(str(tmp_path / "cancel.txt"), JobType.INDEX)
        assert processor.started.wait(5)
        assert queue.cancel(job_id)
        assert wait_for_idle(queue, timeout=5, poll_interval=0.05)
        connection = db.connect(paths.db_path)
        try:
            job = db.get_job(connection, job_id)
            assert job is not None
            assert job.state == JobState.CANCELLED
        finally:
            connection.close()
    finally:
        pool.stop(timeout=5)


def test_shutdown_requeues_running_job(
    tmp_path: Path,
    paths: AppPaths,
    settings: Settings,
) -> None:
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    processor = BlockingProcessor()
    pool = WorkerPool(
        paths.db_path,
        settings,
        worker_count=1,
        processor=processor,
        poll_interval=0.05,
    )
    pool.start()
    job_id = pool.enqueue(str(tmp_path / "shutdown.txt"), JobType.INDEX)
    assert processor.started.wait(5)
    pool.stop(timeout=5)
    connection = db.connect(paths.db_path)
    try:
        job = db.get_job(connection, job_id)
        assert job is not None
        assert job.state == JobState.QUEUED
    finally:
        connection.close()


def test_concurrent_claims_are_atomic(
    tmp_path: Path,
    paths: AppPaths,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    connection = db.connect(paths.db_path)
    try:
        for index in range(4):
            db.enqueue_job(connection, str(tmp_path / f"job-{index}.txt"), "index")
    finally:
        connection.close()

    barrier = threading.Barrier(4)

    def claim_one() -> int | None:
        local = db.connect(paths.db_path)
        try:
            barrier.wait(timeout=5)
            job = db.claim_job(local)
            return int(job.id) if job is not None else None
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        claimed = list(executor.map(lambda _: claim_one(), range(4)))
    assert None not in claimed
    assert len(set(claimed)) == 4

from __future__ import annotations

import time
from pathlib import Path

import pytest

from smartfs import db
from smartfs.config import AppPaths, Settings
from smartfs.daemon import SmartFSDaemon
from smartfs.models import FileStatus, JobState, JobType
from smartfs.suppression import SelfWriteSuppressor
from smartfs.watcher import (
    Debouncer,
    SmartFSEventHandler,
    WatcherError,
    WatcherService,
)


def test_debouncer_coalesces_and_flushes(tmp_path: Path) -> None:
    seen: list[str] = []
    debouncer = Debouncer(seen.append, 0.1)
    path = str(tmp_path / "note.txt")
    debouncer.start()
    try:
        debouncer.schedule(path)
        debouncer.schedule(path)
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == [path]
        time.sleep(0.15)
        assert seen == [path]
    finally:
        debouncer.stop(flush=False)

    pending: list[str] = []
    flushing = Debouncer(pending.append, 30)
    flushing.schedule(path)
    flushing.stop(flush=True)
    assert pending == [path]


def test_event_handler_filters_excluded_ignored_and_suppressed(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    internal = tmp_path / "internal"
    internal.mkdir()
    settings = Settings(exclusions=[".git", "*.key"])
    suppressor = SelfWriteSuppressor()
    seen: list[str] = []
    handler = SmartFSEventHandler(
        settings,
        [str(docs)],
        seen.append,
        ignored_paths=[internal],
        suppressor=suppressor,
    )
    note = docs / "note.txt"
    handler.submit(str(note))
    assert seen == [str(note)]

    handler.submit(str(docs / "secret.key"))
    handler.submit(str(docs / ".git" / "config"))
    handler.submit(str(internal / "private.txt"))
    handler.submit(str(tmp_path / "outside.txt"))
    suppressed = docs / "suppressed.txt"
    suppressor.mark(suppressed)
    handler.submit(str(suppressed))
    assert seen == [str(note)]


def test_watcher_service_dispatches_jobs(
    tmp_path: Path,
    paths: AppPaths,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "dispatch.txt"
    note.write_text("dispatch me", encoding="utf-8")
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    service = WatcherService(
        settings,
        paths.db_path,
        [str(docs)],
        ignored_paths=[paths.data_dir],
    )
    service.handle_path(str(note))
    connection = db.connect(paths.db_path)
    try:
        jobs = db.list_jobs(connection)
        assert len(jobs) == 1
        assert jobs[0].path == str(note)
        assert jobs[0].type == JobType.INDEX
    finally:
        connection.close()

    note.unlink()
    service.handle_path(str(note))
    connection = db.connect(paths.db_path)
    try:
        types = {job.type for job in db.list_jobs(connection)}
        assert types == {JobType.INDEX, JobType.DELETE}
    finally:
        connection.close()


def test_daemon_indexes_new_file_end_to_end(
    tmp_path: Path,
    paths: AppPaths,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    settings = Settings(
        watched_dirs=[str(docs)],
        debounce_seconds=0.1,
        stability_seconds=0.1,
        reconciliation_interval_seconds=10,
        self_write_suppression_seconds=2,
        worker_count=1,
    )
    settings.validate()
    daemon = SmartFSDaemon(
        paths,
        settings,
        worker_count=1,
        reconcile_interval=10,
        startup_reconcile=False,
    )
    daemon.start()
    note = docs / "live.txt"
    try:
        note.write_text("Live daemon content about networking", encoding="utf-8")
        deadline = time.monotonic() + 8
        record = None
        while time.monotonic() < deadline:
            connection = db.connect(paths.db_path)
            try:
                record = db.get_file_by_path(connection, str(note))
            finally:
                connection.close()
            if record is not None and record.status == FileStatus.DONE:
                break
            time.sleep(0.05)
        assert record is not None
        assert record.status == FileStatus.DONE
        assert record.text_excerpt and "networking" in record.text_excerpt
        assert record.analysis_provider == "local"
        assert record.analysis_model == "heuristic-v1"
        assert record.category is not None
        assert record.summary
    finally:
        daemon.stop(timeout=5)

    connection = db.connect(paths.db_path)
    try:
        jobs = db.list_jobs(connection, state=JobState.DONE)
        assert any(job.path == str(note) for job in jobs)
    finally:
        connection.close()


def test_daemon_rejects_second_instance(
    tmp_path: Path,
    paths: AppPaths,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    settings = Settings(
        watched_dirs=[str(docs)],
        reconciliation_interval_seconds=10,
        worker_count=1,
    )
    settings.validate()
    first = SmartFSDaemon(
        paths,
        settings,
        worker_count=1,
        reconcile_interval=10,
        startup_reconcile=False,
    )
    second = SmartFSDaemon(
        paths,
        settings,
        worker_count=1,
        reconcile_interval=10,
        startup_reconcile=False,
    )
    first.start()
    try:
        with pytest.raises(WatcherError):
            second.start()
    finally:
        second.stop()
        first.stop(timeout=5)

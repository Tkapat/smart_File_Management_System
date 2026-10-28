"""Foreground daemon orchestration for watcher, workers, and reconciliation."""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import threading

from smartfs import db
from smartfs.analysis import AnalysisService
from smartfs.config import AppPaths, Settings
from smartfs.embeddings import EmbeddingService
from smartfs.jobs import JobQueue, WorkerPool
from smartfs.scanner import ScanError, discover_changes
from smartfs.watcher import WatcherError, WatcherService

LOGGER = logging.getLogger("smartfs.daemon")


class SmartFSDaemon:
    """Own the worker pool, filesystem watcher, and periodic reconciliation."""

    def __init__(
        self,
        paths: AppPaths,
        settings: Settings,
        *,
        worker_count: int | None = None,
        reconcile_interval: int | None = None,
        startup_reconcile: bool = True,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.worker_count = worker_count or settings.worker_count
        self.reconcile_interval = (
            reconcile_interval
            if reconcile_interval is not None
            else settings.reconciliation_interval_seconds
        )
        if self.reconcile_interval < 10:
            raise ValueError("reconcile_interval must be at least 10 seconds")
        self.startup_reconcile = startup_reconcile
        self.stop_event = threading.Event()
        analysis_service = AnalysisService(settings) if settings.analysis_enabled else None
        embedding_service = (
            EmbeddingService(settings, paths.cache_dir / "fastembed")
            if settings.embeddings_enabled
            else None
        )
        self.analysis_service = analysis_service
        self.embedding_service = embedding_service
        self.pool = WorkerPool(
            paths.db_path,
            settings,
            worker_count=self.worker_count,
            analysis_service=analysis_service,
            embedding_service=embedding_service,
        )
        self.watcher = WatcherService(
            settings,
            paths.db_path,
            settings.watched_dirs,
            ignored_paths=(
                paths.config_dir,
                paths.data_dir,
                paths.cache_dir,
                paths.log_dir,
            ),
        )
        self.queue = JobQueue(paths.db_path)
        self._reconcile_thread: threading.Thread | None = None
        self._started = False
        self._lock_fd: int | None = None

    def start(self) -> None:
        """Initialize storage and start workers, watcher, and reconciliation."""
        if self._started:
            raise RuntimeError("Daemon is already started")
        if not self.settings.watched_dirs:
            raise WatcherError("No watched directories; use 'smartfs watch add <dir>' first.")
        self.paths.ensure()
        self._acquire_lock()
        try:
            db.init_database(
                self.paths.db_path,
                backup_dir=self.paths.backup_dir,
                backup_keep=5,
            )
            self.stop_event.clear()
            self.pool.start()
            try:
                self.watcher.start()
            except Exception:
                self.pool.stop()
                raise
            self._started = True
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop,
                name="smartfs-reconcile",
                daemon=True,
            )
            self._reconcile_thread.start()
        except Exception:
            self._release_lock()
            raise
        LOGGER.info(
            "Daemon started",
            extra={
                "event": "daemon.started",
                "pid": os.getpid(),
                "workers": self.worker_count,
                "reconcile_interval": self.reconcile_interval,
            },
        )

    def stop(self, *, timeout: float = 15.0) -> None:
        """Flush the watcher, settle workers, and stop reconciliation."""
        if not self._started:
            self._release_lock()
            return
        self._started = False
        self.stop_event.set()
        try:
            self.watcher.stop(timeout=timeout)
            thread = self._reconcile_thread
            if thread is not None:
                thread.join(timeout=timeout)
                if thread.is_alive():
                    LOGGER.warning(
                        "Reconciliation did not stop before timeout",
                        extra={"event": "daemon.reconcile_timeout"},
                    )
            self._reconcile_thread = None
        finally:
            try:
                self.pool.stop(timeout=timeout)
            finally:
                self._release_lock()
        LOGGER.info("Daemon stopped", extra={"event": "daemon.stopped"})

    def _acquire_lock(self) -> None:
        fd = os.open(
            self.paths.data_dir / "daemon.lock",
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise WatcherError(
                "Another SmartFS daemon is already running for this database."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd

    def _release_lock(self) -> None:
        fd = self._lock_fd
        if fd is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def wait(self) -> None:
        """Block until the stop event is set."""
        while not self.stop_event.wait(0.5):
            pass

    def reconcile_now(self) -> int:
        """Run one synchronous reconciliation pass and return queued jobs."""
        connection = db.connect(self.paths.db_path)
        queued = 0
        try:
            for root in self.settings.watched_dirs:
                if self.stop_event.is_set():
                    break
                if not os.path.isdir(root):
                    LOGGER.warning(
                        "Skipping missing watched directory",
                        extra={"event": "daemon.missing_root", "path": root},
                    )
                    continue
                try:
                    stats = discover_changes(
                        root,
                        connection,
                        self.settings,
                        lambda job_type, path: db.enqueue_job(
                            connection,
                            path,
                            job_type,
                            max_attempts=self.settings.job_max_attempts,
                        ),
                    )
                except (OSError, ScanError, db.DatabaseError) as exc:
                    LOGGER.error(
                        "Reconciliation root failed",
                        extra={
                            "event": "daemon.reconcile_root_failed",
                            "path": root,
                            "error": str(exc),
                        },
                    )
                    continue
                queued += stats.queued
                LOGGER.info(
                    "Reconciliation root complete",
                    extra={"event": "daemon.reconcile_root", **stats.as_dict()},
                )
            purged = db.purge_finished_jobs(connection, older_than_days=7)
            if purged:
                LOGGER.info(
                    "Purged old jobs",
                    extra={"event": "daemon.jobs_purged", "count": purged},
                )
        finally:
            connection.close()
        return queued

    def _reconcile_loop(self) -> None:
        if not self.startup_reconcile and self.stop_event.wait(self.reconcile_interval):
            return
        while not self.stop_event.is_set():
            try:
                queued = self.reconcile_now()
            except Exception:
                LOGGER.exception(
                    "Reconciliation pass failed", extra={"event": "daemon.reconcile_failed"}
                )
                queued = 0
            LOGGER.info(
                "Reconciliation pass finished",
                extra={"event": "daemon.reconcile_finished", "queued": queued},
            )
            if self.stop_event.wait(self.reconcile_interval):
                return


def install_signal_handlers(stop_event: threading.Event) -> dict[int, object]:
    """Install graceful SIGINT/SIGTERM handlers and return previous handlers."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, object] = {}

    def handler(signum: int, _frame: object) -> None:
        LOGGER.info(
            "Shutdown signal received",
            extra={"event": "daemon.signal", "signal": signum},
        )
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous


def restore_signal_handlers(previous: dict[int, object]) -> None:
    """Restore handlers captured by :func:`install_signal_handlers`."""
    for signum, handler in previous.items():
        signal.signal(signum, handler)

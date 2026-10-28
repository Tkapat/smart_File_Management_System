"""Inotify watchdog integration, debounce, and directory reconciliation."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from smartfs import db
from smartfs.config import Settings
from smartfs.jobs import JobQueue
from smartfs.models import JobType
from smartfs.scanner import (
    discover_changes,
    is_excluded,
    is_partial_name,
    normalize_root,
)
from smartfs.suppression import GLOBAL_SUPPRESSOR, SelfWriteSuppressor

LOGGER = logging.getLogger("smartfs.watcher")


class WatcherError(RuntimeError):
    """Raised when the filesystem watcher cannot start."""


class Debouncer:
    """Coalesce per-path events and invoke a callback after they settle."""

    def __init__(
        self,
        callback: Callable[[str], None],
        delay_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        self.callback = callback
        self.delay_seconds = delay_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def schedule(self, path: str) -> None:
        """Record an event for ``path`` and reset its quiet-period timer."""
        key = os.path.abspath(path)
        with self._lock:
            self._pending[key] = self.clock() + self.delay_seconds

    def start(self) -> None:
        """Start the debounce loop."""
        if self._thread is not None:
            raise RuntimeError("Debouncer is already started")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="smartfs-debouncer", daemon=True)
        self._thread.start()

    def stop(self, *, flush: bool = True, timeout: float = 10.0) -> None:
        """Stop the loop and optionally process every pending path immediately."""
        thread = self._thread
        if thread is not None:
            self._stop_event.set()
            thread.join(timeout=timeout)
            if thread.is_alive():
                LOGGER.warning(
                    "Debouncer did not stop before timeout",
                    extra={"event": "watch.debounce_timeout"},
                )
            self._thread = None
        if flush:
            for path in self._take_pending(force=True):
                self._invoke(path)

    def _take_pending(self, *, force: bool = False) -> list[str]:
        now = self.clock()
        with self._lock:
            if force:
                due = list(self._pending)
                self._pending.clear()
                return due
            due = [path for path, deadline in self._pending.items() if deadline <= now]
            for path in due:
                self._pending.pop(path, None)
        return due

    def _run(self) -> None:
        interval = min(0.1, max(0.01, self.delay_seconds / 2))
        while not self._stop_event.wait(interval):
            for path in self._take_pending():
                self._invoke(path)

    def _invoke(self, path: str) -> None:
        try:
            self.callback(path)
        except Exception:
            LOGGER.exception(
                "Debounced path handler failed",
                extra={"event": "watch.handler_failed", "path": path},
            )


class SmartFSEventHandler(FileSystemEventHandler):
    """Filter watchdog events and submit paths to the debouncer."""

    def __init__(
        self,
        settings: Settings,
        roots: Iterable[str],
        on_path: Callable[[str], None],
        *,
        ignored_paths: Iterable[str | os.PathLike[str]] = (),
        suppressor: SelfWriteSuppressor = GLOBAL_SUPPRESSOR,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.roots = tuple(normalize_root(root) for root in roots)
        self.on_path = on_path
        self.ignored_paths = tuple(os.path.abspath(os.fspath(path)) for path in ignored_paths)
        self.suppressor = suppressor

    def on_created(self, event: FileSystemEvent) -> None:
        self.submit(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self.submit(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self.submit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self.submit(event.src_path)
        destination = getattr(event, "dest_path", "")
        if destination:
            self.submit(destination)

    def submit(self, raw_path: str) -> None:
        """Filter one path and schedule it when relevant."""
        path = os.path.abspath(raw_path)
        if self._ignored(path) or self.suppressor.is_suppressed(path):
            return
        if not any(_is_within(path, root) for root in self.roots):
            return
        if self._excluded(path):
            return
        if is_partial_name(os.path.basename(path)):
            return
        self.on_path(path)

    def _ignored(self, path: str) -> bool:
        return any(_is_within(path, ignored) for ignored in self.ignored_paths)

    def _excluded(self, path: str) -> bool:
        for root in self.roots:
            if not _is_within(path, root):
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative == ".":
                return False
            if is_excluded(relative, os.path.basename(path), self.settings.exclusions):
                return True
            for component in relative.split("/")[:-1]:
                if is_excluded(component, component, self.settings.exclusions):
                    return True
            return False
        return True


class WatcherService:
    """Own the watchdog observer, debouncer, and changed-path dispatch."""

    def __init__(
        self,
        settings: Settings,
        db_path: str | Path,
        roots: Iterable[str],
        *,
        ignored_paths: Iterable[str | os.PathLike[str]] = (),
        debouncer: Debouncer | None = None,
        observer: Observer | None = None,
        suppressor: SelfWriteSuppressor = GLOBAL_SUPPRESSOR,
    ) -> None:
        self.settings = settings
        self.db_path = Path(db_path)
        self.roots = tuple(normalize_root(root) for root in roots)
        self.ignored_paths = tuple(os.path.abspath(os.fspath(path)) for path in ignored_paths)
        self.suppressor = suppressor
        self.queue = JobQueue(self.db_path)
        self.debouncer = debouncer or Debouncer(self.handle_path, settings.debounce_seconds)
        self.handler = SmartFSEventHandler(
            settings,
            self.roots,
            self.debouncer.schedule,
            ignored_paths=self.ignored_paths,
            suppressor=suppressor,
        )
        self.observer = observer or Observer()
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Schedule every existing root and start observer plus debouncer."""
        with self._lock:
            if self._started:
                raise RuntimeError("WatcherService is already started")
            missing = [root for root in self.roots if not os.path.isdir(root)]
            for root in missing:
                LOGGER.warning(
                    "Watched directory is missing",
                    extra={"event": "watch.missing_root", "path": root},
                )
            for root in self.roots:
                if os.path.isdir(root):
                    self.observer.schedule(self.handler, root, recursive=True)
            try:
                self.debouncer.start()
                self.observer.start()
            except Exception:
                self.debouncer.stop(flush=False)
                try:
                    self.observer.stop()
                    self.observer.join(timeout=1.0)
                except Exception:
                    pass
                raise
            self._started = True
        LOGGER.info(
            "Filesystem watcher started",
            extra={"event": "watch.started", "roots": list(self.roots)},
        )

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop observing new events, then flush pending debounced paths."""
        with self._lock:
            if not self._started:
                return
            self._started = False
        try:
            self.observer.stop()
            self.observer.join(timeout=timeout)
        finally:
            self.debouncer.stop(flush=True, timeout=timeout)
        LOGGER.info("Filesystem watcher stopped", extra={"event": "watch.stopped"})

    def handle_path(self, path: str) -> None:
        """Enqueue an index/delete job, or reconcile a directory immediately."""
        normalized = os.path.abspath(path)
        if any(_is_within(normalized, ignored) for ignored in self.ignored_paths):
            return
        if self.suppressor.is_suppressed(normalized):
            return
        if is_partial_name(os.path.basename(normalized)):
            return
        try:
            if os.path.isdir(normalized):
                connection = db.connect(self.db_path)
                try:
                    stats = discover_changes(
                        normalized,
                        connection,
                        self.settings,
                        lambda job_type, file_path: db.enqueue_job(
                            connection,
                            file_path,
                            job_type,
                            max_attempts=self.settings.job_max_attempts,
                        ),
                    )
                finally:
                    connection.close()
                LOGGER.info(
                    "Directory event reconciled",
                    extra={"event": "watch.directory_reconciled", **stats.as_dict()},
                )
            elif os.path.lexists(normalized):
                self.queue.enqueue(
                    normalized,
                    JobType.INDEX,
                    max_attempts=self.settings.job_max_attempts,
                )
            else:
                self.queue.enqueue(
                    normalized,
                    JobType.DELETE,
                    max_attempts=self.settings.job_max_attempts,
                )
        except (OSError, sqlite3.Error, db.DatabaseError) as exc:
            LOGGER.error(
                "Could not dispatch watched path",
                extra={
                    "event": "watch.dispatch_failed",
                    "path": normalized,
                    "error": str(exc),
                },
            )


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

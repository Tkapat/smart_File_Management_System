"""Recursive directory scanning, incremental indexing, and reconciliation."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from smartfs import EXTRACTION_VERSION, INDEX_VERSION, db
from smartfs.analysis import AnalysisService
from smartfs.config import Settings, redact_secrets
from smartfs.embeddings import EmbeddingService
from smartfs.extractor import (
    ExtractedDocument,
    ExtractionError,
    FileTooLarge,
    UnsupportedFormat,
    excerpt,
    mime_for_path,
    supported_extension,
)
from smartfs.extractor import (
    extract as extract_document,
)
from smartfs.models import ExtractedDocument as ExtractedDocumentModel
from smartfs.models import FileRecord, FileStatus
from smartfs.tags import sync_file_xattrs
from smartfs.xattr_store import (
    PROBE_PREFIX,
    SUMMARY_MAX_BYTES,
    XattrOperationError,
    XattrUnsupported,
    one_line,
    read_metadata,
    truncate_utf8,
)

LOGGER = logging.getLogger("smartfs.scanner")
HASH_CHUNK_SIZE = 1024 * 1024
PARTIAL_SUFFIXES = {
    ".part",
    ".partial",
    ".crdownload",
    ".download",
    ".swp",
    ".swx",
    ".tmp",
    ".temp",
}


class ScanError(RuntimeError):
    """Raised when a directory cannot be scanned."""


class PipelineCancelled(RuntimeError):
    """Raised when a queued indexing job is cancelled between stages."""


class UnstableFileError(RuntimeError):
    """Raised when a file does not become stable within the safety deadline."""


@dataclass(slots=True)
class StabilityResult:
    """Result of waiting for an in-progress file write to settle."""

    ready: bool
    reason: str | None = None


@dataclass(slots=True)
class DiscoveryStats:
    """Counters produced by a queue-oriented reconciliation walk."""

    root: str
    files_seen: int = 0
    queued: int = 0
    unchanged: int = 0
    unsupported: int = 0
    missing: int = 0
    errors: int = 0
    xattr_synced: int = 0
    xattr_unsupported: int = 0
    xattr_errors: int = 0

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(slots=True)
class ScanStats:
    """Per-directory scanning counters."""

    root: str
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    unsupported: int = 0
    skipped: int = 0
    errors: int = 0
    missing: int = 0
    xattr_synced: int = 0
    xattr_unsupported: int = 0
    xattr_errors: int = 0
    analyzed: int = 0
    analysis_cached: int = 0
    analysis_failures: int = 0
    embedded: int = 0
    embedding_cached: int = 0
    embedding_failures: int = 0

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


def normalize_root(root: str | os.PathLike[str]) -> str:
    """Return an absolute, expanded directory path without resolving symlinks."""
    return os.path.abspath(os.path.expanduser(os.fspath(root)))


def is_partial_name(name: str) -> bool:
    """Return True for temporary, lock, and incomplete-download filenames."""
    lowered = name.lower()
    return (
        name.startswith("~")
        or name.startswith(".~lock.")
        or name.startswith(PROBE_PREFIX)
        or name.startswith(".goutputstream-")
        or lowered.endswith(tuple(PARTIAL_SUFFIXES))
    )


def is_excluded(
    relative_path: str,
    name: str,
    patterns: Sequence[str],
) -> bool:
    """Match a file or directory against the configured exclusion patterns."""
    relative = relative_path.replace(os.sep, "/")
    for pattern in patterns:
        if not pattern:
            continue
        normalized_pattern = pattern.replace(os.sep, "/")
        if "/" in normalized_pattern:
            if fnmatch.fnmatchcase(relative, normalized_pattern):
                return True
        elif fnmatch.fnmatchcase(name, normalized_pattern):
            return True
    return False


def walk_files(
    root: str | os.PathLike[str],
    settings: Settings,
    *,
    on_error: Callable[[str, OSError], None] | None = None,
) -> Iterator[Path]:
    """Yield regular files recursively using ``os.scandir`` without following symlinks."""
    base = normalize_root(root)
    if not os.path.isdir(base):
        raise ScanError(f"Not a directory: {base}")
    stack: list[tuple[str, str]] = [(base, "")]
    while stack:
        directory, relative_dir = stack.pop()
        try:
            with os.scandir(directory) as scandir:
                entries = sorted(scandir, key=lambda item: item.name)
        except OSError as exc:
            if on_error is not None:
                on_error(directory, exc)
            else:
                LOGGER.warning(
                    "Cannot scan directory",
                    extra={"event": "scan.dir_error", "path": directory, "error": str(exc)},
                )
            continue
        child_directories: list[tuple[str, str]] = []
        for entry in entries:
            relative = (f"{relative_dir}/{entry.name}" if relative_dir else entry.name).replace(
                os.sep, "/"
            )
            try:
                is_symlink = entry.is_symlink()
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                if on_error is not None:
                    on_error(entry.path, exc)
                continue
            if is_symlink:
                continue
            if is_directory:
                if is_excluded(relative, entry.name, settings.exclusions):
                    continue
                child_directories.append((entry.path, relative))
                continue
            if is_file:
                if is_partial_name(entry.name):
                    continue
                if is_excluded(relative, entry.name, settings.exclusions):
                    continue
                yield Path(entry.path)
        stack.extend(reversed(child_directories))


def hash_file(path: str | os.PathLike[str], chunk_size: int = HASH_CHUNK_SIZE) -> str:
    """Compute a SHA-256 digest with bounded memory reads."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_readable(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def wait_until_stable(
    path: str | os.PathLike[str],
    quiet_seconds: float,
    *,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 0.25,
    cancel_check: Callable[[], bool] | None = None,
) -> StabilityResult:
    """Wait until size, mtime, and inode are unchanged for ``quiet_seconds``."""
    if quiet_seconds < 0:
        raise ValueError("quiet_seconds must not be negative")
    target = os.fspath(path)
    if is_partial_name(os.path.basename(target)):
        return StabilityResult(False, "temporary or partial filename")
    started = time.monotonic()
    last_signature: tuple[int, int, int] | None = None
    stable_since = started
    while True:
        if cancel_check is not None and cancel_check():
            raise PipelineCancelled("indexing cancelled")
        now = time.monotonic()
        if now - started > timeout_seconds:
            return StabilityResult(False, "file did not become stable in time")
        try:
            info = os.stat(target, follow_symlinks=False)
        except OSError as exc:
            return StabilityResult(False, f"cannot stat file: {exc.strerror}")
        if not stat.S_ISREG(info.st_mode):
            return StabilityResult(False, "not a regular file")
        signature = (int(info.st_size), int(info.st_mtime_ns), int(info.st_ino))
        if signature != last_signature:
            last_signature = signature
            stable_since = now
        elif now - stable_since >= quiet_seconds:
            if not _is_readable(target):
                return StabilityResult(False, "file is not readable")
            return StabilityResult(True)
        time.sleep(min(poll_seconds, max(0.01, quiet_seconds / 4 or 0.01)))


def _cached_document(connection: sqlite3.Connection, row: sqlite3.Row) -> ExtractedDocument:
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return ExtractedDocument(
        text=str(row["text"]),
        metadata=metadata,
        page_count=int(row["page_count"]) if row["page_count"] is not None else None,
        title=str(row["title"]) if row["title"] is not None else None,
        author=str(row["author"]) if row["author"] is not None else None,
        truncated=bool(row["truncated"]),
    )


def _record_for_rename(
    connection: sqlite3.Connection,
    path: str,
    device: int,
    inode: int,
    sha256: str,
) -> int | None:
    inode_match = db.find_file_by_inode(connection, device, inode, exclude_path=path)
    if (
        inode_match is not None
        and inode_match.path != path
        and not os.path.lexists(inode_match.path)
    ):
        db.update_file_path(
            connection,
            int(inode_match.id),
            path,
            device=device,
            inode=inode,
        )
        LOGGER.info(
            "Detected rename by inode",
            extra={
                "event": "scan.rename_inode",
                "old_path": inode_match.path,
                "new_path": path,
            },
        )
        return int(inode_match.id)
    hash_match = db.find_file_by_hash(connection, sha256, exclude_path=path)
    if hash_match is not None and hash_match.path != path and not os.path.lexists(hash_match.path):
        db.update_file_path(
            connection,
            int(hash_match.id),
            path,
            device=device,
            inode=inode,
        )
        LOGGER.info(
            "Detected move by content hash",
            extra={
                "event": "scan.rename_hash",
                "old_path": hash_match.path,
                "new_path": path,
            },
        )
        return int(hash_match.id)
    return None


def _write_unsupported(
    connection: sqlite3.Connection,
    path: str,
    info: os.stat_result,
    *,
    error_kind: str,
    error_message: str,
) -> None:
    with db.transaction(connection):
        db.upsert_file(
            connection,
            path,
            inode=int(info.st_ino),
            device=int(info.st_dev),
            size=int(info.st_size),
            mtime_ns=int(info.st_mtime_ns),
            mime=mime_for_path(path),
            status=FileStatus.UNSUPPORTED.value,
            error_kind=error_kind,
            error_msg=error_message[:500],
            indexed_at=db.utc_now(),
            index_version=INDEX_VERSION,
            deleted_at=None,
        )


def _write_failure(
    connection: sqlite3.Connection,
    path: str,
    info: os.stat_result,
    sha256: str,
    error: ExtractionError,
) -> int:
    status = (
        FileStatus.UNSUPPORTED
        if isinstance(error, (UnsupportedFormat, FileTooLarge))
        else FileStatus.FAILED
    )
    with db.transaction(connection):
        if db.get_file_by_path(connection, path) is None:
            _record_for_rename(
                connection,
                path,
                int(info.st_dev),
                int(info.st_ino),
                sha256,
            )
        file_id = db.upsert_file(
            connection,
            path,
            inode=int(info.st_ino),
            device=int(info.st_dev),
            size=int(info.st_size),
            mtime_ns=int(info.st_mtime_ns),
            sha256=sha256,
            mime=mime_for_path(path),
            status=status.value,
            error_kind=error.error_kind,
            error_msg=str(error)[:500],
            indexed_at=db.utc_now(),
            index_version=INDEX_VERSION,
            deleted_at=None,
        )
    LOGGER.warning(
        "Extraction failed",
        extra={
            "event": "scan.extraction_failed",
            "path": path,
            "error_kind": error.error_kind,
            "error": str(error),
        },
    )
    return file_id


def _write_io_failure(
    connection: sqlite3.Connection,
    path: str,
    info: os.stat_result | None,
    *,
    error_kind: str,
    error_message: str,
) -> None:
    fields: dict[str, object] = {
        "status": FileStatus.FAILED.value,
        "error_kind": error_kind,
        "error_msg": error_message[:500],
        "indexed_at": db.utc_now(),
        "index_version": INDEX_VERSION,
        "sha256": None,
        "deleted_at": None,
    }
    if info is not None:
        fields.update(
            {
                "inode": int(info.st_ino),
                "device": int(info.st_dev),
                "size": int(info.st_size),
                "mtime_ns": int(info.st_mtime_ns),
                "mime": mime_for_path(path),
            }
        )
    try:
        with db.transaction(connection):
            db.upsert_file(connection, path, **fields)
    except (OSError, sqlite3.Error, db.DatabaseError) as exc:
        LOGGER.error(
            "Could not record indexing failure",
            extra={"event": "scan.record_failure_failed", "path": path, "error": str(exc)},
        )


def _ensure_xattrs(
    connection: sqlite3.Connection,
    record: FileRecord,
    settings: Settings,
    stats: ScanStats,
) -> None:
    if not settings.xattr_enabled or record.id is None:
        return
    try:
        mirrored = read_metadata(record.path)
    except XattrUnsupported as exc:
        stats.xattr_unsupported += 1
        LOGGER.debug(
            "xattrs unsupported",
            extra={"event": "xattr.unsupported", "path": record.path, "error": str(exc)},
        )
        return
    except XattrOperationError as exc:
        stats.xattr_errors += 1
        LOGGER.debug(
            "xattr read failed",
            extra={"event": "xattr.read_failed", "path": record.path, "error": str(exc)},
        )
        return

    tags = db.tag_union(connection, int(record.id))
    expected_summary = one_line(record.summary or "")
    expected_mirror_summary = truncate_utf8(expected_summary, SUMMARY_MAX_BYTES)
    matches = (
        (mirrored.sha256 or None) == (record.sha256 or None)
        and (mirrored.version or None) == str(record.index_version)
        and (mirrored.category or None) == (record.category or None)
        and mirrored.tags == tags
        and (mirrored.summary or "") == expected_mirror_summary
    )
    if matches:
        return
    try:
        result = sync_file_xattrs(
            connection,
            int(record.id),
            suppression_ttl=settings.self_write_suppression_seconds,
        )
    except (XattrUnsupported, XattrOperationError) as exc:
        stats.xattr_errors += 1
        LOGGER.warning(
            "xattr sync failed",
            extra={"event": "xattr.sync_failed", "path": record.path, "error": str(exc)},
        )
        return
    if result.supported:
        stats.xattr_synced += 1
    else:
        stats.xattr_unsupported += 1


def _handle_extraction_error(
    connection: sqlite3.Connection,
    path: str,
    info: os.stat_result,
    sha256: str,
    error: ExtractionError,
    stats: ScanStats,
) -> None:
    _write_failure(connection, path, info, sha256, error)
    if isinstance(error, UnsupportedFormat):
        stats.unsupported += 1
    else:
        stats.errors += 1


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise PipelineCancelled("indexing cancelled")


def _scan_one_file(
    connection: sqlite3.Connection,
    file_path: str,
    settings: Settings,
    stats: ScanStats,
    *,
    force: bool,
    cancel_check: Callable[[], bool] | None = None,
    analysis_service: AnalysisService | None = None,
    embedding_service: EmbeddingService | None = None,
) -> None:
    _check_cancel(cancel_check)
    try:
        info = os.stat(file_path, follow_symlinks=False)
    except OSError as exc:
        stats.errors += 1
        _write_io_failure(
            connection,
            file_path,
            None,
            error_kind="io_error",
            error_message=f"Cannot stat file: {exc.strerror}",
        )
        LOGGER.warning(
            "Cannot stat file",
            extra={"event": "scan.stat_failed", "path": file_path, "error": str(exc)},
        )
        return
    if not stat.S_ISREG(info.st_mode):
        stats.skipped += 1
        return
    if info.st_size > settings.max_file_size_bytes:
        _write_unsupported(
            connection,
            file_path,
            info,
            error_kind="file_too_large",
            error_message=(
                f"File is {info.st_size} bytes; limit is {settings.max_file_size_bytes} bytes"
            ),
        )
        stats.unsupported += 1
        return
    if not supported_extension(file_path):
        existing = db.get_file_by_path(connection, file_path)
        if (
            existing is not None
            and existing.status == FileStatus.UNSUPPORTED
            and existing.size == info.st_size
            and existing.mtime_ns == info.st_mtime_ns
        ):
            stats.unchanged += 1
            return
        _write_unsupported(
            connection,
            file_path,
            info,
            error_kind="unsupported_format",
            error_message="Unsupported file type",
        )
        stats.unsupported += 1
        return

    existing = db.get_file_by_path(connection, file_path)
    same_metadata = (
        existing is not None
        and existing.size == info.st_size
        and existing.mtime_ns == info.st_mtime_ns
        and existing.inode == info.st_ino
        and existing.device == info.st_dev
        and existing.index_version == INDEX_VERSION
        and existing.sha256 is not None
        and existing.status in {FileStatus.DONE, FileStatus.FAILED, FileStatus.UNSUPPORTED}
    )
    if same_metadata and not force and existing is not None:
        _check_cancel(cancel_check)
        with db.transaction(connection):
            db.upsert_file(
                connection,
                file_path,
                inode=int(info.st_ino),
                device=int(info.st_dev),
                size=int(info.st_size),
                mtime_ns=int(info.st_mtime_ns),
                mime=mime_for_path(file_path),
                deleted_at=None,
            )
        stats.unchanged += 1
        refreshed = db.get_file_by_path(connection, file_path)
        if refreshed is not None and refreshed.status == FileStatus.DONE:
            _ensure_xattrs(connection, refreshed, settings, stats)
        return

    _check_cancel(cancel_check)
    try:
        sha256 = hash_file(file_path)
    except OSError as exc:
        stats.errors += 1
        _write_io_failure(
            connection,
            file_path,
            info,
            error_kind="io_error",
            error_message=f"Cannot read file: {exc.strerror}",
        )
        LOGGER.warning(
            "Cannot hash file",
            extra={"event": "scan.hash_failed", "path": file_path, "error": str(exc)},
        )
        return

    if (
        existing is not None
        and not force
        and existing.sha256 == sha256
        and existing.index_version == INDEX_VERSION
        and existing.status == FileStatus.DONE
    ):
        _check_cancel(cancel_check)
        with db.transaction(connection):
            db.upsert_file(
                connection,
                file_path,
                inode=int(info.st_ino),
                device=int(info.st_dev),
                size=int(info.st_size),
                mtime_ns=int(info.st_mtime_ns),
                mime=mime_for_path(file_path),
                deleted_at=None,
            )
        stats.unchanged += 1
        refreshed = db.get_file_by_path(connection, file_path)
        if refreshed is not None:
            _ensure_xattrs(connection, refreshed, settings, stats)
        return

    _check_cancel(cancel_check)
    cached_row = None
    if not force:
        cached_row = db.get_cached_extraction(connection, sha256, EXTRACTION_VERSION)

    if cached_row is not None:
        document: ExtractedDocumentModel = _cached_document(connection, cached_row)
        from_cache = True
    else:
        try:
            document = extract_document(file_path, settings)
        except ExtractionError as exc:
            _handle_extraction_error(connection, file_path, info, sha256, exc, stats)
            return
        except OSError as exc:
            stats.errors += 1
            LOGGER.warning(
                "Extraction I/O failed",
                extra={"event": "scan.extract_io_failed", "path": file_path, "error": str(exc)},
            )
            return
        from_cache = False

    analysis = None
    if analysis_service is not None:
        _check_cancel(cancel_check)
        try:
            analysis = analysis_service.analyze(
                connection,
                document,
                file_path,
                sha256,
            )
        except (PipelineCancelled, sqlite3.Error, db.DatabaseError):
            raise
        except Exception as exc:
            stats.analysis_failures += 1
            LOGGER.warning(
                "Analysis failed; indexing without new AI metadata",
                extra={
                    "event": "scan.analysis_failed",
                    "path": file_path,
                    "error_type": type(exc).__name__,
                    "error": redact_secrets(str(exc))[:300],
                },
            )
        else:
            if analysis is not None:
                if analysis.cached:
                    stats.analysis_cached += 1
                else:
                    stats.analyzed += 1

    embedding = None
    if embedding_service is not None:
        _check_cancel(cancel_check)
        try:
            embedding = embedding_service.embed(
                connection,
                sha256,
                text=document.text,
                title=document.title,
            )
        except (PipelineCancelled, sqlite3.Error, db.DatabaseError):
            raise
        except Exception as exc:
            stats.embedding_failures += 1
            LOGGER.warning(
                "Embedding failed; indexing without a new vector",
                extra={
                    "event": "scan.embedding_failed",
                    "path": file_path,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
        else:
            if embedding is not None:
                if embedding.cached:
                    stats.embedding_cached += 1
                else:
                    stats.embedded += 1

    _check_cancel(cancel_check)
    excerpt_text = excerpt(document.text)
    _check_cancel(cancel_check)
    now = db.utc_now()
    analysis_fields: dict[str, object] = {}
    if analysis is not None:
        analysis_fields.update(
            {
                "analysis_provider": analysis.provider,
                "analysis_model": analysis.model,
                "category": analysis.result.category.value,
                "summary": analysis.result.summary,
            }
        )
    renamed = False
    try:
        with db.transaction(connection):
            if existing is None:
                renamed_id = _record_for_rename(
                    connection,
                    file_path,
                    int(info.st_dev),
                    int(info.st_ino),
                    sha256,
                )
                renamed = renamed_id is not None
            else:
                renamed_id = int(existing.id) if existing.id is not None else None
            file_id = db.upsert_file(
                connection,
                file_path,
                inode=int(info.st_ino),
                device=int(info.st_dev),
                size=int(info.st_size),
                mtime_ns=int(info.st_mtime_ns),
                sha256=sha256,
                mime=mime_for_path(file_path),
                status=FileStatus.DONE.value,
                error_kind=None,
                error_msg=None,
                indexed_at=now,
                index_version=INDEX_VERSION,
                text_excerpt=excerpt_text,
                page_count=document.page_count,
                title=document.title,
                author=document.author,
                deleted_at=None,
                **analysis_fields,
            )
            if renamed_id is not None and renamed_id != file_id:
                raise db.DatabaseError("Rename resolution produced inconsistent file ids")
            if not from_cache:
                db.cache_extraction(
                    connection,
                    sha256,
                    EXTRACTION_VERSION,
                    document.text,
                    json.dumps(document.metadata, ensure_ascii=False, default=str),
                    page_count=document.page_count,
                    title=document.title,
                    author=document.author,
                    truncated=document.truncated,
                )
            db.upsert_search_document(
                connection,
                file_id,
                path=file_path,
                title=document.title,
                author=document.author,
                category=(
                    analysis.result.category.value
                    if analysis is not None
                    else (existing.category if existing is not None else None)
                ),
                summary=(
                    analysis.result.summary
                    if analysis is not None
                    else (existing.summary if existing is not None else None)
                ),
                body=document.text,
            )
            if analysis is not None:
                db.replace_ai_tags(
                    connection,
                    file_id,
                    {tag: analysis.result.confidence for tag in analysis.result.tags},
                )
            if embedding is not None and embedding.vector is not None and not embedding.cached:
                db.upsert_embedding(
                    connection,
                    sha256,
                    embedding.model,
                    embedding.dimension,
                    embedding.vector,
                )
    except (OSError, sqlite3.Error, db.DatabaseError) as exc:
        stats.errors += 1
        LOGGER.error(
            "Database write failed",
            extra={"event": "scan.db_write_failed", "path": file_path, "error": str(exc)},
        )
        return

    if existing is None and not renamed:
        stats.added += 1
    else:
        stats.updated += 1
    refreshed = db.get_file_by_id(connection, file_id)
    if refreshed is not None:
        _ensure_xattrs(connection, refreshed, settings, stats)


def record_needs_index(
    record: FileRecord | None,
    info: os.stat_result,
    *,
    force: bool = False,
) -> bool:
    """Decide whether reconciliation should enqueue an index job for a file."""
    if force or record is None:
        return True
    if record.status in {FileStatus.MISSING, FileStatus.PENDING, FileStatus.PROCESSING}:
        return True
    if record.index_version != INDEX_VERSION:
        return True
    if (
        record.size != info.st_size
        or record.mtime_ns != info.st_mtime_ns
        or record.inode != info.st_ino
        or record.device != info.st_dev
    ):
        return True
    if record.status == FileStatus.UNSUPPORTED:
        return False
    return record.sha256 is None


def discover_changes(
    root: str | os.PathLike[str],
    connection: sqlite3.Connection,
    settings: Settings,
    enqueue: Callable[[str, str], None],
    *,
    force: bool = False,
) -> DiscoveryStats:
    """Walk a root, enqueue only changed files, repair mirrors, and mark vanished rows."""
    base = normalize_root(root)
    if not os.path.isdir(base):
        raise ScanError(f"Not a directory: {base}")
    stats = DiscoveryStats(root=base)
    seen: set[str] = set()

    def record_error(path: str, error: OSError) -> None:
        stats.errors += 1
        LOGGER.warning(
            "Directory traversal error",
            extra={"event": "reconcile.walk_error", "path": path, "error": str(error)},
        )

    for path in walk_files(base, settings, on_error=record_error):
        path_string = str(path)
        seen.add(path_string)
        stats.files_seen += 1
        try:
            info = os.stat(path_string, follow_symlinks=False)
        except OSError as exc:
            stats.errors += 1
            LOGGER.warning(
                "Cannot stat during reconciliation",
                extra={"event": "reconcile.stat_failed", "path": path_string, "error": str(exc)},
            )
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        existing = db.get_file_by_path(connection, path_string)
        unsupported = (
            not supported_extension(path_string) or info.st_size > settings.max_file_size_bytes
        )
        if unsupported:
            stats.unsupported += 1
        if record_needs_index(existing, info, force=force):
            try:
                enqueue("index", path_string)
            except (sqlite3.Error, db.DatabaseError, OSError) as exc:
                stats.errors += 1
                LOGGER.error(
                    "Could not enqueue reconciliation job",
                    extra={
                        "event": "reconcile.enqueue_failed",
                        "path": path_string,
                        "error": str(exc),
                    },
                )
            else:
                stats.queued += 1
            continue
        stats.unchanged += 1
        if existing is not None and existing.status == FileStatus.DONE and settings.xattr_enabled:
            _ensure_xattrs(connection, existing, settings, stats)

    _mark_vanished(connection, base, seen, stats)
    LOGGER.info(
        "Reconciliation complete",
        extra={"event": "reconcile.complete", **stats.as_dict()},
    )
    return stats


def index_file(
    connection: sqlite3.Connection,
    file_path: str | os.PathLike[str],
    settings: Settings,
    *,
    force: bool = False,
    stats: ScanStats | None = None,
    cancel_check: Callable[[], bool] | None = None,
    analysis_service: AnalysisService | None = None,
    embedding_service: EmbeddingService | None = None,
) -> ScanStats:
    """Run the hash/extract/index/xattr pipeline for one path.

    Raises ``PipelineCancelled`` when cancellation is requested between stages.
    """
    target = os.fspath(file_path)
    result_stats = stats or ScanStats(root=os.path.dirname(target))
    _scan_one_file(
        connection,
        target,
        settings,
        result_stats,
        force=force,
        cancel_check=cancel_check,
        analysis_service=analysis_service,
        embedding_service=embedding_service,
    )
    return result_stats


def _mark_vanished(
    connection: sqlite3.Connection,
    root: str,
    seen: set[str],
    stats: ScanStats | DiscoveryStats,
) -> None:
    rows = db.list_files_under(connection, root)
    missing_ids = [
        int(record.id)
        for record in rows
        if record.id is not None
        and record.path not in seen
        and record.status != FileStatus.MISSING
        and not os.path.lexists(record.path)
    ]
    if not missing_ids:
        return
    with db.transaction(connection):
        stats.missing = db.mark_missing(connection, missing_ids)
    LOGGER.info(
        "Marked files missing",
        extra={"event": "scan.missing", "root": root, "count": stats.missing},
    )


def scan_directory(
    root: str | os.PathLike[str],
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    force: bool = False,
    analysis_service: AnalysisService | None = None,
    embedding_service: EmbeddingService | None = None,
) -> ScanStats:
    """Scan one directory tree, indexing changed files and marking vanished ones."""
    base = normalize_root(root)
    if not os.path.isdir(base):
        raise ScanError(f"Not a directory: {base}")
    stats = ScanStats(root=base)
    seen: set[str] = set()

    def record_error(path: str, error: OSError) -> None:
        stats.errors += 1
        LOGGER.warning(
            "Directory traversal error",
            extra={"event": "scan.walk_error", "path": path, "error": str(error)},
        )

    for path in walk_files(base, settings, on_error=record_error):
        path_string = str(path)
        seen.add(path_string)
        stats.files_seen += 1
        try:
            _scan_one_file(
                connection,
                path_string,
                settings,
                stats,
                force=force,
                analysis_service=analysis_service,
                embedding_service=embedding_service,
            )
        except (OSError, sqlite3.Error, db.DatabaseError) as exc:
            stats.errors += 1
            LOGGER.error(
                "Unexpected file scan failure",
                extra={"event": "scan.file_failed", "path": path_string, "error": str(exc)},
            )
        except Exception as exc:
            stats.errors += 1
            LOGGER.exception(
                "Unhandled file scan failure",
                extra={
                    "event": "scan.file_unhandled",
                    "path": path_string,
                    "error_type": type(exc).__name__,
                },
            )

    _mark_vanished(connection, base, seen, stats)
    LOGGER.info(
        "Directory scan complete",
        extra={"event": "scan.complete", **stats.as_dict()},
    )
    return stats


def scan_directories(
    roots: Sequence[str | os.PathLike[str]],
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    force: bool = False,
    analysis_service: AnalysisService | None = None,
    embedding_service: EmbeddingService | None = None,
) -> list[ScanStats]:
    """Scan multiple roots sequentially and return per-root statistics."""
    results: list[ScanStats] = []
    for root in roots:
        results.append(
            scan_directory(
                root,
                connection,
                settings,
                force=force,
                analysis_service=analysis_service,
                embedding_service=embedding_service,
            )
        )
    return results

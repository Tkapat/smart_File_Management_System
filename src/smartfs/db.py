"""SQLite connection management, schema migrations, and data access helpers."""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from smartfs.models import (
    AnalysisResult,
    Collection,
    DuplicateGroup,
    FileRecord,
    FileStatus,
    JobRecord,
    JobState,
    JobType,
    TagSource,
    TagStat,
)

LOGGER = logging.getLogger("smartfs.db")
SCHEMA_VERSION = 5

FILE_COLUMNS = (
    "id",
    "path",
    "inode",
    "device",
    "size",
    "mtime_ns",
    "sha256",
    "mime",
    "status",
    "error_kind",
    "error_msg",
    "indexed_at",
    "index_version",
    "analysis_provider",
    "analysis_model",
    "category",
    "summary",
    "text_excerpt",
    "page_count",
    "title",
    "author",
    "first_seen_at",
    "last_seen_at",
    "deleted_at",
)

_WRITABLE_FILE_COLUMNS = set(FILE_COLUMNS) - {"id", "path", "first_seen_at", "last_seen_at"}

_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            inode INTEGER,
            device INTEGER,
            size INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            mime TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','done','failed','unsupported','missing')),
            error_kind TEXT,
            error_msg TEXT,
            indexed_at TEXT,
            index_version INTEGER NOT NULL DEFAULT 1,
            analysis_provider TEXT,
            analysis_model TEXT,
            category TEXT,
            summary TEXT,
            text_excerpt TEXT,
            page_count INTEGER,
            title TEXT,
            author TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE INDEX idx_files_sha256 ON files(sha256);
        CREATE INDEX idx_files_status ON files(status);
        CREATE INDEX idx_files_inode_device ON files(device, inode);
        CREATE INDEX idx_files_deleted_at ON files(deleted_at);

        CREATE TABLE tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );

        CREATE TABLE file_tags (
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            source TEXT NOT NULL CHECK (source IN ('user','ai')),
            confidence REAL
                CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
            created_at TEXT NOT NULL,
            PRIMARY KEY (file_id, tag_id, source)
        ) WITHOUT ROWID;
        CREATE INDEX idx_file_tags_tag ON file_tags(tag_id, source);
        CREATE INDEX idx_file_tags_file ON file_tags(file_id);

        CREATE TABLE content_cache (
            sha256 TEXT NOT NULL,
            extraction_version INTEGER NOT NULL,
            text TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            page_count INTEGER,
            title TEXT,
            author TEXT,
            truncated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (sha256, extraction_version)
        );

        CREATE TABLE analysis_cache (
            sha256 TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (sha256, provider, model, prompt_version)
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('index','delete')),
            file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
            state TEXT NOT NULL CHECK (
                state IN ('queued','running','done','failed','retry_scheduled','cancelled')
            ),
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_run_at TEXT NOT NULL,
            last_error TEXT,
            rerun_requested INTEGER NOT NULL DEFAULT 0
                CHECK (rerun_requested IN (0,1)),
            cancel_requested INTEGER NOT NULL DEFAULT 0
                CHECK (cancel_requested IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_jobs_active_subject
            ON jobs(path, type)
            WHERE state IN ('queued','running','retry_scheduled');
        CREATE INDEX idx_jobs_due ON jobs(state, next_run_at);
        CREATE INDEX idx_jobs_file ON jobs(file_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE embeddings (
            sha256 TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL
                CHECK (dimension > 0 AND dimension <= 65536),
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (sha256, model)
        );
        CREATE INDEX idx_embeddings_model ON embeddings(model);
        """,
    ),
    (
        4,
        """
        CREATE VIRTUAL TABLE files_fts USING fts5(
            path,
            title,
            author,
            category,
            summary,
            body,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        INSERT INTO files_fts(rowid, path, title, author, category, summary, body)
        SELECT id, path, title, author, category, summary, text_excerpt FROM files;
        """,
    ),
    (
        5,
        """
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            query TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            category TEXT,
            mode TEXT NOT NULL DEFAULT 'auto'
                CHECK (mode IN ('auto','text','semantic','hybrid')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
)


class DatabaseError(RuntimeError):
    """Raised for unrecoverable database errors."""


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def connect(
    path: str | Path,
    *,
    timeout: float = 5.0,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open one SQLite connection configured for SmartFS.

    A connection must only be used by the thread that created it.
    """
    db_path = str(path)
    if db_path != ":memory:":
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        if db_path == ":memory:":
            raise DatabaseError("read_only is not supported for in-memory databases")
        uri = f"file:{quote(str(Path(db_path).expanduser().absolute()))}?mode=ro"
        connection = sqlite3.connect(uri, timeout=timeout, uri=True, isolation_level=None)
    else:
        connection = sqlite3.connect(db_path, timeout=timeout, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a short, explicit write transaction with rollback on failure."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _apply_migration(connection: sqlite3.Connection, version: int, script: str) -> None:
    body = f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
    try:
        connection.executescript(body)
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise DatabaseError(f"Migration {version} failed: {exc}") from exc


def init_database(
    path: str | Path,
    *,
    backup_dir: str | Path | None = None,
    backup_keep: int = 5,
) -> None:
    """Create or migrate the SmartFS schema, backing up before migrations."""
    db_path = Path(path).expanduser()
    connection = connect(db_path)
    try:
        version = _current_version(connection)
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                f"Database schema version {version} is newer than supported {SCHEMA_VERSION}."
            )
        if version < SCHEMA_VERSION and _has_user_tables(connection) and backup_dir:
            backup_database(
                connection,
                Path(backup_dir),
                keep=backup_keep,
                prefix="pre-migration",
            )
        for target, script in _MIGRATIONS:
            if target > version:
                _apply_migration(connection, target, script)
                version = target
        connection.execute("PRAGMA optimize")
    finally:
        connection.close()


def backup_database(
    source: sqlite3.Connection | str | Path,
    destination_dir: str | Path,
    *,
    keep: int = 10,
    prefix: str = "smartfs",
) -> Path:
    """Create a consistent SQLite backup and prune old timestamped copies."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    destination = Path(destination_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    target = destination / f"{prefix}-{timestamp}.db"

    owned_connection = isinstance(source, (str, Path)) or source == ":memory:"
    if isinstance(source, sqlite3.Connection):
        source_connection = source
    else:
        source_connection = connect(source, read_only=True)
    try:
        target_connection = sqlite3.connect(target, timeout=5.0)
        try:
            source_connection.backup(target_connection)
            integrity = target_connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise DatabaseError("Backup integrity check failed")
        finally:
            target_connection.close()
    except Exception:
        with suppress(OSError):
            target.unlink()
        raise
    finally:
        if owned_connection:
            source_connection.close()

    os.chmod(target, 0o600)
    backups = sorted(
        destination.glob(f"{prefix}-*.db"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for obsolete in backups[max(keep, 1) :]:
        try:
            obsolete.unlink()
        except OSError as exc:
            LOGGER.warning(
                "Could not prune backup",
                extra={"event": "backup.prune_failed", "path": str(obsolete), "error": str(exc)},
            )
    return target


def get_file_by_path(connection: sqlite3.Connection, path: str) -> FileRecord | None:
    """Fetch one indexed file by absolute path."""
    row = connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
    return FileRecord.model_validate(dict(row)) if row else None


def get_file_by_id(connection: sqlite3.Connection, file_id: int) -> FileRecord | None:
    """Fetch one indexed file by primary key."""
    row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return FileRecord.model_validate(dict(row)) if row else None


def find_file_by_inode(
    connection: sqlite3.Connection,
    device: int,
    inode: int,
    *,
    exclude_path: str | None = None,
) -> FileRecord | None:
    """Find a file by device/inode, optionally excluding a path."""
    row = connection.execute(
        "SELECT * FROM files WHERE device = ? AND inode = ? AND (? IS NULL OR path != ?) "
        "ORDER BY id LIMIT 1",
        (device, inode, exclude_path, exclude_path),
    ).fetchone()
    return FileRecord.model_validate(dict(row)) if row else None


def find_file_by_hash(
    connection: sqlite3.Connection,
    sha256: str,
    *,
    exclude_path: str | None = None,
) -> FileRecord | None:
    """Find a file by content hash, optionally excluding a path."""
    row = connection.execute(
        "SELECT * FROM files WHERE sha256 = ? AND (? IS NULL OR path != ?) ORDER BY id LIMIT 1",
        (sha256, exclude_path, exclude_path),
    ).fetchone()
    return FileRecord.model_validate(dict(row)) if row else None


def upsert_file(
    connection: sqlite3.Connection,
    path: str,
    **fields: Any,
) -> int:
    """Insert or update a file row using only the supplied fields."""
    invalid = set(fields) - _WRITABLE_FILE_COLUMNS
    if invalid:
        raise ValueError(f"Unknown file columns: {', '.join(sorted(invalid))}")
    now = utc_now()
    values: dict[str, Any] = {
        "path": path,
        "first_seen_at": now,
        "last_seen_at": now,
    }
    values.update(fields)
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [name for name in columns if name not in {"path", "first_seen_at"}]
    update_columns.append("last_seen_at")
    assignments = ", ".join(f"{name} = excluded.{name}" for name in dict.fromkeys(update_columns))
    sql = (
        f"INSERT INTO files ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {assignments} RETURNING id"
    )
    row = connection.execute(sql, [values[name] for name in columns]).fetchone()
    if row is None:
        raise DatabaseError(f"Failed to upsert file row for {path}")
    return int(row[0])


def update_file_path(
    connection: sqlite3.Connection,
    file_id: int,
    new_path: str,
    *,
    device: int | None = None,
    inode: int | None = None,
    mtime_ns: int | None = None,
    size: int | None = None,
) -> None:
    """Update a file's path after a detected rename or move."""
    connection.execute(
        "UPDATE files SET path = ?, device = COALESCE(?, device), "
        "inode = COALESCE(?, inode), mtime_ns = COALESCE(?, mtime_ns), "
        "size = COALESCE(?, size), status = CASE WHEN status = 'missing' THEN 'done' "
        "ELSE status END, deleted_at = NULL, last_seen_at = ? WHERE id = ?",
        (new_path, device, inode, mtime_ns, size, utc_now(), file_id),
    )


def mark_missing(connection: sqlite3.Connection, file_ids: Sequence[int]) -> int:
    """Mark files as missing without deleting their index metadata or tags."""
    if not file_ids:
        return 0
    now = utc_now()
    changed = 0
    for file_id in file_ids:
        cursor = connection.execute(
            "UPDATE files SET status = 'missing', deleted_at = ?, last_seen_at = ? "
            "WHERE id = ? AND status != 'missing'",
            (now, now, file_id),
        )
        changed += cursor.rowcount
    return changed


def list_files_under(connection: sqlite3.Connection, root: str) -> list[FileRecord]:
    """List indexed files at or below a directory."""
    normalized = os.path.abspath(root).rstrip(os.sep)
    prefix = normalized + os.sep
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = connection.execute(
        "SELECT * FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\' ORDER BY path",
        (normalized, escaped + "%"),
    ).fetchall()
    return [FileRecord.model_validate(dict(row)) for row in rows]


def tag_union(connection: sqlite3.Connection, file_id: int) -> list[str]:
    """Return the deduplicated union of AI and user tags for a file."""
    rows = connection.execute(
        "SELECT DISTINCT t.name FROM tags t "
        "JOIN file_tags ft ON ft.tag_id = t.id "
        "WHERE ft.file_id = ? ORDER BY t.name",
        (file_id,),
    ).fetchall()
    return [str(row["name"]) for row in rows]


def file_tags(connection: sqlite3.Connection, file_id: int) -> list[sqlite3.Row]:
    """Return tag rows with provenance for a file."""
    return connection.execute(
        "SELECT t.name, ft.source, ft.confidence, ft.created_at "
        "FROM file_tags ft JOIN tags t ON t.id = ft.tag_id "
        "WHERE ft.file_id = ? ORDER BY t.name, ft.source",
        (file_id,),
    ).fetchall()


def get_or_create_tag(connection: sqlite3.Connection, name: str) -> int:
    """Return the id of a normalized tag, creating it when absent."""
    connection.execute("INSERT INTO tags(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    row = connection.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise DatabaseError(f"Tag lookup failed for {name!r}")
    return int(row["id"])


def add_file_tag(
    connection: sqlite3.Connection,
    file_id: int,
    tag: str,
    *,
    source: TagSource | str = TagSource.USER,
    confidence: float | None = None,
) -> None:
    """Attach a tag to a file while preserving source provenance."""
    source_value = TagSource(source).value
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    tag_id = get_or_create_tag(connection, tag)
    connection.execute(
        "INSERT INTO file_tags(file_id, tag_id, source, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(file_id, tag_id, source) DO UPDATE SET "
        "confidence = COALESCE(excluded.confidence, file_tags.confidence)",
        (file_id, tag_id, source_value, confidence, utc_now()),
    )


def remove_file_tag(
    connection: sqlite3.Connection,
    file_id: int,
    tag: str,
    *,
    source: TagSource | str | None = None,
) -> int:
    """Remove a tag association from a file, optionally for one source only."""
    if source is None:
        cursor = connection.execute(
            "DELETE FROM file_tags WHERE file_id = ? AND tag_id IN "
            "(SELECT id FROM tags WHERE name = ?)",
            (file_id, tag),
        )
    else:
        cursor = connection.execute(
            "DELETE FROM file_tags WHERE file_id = ? AND source = ? AND tag_id IN "
            "(SELECT id FROM tags WHERE name = ?)",
            (file_id, TagSource(source).value, tag),
        )
    return cursor.rowcount


def set_category(connection: sqlite3.Connection, file_id: int, category: str | None) -> None:
    """Set the indexed category for a file."""
    connection.execute(
        "UPDATE files SET category = ?, last_seen_at = ? WHERE id = ?",
        (category, utc_now(), file_id),
    )


def cache_extraction(
    connection: sqlite3.Connection,
    sha256: str,
    extraction_version: int,
    text: str,
    metadata_json: str,
    *,
    page_count: int | None,
    title: str | None,
    author: str | None,
    truncated: bool,
) -> None:
    """Store extraction output for reuse by identical content."""
    connection.execute(
        "INSERT INTO content_cache("
        "sha256, extraction_version, text, metadata_json, page_count, title, author, "
        "truncated, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(sha256, extraction_version) DO UPDATE SET "
        "text = excluded.text, metadata_json = excluded.metadata_json, "
        "page_count = excluded.page_count, title = excluded.title, "
        "author = excluded.author, truncated = excluded.truncated",
        (
            sha256,
            extraction_version,
            text,
            metadata_json,
            page_count,
            title,
            author,
            1 if truncated else 0,
            utc_now(),
        ),
    )


def get_cached_extraction(
    connection: sqlite3.Connection, sha256: str, extraction_version: int
) -> sqlite3.Row | None:
    """Return cached extraction output for a content hash and pipeline version."""
    row = connection.execute(
        "SELECT * FROM content_cache WHERE sha256 = ? AND extraction_version = ?",
        (sha256, extraction_version),
    ).fetchone()
    return row


def cache_analysis(
    connection: sqlite3.Connection,
    sha256: str,
    provider: str,
    model: str,
    prompt_version: int,
    result: AnalysisResult,
) -> None:
    """Cache a validated analysis result by content and provider identity."""
    connection.execute(
        "INSERT INTO analysis_cache("
        "sha256, provider, model, prompt_version, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(sha256, provider, model, prompt_version) DO UPDATE SET "
        "result_json = excluded.result_json",
        (
            sha256,
            provider,
            model,
            prompt_version,
            result.model_dump_json(),
            utc_now(),
        ),
    )


def get_cached_analysis(
    connection: sqlite3.Connection,
    sha256: str,
    provider: str,
    model: str,
    prompt_version: int,
) -> AnalysisResult | None:
    """Load and validate a cached analysis result."""
    row = connection.execute(
        "SELECT result_json FROM analysis_cache WHERE sha256 = ? AND provider = ? "
        "AND model = ? AND prompt_version = ?",
        (sha256, provider, model, prompt_version),
    ).fetchone()
    if row is None:
        return None
    try:
        return AnalysisResult.model_validate_json(row["result_json"])
    except ValueError:
        LOGGER.warning(
            "Discarding invalid cached analysis",
            extra={"event": "cache.analysis_invalid", "sha256": sha256},
        )
        return None


def status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return file counts by status."""
    counts: dict[str, int] = {}
    for row in connection.execute("SELECT status, COUNT(*) AS count FROM files GROUP BY status"):
        counts[str(row["status"])] = int(row["count"])
    return counts


def upsert_search_document(
    connection: sqlite3.Connection,
    file_id: int,
    *,
    path: str,
    title: str | None,
    author: str | None,
    category: str | None,
    summary: str | None,
    body: str | None,
) -> None:
    """Replace the FTS5 row for one file inside the caller's transaction."""
    connection.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
    connection.execute(
        "INSERT INTO files_fts(rowid, path, title, author, category, summary, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            (path or "").replace("\x00", " "),
            (title or "").replace("\x00", " "),
            (author or "").replace("\x00", " "),
            (category or "").replace("\x00", " "),
            (summary or "").replace("\x00", " "),
            (body or "").replace("\x00", " "),
        ),
    )


def delete_search_document(connection: sqlite3.Connection, file_id: int) -> None:
    """Remove one file's FTS5 row inside the caller's transaction."""
    connection.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))


def query_files(
    connection: sqlite3.Connection,
    *,
    tags: Sequence[str] = (),
    category: str | None = None,
    text: str | None = None,
    status: FileStatus | str | None = FileStatus.DONE,
    limit: int = 200,
) -> list[FileRecord]:
    """Look up files by tag, category, and a simple literal text filter."""
    if limit < 1:
        raise ValueError("limit must be positive")
    clauses: list[str] = []
    parameters: list[Any] = []
    if status is not None:
        clauses.append("f.status = ?")
        parameters.append(FileStatus(status).value)
    if category is not None:
        clauses.append("f.category = ?")
        parameters.append(category.strip().lower())
    for tag in tags:
        clauses.append(
            "EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id = ft.tag_id "
            "WHERE ft.file_id = f.id AND t.name = ?)"
        )
        parameters.append(tag)
    if text:
        clauses.append(
            "(f.path LIKE ? ESCAPE '\\' OR COALESCE(f.summary, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(f.text_excerpt, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(f.category, '') LIKE ? ESCAPE '\\')"
        )
        escaped_text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped_text}%"
        parameters.extend([like] * 4)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    rows = connection.execute(
        f"SELECT f.* FROM files f {where} ORDER BY f.mtime_ns DESC, f.path LIMIT ?",
        parameters,
    ).fetchall()
    return [FileRecord.model_validate(dict(row)) for row in rows]


def rows_as_dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert SQLite rows to dictionaries."""
    return [dict(row) for row in rows]


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Raise if the connection's database is not at the expected schema version."""
    version = _current_version(connection)
    if version != SCHEMA_VERSION:
        raise DatabaseError(f"Expected schema version {SCHEMA_VERSION}, found {version}")


def insert_many_file_tags(
    connection: sqlite3.Connection,
    file_id: int,
    tags: Mapping[str, tuple[TagSource, float | None]],
) -> None:
    """Insert multiple tag associations in one transaction."""
    for name, (source, confidence) in tags.items():
        add_file_tag(
            connection,
            file_id,
            name,
            source=source,
            confidence=confidence,
        )


def replace_ai_tags(
    connection: sqlite3.Connection,
    file_id: int,
    tags: Mapping[str, float | None],
) -> None:
    """Replace AI tags for a file inside the caller's transaction."""
    connection.execute(
        "DELETE FROM file_tags WHERE file_id = ? AND source = ?",
        (file_id, TagSource.AI.value),
    )
    for name, confidence in tags.items():
        add_file_tag(
            connection,
            file_id,
            name,
            source=TagSource.AI,
            confidence=confidence,
        )


def upsert_embedding(
    connection: sqlite3.Connection,
    sha256: str,
    model: str,
    dimension: int,
    vector: bytes,
) -> None:
    """Store one float32 embedding vector inside the caller's transaction."""
    if not sha256:
        raise ValueError("sha256 must not be empty")
    if not model:
        raise ValueError("model must not be empty")
    if dimension < 1 or len(vector) != dimension * 4:
        raise ValueError("embedding dimension does not match vector length")
    connection.execute(
        "INSERT INTO embeddings(sha256, model, dimension, vector, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(sha256, model) DO UPDATE SET "
        "dimension = excluded.dimension, vector = excluded.vector, "
        "created_at = excluded.created_at",
        (sha256, model, dimension, vector, utc_now()),
    )


def get_embedding(
    connection: sqlite3.Connection,
    sha256: str,
    model: str,
) -> sqlite3.Row | None:
    """Return a stored embedding row for content and model."""
    return connection.execute(
        "SELECT * FROM embeddings WHERE sha256 = ? AND model = ?",
        (sha256, model),
    ).fetchone()


def embedding_stats(connection: sqlite3.Connection) -> dict[str, int]:
    """Return embedding counts by model plus a total."""
    counts: dict[str, int] = {}
    for row in connection.execute("SELECT model, COUNT(*) AS count FROM embeddings GROUP BY model"):
        counts[str(row["model"])] = int(row["count"])
    counts["total"] = sum(value for key, value in counts.items() if key != "total")
    return counts


def analysis_stats(connection: sqlite3.Connection) -> dict[str, int]:
    """Return indexed-file counts by analysis provider."""
    counts: dict[str, int] = {}
    for row in connection.execute(
        "SELECT COALESCE(analysis_provider, 'none') AS provider, COUNT(*) AS count "
        "FROM files WHERE status = 'done' GROUP BY provider"
    ):
        counts[str(row["provider"])] = int(row["count"])
    return counts


def utc_after(seconds: float) -> str:
    """Return an ISO-8601 UTC timestamp ``seconds`` from now, rounded to seconds."""
    return (datetime.now(tz=UTC) + timedelta(seconds=max(0.0, seconds))).isoformat(
        timespec="seconds"
    )


def enqueue_job(
    connection: sqlite3.Connection,
    path: str,
    job_type: JobType | str,
    *,
    file_id: int | None = None,
    max_attempts: int = 3,
    next_run_at: str | None = None,
) -> int:
    """Insert or coalesce an active job for ``(path, type)``.

    A change arriving while the same job is running sets ``rerun_requested`` so
    the worker reschedules it on completion instead of losing the event.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    type_value = JobType(job_type).value
    now = utc_now()
    run_at = next_run_at or now
    with transaction(connection):
        cursor = connection.execute(
            "INSERT OR IGNORE INTO jobs("
            "path, type, file_id, state, attempts, max_attempts, next_run_at, "
            "created_at, updated_at) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
            (path, type_value, file_id, max_attempts, run_at, now, now),
        )
        if cursor.rowcount:
            return int(cursor.lastrowid)
        connection.execute(
            "UPDATE jobs SET "
            "rerun_requested = CASE WHEN state = 'running' THEN 1 ELSE rerun_requested END, "
            "state = CASE WHEN state = 'retry_scheduled' THEN 'queued' ELSE state END, "
            "next_run_at = CASE WHEN state = 'retry_scheduled' THEN ? ELSE next_run_at END, "
            "file_id = COALESCE(file_id, ?), "
            "updated_at = ? "
            "WHERE path = ? AND type = ? "
            "AND state IN ('queued','running','retry_scheduled')",
            (now, file_id, now, path, type_value),
        )
        row = connection.execute(
            "SELECT id FROM jobs WHERE path = ? AND type = ? "
            "AND state IN ('queued','running','retry_scheduled') LIMIT 1",
            (path, type_value),
        ).fetchone()
    if row is None:
        raise DatabaseError(f"Could not enqueue {type_value} job for {path}")
    return int(row["id"])


def claim_job(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
) -> JobRecord | None:
    """Atomically claim the oldest due job and mark it running."""
    timestamp = now or utc_now()
    row = connection.execute(
        "UPDATE jobs SET state = 'running', attempts = attempts + 1, updated_at = ? "
        "WHERE id = ("
        "  SELECT id FROM jobs "
        "  WHERE state IN ('queued','retry_scheduled') "
        "    AND next_run_at <= ? AND cancel_requested = 0 "
        "  ORDER BY next_run_at, id LIMIT 1"
        ") AND state IN ('queued','retry_scheduled') "
        "RETURNING *",
        (timestamp, timestamp),
    ).fetchone()
    return JobRecord.model_validate(dict(row)) if row else None


def get_job(connection: sqlite3.Connection, job_id: int) -> JobRecord | None:
    """Fetch a job by id."""
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return JobRecord.model_validate(dict(row)) if row else None


def is_job_cancelled(connection: sqlite3.Connection, job_id: int) -> bool:
    """Check the persistent cancellation flag for a job."""
    row = connection.execute(
        "SELECT state, cancel_requested FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return True
    return bool(row["cancel_requested"]) or row["state"] == JobState.CANCELLED.value


def complete_job(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    requeue: bool = False,
) -> bool:
    """Mark a running job done, or requeue it when a rerun was requested."""
    now = utc_now()
    if requeue:
        row = connection.execute(
            "UPDATE jobs SET state = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END, "
            "attempts = 0, rerun_requested = 0, last_error = NULL, "
            "next_run_at = ?, updated_at = ? WHERE id = ? AND state = 'running' RETURNING *",
            (now, now, job_id),
        ).fetchone()
    else:
        row = connection.execute(
            "UPDATE jobs SET state = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'done' END, "
            "rerun_requested = 0, last_error = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'running' RETURNING *",
            (now, job_id),
        ).fetchone()
    return row is not None


def fail_job(
    connection: sqlite3.Connection,
    job_id: int,
    error: str,
    *,
    retryable: bool = True,
    base_delay_seconds: float = 2.0,
    max_delay_seconds: float = 60.0,
    jitter_seconds: float = 1.0,
) -> JobRecord | None:
    """Record a failure and either schedule a retry or mark the job failed."""
    with transaction(connection):
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])
        should_retry = (
            retryable
            and attempts < max_attempts
            and not bool(row["cancel_requested"])
            and row["state"] == JobState.RUNNING.value
        )
        now = utc_now()
        if should_retry:
            delay = min(max_delay_seconds, base_delay_seconds * (2 ** max(0, attempts - 1)))
            if jitter_seconds > 0:
                delay += random.uniform(0.0, jitter_seconds)
            state = JobState.RETRY_SCHEDULED.value
            next_run = utc_after(delay)
        else:
            state = (
                JobState.CANCELLED.value if bool(row["cancel_requested"]) else JobState.FAILED.value
            )
            next_run = str(row["next_run_at"])
        updated = connection.execute(
            "UPDATE jobs SET state = ?, next_run_at = ?, last_error = ?, updated_at = ? "
            "WHERE id = ? RETURNING *",
            (state, next_run, error[:1000], now, job_id),
        ).fetchone()
    return JobRecord.model_validate(dict(updated)) if updated else None


def request_job_cancel(connection: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a queued job or request cancellation of a running job."""
    now = utc_now()
    with transaction(connection):
        row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return False
        if row["state"] in {
            JobState.QUEUED.value,
            JobState.RETRY_SCHEDULED.value,
        }:
            cursor = connection.execute(
                "UPDATE jobs SET state = 'cancelled', cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND state IN ('queued','retry_scheduled')",
                (now, job_id),
            )
            return cursor.rowcount > 0
        if row["state"] == JobState.RUNNING.value:
            cursor = connection.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND state = 'running'",
                (now, job_id),
            )
            return cursor.rowcount > 0
    return False


def reset_running_jobs(connection: sqlite3.Connection) -> int:
    """Recover jobs left running by a crashed or stopped worker."""
    now = utc_now()
    cursor = connection.execute(
        "UPDATE jobs SET "
        "state = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END, "
        "next_run_at = ?, rerun_requested = 0, "
        "last_error = COALESCE(last_error, 'worker stopped before completion'), "
        "updated_at = ? WHERE state = 'running'",
        (now, now),
    )
    return cursor.rowcount


def requeue_job(connection: sqlite3.Connection, job_id: int) -> bool:
    """Return a claimed-but-not-started job to the queue during shutdown."""
    now = utc_now()
    cursor = connection.execute(
        "UPDATE jobs SET state = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END, "
        "next_run_at = ?, rerun_requested = 0, updated_at = ? "
        "WHERE id = ? AND state = 'running'",
        (now, now, job_id),
    )
    return cursor.rowcount > 0


def set_job_file_id(connection: sqlite3.Connection, job_id: int, file_id: int) -> None:
    """Associate a processed path with its indexed file row."""
    connection.execute(
        "UPDATE jobs SET file_id = ?, updated_at = ? WHERE id = ?",
        (file_id, utc_now(), job_id),
    )


def job_stats(connection: sqlite3.Connection) -> dict[str, int]:
    """Return job counts by state, plus a ``total`` count."""
    counts = {state.value: 0 for state in JobState}
    for row in connection.execute("SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"):
        counts[str(row["state"])] = int(row["count"])
    counts["total"] = sum(counts[state.value] for state in JobState)
    return counts


def list_jobs(
    connection: sqlite3.Connection,
    *,
    state: JobState | str | None = None,
    limit: int = 50,
) -> list[JobRecord]:
    """List recent jobs, optionally filtered by state."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if state is None:
        rows = connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY id DESC LIMIT ?",
            (JobState(state).value, limit),
        ).fetchall()
    return [JobRecord.model_validate(dict(row)) for row in rows]


def purge_finished_jobs(connection: sqlite3.Connection, older_than_days: int = 7) -> int:
    """Delete completed/failed/cancelled jobs older than the retention window."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    cutoff = (datetime.now(tz=UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    cursor = connection.execute(
        "DELETE FROM jobs WHERE state IN ('done','failed','cancelled') AND updated_at < ?",
        (cutoff,),
    )
    return cursor.rowcount


def save_collection(
    connection: sqlite3.Connection,
    name: str,
    *,
    query: str = "",
    tags: Sequence[str] = (),
    category: str | None = None,
    mode: str = "auto",
) -> Collection:
    """Create or replace a named saved search."""
    cleaned = " ".join((name or "").replace("\x00", " ").split())
    if not cleaned:
        raise ValueError("collection name must not be empty")
    if len(cleaned) > 80:
        raise ValueError("collection name must be at most 80 characters")
    if mode not in {"auto", "text", "semantic", "hybrid"}:
        raise ValueError(f"unknown collection search mode: {mode}")
    normalized_tags = []
    for tag in tags:
        value = " ".join(str(tag).replace("\x00", " ").split())
        if value and value not in normalized_tags:
            normalized_tags.append(value)
    tags_json = json.dumps(normalized_tags, ensure_ascii=False)
    now = utc_now()
    connection.execute(
        "INSERT INTO collections(name, query, tags_json, category, mode, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET query = excluded.query, "
        "tags_json = excluded.tags_json, category = excluded.category, "
        "mode = excluded.mode, updated_at = excluded.updated_at",
        (cleaned, (query or "").replace("\x00", " "), tags_json, category, mode, now, now),
    )
    collection = get_collection(connection, cleaned)
    if collection is None:
        raise DatabaseError(f"Failed to save collection {cleaned!r}")
    return collection


def get_collection(connection: sqlite3.Connection, name: str) -> Collection | None:
    """Fetch one collection by its case-insensitive name."""
    row = connection.execute(
        "SELECT * FROM collections WHERE name = ? COLLATE NOCASE",
        (" ".join((name or "").split()),),
    ).fetchone()
    return _collection_from_row(row) if row is not None else None


def list_collections(connection: sqlite3.Connection) -> list[Collection]:
    """List saved searches ordered by name."""
    rows = connection.execute("SELECT * FROM collections ORDER BY name COLLATE NOCASE").fetchall()
    return [_collection_from_row(row) for row in rows]


def delete_collection(connection: sqlite3.Connection, name: str) -> bool:
    """Delete a saved search by name and report whether it existed."""
    cursor = connection.execute(
        "DELETE FROM collections WHERE name = ? COLLATE NOCASE",
        (" ".join((name or "").split()),),
    )
    return cursor.rowcount > 0


def _collection_from_row(row: sqlite3.Row) -> Collection:
    tags: list[str] = []
    try:
        loaded = json.loads(row["tags_json"])
        if isinstance(loaded, list):
            tags = [str(value) for value in loaded if str(value).strip()]
    except (TypeError, ValueError):
        tags = []
    data = dict(row)
    data["tags"] = tags
    return Collection.model_validate(data)


def duplicate_groups(connection: sqlite3.Connection, *, limit: int = 100) -> list[DuplicateGroup]:
    """Return groups of indexed files with identical SHA-256 content."""
    if limit < 1:
        raise ValueError("limit must be positive")
    groups = connection.execute(
        "SELECT sha256, MAX(size) AS size, COUNT(*) AS copies FROM files "
        "WHERE status = 'done' AND sha256 IS NOT NULL AND sha256 != '' "
        "GROUP BY sha256 HAVING COUNT(*) > 1 "
        "ORDER BY copies DESC, size DESC, sha256 LIMIT ?",
        (limit,),
    ).fetchall()
    results: list[DuplicateGroup] = []
    for group in groups:
        rows = connection.execute(
            "SELECT * FROM files WHERE sha256 = ? AND status = 'done' ORDER BY path COLLATE NOCASE",
            (str(group["sha256"]),),
        ).fetchall()
        results.append(
            DuplicateGroup(
                sha256=str(group["sha256"]),
                size=int(group["size"] or 0),
                files=[FileRecord.model_validate(dict(row)) for row in rows],
            )
        )
    return results


def tag_overview(connection: sqlite3.Connection) -> list[TagStat]:
    """Return all tags with distinct file and provenance counts."""
    rows = connection.execute(
        "SELECT t.name, COUNT(DISTINCT ft.file_id) AS file_count, "
        "COUNT(DISTINCT CASE WHEN ft.source = 'ai' THEN ft.file_id END) AS ai_count, "
        "COUNT(DISTINCT CASE WHEN ft.source = 'user' THEN ft.file_id END) AS user_count "
        "FROM tags t LEFT JOIN file_tags ft ON ft.tag_id = t.id "
        "GROUP BY t.id ORDER BY file_count DESC, t.name COLLATE NOCASE"
    ).fetchall()
    return [
        TagStat(
            name=str(row["name"]),
            file_count=int(row["file_count"]),
            ai_count=int(row["ai_count"]),
            user_count=int(row["user_count"]),
        )
        for row in rows
    ]


def list_backups(destination_dir: str | Path) -> list[Path]:
    """Return SQLite backups newest first."""
    directory = Path(destination_dir).expanduser()
    if not directory.is_dir():
        return []
    return sorted(
        directory.glob("*.db"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )


def prune_backups(
    destination_dir: str | Path,
    *,
    keep: int = 10,
    prefix: str | None = None,
) -> list[Path]:
    """Delete old backups beyond ``keep`` and return the removed paths."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    directory = Path(destination_dir).expanduser()
    pattern = f"{prefix}-*.db" if prefix else "*.db"
    if not directory.is_dir():
        return []
    backups = sorted(
        directory.glob(pattern),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    removed: list[Path] = []
    for obsolete in backups[keep:]:
        try:
            obsolete.unlink()
        except OSError as exc:
            LOGGER.warning(
                "Could not prune backup",
                extra={"event": "backup.prune_failed", "path": str(obsolete), "error": str(exc)},
            )
            continue
        removed.append(obsolete)
    return removed

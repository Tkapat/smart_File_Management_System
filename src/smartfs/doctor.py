"""Index diagnostics and safe repair operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from smartfs import db
from smartfs.config import AppPaths, Settings
from smartfs.models import Category, FileRecord
from smartfs.tags import sync_file_xattrs
from smartfs.xattr_store import probe_xattr_support


@dataclass(slots=True)
class DoctorIssue:
    """One diagnostic finding."""

    severity: str
    code: str
    message: str
    repaired: bool = False


@dataclass(slots=True)
class DoctorReport:
    """Diagnostics, repair actions, and informational counters."""

    issues: list[DoctorIssue] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    backup: Path | None = None

    @property
    def errors(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error" and not issue.repaired)

    @property
    def healthy(self) -> bool:
        return self.errors == 0


def _add(report: DoctorReport, severity: str, code: str, message: str) -> None:
    report.issues.append(DoctorIssue(severity, code, message))


def _mark_repaired(report: DoctorReport, code: str) -> None:
    for issue in report.issues:
        if issue.code == code:
            issue.repaired = True


def run_doctor(
    paths: AppPaths,
    settings: Settings,
    *,
    repair: bool = False,
    vacuum: bool = False,
    backup_keep: int = 5,
) -> DoctorReport:
    """Inspect the index and, when requested, safely repair derived data."""
    report = DoctorReport()
    if not paths.db_path.exists():
        raise FileNotFoundError(f"Database not found at {paths.db_path}")
    db.init_database(paths.db_path, backup_dir=paths.backup_dir, backup_keep=backup_keep)
    if repair:
        report.backup = db.backup_database(
            paths.db_path,
            paths.backup_dir,
            keep=backup_keep,
            prefix="pre-repair",
        )

    write_needed = repair or vacuum
    connection = db.connect(paths.db_path, read_only=not write_needed)
    try:
        _check_integrity(connection, report)
        _check_schema(connection, report)
        _check_files(connection, report)
        _check_categories(connection, report, repair)
        _check_fts(connection, report, repair)
        _check_embeddings(connection, report, repair)
        _check_tags(connection, report, repair)
        _check_xattrs(report, settings)
        _check_jobs(connection, report)
        if repair:
            _repair_xattrs(connection, report, settings)
    finally:
        connection.close()

    if vacuum:
        connection = db.connect(paths.db_path)
        try:
            connection.execute("VACUUM")
            report.repaired.append("vacuum: database compacted")
        finally:
            connection.close()

    return report


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _check_integrity(connection: sqlite3.Connection, report: DoctorReport) -> None:
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
        result = str(row[0]) if row else "unknown"
    except sqlite3.Error as exc:
        _add(report, "error", "sqlite.integrity", f"quick_check failed: {exc}")
        return
    report.counts["quick_check"] = 0 if result == "ok" else 1
    if result != "ok":
        _add(report, "error", "sqlite.integrity", f"quick_check reported: {result}")
    try:
        rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        _add(report, "error", "sqlite.foreign_keys", f"foreign_key_check failed: {exc}")
        return
    if rows:
        _add(
            report,
            "error",
            "sqlite.foreign_keys",
            f"{len(rows)} foreign-key violation(s)",
        )


def _check_schema(connection: sqlite3.Connection, report: DoctorReport) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    report.counts["schema_version"] = version
    if version != db.SCHEMA_VERSION:
        _add(
            report,
            "error",
            "schema.version",
            f"schema version {version}; expected {db.SCHEMA_VERSION}",
        )


def _check_fts(
    connection: sqlite3.Connection,
    report: DoctorReport,
    repair: bool,
) -> None:
    if not _table_exists(connection, "files_fts"):
        _add(report, "error", "fts.missing", "FTS5 table files_fts is missing")
        if repair:
            with db.transaction(connection):
                connection.execute(
                    "CREATE VIRTUAL TABLE files_fts USING fts5("
                    "path, title, author, category, summary, body, "
                    "tokenize = 'unicode61 remove_diacritics 2')"
                )
            report.repaired.append("fts: created files_fts table")
            _mark_repaired(report, "fts.missing")
            _rebuild_fts(connection, report)
        return
    indexed = int(
        connection.execute(
            "SELECT COUNT(*) FROM files_fts JOIN files f ON f.id = files_fts.rowid "
            "WHERE f.status = 'done'"
        ).fetchone()[0]
    )
    missing = int(
        connection.execute(
            "SELECT COUNT(*) FROM files f LEFT JOIN files_fts fts ON fts.rowid = f.id "
            "WHERE f.status = 'done' AND fts.rowid IS NULL"
        ).fetchone()[0]
    )
    orphans = int(
        connection.execute(
            "SELECT COUNT(*) FROM files_fts LEFT JOIN files f ON f.id = files_fts.rowid "
            "WHERE f.id IS NULL"
        ).fetchone()[0]
    )
    report.counts["fts_documents"] = indexed
    report.counts["fts_missing"] = missing
    report.counts["fts_orphans"] = orphans
    if missing:
        _add(report, "error", "fts.missing_documents", f"{missing} done file(s) lack FTS rows")
    if orphans:
        _add(report, "warning", "fts.orphans", f"{orphans} orphan FTS row(s)")
    if not repair:
        return
    _rebuild_fts(connection, report)


def _rebuild_fts(connection: sqlite3.Connection, report: DoctorReport) -> None:
    rows = connection.execute(
        "SELECT f.*, (SELECT c.text FROM content_cache c WHERE c.sha256 = f.sha256 "
        "ORDER BY c.extraction_version DESC LIMIT 1) AS cached_text "
        "FROM files f WHERE f.status = 'done'"
    ).fetchall()
    count = 0
    with db.transaction(connection):
        for row in rows:
            record = FileRecord.model_validate(dict(row))
            if record.id is None:
                continue
            db.upsert_search_document(
                connection,
                record.id,
                path=record.path,
                title=record.title,
                author=record.author,
                category=record.category,
                summary=record.summary,
                body=row["cached_text"] or record.text_excerpt or "",
            )
            count += 1
        connection.execute("DELETE FROM files_fts WHERE rowid NOT IN (SELECT id FROM files)")
    report.repaired.append(f"fts: rebuilt {count} document(s)")
    _mark_repaired(report, "fts.missing_documents")
    _mark_repaired(report, "fts.orphans")


def _check_embeddings(connection: sqlite3.Connection, report: DoctorReport, repair: bool) -> None:
    total = int(connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
    invalid = connection.execute(
        "SELECT sha256, model FROM embeddings "
        "WHERE dimension < 1 OR dimension > 65536 OR length(vector) != dimension * 4"
    ).fetchall()
    report.counts["embeddings"] = total
    report.counts["embeddings_invalid"] = len(invalid)
    if invalid:
        _add(
            report,
            "error",
            "embeddings.invalid",
            f"{len(invalid)} embedding blob(s) have invalid dimensions",
        )
        if repair:
            with db.transaction(connection):
                for row in invalid:
                    connection.execute(
                        "DELETE FROM embeddings WHERE sha256 = ? AND model = ?",
                        (row["sha256"], row["model"]),
                    )
            report.repaired.append(f"embeddings: removed {len(invalid)} invalid row(s)")
            _mark_repaired(report, "embeddings.invalid")


def _check_tags(connection: sqlite3.Connection, report: DoctorReport, repair: bool) -> None:
    orphan_rows = connection.execute(
        "SELECT t.id, t.name FROM tags t WHERE NOT EXISTS "
        "(SELECT 1 FROM file_tags ft WHERE ft.tag_id = t.id)"
    ).fetchall()
    report.counts["orphan_tags"] = len(orphan_rows)
    if orphan_rows:
        _add(report, "warning", "tags.orphans", f"{len(orphan_rows)} tag(s) have no file links")
        if repair:
            with db.transaction(connection):
                for row in orphan_rows:
                    connection.execute("DELETE FROM tags WHERE id = ?", (int(row["id"]),))
            report.repaired.append(f"tags: removed {len(orphan_rows)} orphan tag(s)")
            _mark_repaired(report, "tags.orphans")


def _check_files(connection: sqlite3.Connection, report: DoctorReport) -> None:
    counts = db.status_counts(connection)
    report.counts.update(counts)
    if counts.get("missing", 0):
        _add(
            report,
            "warning",
            "files.missing",
            f"{counts['missing']} file(s) are marked missing; run a scan to reconcile",
        )


def _check_xattrs(report: DoctorReport, settings: Settings) -> None:
    if not settings.xattr_enabled:
        report.warnings.append("xattrs: disabled in config; SQLite is authoritative")
        return
    for root in settings.watched_dirs:
        if not Path(root).is_dir():
            continue
        support = probe_xattr_support(root)
        if not support.supported:
            report.warnings.append(
                f"xattrs: unsupported at {root} ({support.reason or 'unknown reason'})"
            )
            _add(report, "warning", "xattr.unsupported", f"xattrs unsupported at {root}")


def _repair_xattrs(
    connection: sqlite3.Connection,
    report: DoctorReport,
    settings: Settings,
) -> None:
    if not settings.xattr_enabled:
        return
    rows = connection.execute("SELECT id FROM files WHERE status = 'done'").fetchall()
    synced = 0
    unsupported = 0
    for row in rows:
        result = sync_file_xattrs(
            connection,
            int(row["id"]),
            suppression_ttl=settings.self_write_suppression_seconds,
        )
        if result.supported:
            synced += 1
        else:
            unsupported += 1
    report.repaired.append(f"xattrs: re-synced {synced} file(s)")
    if unsupported:
        report.warnings.append(f"xattrs: {unsupported} file(s) use DB-only metadata")


def _check_categories(connection: sqlite3.Connection, report: DoctorReport, repair: bool) -> None:
    allowed = {category.value for category in Category}
    rows = connection.execute(
        "SELECT id, category FROM files WHERE category IS NOT NULL AND category != ''"
    ).fetchall()
    invalid = [
        (int(row["id"]), str(row["category"])) for row in rows if row["category"] not in allowed
    ]
    if invalid:
        _add(report, "warning", "files.category", f"{len(invalid)} file(s) have unknown categories")
        if repair:
            with db.transaction(connection):
                for file_id, _category in invalid:
                    connection.execute(
                        "UPDATE files SET category = 'other' WHERE id = ?", (file_id,)
                    )
            report.repaired.append(f"categories: normalized {len(invalid)} file(s) to 'other'")
            _mark_repaired(report, "files.category")


def _check_jobs(connection: sqlite3.Connection, report: DoctorReport) -> None:
    rows = connection.execute("SELECT state, COUNT(*) AS count FROM jobs GROUP BY state").fetchall()
    for row in rows:
        report.counts[f"jobs_{row['state']}"] = int(row["count"])
    if report.counts.get("jobs_running", 0):
        _add(
            report,
            "warning",
            "jobs.running",
            f"{report.counts['jobs_running']} job(s) marked running; daemon recovery requeues them",
        )

"""Tag normalization, mutation, provenance, and xattr synchronization."""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass

from smartfs import db
from smartfs.models import TagSource, XattrMetadata
from smartfs.xattr_store import (
    XattrOperationError,
    XattrUnsupported,
    XattrWriteResult,
    write_metadata,
)

TAG_MAX_CHARS = 64
_ALLOWED_SYMBOLS = {"-", "_", "+", "#", ".", "'"}


class TagError(ValueError):
    """Raised when a tag cannot be normalized or mutated safely."""


@dataclass(slots=True)
class TagSyncResult:
    """Result of mirroring a file's database metadata to xattrs."""

    file_id: int
    path: str
    tags: list[str]
    supported: bool
    message: str | None = None
    write_result: XattrWriteResult | None = None


@dataclass(slots=True)
class TagMutationResult:
    """Result of a tag mutation plus xattr mirror status."""

    file_id: int
    path: str
    tags: list[str]
    sync: TagSyncResult


def normalize_tag(value: str, *, max_words: int | None = None) -> str:
    """Normalize a tag: Unicode NFKC, lowercase, whitespace collapse, charset check."""
    if not isinstance(value, str):
        raise TagError("tag must be a string")
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        raise TagError("tag must not be empty")
    if "/" in text or "\\" in text:
        raise TagError("tag must not contain path separators")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise TagError("tag must not contain control characters")
    lowered = text.casefold()
    cleaned_chars: list[str] = []
    for character in lowered:
        if character.isspace():
            cleaned_chars.append(" ")
        elif character.isalnum() or character in _ALLOWED_SYMBOLS:
            cleaned_chars.append(character)
        else:
            raise TagError(f"tag contains a disallowed character: {character!r}")
    normalized = " ".join("".join(cleaned_chars).split())
    if not normalized or set(normalized) <= {"."}:
        raise TagError("tag must contain letters or digits")
    if len(normalized) > TAG_MAX_CHARS:
        raise TagError(f"tag must be at most {TAG_MAX_CHARS} characters")
    if max_words is not None and len(normalized.split()) > max_words:
        raise TagError(f"tag must contain at most {max_words} words")
    return normalized


def normalize_tags(values: list[str], *, max_words: int | None = None) -> list[str]:
    """Normalize and deduplicate tags while preserving order."""
    result: list[str] = []
    for value in values:
        normalized = normalize_tag(value, max_words=max_words)
        if normalized not in result:
            result.append(normalized)
    return result


def sync_file_xattrs(
    connection: sqlite3.Connection,
    file_id: int,
    *,
    suppression_ttl: float | None = None,
) -> TagSyncResult:
    """Mirror committed database metadata to a file's xattrs.

    Call this only after the database transaction has committed. Unsupported
    filesystems are reported, never raised to crash workers or the CLI.
    """
    record = db.get_file_by_id(connection, file_id)
    if record is None:
        return TagSyncResult(file_id, "", [], False, "file is not indexed")
    tags = db.tag_union(connection, file_id)
    metadata = XattrMetadata(
        category=record.category,
        tags=tags,
        summary=record.summary,
        sha256=record.sha256,
        version=str(record.index_version),
    )
    try:
        write_result = write_metadata(record.path, metadata, suppression_ttl=suppression_ttl)
    except XattrUnsupported as exc:
        return TagSyncResult(file_id, record.path, tags, False, str(exc))
    except XattrOperationError as exc:
        return TagSyncResult(file_id, record.path, tags, False, str(exc))
    return TagSyncResult(file_id, record.path, tags, True, None, write_result)


def add_tag(
    connection: sqlite3.Connection,
    file_id: int,
    tag: str,
    *,
    source: TagSource | str = TagSource.USER,
    confidence: float | None = None,
    suppression_ttl: float | None = None,
) -> TagMutationResult:
    """Add a normalized tag and re-sync xattrs after commit."""
    normalized = normalize_tag(tag)
    record = db.get_file_by_id(connection, file_id)
    if record is None:
        raise TagError("file is not indexed")
    with db.transaction(connection):
        db.add_file_tag(
            connection,
            file_id,
            normalized,
            source=source,
            confidence=confidence,
        )
    sync = sync_file_xattrs(connection, file_id, suppression_ttl=suppression_ttl)
    return TagMutationResult(file_id, record.path, sync.tags, sync)


def remove_tag(
    connection: sqlite3.Connection,
    file_id: int,
    tag: str,
    *,
    source: TagSource | str | None = None,
    suppression_ttl: float | None = None,
) -> TagMutationResult:
    """Remove a tag association and re-sync xattrs after commit."""
    normalized = normalize_tag(tag)
    record = db.get_file_by_id(connection, file_id)
    if record is None:
        raise TagError("file is not indexed")
    with db.transaction(connection):
        removed = db.remove_file_tag(connection, file_id, normalized, source=source)
    if removed == 0:
        raise TagError(f"tag {normalized!r} is not attached to this file")
    sync = sync_file_xattrs(connection, file_id, suppression_ttl=suppression_ttl)
    return TagMutationResult(file_id, record.path, sync.tags, sync)


def rename_tag(
    connection: sqlite3.Connection,
    old: str,
    new: str,
    *,
    suppression_ttl: float | None = None,
) -> list[TagSyncResult]:
    """Rename a tag globally; refuse when the target already exists."""
    old_name = normalize_tag(old)
    new_name = normalize_tag(new)
    if old_name == new_name:
        return []
    with db.transaction(connection):
        old_row = connection.execute("SELECT id FROM tags WHERE name = ?", (old_name,)).fetchone()
        if old_row is None:
            raise TagError(f"tag {old_name!r} does not exist")
        target = connection.execute("SELECT id FROM tags WHERE name = ?", (new_name,)).fetchone()
        if target is not None:
            raise TagError(f"tag {new_name!r} already exists; use merge")
        file_ids = [
            int(row["file_id"])
            for row in connection.execute(
                "SELECT DISTINCT file_id FROM file_tags WHERE tag_id = ?",
                (int(old_row["id"]),),
            )
        ]
        connection.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, int(old_row["id"])))
    return [
        sync_file_xattrs(connection, file_id, suppression_ttl=suppression_ttl)
        for file_id in file_ids
    ]


def merge_tags(
    connection: sqlite3.Connection,
    source: str,
    target: str,
    *,
    suppression_ttl: float | None = None,
) -> list[TagSyncResult]:
    """Merge ``source`` into ``target`` preserving user and AI provenance."""
    source_name = normalize_tag(source)
    target_name = normalize_tag(target)
    if source_name == target_name:
        return []
    with db.transaction(connection):
        source_row = connection.execute(
            "SELECT id FROM tags WHERE name = ?", (source_name,)
        ).fetchone()
        if source_row is None:
            raise TagError(f"tag {source_name!r} does not exist")
        source_id = int(source_row["id"])
        target_id = db.get_or_create_tag(connection, target_name)
        associations = connection.execute(
            "SELECT file_id, source, confidence, created_at FROM file_tags WHERE tag_id = ?",
            (source_id,),
        ).fetchall()
        file_ids: set[int] = set()
        for row in associations:
            file_id = int(row["file_id"])
            file_ids.add(file_id)
            connection.execute(
                "INSERT INTO file_tags(file_id, tag_id, source, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(file_id, tag_id, source) DO UPDATE SET "
                "confidence = COALESCE(file_tags.confidence, excluded.confidence)",
                (
                    file_id,
                    target_id,
                    row["source"],
                    row["confidence"],
                    row["created_at"],
                ),
            )
        connection.execute("DELETE FROM file_tags WHERE tag_id = ?", (source_id,))
        connection.execute("DELETE FROM tags WHERE id = ?", (source_id,))
    return [
        sync_file_xattrs(connection, file_id, suppression_ttl=suppression_ttl)
        for file_id in sorted(file_ids)
    ]


def tag_counts(connection: sqlite3.Connection, limit: int = 50) -> list[tuple[str, int]]:
    """Return most frequently used tags and their file counts."""
    rows = connection.execute(
        "SELECT t.name, COUNT(DISTINCT ft.file_id) AS count FROM tags t "
        "JOIN file_tags ft ON ft.tag_id = t.id GROUP BY t.id "
        "ORDER BY count DESC, t.name LIMIT ?",
        (limit,),
    ).fetchall()
    return [(str(row["name"]), int(row["count"])) for row in rows]

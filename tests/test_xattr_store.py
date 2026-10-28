from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import pytest

from smartfs import db
from smartfs.config import Settings
from smartfs.models import TagSource, XattrMetadata
from smartfs.scanner import scan_directory
from smartfs.tags import add_tag, remove_tag
from smartfs.xattr_store import (
    SUMMARY_MAX_BYTES,
    read_metadata,
    remove_metadata,
    supports_xattr,
    truncate_utf8,
    write_metadata,
)


def _require_xattr(path: Path) -> None:
    if not supports_xattr(path):
        pytest.skip("filesystem does not support user extended attributes")


def test_truncate_utf8_never_splits_multibyte() -> None:
    value = "a" * 511 + "é"
    truncated = truncate_utf8(value, 512)
    assert truncated == "a" * 511
    assert len(truncated.encode("utf-8")) == 511

    value = "é" * 300
    truncated = truncate_utf8(value, 512)
    assert truncated == "é" * 256
    assert len(truncated.encode("utf-8")) == 512


def test_xattr_roundtrip(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("content", encoding="utf-8")
    _require_xattr(file_path)
    before_mtime = file_path.stat().st_mtime_ns
    metadata = XattrMetadata(
        category="lecture notes",
        tags=["physics", "thermodynamics"],
        summary="é" * 300,
        sha256="a" * 64,
        version="1",
    )
    result = write_metadata(file_path, metadata)
    assert result.changed
    assert file_path.stat().st_mtime_ns == before_mtime
    assert os.getxattr(file_path, "user.smartfs.category") == b"lecture notes"
    assert os.getxattr(file_path, "user.smartfs.tags") == b"physics,thermodynamics"
    raw_summary = os.getxattr(file_path, "user.smartfs.summary")
    assert len(raw_summary) <= SUMMARY_MAX_BYTES
    assert raw_summary.decode("utf-8") == "é" * 256
    loaded = read_metadata(file_path)
    assert loaded.category == "lecture notes"
    assert loaded.tags == ["physics", "thermodynamics"]
    assert loaded.summary == "é" * 256
    assert loaded.sha256 == "a" * 64
    assert loaded.version == "1"
    remove_metadata(file_path)
    assert read_metadata(file_path).tags == []


def test_scanner_mirrors_and_restores_tags_after_atomic_replace(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    file_path = docs / "notes.txt"
    file_path.write_text("first version about entropy", encoding="utf-8")
    if not supports_xattr(file_path):
        pytest.skip("filesystem does not support user extended attributes")

    scan_directory(docs, connection, settings)
    record = db.get_file_by_path(connection, str(file_path))
    assert record is not None
    assert record.id is not None
    add_tag(connection, int(record.id), "physics")
    assert os.getxattr(file_path, "user.smartfs.tags") == b"physics"

    replacement = docs / ".replacement"
    replacement.write_text("second version about heat engines", encoding="utf-8")
    os.replace(replacement, file_path)
    with pytest.raises(OSError) as exc_info:
        os.getxattr(file_path, "user.smartfs.tags")
    assert exc_info.value.errno in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}

    scan_directory(docs, connection, settings)
    updated = db.get_file_by_path(connection, str(file_path))
    assert updated is not None
    assert updated.id == record.id
    assert updated.sha256 != record.sha256
    assert db.tag_union(connection, int(record.id)) == ["physics"]
    assert os.getxattr(file_path, "user.smartfs.tags") == b"physics"


def test_remove_tag_updates_mirror(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("notes", encoding="utf-8")
    if not supports_xattr(file_path):
        pytest.skip("filesystem does not support user extended attributes")
    scan_directory(tmp_path, connection, settings)
    record = db.get_file_by_path(connection, str(file_path))
    assert record is not None and record.id is not None
    add_tag(connection, int(record.id), "semantic")
    remove_tag(connection, int(record.id), "semantic", source=TagSource.USER)
    with pytest.raises(OSError):
        os.getxattr(file_path, "user.smartfs.tags")

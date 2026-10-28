from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smartfs import db
from smartfs.config import Settings
from smartfs.embeddings import pack_embedding
from smartfs.models import TagSource
from smartfs.related import RelatedError, related_files


def _add_file(connection: sqlite3.Connection, path: Path, digest: str) -> int:
    path.write_text(digest, encoding="utf-8")
    return db.upsert_file(
        connection,
        str(path),
        status="done",
        sha256=digest * 64,
        size=10,
        mtime_ns=10,
    )


def test_related_by_shared_tags(
    tmp_path: Path, connection: sqlite3.Connection, settings: Settings
) -> None:
    with db.transaction(connection):
        source = _add_file(connection, tmp_path / "source.txt", "a")
        related = _add_file(connection, tmp_path / "related.txt", "b")
        unrelated = _add_file(connection, tmp_path / "unrelated.txt", "c")
        for tag in ("physics", "entropy"):
            db.add_file_tag(connection, source, tag, source=TagSource.USER)
        db.add_file_tag(connection, related, "physics", source=TagSource.AI)
        db.add_file_tag(connection, unrelated, "cooking", source=TagSource.USER)

    hits = related_files(connection, settings, source)
    assert [hit.record.id for hit in hits] == [related]
    assert hits[0].shared_tags == ["physics"]
    assert "shared tags: physics" in hits[0].reason
    assert hits[0].similarity is None


def test_related_by_local_embeddings(
    tmp_path: Path, connection: sqlite3.Connection, settings: Settings
) -> None:
    settings.embeddings_enabled = True
    settings.embedding_model = "test-model"
    with db.transaction(connection):
        source = _add_file(connection, tmp_path / "source.txt", "a")
        related = _add_file(connection, tmp_path / "related.txt", "b")
        far = _add_file(connection, tmp_path / "far.txt", "c")
        for file_id, vector in (
            (source, [1.0, 0.0]),
            (related, [0.9, 0.1]),
            (far, [0.0, 1.0]),
        ):
            record = db.get_file_by_id(connection, file_id)
            assert record is not None and record.sha256
            blob, dimension = pack_embedding(vector)
            db.upsert_embedding(connection, record.sha256, "test-model", dimension, blob)

    hits = related_files(connection, settings, source)
    assert [hit.record.id for hit in hits] == [related]
    assert hits[0].similarity is not None
    assert hits[0].similarity > 0.99
    assert "semantic cosine" in hits[0].reason


def test_related_rejects_unknown_file(connection: sqlite3.Connection, settings: Settings) -> None:
    with pytest.raises(RelatedError):
        related_files(connection, settings, 9999)

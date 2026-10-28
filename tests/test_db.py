from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smartfs import db
from smartfs.embeddings import pack_embedding, unpack_embedding
from smartfs.models import AnalysisResult, Category, TagSource
from smartfs.tags import TagError, merge_tags, normalize_tag


def test_migration_sets_version_and_backs_up(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()
    backup_dir = tmp_path / "backups"
    db.init_database(db_path, backup_dir=backup_dir)
    conn = db.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "files",
            "tags",
            "file_tags",
            "content_cache",
            "embeddings",
            "files_fts",
            "collections",
        } <= tables
    finally:
        conn.close()
    backups = list(backup_dir.glob("pre-migration-*.db"))
    assert len(backups) == 1


def test_tag_merge_preserves_source(tmp_path: Path, connection: sqlite3.Connection) -> None:
    file_path = tmp_path / "note.txt"
    file_path.write_text("content", encoding="utf-8")
    with db.transaction(connection):
        file_id = db.upsert_file(connection, str(file_path), status="done", sha256="a" * 64)
        db.add_file_tag(connection, file_id, "old", source=TagSource.USER)
    merge_tags(connection, "old", "new")
    rows = db.file_tags(connection, file_id)
    assert [(row["name"], row["source"]) for row in rows] == [("new", "user")]


def test_analysis_result_validation() -> None:
    valid = AnalysisResult(
        category=Category.LECTURE_NOTES,
        tags=["Physics", "thermodynamics", "exam prep"],
        summary="Entropy notes",
        topics=["physics"],
        confidence=0.8,
    )
    assert valid.category == Category.LECTURE_NOTES
    assert valid.tags == ["physics", "thermodynamics", "exam prep"]
    with pytest.raises(ValueError):
        AnalysisResult(
            category="hacked",
            tags=["a", "b", "c"],
            summary="x",
            confidence=0.9,
        )
    with pytest.raises(ValueError):
        AnalysisResult(
            category="report",
            tags=["one", "two"],
            summary="x",
            confidence=0.9,
        )
    with pytest.raises(ValueError):
        normalize_tag("../escape")
    assert normalize_tag("  Machine-Learning  ") == "machine-learning"


def test_tag_normalization() -> None:
    assert normalize_tag("  Lecture   Notes ") == "lecture notes"
    assert normalize_tag("C++") == "c++"
    with pytest.raises(TagError):
        normalize_tag("")
    with pytest.raises(TagError):
        normalize_tag("a" * 65)
    with pytest.raises(TagError):
        normalize_tag("hello\x00world")


def test_phase1_database_migrates_to_current_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "phase1.db"
    db.init_database(db_path, backup_dir=tmp_path / "backups")
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE jobs")
    raw.execute("DROP TABLE embeddings")
    raw.execute("DROP TABLE files_fts")
    raw.execute("DROP TABLE collections")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    db.init_database(db_path, backup_dir=tmp_path / "backups")
    conn = db.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"jobs", "embeddings", "files_fts"} <= tables
    finally:
        conn.close()
    assert list((tmp_path / "backups").glob("pre-migration-*.db"))


def test_analysis_tags_and_embeddings_helpers(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("notes", encoding="utf-8")
    blob, dimension = pack_embedding([0.25, -0.5, 1.0])
    with db.transaction(connection):
        file_id = db.upsert_file(
            connection,
            str(file_path),
            status="done",
            sha256="b" * 64,
            analysis_provider="local",
            analysis_model="heuristic-v1",
            category="lecture_notes",
            summary="Thermodynamics notes",
        )
        db.add_file_tag(connection, file_id, "keep", source=TagSource.USER)
        db.replace_ai_tags(connection, file_id, {"physics": 0.8, "entropy": 0.7})
        db.upsert_embedding(connection, "b" * 64, "test-model", dimension, blob)
        db.replace_ai_tags(connection, file_id, {"thermodynamics": 0.9})

    tags = db.file_tags(connection, file_id)
    assert [(row["name"], row["source"]) for row in tags] == [
        ("keep", "user"),
        ("thermodynamics", "ai"),
    ]
    stored = db.get_embedding(connection, "b" * 64, "test-model")
    assert stored is not None
    assert stored["dimension"] == dimension
    assert stored["vector"] == blob
    assert unpack_embedding(stored["vector"], dimension) == pytest.approx([0.25, -0.5, 1.0])
    assert db.analysis_stats(connection) == {"local": 1}
    assert db.embedding_stats(connection) == {"test-model": 1, "total": 1}


def test_fts_migration_backfills_existing_files(tmp_path: Path) -> None:
    db_path = tmp_path / "phase3.db"
    backups = tmp_path / "backups"
    db.init_database(db_path, backup_dir=backups)
    connection = db.connect(db_path)
    try:
        with db.transaction(connection):
            file_id = db.upsert_file(
                connection,
                str(tmp_path / "legacy.txt"),
                status="done",
                sha256="c" * 64,
                text_excerpt="BackfillTerm survives migration",
            )
        raw = sqlite3.connect(db_path)
        raw.execute("DROP TABLE files_fts")
        raw.execute("DROP TABLE collections")
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
        raw.close()
    finally:
        connection.close()

    db.init_database(db_path, backup_dir=backups)
    connection = db.connect(db_path)
    try:
        row = connection.execute(
            "SELECT rowid, path FROM files_fts WHERE files_fts MATCH ?",
            ("backfillterm",),
        ).fetchone()
        assert row is not None
        assert row["rowid"] == file_id
        assert row["path"].endswith("legacy.txt")
    finally:
        connection.close()


def test_collections_roundtrip(tmp_path: Path, connection: sqlite3.Connection) -> None:
    with db.transaction(connection):
        saved = db.save_collection(
            connection,
            " Physics Review ",
            query="entropy",
            tags=["physics", "exam", "physics"],
            category="lecture notes",
            mode="hybrid",
        )
    assert saved.id is not None
    assert saved.name == "Physics Review"
    assert saved.tags == ["physics", "exam"]

    with db.transaction(connection):
        updated = db.save_collection(
            connection,
            "physics review",
            query="thermodynamics",
            tags=["physics"],
            category=None,
            mode="text",
        )
    assert updated.id == saved.id
    assert updated.query == "thermodynamics"
    assert updated.mode == "text"
    assert db.list_collections(connection) == [updated]
    assert db.get_collection(connection, "PHYSICS REVIEW") == updated

    with db.transaction(connection):
        assert db.delete_collection(connection, "physics review") is True
        assert db.delete_collection(connection, "physics review") is False
    assert db.list_collections(connection) == []


def test_duplicate_groups_and_tag_overview(tmp_path: Path, connection: sqlite3.Connection) -> None:
    digest = "d" * 64
    with db.transaction(connection):
        first = db.upsert_file(
            connection,
            str(tmp_path / "a.txt"),
            status="done",
            sha256=digest,
            size=10,
        )
        second = db.upsert_file(
            connection,
            str(tmp_path / "b.txt"),
            status="done",
            sha256=digest,
            size=10,
        )
        db.upsert_file(
            connection,
            str(tmp_path / "unique.txt"),
            status="done",
            sha256="e" * 64,
            size=20,
        )
        db.add_file_tag(connection, first, "physics", source=TagSource.AI)
        db.add_file_tag(connection, first, "exam", source=TagSource.USER)
    groups = db.duplicate_groups(connection)
    assert len(groups) == 1
    assert groups[0].sha256 == digest
    assert {record.path for record in groups[0].files} == {
        str(tmp_path / "a.txt"),
        str(tmp_path / "b.txt"),
    }
    assert second in {record.id for record in groups[0].files}

    overview = {stat.name: stat for stat in db.tag_overview(connection)}
    assert overview["physics"].file_count == 1
    assert overview["physics"].ai_count == 1
    assert overview["exam"].user_count == 1

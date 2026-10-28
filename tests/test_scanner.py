from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from smartfs import db
from smartfs.config import Settings
from smartfs.models import FileStatus
from smartfs.scanner import (
    discover_changes,
    hash_file,
    index_file,
    is_excluded,
    is_partial_name,
    scan_directory,
    wait_until_stable,
)
from smartfs.tags import add_tag


def test_scan_indexes_text_and_is_incremental(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    first = docs / "alpha.txt"
    first.write_text("Alpha content about databases", encoding="utf-8")
    second = docs / "beta.md"
    second.write_text("# Beta\n\nNetwork protocols and sockets.", encoding="utf-8")

    stats = scan_directory(docs, connection, settings)
    assert stats.files_seen == 2
    assert stats.added == 2
    assert stats.errors == 0
    record = db.get_file_by_path(connection, str(first))
    assert record is not None
    assert record.status == FileStatus.DONE
    assert record.sha256 == hash_file(first)
    assert record.text_excerpt and "databases" in record.text_excerpt

    second_scan = scan_directory(docs, connection, settings)
    assert second_scan.unchanged == 2
    assert second_scan.added == 0
    assert second_scan.updated == 0

    first.write_text("Changed content", encoding="utf-8")
    third_scan = scan_directory(docs, connection, settings)
    assert third_scan.updated == 1
    changed = db.get_file_by_path(connection, str(first))
    assert changed is not None
    assert changed.sha256 == hash_file(first)


def test_exclusions_and_partial_files_are_ignored(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    (docs / ".git").mkdir(parents=True)
    (docs / "node_modules").mkdir()
    (docs / ".git" / "config").write_text("secret", encoding="utf-8")
    (docs / "node_modules" / "package.txt").write_text("ignored", encoding="utf-8")
    (docs / "api.key").write_text("secret", encoding="utf-8")
    (docs / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (docs / "download.part").write_text("incomplete", encoding="utf-8")
    (docs / "~lock.txt").write_text("lock", encoding="utf-8")
    (docs / "keep.txt").write_text("keep me", encoding="utf-8")

    stats = scan_directory(docs, connection, settings)
    assert stats.files_seen == 1
    paths = [row["path"] for row in connection.execute("SELECT path FROM files")]
    assert paths == [str(docs / "keep.txt")]


def test_rename_by_inode_preserves_index_and_tags(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    file_path = tmp_path / "old.txt"
    file_path.write_text("Thermodynamics lecture notes", encoding="utf-8")
    scan_directory(tmp_path, connection, settings)
    record = db.get_file_by_path(connection, str(file_path))
    assert record is not None and record.id is not None
    add_tag(connection, int(record.id), "physics")

    new_path = tmp_path / "new.txt"
    os.rename(file_path, new_path)
    scan_directory(tmp_path, connection, settings)
    moved = db.get_file_by_path(connection, str(new_path))
    assert moved is not None
    assert moved.id == record.id
    assert moved.status == FileStatus.DONE
    assert db.get_file_by_path(connection, str(file_path)) is None
    assert db.tag_union(connection, int(record.id)) == ["physics"]
    assert db.status_counts(connection).get("missing", 0) == 0


def test_deleted_file_is_marked_missing(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    file_path = docs / "notes.txt"
    file_path.write_text("temporary", encoding="utf-8")
    scan_directory(docs, connection, settings)
    record = db.get_file_by_path(connection, str(file_path))
    assert record is not None and record.id is not None
    add_tag(connection, int(record.id), "keepme")
    file_path.unlink()

    stats = scan_directory(docs, connection, settings)
    assert stats.missing == 1
    missing = db.get_file_by_id(connection, int(record.id))
    assert missing is not None
    assert missing.status == FileStatus.MISSING
    assert missing.deleted_at is not None
    assert db.tag_union(connection, int(record.id)) == ["keepme"]


def test_unsupported_and_too_large(tmp_path: Path, connection: sqlite3.Connection) -> None:
    settings = Settings(max_file_size_bytes=1024)
    settings.validate()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "image.png").write_bytes(b"\x89PNG\r\n")
    (docs / "big.txt").write_text("x" * 2048, encoding="utf-8")
    (docs / "small.txt").write_text("hello", encoding="utf-8")
    stats = scan_directory(docs, connection, settings)
    assert stats.unsupported == 2
    image = db.get_file_by_path(connection, str(docs / "image.png"))
    big = db.get_file_by_path(connection, str(docs / "big.txt"))
    small = db.get_file_by_path(connection, str(docs / "small.txt"))
    assert image is not None and image.status == FileStatus.UNSUPPORTED
    assert big is not None and big.error_kind == "file_too_large"
    assert small is not None and small.status == FileStatus.DONE


def test_partial_and_exclusion_helpers() -> None:
    assert is_partial_name("file.part")
    assert is_partial_name(".~lock.notes.txt")
    assert is_partial_name("~$report.docx")
    assert not is_partial_name("report.txt")
    assert is_excluded("src/secret.key", "secret.key", ["*.key"])
    assert is_excluded(".git", ".git", [".git"])
    assert not is_excluded("src/main.py", "main.py", ["*.key"])


def test_query_special_characters_are_literal(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    percent = docs / "100%.txt"
    percent.write_text("discount table", encoding="utf-8")
    plain = docs / "plain.txt"
    plain.write_text("discount table too", encoding="utf-8")
    scan_directory(docs, connection, settings)
    results = db.query_files(connection, text="%")
    assert [item.path for item in results] == [str(percent)]


def test_unchanged_does_not_reextract_and_cache_is_shared(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
    monkeypatch,
) -> None:
    from smartfs import scanner
    from smartfs.extractor import extract as real_extract

    calls = 0

    def counting_extract(path, settings):
        nonlocal calls
        calls += 1
        return real_extract(path, settings)

    monkeypatch.setattr(scanner, "extract_document", counting_extract)
    docs = tmp_path / "docs"
    docs.mkdir()
    first = docs / "one.txt"
    first.write_text("shared content for caching", encoding="utf-8")
    scan_directory(docs, connection, settings)
    assert calls == 1
    scan_directory(docs, connection, settings)
    assert calls == 1

    second = docs / "two.txt"
    second.write_text("shared content for caching", encoding="utf-8")
    stats = scan_directory(docs, connection, settings)
    assert stats.added == 1
    assert calls == 1
    one = db.get_file_by_path(connection, str(first))
    two = db.get_file_by_path(connection, str(second))
    assert one is not None and two is not None
    assert one.id != two.id
    assert one.sha256 == two.sha256


def test_rename_by_hash_when_inode_changes(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    import shutil

    old_path = tmp_path / "old.txt"
    old_path.write_text("portable content", encoding="utf-8")
    scan_directory(tmp_path, connection, settings)
    record = db.get_file_by_path(connection, str(old_path))
    assert record is not None and record.id is not None
    add_tag(connection, int(record.id), "portable")

    new_path = tmp_path / "new.txt"
    shutil.copyfile(old_path, new_path)
    old_path.unlink()
    scan_directory(tmp_path, connection, settings)
    moved = db.get_file_by_path(connection, str(new_path))
    assert moved is not None
    assert moved.id == record.id
    assert db.tag_union(connection, int(record.id)) == ["portable"]
    assert db.get_file_by_path(connection, str(old_path)) is None


def test_wait_until_stable_reports_partial_and_missing(tmp_path: Path) -> None:
    stable = tmp_path / "stable.txt"
    stable.write_text("complete", encoding="utf-8")
    ready = wait_until_stable(stable, 0.05, timeout_seconds=1)
    assert ready.ready is True

    partial = tmp_path / "download.part"
    partial.write_text("partial", encoding="utf-8")
    pending = wait_until_stable(partial, 0.05, timeout_seconds=1)
    assert pending.ready is False
    assert pending.reason == "temporary or partial filename"

    missing = wait_until_stable(tmp_path / "none.txt", 0.05, timeout_seconds=1)
    assert missing.ready is False
    assert "cannot stat" in (missing.reason or "")


def test_discover_changes_enqueues_only_changed_files(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    existing = docs / "existing.txt"
    existing.write_text("already indexed", encoding="utf-8")
    scan_directory(docs, connection, settings)

    new_file = docs / "new.txt"
    new_file.write_text("new content", encoding="utf-8")
    enqueued: list[tuple[str, str]] = []
    first = discover_changes(
        docs,
        connection,
        settings,
        lambda job_type, path: enqueued.append((job_type, path)),
    )
    assert first.queued == 1
    assert enqueued == [("index", str(new_file))]

    existing.unlink()
    enqueued.clear()
    second = discover_changes(
        docs,
        connection,
        settings,
        lambda job_type, path: enqueued.append((job_type, path)),
    )
    assert second.missing == 1
    assert enqueued == [("index", str(new_file))]
    missing = db.get_file_by_path(connection, str(existing))
    assert missing is not None
    assert missing.status == FileStatus.MISSING


def test_index_file_indexes_one_path(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    note = tmp_path / "single.txt"
    note.write_text("single file content", encoding="utf-8")
    stats = index_file(connection, note, settings)
    assert stats.added == 1
    record = db.get_file_by_path(connection, str(note))
    assert record is not None
    assert record.status == FileStatus.DONE


class _FakeEmbeddingProvider:
    model_name = "fake-embed"

    def embed(self, text: str) -> list[float]:
        assert text
        return [0.25, -0.5, 1.0]


def test_scan_runs_local_analysis_and_embeddings(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    from smartfs.analysis import AnalysisService
    from smartfs.embeddings import EmbeddingService

    settings = Settings(embeddings_enabled=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "lecture.txt"
    note.write_text(
        "Thermodynamics lecture notes. Entropy increases in an isolated system.",
        encoding="utf-8",
    )
    analysis = AnalysisService(settings)
    embeddings = EmbeddingService(
        settings, tmp_path / "fastembed", provider=_FakeEmbeddingProvider()
    )

    stats = scan_directory(
        docs,
        connection,
        settings,
        analysis_service=analysis,
        embedding_service=embeddings,
    )
    assert stats.analyzed == 1
    assert stats.embedded == 1
    assert stats.analysis_failures == 0
    assert stats.embedding_failures == 0
    record = db.get_file_by_path(connection, str(note))
    assert record is not None and record.id is not None
    assert record.analysis_provider == "local"
    assert record.analysis_model == "heuristic-v1"
    assert record.category == "lecture notes"
    assert record.summary
    assert db.get_embedding(connection, record.sha256 or "", "fake-embed") is not None
    assert "thermodynamics" in db.tag_union(connection, record.id)

    forced = scan_directory(
        docs,
        connection,
        settings,
        force=True,
        analysis_service=analysis,
        embedding_service=embeddings,
    )
    assert forced.analysis_cached == 1
    assert forced.embedding_cached == 1


def test_scan_contains_analysis_and_embedding_failures(
    tmp_path: Path,
    connection: sqlite3.Connection,
    settings: Settings,
) -> None:
    class _FailingAnalysis:
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("analysis exploded")

    class _FailingEmbeddings:
        def embed(self, *_args, **_kwargs):
            raise RuntimeError("embedding exploded")

    note = tmp_path / "note.txt"
    note.write_text("Resilient indexing content", encoding="utf-8")
    stats = scan_directory(
        tmp_path,
        connection,
        settings,
        analysis_service=_FailingAnalysis(),
        embedding_service=_FailingEmbeddings(),
    )
    assert stats.analyzed == 0
    assert stats.analysis_failures == 1
    assert stats.embedding_failures == 1
    record = db.get_file_by_path(connection, str(note))
    assert record is not None
    assert record.status == FileStatus.DONE
    assert record.analysis_provider is None

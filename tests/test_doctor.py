from __future__ import annotations

from pathlib import Path

from smartfs import db
from smartfs.config import AppPaths, Settings
from smartfs.doctor import run_doctor


def _broken_index(tmp_path: Path) -> tuple[AppPaths, Settings, int]:
    paths = AppPaths.for_root(tmp_path / "home")
    paths.ensure()
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    connection = db.connect(paths.db_path)
    try:
        with db.transaction(connection):
            file_id = db.upsert_file(
                connection,
                str(tmp_path / "doc.txt"),
                status="done",
                sha256="a" * 64,
                category="not-a-real-category",
                text_excerpt="Entropy in an isolated system",
            )
            connection.execute("INSERT INTO tags(name) VALUES ('orphan')")
            connection.execute(
                "INSERT INTO embeddings(sha256, model, dimension, vector, created_at) "
                "VALUES ('x', 'test-model', 2, ?, 'now')",
                (b"x",),
            )
            connection.execute(
                "INSERT INTO files_fts(rowid, path, body) VALUES (999, '/orphan', 'orphan')"
            )
    finally:
        connection.close()
    settings = Settings(xattr_enabled=False)
    settings.validate()
    return paths, settings, file_id


def test_doctor_reports_and_repairs_derived_data(tmp_path: Path) -> None:
    paths, settings, file_id = _broken_index(tmp_path)
    report = run_doctor(paths, settings)
    codes = {issue.code for issue in report.issues}
    assert {
        "files.category",
        "fts.missing_documents",
        "fts.orphans",
        "embeddings.invalid",
        "tags.orphans",
    } <= codes
    assert report.errors >= 2
    assert not report.healthy

    repaired = run_doctor(paths, settings, repair=True)
    assert repaired.healthy
    assert repaired.backup is not None and repaired.backup.exists()
    assert any(action.startswith("fts: rebuilt") for action in repaired.repaired)
    assert any(action.startswith("tags: removed") for action in repaired.repaired)
    assert any(action.startswith("embeddings: removed") for action in repaired.repaired)

    connection = db.connect(paths.db_path, read_only=True)
    try:
        row = connection.execute(
            "SELECT path FROM files_fts WHERE files_fts MATCH 'entropy'"
        ).fetchone()
        assert row is not None
        assert row["path"].endswith("doc.txt")
        assert connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        category = connection.execute(
            "SELECT category FROM files WHERE id = ?", (file_id,)
        ).fetchone()[0]
        assert category == "other"
        assert (
            connection.execute("SELECT COUNT(*) FROM files_fts WHERE rowid = 999").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_doctor_healthy_index(tmp_path: Path) -> None:
    paths = AppPaths.for_root(tmp_path / "home")
    paths.ensure()
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    settings = Settings(xattr_enabled=False)
    settings.validate()
    report = run_doctor(paths, settings)
    assert report.healthy
    assert report.backup is None

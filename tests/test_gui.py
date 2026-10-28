from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from smartfs import db, gui  # noqa: E402
from smartfs.config import AppPaths, Settings, save_settings  # noqa: E402
from smartfs.models import FileRecord, FileStatus  # noqa: E402
from smartfs.search import SearchHit, SearchMode  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    application = QApplication.instance() or QApplication(["smartfs-tests"])
    yield application


def _indexed_home(tmp_path: Path, monkeypatch) -> tuple[AppPaths, Settings]:
    monkeypatch.setenv("SMARTFS_HOME", str(tmp_path / "home"))
    paths = AppPaths.discover()
    paths.ensure()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "thermo.txt").write_text(
        "Thermodynamics lecture notes. Entropy increases in an isolated system.\n",
        encoding="utf-8",
    )
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    settings = Settings(watched_dirs=[str(docs)], analysis_enabled=False)
    settings.validate()
    save_settings(settings, paths)
    return paths, settings


def _index(paths: AppPaths, settings: Settings) -> gui.IndexPayload:
    payloads: list[gui.IndexPayload] = []
    failures: list[str] = []
    task = gui.IndexTask(paths, settings, 1)
    task.signals.finished.connect(lambda _id, payload: payloads.append(payload))
    task.signals.failed.connect(lambda _id, message: failures.append(message))
    task.run()
    assert not failures
    assert len(payloads) == 1
    return payloads[0]


def _search(paths: AppPaths, settings: Settings, query: str) -> gui.SearchPayload:
    payloads: list[gui.SearchPayload] = []
    failures: list[str] = []
    task = gui.SearchTask(
        paths,
        settings,
        2,
        query,
        [],
        None,
        SearchMode.TEXT,
    )
    task.signals.finished.connect(lambda _id, payload: payloads.append(payload))
    task.signals.failed.connect(lambda _id, message: failures.append(message))
    task.run()
    assert not failures
    assert len(payloads) == 1
    return payloads[0]


def test_results_model_and_details(qt_app) -> None:
    record = FileRecord(
        id=7,
        path="/tmp/docs/thermo.txt",
        status=FileStatus.DONE,
        category="lecture notes",
        summary="Second law and entropy",
        title="Thermodynamics",
    )
    hit = SearchHit(
        record=record,
        rrf_score=0.03252,
        text_rank=1,
        text_score=1.234,
        text_snippet="[Entropy] increases in an isolated system.",
        semantic_rank=2,
        semantic_similarity=0.4,
    )
    row = gui.GuiHit(hit, ["entropy", "physics"])
    model = gui.SearchResultsModel()
    model.set_rows([row])
    assert model.rowCount() == 1
    assert model.columnCount() == 5
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "thermo.txt"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "lecture notes"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "entropy, physics"
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "text + semantic"
    assert "RRF" in str(model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole))
    assert model.row_at(0) is row
    assert model.row_at(9) is None

    details = gui.hit_details_html(row)
    assert "Second law and entropy" in details
    assert "BM25 1.234" in details
    assert "cosine 0.400" in details
    assert "RRF 0.03252" in details


def test_background_index_and_search_tasks(qt_app, tmp_path: Path, monkeypatch) -> None:
    paths, settings = _indexed_home(tmp_path, monkeypatch)
    payload = _index(paths, settings)
    assert payload.stats[0].added == 1
    assert not payload.errors

    results = _search(paths, settings, "entropy")
    assert results.report.used_mode == "text"
    assert len(results.rows) == 1
    assert results.rows[0].hit.record.path.endswith("thermo.txt")
    assert results.rows[0].hit.text_rank == 1
    assert "[Entropy]" in (results.rows[0].hit.text_snippet or "")


def test_search_task_reports_missing_database(qt_app, tmp_path: Path) -> None:
    paths = AppPaths.for_root(tmp_path / "empty")
    paths.ensure()
    settings = Settings(analysis_enabled=False)
    failures: list[str] = []
    task = gui.SearchTask(paths, settings, 1, "anything", [], None, SearchMode.TEXT)
    task.signals.failed.connect(lambda _id, message: failures.append(message))
    task.run()
    assert failures
    assert "not initialized" in failures[0]


def test_window_applies_payload(qt_app, tmp_path: Path, monkeypatch) -> None:
    paths, settings = _indexed_home(tmp_path, monkeypatch)
    _index(paths, settings)
    payload = _search(paths, settings, "thermodynamics")
    window = gui.SmartFSWindow(paths, settings, start_search=False, load_related=False)
    try:
        window._apply_payload(payload)
        assert window.results_model.rowCount() == 1
        assert window.table.selectionModel().selectedRows()
        assert "thermo.txt" in window.details.toPlainText()
        assert "1 ready" in window.stats_label.text()
    finally:
        window.close()


def test_cli_gui_invokes_runner(qt_app, tmp_path: Path, monkeypatch) -> None:
    from smartfs import cli

    paths, settings = _indexed_home(tmp_path, monkeypatch)
    calls: list[bool] = []

    def fake_runner(paths_arg, settings_arg, *, scan_on_start=False):
        assert paths_arg.db_path == paths.db_path
        assert settings_arg.watched_dirs == settings.watched_dirs
        calls.append(scan_on_start)
        return 0

    monkeypatch.setattr(gui, "run_gui", fake_runner)
    assert cli.main(["gui", "--scan"]) == 0
    assert calls == [True]


def test_related_task(qt_app, tmp_path: Path, monkeypatch) -> None:
    from smartfs import gui_dialogs
    from smartfs.models import TagSource

    paths, settings = _indexed_home(tmp_path, monkeypatch)
    docs = Path(settings.watched_dirs[0])
    (docs / "second.txt").write_text("Another thermodynamics note.", encoding="utf-8")
    _index(paths, settings)
    connection = db.connect(paths.db_path)
    try:
        first = db.get_file_by_path(connection, str(docs / "thermo.txt"))
        second = db.get_file_by_path(connection, str(docs / "second.txt"))
        assert first is not None and first.id is not None
        assert second is not None and second.id is not None
        with db.transaction(connection):
            db.add_file_tag(connection, first.id, "physics", source=TagSource.USER)
            db.add_file_tag(connection, second.id, "physics", source=TagSource.USER)
    finally:
        connection.close()

    results = []
    failures = []
    task = gui.RelatedTask(paths, settings, 1, first.id)
    task.signals.finished.connect(lambda _id, hits: results.extend(hits))
    task.signals.failed.connect(lambda _id, message: failures.append(message))
    task.run()
    assert not failures
    assert [hit.record.id for hit in results] == [second.id]
    assert "shared tags: physics" in results[0].reason
    assert gui_dialogs.format_size(2048) == "2.0 KiB"


def test_dialogs_and_collection_load(qt_app, tmp_path: Path, monkeypatch) -> None:
    from smartfs import gui_dialogs
    from smartfs.models import Collection, TagSource

    paths, settings = _indexed_home(tmp_path, monkeypatch)
    docs = Path(settings.watched_dirs[0])
    (docs / "copy.txt").write_text((docs / "thermo.txt").read_text(encoding="utf-8"))
    _index(paths, settings)
    connection = db.connect(paths.db_path)
    try:
        record = db.get_file_by_path(connection, str(docs / "thermo.txt"))
        assert record is not None and record.id is not None
        with db.transaction(connection):
            db.save_collection(
                connection,
                "Physics Review",
                query="entropy",
                tags=["physics"],
                category="lecture notes",
                mode="hybrid",
            )
            db.add_file_tag(
                connection,
                record.id,
                "physics",
                source=TagSource.USER,
            )
    finally:
        connection.close()

    tags_dialog = gui_dialogs.TagManagerDialog(paths)
    duplicates_dialog = gui_dialogs.DuplicatesDialog(paths)
    collections_dialog = gui_dialogs.CollectionsDialog(
        paths,
        lambda: ("entropy", ["physics"], None, "hybrid"),
        lambda _collection: None,
    )
    window = gui.SmartFSWindow(paths, settings, start_search=False, load_related=False)
    try:
        assert tags_dialog.table.rowCount() >= 1
        assert duplicates_dialog.tree.topLevelItemCount() == 1
        assert collections_dialog.table.rowCount() == 1
        collections_dialog.table.selectRow(0)
        selected = collections_dialog._selected()
        assert selected is not None and selected.name == "Physics Review"

        calls: list[bool] = []
        window.trigger_search = lambda: calls.append(True)  # type: ignore[method-assign]
        window.load_collection(
            Collection(
                name="Review",
                query="thermodynamics",
                tags=["physics", "exam"],
                category="lecture notes",
                mode="text",
            )
        )
        assert window.query_edit.text() == "thermodynamics"
        assert window.tags_edit.text() == "physics, exam"
        assert window.category_combo.currentData() == "lecture notes"
        assert window.mode_combo.currentData() == "text"
        assert calls == [True]
        assert window.current_search_state() == (
            "thermodynamics",
            ["physics", "exam"],
            "lecture notes",
            "text",
        )
    finally:
        window.close()
        tags_dialog.close()
        duplicates_dialog.close()
        collections_dialog.close()


def test_tag_change_task(qt_app, tmp_path: Path, monkeypatch) -> None:
    from smartfs import gui_dialogs
    from smartfs.models import TagSource

    paths, settings = _indexed_home(tmp_path, monkeypatch)
    _index(paths, settings)
    connection = db.connect(paths.db_path)
    try:
        record = db.get_file_by_path(connection, str(Path(settings.watched_dirs[0]) / "thermo.txt"))
        assert record is not None and record.id is not None
        with db.transaction(connection):
            db.add_file_tag(connection, record.id, "physics", source=TagSource.USER)
    finally:
        connection.close()

    counts: list[int] = []
    task = gui_dialogs.TagChangeTask(paths, "rename", "physics", "natural-science")
    task.signals.finished.connect(counts.append)
    task.run()
    assert counts == [1]
    connection = db.connect(paths.db_path, read_only=True)
    try:
        assert db.tag_union(connection, record.id) == ["natural-science"]
    finally:
        connection.close()

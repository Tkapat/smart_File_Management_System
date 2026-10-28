"""PySide6 desktop interface with background indexing and search workers."""

from __future__ import annotations

import html
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from smartfs import __version__, db
from smartfs.analysis import AnalysisService
from smartfs.config import AppPaths, ConfigError, Settings, load_settings
from smartfs.embeddings import EmbeddingService
from smartfs.gui_dialogs import CollectionsDialog, DuplicatesDialog, TagManagerDialog
from smartfs.models import Collection, FileStatus
from smartfs.related import RelatedError, RelatedHit, related_files
from smartfs.scanner import ScanError, ScanStats, scan_directory
from smartfs.search import SearchError, SearchHit, SearchMode, SearchReport, search_files
from smartfs.tags import TagError, normalize_tag

LOGGER = logging.getLogger("smartfs.gui")

CATEGORY_ANY = "__any__"
_NULL_INDEX = QtCore.QModelIndex()

MODE_LABELS = (
    ("Auto (hybrid when available)", SearchMode.AUTO.value),
    ("Text (FTS5/BM25)", SearchMode.TEXT.value),
    ("Semantic (local vectors)", SearchMode.SEMANTIC.value),
    ("Hybrid (RRF fusion)", SearchMode.HYBRID.value),
)


@dataclass(slots=True)
class GuiHit:
    """A search hit plus the tag union needed by the table and detail pane."""

    hit: SearchHit
    tags: list[str]


@dataclass(slots=True)
class SearchPayload:
    """Data handed from a background search to the GUI thread."""

    report: SearchReport
    rows: list[GuiHit]


@dataclass(slots=True)
class IndexPayload:
    """Data handed from a background scan to the GUI thread."""

    stats: list[ScanStats]
    errors: list[str]


class SearchSignals(QtCore.QObject):
    """Signals emitted by :class:`SearchTask`."""

    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class SearchTask(QtCore.QRunnable):
    """Run one hybrid search against an independent read-only connection."""

    def __init__(
        self,
        paths: AppPaths,
        settings: Settings,
        request_id: int,
        query: str,
        tags: list[str],
        category: str | None,
        mode: SearchMode,
        limit: int = 200,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.settings = settings
        self.request_id = request_id
        self.query = query
        self.tags = tags
        self.category = category
        self.mode = mode
        self.limit = limit
        self.signals = SearchSignals()

    def run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            if not self.paths.db_path.exists():
                raise SearchError("index is not initialized; run 'smartfs init' first")
            connection = db.connect(self.paths.db_path, read_only=True)
            embedding_service = (
                EmbeddingService(self.settings, self.paths.cache_dir / "fastembed")
                if self.settings.embeddings_enabled
                else None
            )
            report = search_files(
                connection,
                self.settings,
                self.query,
                tags=self.tags,
                category=self.category,
                limit=self.limit,
                mode=self.mode,
                embedding_service=embedding_service,
            )
            rows: list[GuiHit] = []
            for hit in report.hits:
                tags = db.tag_union(connection, hit.record.id) if hit.record.id else []
                rows.append(GuiHit(hit, tags))
            self.signals.finished.emit(self.request_id, SearchPayload(report, rows))
        except (SearchError, db.DatabaseError, sqlite3.Error, OSError) as exc:
            self.signals.failed.emit(self.request_id, str(exc))
        except Exception as exc:
            LOGGER.exception(
                "Unexpected GUI search failure",
                extra={"event": "gui.search_failed", "error_type": type(exc).__name__},
            )
            self.signals.failed.emit(self.request_id, f"Search failed: {type(exc).__name__}")
        finally:
            if connection is not None:
                connection.close()


class IndexSignals(QtCore.QObject):
    """Signals emitted by :class:`IndexTask`."""

    progress = QtCore.Signal(int, object)
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class IndexTask(QtCore.QRunnable):
    """Scan watched directories off the GUI thread, one root at a time."""

    def __init__(self, paths: AppPaths, settings: Settings, request_id: int) -> None:
        super().__init__()
        self.paths = paths
        self.settings = settings
        self.request_id = request_id
        self.signals = IndexSignals()

    def run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            if not self.settings.watched_dirs:
                raise ScanError("no watched directories; add one with 'smartfs watch add <dir>'")
            db.init_database(
                self.paths.db_path,
                backup_dir=self.paths.backup_dir,
                backup_keep=5,
            )
            connection = db.connect(self.paths.db_path)
            analysis_service = (
                AnalysisService(self.settings) if self.settings.analysis_enabled else None
            )
            embedding_service = (
                EmbeddingService(self.settings, self.paths.cache_dir / "fastembed")
                if self.settings.embeddings_enabled
                else None
            )
            results: list[ScanStats] = []
            errors: list[str] = []
            for root in list(self.settings.watched_dirs):
                try:
                    result = scan_directory(
                        root,
                        connection,
                        self.settings,
                        analysis_service=analysis_service,
                        embedding_service=embedding_service,
                    )
                except ScanError as exc:
                    errors.append(str(exc))
                    continue
                results.append(result)
                self.signals.progress.emit(self.request_id, result)
            self.signals.finished.emit(self.request_id, IndexPayload(results, errors))
        except (ScanError, db.DatabaseError, sqlite3.Error, OSError) as exc:
            self.signals.failed.emit(self.request_id, str(exc))
        except Exception as exc:
            LOGGER.exception(
                "Unexpected GUI scan failure",
                extra={"event": "gui.scan_failed", "error_type": type(exc).__name__},
            )
            self.signals.failed.emit(self.request_id, f"Scan failed: {type(exc).__name__}")
        finally:
            if connection is not None:
                connection.close()


class RelatedSignals(QtCore.QObject):
    """Signals emitted by :class:`RelatedTask`."""

    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)


class RelatedTask(QtCore.QRunnable):
    """Compute related documents without blocking result selection."""

    def __init__(
        self,
        paths: AppPaths,
        settings: Settings,
        request_id: int,
        file_id: int,
        limit: int = 8,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.settings = settings
        self.request_id = request_id
        self.file_id = file_id
        self.limit = limit
        self.signals = RelatedSignals()

    def run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path, read_only=True)
            hits = related_files(
                connection,
                self.settings,
                self.file_id,
                limit=self.limit,
            )
            self.signals.finished.emit(self.request_id, hits)
        except (RelatedError, db.DatabaseError, sqlite3.Error, OSError) as exc:
            self.signals.failed.emit(self.request_id, str(exc))
        except Exception as exc:
            LOGGER.exception(
                "Unexpected related-documents failure",
                extra={"event": "gui.related_failed", "error_type": type(exc).__name__},
            )
            self.signals.failed.emit(
                self.request_id, f"Related documents failed: {type(exc).__name__}"
            )
        finally:
            if connection is not None:
                connection.close()


class SearchResultsModel(QtCore.QAbstractTableModel):
    """Read-only table model backed by :class:`GuiHit` objects."""

    HEADERS = ("File", "Category", "Tags", "Sources", "Score")

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[GuiHit] = []

    def set_rows(self, rows: list[GuiHit]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> GuiHit | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent: QtCore.QModelIndex = _NULL_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QtCore.QModelIndex = _NULL_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(
        self, index: QtCore.QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object | None:
        if not index.isValid() or not (row := self.row_at(index.row())):
            return None
        record = row.hit.record
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            return record.path
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _display(self, row: GuiHit, column: int) -> str:
        record = row.hit.record
        if column == 0:
            return Path(record.path).name or record.path
        if column == 1:
            return record.category or "—"
        if column == 2:
            return ", ".join(row.tags) if row.tags else "—"
        if column == 3:
            return " + ".join(row.hit.sources) or "filter"
        if column == 4:
            return score_text(row.hit)
        return ""


def score_text(hit: SearchHit) -> str:
    """Format the most relevant score for a result row."""
    if hit.text_rank is not None and hit.semantic_rank is not None:
        return f"{hit.rrf_score:.5f} RRF"
    if hit.semantic_rank is not None and hit.semantic_similarity is not None:
        return f"{hit.semantic_similarity:.3f} cos"
    if hit.text_score is not None:
        return f"{hit.text_score:.3f} BM25"
    return "—"


def hit_details_html(row: GuiHit) -> str:
    """Render safe, escaped details and ranking evidence for one result."""
    hit = row.hit
    record = hit.record
    parts = [
        f"<h3>{html.escape(Path(record.path).name or record.path)}</h3>",
        f"<p><b>Path:</b> <code>{html.escape(record.path)}</code><br>",
        f"<b>Category:</b> {html.escape(record.category or '—')}<br>",
        f"<b>Status:</b> {html.escape(record.status.value if isinstance(record.status, FileStatus) else str(record.status))}<br>",
        f"<b>Sources:</b> {html.escape(' + '.join(hit.sources) or 'filters')}<br>",
        f"<b>Tags:</b> {html.escape(', '.join(row.tags) or '—')}</p>",
    ]
    if record.summary:
        parts.append(f"<p><b>Summary:</b> {html.escape(record.summary)}</p>")
    if hit.text_snippet:
        parts.append(f"<p><b>Matched text:</b> {html.escape(hit.text_snippet)}</p>")

    evidence: list[str] = []
    if hit.text_rank is not None:
        bm25 = f", BM25 {hit.text_score:.3f}" if hit.text_score is not None else ""
        evidence.append(f"full-text rank #{hit.text_rank}{bm25}")
    if hit.semantic_rank is not None:
        cosine = (
            f", cosine {hit.semantic_similarity:.3f}" if hit.semantic_similarity is not None else ""
        )
        evidence.append(f"semantic rank #{hit.semantic_rank}{cosine}")
    if len(evidence) > 1:
        evidence.append(f"RRF {hit.rrf_score:.5f}")
    if evidence:
        parts.append(f"<p><b>Why:</b> {html.escape('; '.join(evidence))}</p>")
    if record.title:
        parts.append(f"<p><b>Title:</b> {html.escape(record.title)}</p>")
    if record.author:
        parts.append(f"<p><b>Author:</b> {html.escape(record.author)}</p>")
    return "".join(parts)


class SmartFSWindow(QtWidgets.QMainWindow):
    """Main GUI window: filters, hybrid search, details, and background scanning."""

    def __init__(
        self,
        paths: AppPaths,
        settings: Settings,
        *,
        start_search: bool = True,
        load_related: bool = True,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.settings = settings
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(3)
        self._search_tasks: dict[int, SearchTask] = {}
        self._related_tasks: dict[int, RelatedTask] = {}
        self._scan_task: IndexTask | None = None
        self._request_id = 0
        self._scan_request_id = 0
        self._related_request_id = 0
        self._load_related = load_related
        self._current_payload: SearchPayload | None = None
        self._selected_row: GuiHit | None = None
        self._last_index_message = ""
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.trigger_search)

        self.setWindowTitle(f"SmartFS {__version__} — Semantic File Manager")
        self.resize(1120, 760)
        self._build_actions()
        self._build_ui()
        self._refresh_stats()
        self.statusBar().showMessage("Ready")
        if start_search:
            QtCore.QTimer.singleShot(0, self.trigger_search)

    def _build_actions(self) -> None:
        refresh = QtGui.QAction("Refresh", self)
        refresh.setShortcut(QtGui.QKeySequence.StandardKey.Refresh)
        refresh.triggered.connect(self.refresh)

        scan = QtGui.QAction("Scan now", self)
        scan.setShortcut(QtGui.QKeySequence("Ctrl+Shift+S"))
        scan.triggered.connect(self.start_scan)

        focus = QtGui.QAction("Focus search", self)
        focus.setShortcut(QtGui.QKeySequence.StandardKey.Find)
        focus.triggered.connect(self._focus_search)

        quit_action = QtGui.QAction("Quit", self)
        quit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        manage_tags = QtGui.QAction("Manage tags…", self)
        manage_tags.triggered.connect(self._open_tag_manager)

        duplicates = QtGui.QAction("Duplicate content…", self)
        duplicates.triggered.connect(self._open_duplicates)

        collections = QtGui.QAction("Saved searches…", self)
        collections.triggered.connect(self._open_collections)

        save_search = QtGui.QAction("Save current search…", self)
        save_search.setShortcut(QtGui.QKeySequence("Ctrl+S"))
        save_search.triggered.connect(self._save_current_search)

        about = QtGui.QAction("About SmartFS", self)
        about.triggered.connect(self._show_about)

        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(scan)
        tools_menu.addAction(refresh)
        tools_menu.addAction(focus)
        tools_menu.addSeparator()
        tools_menu.addAction(manage_tags)
        tools_menu.addAction(duplicates)
        tools_menu.addAction(collections)
        tools_menu.addAction(save_search)
        tools_menu.addSeparator()
        tools_menu.addAction(quit_action)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(about)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("SmartFS")
        title.setObjectName("title")
        subtitle = QtWidgets.QLabel(
            "Search indexed content locally. Full-text and semantic results are fused with RRF."
        )
        subtitle.setObjectName("subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        filters = QtWidgets.QHBoxLayout()
        self.query_edit = QtWidgets.QLineEdit()
        self.query_edit.setPlaceholderText(
            "Search full text, summaries, paths, or describe a document…"
        )
        self.query_edit.setClearButtonEnabled(True)
        self.query_edit.returnPressed.connect(self.trigger_search)
        self.query_edit.textChanged.connect(self._schedule_search)
        self.tags_edit = QtWidgets.QLineEdit()
        self.tags_edit.setPlaceholderText("tags: physics, exam")
        self.tags_edit.setClearButtonEnabled(True)
        self.tags_edit.textChanged.connect(self._schedule_search)
        self.category_combo = QtWidgets.QComboBox()
        self.category_combo.addItem("Any category", CATEGORY_ANY)
        self.category_combo.currentIndexChanged.connect(self._schedule_search)
        self.mode_combo = QtWidgets.QComboBox()
        for label, value in MODE_LABELS:
            self.mode_combo.addItem(label, value)
        self.mode_combo.currentIndexChanged.connect(self._schedule_search)
        self.search_button = QtWidgets.QPushButton("Search")
        self.search_button.setDefault(True)
        self.search_button.clicked.connect(self.trigger_search)
        filters.addWidget(self.query_edit, 4)
        filters.addWidget(self.tags_edit, 2)
        filters.addWidget(self.category_combo, 2)
        filters.addWidget(self.mode_combo, 2)
        filters.addWidget(self.search_button)
        outer.addLayout(filters)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.table = QtWidgets.QTableView()
        self.results_model = SearchResultsModel(self)
        self.table.setModel(self.results_model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.doubleClicked.connect(self._open_selected)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)

        details_panel = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_header = QtWidgets.QHBoxLayout()
        details_label = QtWidgets.QLabel("Details")
        details_label.setObjectName("section")
        self.copy_button = QtWidgets.QPushButton("Copy path")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_path)
        details_header.addWidget(details_label)
        details_header.addStretch(1)
        details_header.addWidget(self.copy_button)
        details_layout.addLayout(details_header)
        details_body = QtWidgets.QHBoxLayout()
        self.details = QtWidgets.QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.details.setPlaceholderText("Select a result to inspect its metadata and ranking.")
        details_body.addWidget(self.details, 3)
        related_panel = QtWidgets.QWidget()
        related_layout = QtWidgets.QVBoxLayout(related_panel)
        related_layout.setContentsMargins(8, 0, 0, 0)
        related_label = QtWidgets.QLabel("Related documents")
        related_label.setObjectName("section")
        self.related_list = QtWidgets.QListWidget()
        self.related_list.setWordWrap(True)
        self.related_list.itemDoubleClicked.connect(self._open_related)
        related_layout.addWidget(related_label)
        related_layout.addWidget(self.related_list, 1)
        related_hint = QtWidgets.QLabel(
            "Double-click to open. Ranked by shared tags and local similarity."
        )
        related_hint.setObjectName("help")
        related_hint.setWordWrap(True)
        related_layout.addWidget(related_hint)
        details_body.addWidget(related_panel, 2)
        details_layout.addLayout(details_body, 1)
        splitter.addWidget(details_panel)
        splitter.setSizes([470, 220])
        outer.addWidget(splitter, 1)

        footer = QtWidgets.QHBoxLayout()
        self.stats_label = QtWidgets.QLabel("Index: loading…")
        self.stats_label.setObjectName("stats")
        self.scan_button = QtWidgets.QPushButton("Scan now")
        self.scan_button.clicked.connect(self.start_scan)
        refresh_button = QtWidgets.QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh)
        footer.addWidget(self.stats_label, 1)
        footer.addWidget(refresh_button)
        footer.addWidget(self.scan_button)
        outer.addLayout(footer)

        self.help_label = QtWidgets.QLabel(
            "Local-only by default. Semantic mode uses locally generated fastembed vectors."
        )
        self.help_label.setObjectName("help")
        outer.addWidget(self.help_label)

        self.setStyleSheet(
            """
            QLabel#title { font-size: 24px; font-weight: 700; }
            QLabel#subtitle { color: palette(mid); margin-bottom: 4px; }
            QLabel#section { font-weight: 700; }
            QLabel#stats { color: palette(mid); }
            QLabel#help { color: palette(mid); font-size: 11px; }
            QTableView { border: 1px solid palette(midlight); }
            QTextBrowser { border: 1px solid palette(midlight); padding: 8px; }
            """
        )

    def _open_tag_manager(self) -> None:
        dialog = TagManagerDialog(self.paths, self)
        dialog.changed.connect(self.refresh)
        dialog.exec()

    def _open_duplicates(self) -> None:
        DuplicatesDialog(self.paths, self).exec()

    def current_search_state(self) -> tuple[str, list[str], str | None, str]:
        return (
            self.query_edit.text().strip(),
            self._selected_tags(),
            self._selected_category(),
            str(self.mode_combo.currentData()),
        )

    def _open_collections(self) -> None:
        dialog = CollectionsDialog(
            self.paths,
            self.current_search_state,
            self.load_collection,
            self,
        )
        dialog.exec()

    def _save_current_search(self) -> None:
        name, accepted = QtWidgets.QInputDialog.getText(
            self, "Save current search", "Collection name:"
        )
        if not accepted or not name.strip():
            return
        query, tags, category, mode = self.current_search_state()
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path)
            with db.transaction(connection):
                collection = db.save_collection(
                    connection,
                    name,
                    query=query,
                    tags=tags,
                    category=category,
                    mode=mode,
                )
        except (sqlite3.Error, db.DatabaseError, ValueError) as exc:
            self.statusBar().showMessage(f"Could not save collection: {exc}", 6000)
            return
        finally:
            if connection is not None:
                connection.close()
        self.statusBar().showMessage(f"Saved collection: {collection.name}", 5000)

    def load_collection(self, collection: Collection) -> None:
        """Load a saved search into the filter controls and run it."""
        self.query_edit.setText(collection.query)
        self.tags_edit.setText(", ".join(collection.tags))
        if collection.category:
            position = self.category_combo.findData(collection.category)
            if position < 0:
                self.category_combo.addItem(collection.category, collection.category)
                position = self.category_combo.findData(collection.category)
            self.category_combo.setCurrentIndex(position)
        else:
            self.category_combo.setCurrentIndex(0)
        mode_position = self.mode_combo.findData(collection.mode)
        if mode_position >= 0:
            self.mode_combo.setCurrentIndex(mode_position)
        self.trigger_search()

    def _focus_search(self) -> None:
        self.query_edit.setFocus()
        self.query_edit.selectAll()

    def _show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            "About SmartFS",
            f"SmartFS {__version__}\n\n"
            "Linux-native semantic file management with SQLite FTS5, optional local "
            "embeddings, Gemini enrichment, and user.smartfs.* extended attributes.",
        )

    def _schedule_search(self, *_args: object) -> None:
        self._search_timer.start()

    def _selected_tags(self) -> list[str]:
        tags: list[str] = []
        for raw in self.tags_edit.text().split(","):
            if not raw.strip():
                continue
            try:
                tag = normalize_tag(raw)
            except TagError:
                continue
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def _selected_category(self) -> str | None:
        value = self.category_combo.currentData()
        return None if value == CATEGORY_ANY else str(value)

    def trigger_search(self) -> None:
        """Start a search using the current filters without blocking the GUI thread."""
        try:
            self.settings = load_settings(self.paths)
        except ConfigError as exc:
            self.statusBar().showMessage(str(exc))
            return
        mode = SearchMode(str(self.mode_combo.currentData()))
        self._search_timer.stop()
        self._request_id += 1
        request_id = self._request_id
        task = SearchTask(
            self.paths,
            self.settings,
            request_id,
            self.query_edit.text().strip(),
            self._selected_tags(),
            self._selected_category(),
            mode,
        )
        task.signals.finished.connect(self._search_finished)
        task.signals.failed.connect(self._search_failed)
        self._search_tasks[request_id] = task
        self.search_button.setEnabled(False)
        self.statusBar().showMessage("Searching…")
        self._pool.start(task)

    def _search_finished(self, request_id: int, payload: SearchPayload) -> None:
        self._search_tasks.pop(request_id, None)
        if request_id != self._request_id:
            return
        self.search_button.setEnabled(True)
        self._apply_payload(payload)

    def _search_failed(self, request_id: int, message: str) -> None:
        self._search_tasks.pop(request_id, None)
        if request_id != self._request_id:
            return
        self.search_button.setEnabled(True)
        self.results_model.set_rows([])
        self.details.setHtml(f"<p><b>Search error:</b> {html.escape(message)}</p>")
        self.statusBar().showMessage(message)

    def _apply_payload(self, payload: SearchPayload) -> None:
        """Apply a completed search payload to the table and status line."""
        self._current_payload = payload
        self.results_model.set_rows(payload.rows)
        self._selected_row = None
        self.copy_button.setEnabled(False)
        report = payload.report
        counts = (
            f"{len(payload.rows)} result(s) — mode {report.used_mode} "
            f"(text={report.text_candidates}, semantic={report.semantic_candidates})"
        )
        if report.fts_query:
            counts += f" — FTS {report.fts_query}"
        if report.note:
            counts += f" — note: {report.note}"
        self.statusBar().showMessage(counts)
        if payload.rows:
            self.table.selectRow(0)
        else:
            self.details.setHtml("<p>No matching files.</p>")
        self._refresh_category_options()
        self._refresh_stats()

    def _selection_changed(self, *_args: object) -> None:
        indexes = self.table.selectionModel().selectedRows()
        row = self.results_model.row_at(indexes[0].row()) if indexes else None
        self._selected_row = row
        self.copy_button.setEnabled(row is not None)
        self.details.setHtml(hit_details_html(row) if row is not None else "")
        self._start_related(row)

    def _start_related(self, row: GuiHit | None) -> None:
        self._related_request_id += 1
        request_id = self._related_request_id
        self.related_list.clear()
        if not self._load_related or row is None or row.hit.record.id is None:
            return
        task = RelatedTask(
            self.paths,
            self.settings,
            request_id,
            int(row.hit.record.id),
        )
        task.signals.finished.connect(self._related_finished)
        task.signals.failed.connect(self._related_failed)
        self._related_tasks[request_id] = task
        self._pool.start(task)

    def _related_finished(self, request_id: int, hits: list[RelatedHit]) -> None:
        self._related_tasks.pop(request_id, None)
        if request_id != self._related_request_id:
            return
        self.related_list.clear()
        for hit in hits:
            item = QtWidgets.QListWidgetItem(Path(hit.record.path).name or hit.record.path)
            item.setToolTip(f"{hit.record.path}\n{hit.reason}\nscore {hit.score:.3f}")
            item.setData(Qt.ItemDataRole.UserRole, hit.record.path)
            self.related_list.addItem(item)
        if not hits:
            placeholder = QtWidgets.QListWidgetItem("No related documents found.")
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.related_list.addItem(placeholder)

    def _related_failed(self, request_id: int, message: str) -> None:
        self._related_tasks.pop(request_id, None)
        if request_id != self._related_request_id:
            return
        self.related_list.clear()
        placeholder = QtWidgets.QListWidgetItem(f"Related unavailable: {message}")
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.related_list.addItem(placeholder)

    def _open_related(self, item: QtWidgets.QListWidgetItem) -> None:
        value = item.data(Qt.ItemDataRole.UserRole)
        if not value:
            return
        path = str(value)
        if not Path(path).exists():
            self.statusBar().showMessage(f"File is missing: {path}", 5000)
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _copy_path(self) -> None:
        if self._selected_row is not None:
            QtWidgets.QApplication.clipboard().setText(self._selected_row.hit.record.path)
            self.statusBar().showMessage("Path copied to clipboard", 3000)

    def _open_selected(self, *_args: object) -> None:
        if self._selected_row is None:
            return
        path = self._selected_row.hit.record.path
        if not Path(path).exists():
            self.statusBar().showMessage(f"File is missing: {path}", 5000)
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _refresh_category_options(self) -> None:
        if not self.paths.db_path.exists():
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path, read_only=True)
            categories = {
                str(row["category"])
                for row in connection.execute(
                    "SELECT DISTINCT category FROM files "
                    "WHERE status = 'done' AND category IS NOT NULL AND category != ''"
                )
            }
        except (sqlite3.Error, db.DatabaseError):
            return
        finally:
            if connection is not None:
                connection.close()
        selected = self._selected_category()
        existing = {
            str(self.category_combo.itemData(index)) for index in range(self.category_combo.count())
        }
        for category in sorted(categories):
            if category not in existing:
                self.category_combo.addItem(category, category)
        if selected is not None:
            position = self.category_combo.findData(selected)
            if position >= 0:
                self.category_combo.setCurrentIndex(position)

    def _refresh_stats(self) -> None:
        if not self.paths.db_path.exists():
            self.stats_label.setText("Index: not initialized")
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path, read_only=True)
            counts = db.status_counts(connection)
            embeddings = db.embedding_stats(connection)
            done = counts.get("done", 0)
            missing = counts.get("missing", 0)
            failed = counts.get("failed", 0)
            text = f"Index: {done} ready, {missing} missing, {failed} failed"
            if embeddings.get("total", 0):
                text += f" — {embeddings['total']} local vector(s)"
            self.stats_label.setText(text)
        except (sqlite3.Error, db.DatabaseError) as exc:
            self.stats_label.setText(f"Index: unavailable ({type(exc).__name__})")
        finally:
            if connection is not None:
                connection.close()

    def refresh(self) -> None:
        self._refresh_stats()
        self.trigger_search()

    def start_scan(self) -> None:
        """Launch an incremental scan of configured directories in the background."""
        if self._scan_task is not None:
            self.statusBar().showMessage("A scan is already running", 3000)
            return
        try:
            settings = load_settings(self.paths)
        except ConfigError as exc:
            self.statusBar().showMessage(str(exc))
            return
        if not settings.watched_dirs:
            self.statusBar().showMessage(
                "No watched directories; add one with 'smartfs watch add <dir>'"
            )
            return
        self.settings = settings
        self._scan_request_id += 1
        task = IndexTask(self.paths, settings, self._scan_request_id)
        task.signals.progress.connect(self._scan_progress)
        task.signals.finished.connect(self._scan_finished)
        task.signals.failed.connect(self._scan_failed)
        self._scan_task = task
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning…")
        self.statusBar().showMessage("Scanning watched directories…")
        self._pool.start(task)

    def _scan_progress(self, request_id: int, stats: ScanStats) -> None:
        if request_id != self._scan_request_id:
            return
        self.statusBar().showMessage(
            f"Scanned {stats.root}: {stats.files_seen} seen, {stats.added} added, "
            f"{stats.updated} updated, {stats.errors} errors"
        )

    def _scan_finished(self, request_id: int, payload: IndexPayload) -> None:
        if request_id != self._scan_request_id:
            return
        self._scan_task = None
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Scan now")
        seen = sum(item.files_seen for item in payload.stats)
        added = sum(item.added for item in payload.stats)
        updated = sum(item.updated for item in payload.stats)
        errors = sum(item.errors for item in payload.stats)
        message = f"Scan complete: {seen} seen, {added} added, {updated} updated, {errors} errors"
        if payload.errors:
            message += f"; {len(payload.errors)} root error(s)"
        self._last_index_message = message
        self.statusBar().showMessage(message, 8000)
        self._refresh_stats()
        self.trigger_search()

    def _scan_failed(self, request_id: int, message: str) -> None:
        if request_id != self._scan_request_id:
            return
        self._scan_task = None
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Scan now")
        self.statusBar().showMessage(f"Scan error: {message}", 8000)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._pool.clear()
        super().closeEvent(event)


def run_gui(
    paths: AppPaths,
    settings: Settings,
    *,
    scan_on_start: bool = False,
) -> int:
    """Create the QApplication and run the SmartFS desktop window."""
    application = QtWidgets.QApplication.instance()
    if application is None:
        application = QtWidgets.QApplication(["smartfs"])
    application.setApplicationName("SmartFS")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("SmartFS")
    window = SmartFSWindow(paths, settings)
    window.show()
    if scan_on_start:
        QtCore.QTimer.singleShot(250, window.start_scan)
    return int(application.exec())

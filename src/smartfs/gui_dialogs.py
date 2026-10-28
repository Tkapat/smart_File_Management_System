"""Secondary dialogs for tags, duplicates, and saved-search collections."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from smartfs import db
from smartfs.config import AppPaths, load_settings
from smartfs.models import Collection
from smartfs.tags import TagError, merge_tags, rename_tag


def format_size(size: int) -> str:
    """Format a byte count for compact display."""
    value = float(max(size, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(value)} B"


def open_local_path(path: str) -> None:
    """Open a path with the desktop's default handler."""
    if Path(path).exists():
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))


class TagChangeSignals(QtCore.QObject):
    """Signals emitted by :class:`TagChangeTask`."""

    finished = QtCore.Signal(int)
    failed = QtCore.Signal(str)


class TagChangeTask(QtCore.QRunnable):
    """Rename or merge a tag across the index without blocking the dialog."""

    def __init__(
        self,
        paths: AppPaths,
        action: str,
        source: str,
        target: str,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.action = action
        self.source = source
        self.target = target
        self.signals = TagChangeSignals()

    def run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            settings = load_settings(self.paths)
            connection = db.connect(self.paths.db_path)
            if self.action == "rename":
                results = rename_tag(
                    connection,
                    self.source,
                    self.target,
                    suppression_ttl=settings.self_write_suppression_seconds,
                )
            elif self.action == "merge":
                results = merge_tags(
                    connection,
                    self.source,
                    self.target,
                    suppression_ttl=settings.self_write_suppression_seconds,
                )
            else:
                raise TagError(f"unknown tag action: {self.action}")
            self.signals.finished.emit(len(results))
        except (TagError, db.DatabaseError, sqlite3.Error, OSError) as exc:
            self.signals.failed.emit(str(exc))
        finally:
            if connection is not None:
                connection.close()


class TagManagerDialog(QtWidgets.QDialog):
    """List tag usage and perform background rename/merge operations."""

    changed = QtCore.Signal()

    def __init__(self, paths: AppPaths, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = paths
        self._task: TagChangeTask | None = None
        self.setWindowTitle("SmartFS — Tags")
        self.resize(680, 480)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(
            QtWidgets.QLabel(
                "Renaming or merging updates SQLite and re-syncs user.smartfs.tags. "
                "No document contents are changed."
            )
        )
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Tag", "Files", "AI links", "User links"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("stats")
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        self.rename_button = QtWidgets.QPushButton("Rename…")
        self.rename_button.clicked.connect(self._rename)
        self.merge_button = QtWidgets.QPushButton("Merge into…")
        self.merge_button.clicked.connect(self._merge)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.merge_button)
        buttons.addStretch(1)
        buttons.addWidget(refresh)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path, read_only=True)
            stats = db.tag_overview(connection)
        except (sqlite3.Error, db.DatabaseError) as exc:
            self.status.setText(f"Could not load tags: {exc}")
            return
        finally:
            if connection is not None:
                connection.close()
        self.table.setRowCount(len(stats))
        for row, stat in enumerate(stats):
            name = QtWidgets.QTableWidgetItem(stat.name)
            name.setData(Qt.ItemDataRole.UserRole, stat.name)
            self.table.setItem(row, 0, name)
            for column, value in enumerate(
                (stat.file_count, stat.ai_count, stat.user_count), start=1
            ):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        self.status.setText(f"{len(stats)} tag(s)")
        self._update_buttons()

    def _selected_tag(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _update_buttons(self) -> None:
        enabled = self._selected_tag() is not None and self._task is None
        self.rename_button.setEnabled(enabled)
        self.merge_button.setEnabled(enabled)

    def _rename(self) -> None:
        source = self._selected_tag()
        if source is None:
            return
        target, accepted = QtWidgets.QInputDialog.getText(
            self, "Rename tag", f"Rename {source!r} to:"
        )
        if not accepted or not target.strip():
            return
        self._start("rename", source, target.strip())

    def _merge(self) -> None:
        source = self._selected_tag()
        if source is None:
            return
        target, accepted = QtWidgets.QInputDialog.getText(
            self, "Merge tag", f"Merge {source!r} into:"
        )
        if not accepted or not target.strip():
            return
        self._start("merge", source, target.strip())

    def _start(self, action: str, source: str, target: str) -> None:
        self._task = TagChangeTask(self.paths, action, source, target)
        self._task.signals.finished.connect(self._finished)
        self._task.signals.failed.connect(self._failed)
        self.status.setText(f"{action.title()} in progress…")
        self._update_buttons()
        QtCore.QThreadPool.globalInstance().start(self._task)

    def _finished(self, count: int) -> None:
        self._task = None
        self.refresh()
        self.status.setText(f"Updated {count} file(s).")
        self.changed.emit()

    def _failed(self, message: str) -> None:
        self._task = None
        self.status.setText(f"Error: {message}")
        self._update_buttons()


class DuplicatesDialog(QtWidgets.QDialog):
    """Show exact-content duplicate groups without deleting anything."""

    def __init__(self, paths: AppPaths, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.setWindowTitle("SmartFS — Duplicate content")
        self.resize(820, 560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(
            QtWidgets.QLabel(
                "Exact duplicates share a SHA-256 content hash. SmartFS only reports "
                "them; removal stays under your control."
            )
        )
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Duplicate groups"])
        self.tree.setColumnWidth(0, 760)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.tree, 1)
        self.status = QtWidgets.QLabel("")
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        self.copy_button = QtWidgets.QPushButton("Copy path")
        self.copy_button.clicked.connect(self._copy)
        self.open_button = QtWidgets.QPushButton("Open")
        self.open_button.clicked.connect(self._open)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(refresh)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path, read_only=True)
            groups = db.duplicate_groups(connection)
        except (sqlite3.Error, db.DatabaseError) as exc:
            self.status.setText(f"Could not load duplicates: {exc}")
            return
        finally:
            if connection is not None:
                connection.close()
        self.tree.clear()
        for group in groups:
            parent = QtWidgets.QTreeWidgetItem(
                [f"{len(group.files)} copies — {format_size(group.size)} — {group.sha256[:12]}…"]
            )
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(parent)
            for record in group.files:
                child = QtWidgets.QTreeWidgetItem([record.path])
                child.setData(0, Qt.ItemDataRole.UserRole, record.path)
                child.setToolTip(0, record.path)
                parent.addChild(child)
        self.tree.expandAll()
        total = sum(len(group.files) for group in groups)
        self.status.setText(
            f"{len(groups)} group(s), {total} file(s). No files are deleted by this view."
        )
        self._update_buttons()

    def _selected_path(self) -> str | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _update_buttons(self) -> None:
        enabled = self._selected_path() is not None
        self.copy_button.setEnabled(enabled)
        self.open_button.setEnabled(enabled)

    def _copy(self) -> None:
        path = self._selected_path()
        if path:
            QtWidgets.QApplication.clipboard().setText(path)
            self.status.setText("Path copied to clipboard.")

    def _open(self, *_args: object) -> None:
        path = self._selected_path()
        if path:
            open_local_path(path)


class CollectionsDialog(QtWidgets.QDialog):
    """Manage saved searches and load one into the main window."""

    def __init__(
        self,
        paths: AppPaths,
        current_search: Callable[[], tuple[str, list[str], str | None, str]],
        load_collection: Callable[[Collection], None],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.current_search = current_search
        self.load_collection = load_collection
        self.setWindowTitle("SmartFS — Saved searches")
        self.resize(860, 480)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(
            QtWidgets.QLabel(
                "Collections store the query, tag/category filters, and search mode. "
                "They never copy or move files."
            )
        )
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Query", "Tags", "Category", "Mode"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column in (2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.itemDoubleClicked.connect(self._load)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel("")
        layout.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        self.save_button = QtWidgets.QPushButton("Save current search…")
        self.save_button.clicked.connect(self._save_current)
        self.load_button = QtWidgets.QPushButton("Load")
        self.load_button.clicked.connect(self._load)
        self.delete_button = QtWidgets.QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.load_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path, read_only=True)
            collections = db.list_collections(connection)
        except (sqlite3.Error, db.DatabaseError) as exc:
            self.status.setText(f"Could not load collections: {exc}")
            return
        finally:
            if connection is not None:
                connection.close()
        self.table.setRowCount(len(collections))
        for row, collection in enumerate(collections):
            values = (
                collection.name,
                collection.query or "(none)",
                ", ".join(collection.tags) or "(none)",
                collection.category or "(any)",
                collection.mode,
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, collection)
                self.table.setItem(row, column, item)
        self.status.setText(f"{len(collections)} saved search(es)")
        self._update_buttons()

    def _selected(self) -> Collection | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, Collection) else None

    def _update_buttons(self) -> None:
        enabled = self._selected() is not None
        self.load_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def _save_current(self) -> None:
        name, accepted = QtWidgets.QInputDialog.getText(
            self, "Save current search", "Collection name:"
        )
        if not accepted or not name.strip():
            return
        query, tags, category, mode = self.current_search()
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
            self.status.setText(f"Could not save collection: {exc}")
            return
        finally:
            if connection is not None:
                connection.close()
        self.status.setText(f"Saved collection: {collection.name}")
        self.refresh()

    def _load(self, *_args: object) -> None:
        collection = self._selected()
        if collection is None:
            return
        self.load_collection(collection)
        self.accept()

    def _delete(self) -> None:
        collection = self._selected()
        if collection is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete collection",
            f"Delete the saved search {collection.name!r}? Files are not affected.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = db.connect(self.paths.db_path)
            with db.transaction(connection):
                db.delete_collection(connection, collection.name)
        except (sqlite3.Error, db.DatabaseError) as exc:
            self.status.setText(f"Could not delete collection: {exc}")
            return
        finally:
            if connection is not None:
                connection.close()
        self.status.setText(f"Deleted collection: {collection.name}")
        self.refresh()

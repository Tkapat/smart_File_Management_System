from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from smartfs import db
from smartfs.config import AppPaths, Settings


@pytest.fixture
def settings() -> Settings:
    value = Settings()
    value.validate()
    return value


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    value = AppPaths.for_root(tmp_path / "home")
    value.ensure()
    return value


@pytest.fixture
def connection(paths: AppPaths) -> Iterator[sqlite3.Connection]:
    db.init_database(paths.db_path, backup_dir=paths.backup_dir)
    conn = db.connect(paths.db_path)
    try:
        yield conn
    finally:
        conn.close()

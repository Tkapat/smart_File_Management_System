from __future__ import annotations

import time
from pathlib import Path

from smartfs import db
from smartfs.cli import main
from smartfs.config import AppPaths


def _initialize(tmp_path: Path, monkeypatch) -> AppPaths:
    monkeypatch.setenv("SMARTFS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--non-interactive"]) == 0
    return AppPaths.discover()


def test_cli_backup_create_list_prune(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _initialize(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["backup", "create"]) == 0
    assert "Backup created:" in capsys.readouterr().out
    time.sleep(0.02)
    assert main(["backup", "create"]) == 0
    capsys.readouterr()

    assert main(["backup", "list"]) == 0
    output = capsys.readouterr().out
    assert output.count("manual-") == 2

    assert main(["backup", "prune", "--keep", "1"]) == 0
    output = capsys.readouterr().out
    assert "Removed 1 old backup(s)" in output
    leftovers = list(paths.backup_dir.glob("manual-*.db"))
    assert len(leftovers) == 1


def test_cli_doctor_repairs_fts(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _initialize(tmp_path, monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "thermo.txt").write_text("Entropy in an isolated system", encoding="utf-8")
    assert main(["scan", str(docs)]) == 0
    connection = db.connect(paths.db_path)
    try:
        with db.transaction(connection):
            connection.execute("DELETE FROM files_fts")
    finally:
        connection.close()
    capsys.readouterr()

    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "fts.missing_documents" in output
    assert "Status: 1 unresolved error(s)" in output

    assert main(["doctor", "--repair"]) == 0
    output = capsys.readouterr().out
    assert "Pre-repair backup:" in output
    assert "fts: rebuilt 1 document(s)" in output
    assert "Status: healthy" in output

    assert main(["find", "entropy"]) == 0
    assert "thermo.txt" in capsys.readouterr().out

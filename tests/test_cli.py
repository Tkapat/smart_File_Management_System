from __future__ import annotations

import os
from pathlib import Path

from smartfs.cli import main
from smartfs.config import AppPaths, load_settings


def test_cli_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "lecture.txt"
    note.write_text("Thermodynamics and entropy", encoding="utf-8")
    monkeypatch.setenv("SMARTFS_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--non-interactive"]) == 0
    paths = AppPaths.discover()
    settings = load_settings(paths)
    assert settings.local_only is True
    capsys.readouterr()

    assert main(["watch", "add", str(docs)]) == 0
    capsys.readouterr()
    assert main(["scan"]) == 0
    output = capsys.readouterr().out
    assert "seen=1" in output
    assert "added=1" in output
    assert "xattr_synced=1" in output
    assert "analyzed=1" in output

    assert main(["tag", "add", str(note), "Physics"]) == 0
    tag_output = capsys.readouterr().out
    union_line = next(line for line in tag_output.splitlines() if "Tags (union):" in line)
    assert "physics" in {tag.strip() for tag in union_line.split(":", 1)[1].split(",")}
    mirrored = os.getxattr(note, "user.smartfs.tags").decode()
    assert "physics" in {tag.strip() for tag in mirrored.split(",")}

    assert main(["find", "--tag", "physics", "--explain"]) == 0
    found = capsys.readouterr().out
    assert str(note) in found
    assert "matched tag(s): 'physics'" in found

    assert main(["tags", str(note)]) == 0
    shown = capsys.readouterr().out
    xattr_line = next(
        line.strip() for line in shown.splitlines() if line.strip().startswith("user.smartfs.tags=")
    )
    assert "physics" in {tag.strip() for tag in xattr_line.split("=", 1)[1].split(",")}
    assert "Database tags:" in shown
    assert "[user]" in shown
    assert "[ai]" in shown


def test_cli_full_text_search_and_explain(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "physics.txt"
    note.write_text(
        "Thermodynamics lecture notes. Entropy increases in an isolated system.",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMARTFS_HOME", str(home))
    assert main(["init", "--non-interactive"]) == 0
    assert main(["scan", str(docs)]) == 0
    capsys.readouterr()

    assert main(["find", "entropy", "--mode", "text", "--explain"]) == 0
    output = capsys.readouterr().out
    assert str(note) in output
    assert "Search mode: text" in output
    assert "full-text rank #1" in output
    assert "matched text:" in output

    assert main(["find", "entropy", "--mode", "semantic"]) == 1
    error = capsys.readouterr().err
    assert "requires embeddings" in error

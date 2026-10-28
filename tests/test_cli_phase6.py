from __future__ import annotations

from pathlib import Path

from smartfs.cli import main


def _initialize(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("SMARTFS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--non-interactive"]) == 0
    return tmp_path / "docs"


def test_cli_duplicates(tmp_path: Path, monkeypatch, capsys) -> None:
    docs = _initialize(tmp_path, monkeypatch)
    docs.mkdir()
    (docs / "a.txt").write_text("identical content", encoding="utf-8")
    (docs / "b.txt").write_text("identical content", encoding="utf-8")
    (docs / "unique.txt").write_text("different", encoding="utf-8")
    assert main(["scan", str(docs)]) == 0
    capsys.readouterr()
    assert main(["duplicates"]) == 0
    output = capsys.readouterr().out
    assert "1 duplicate group(s), 2 file(s)" in output
    assert str(docs / "a.txt") in output
    assert str(docs / "b.txt") in output
    assert "unique.txt" not in output


def test_cli_tag_rename_and_merge(tmp_path: Path, monkeypatch, capsys) -> None:
    docs = _initialize(tmp_path, monkeypatch)
    docs.mkdir()
    first = docs / "first.txt"
    second = docs / "second.txt"
    first.write_text("physics notes", encoding="utf-8")
    second.write_text("physics homework", encoding="utf-8")
    assert main(["scan", str(docs)]) == 0
    assert main(["tag", "add", str(first), "physics"]) == 0
    assert main(["tag", "add", str(second), "physics"]) == 0
    capsys.readouterr()

    assert main(["tag", "rename", "physics", "natural-science"]) == 0
    assert "Renamed tag across 2 file(s)." in capsys.readouterr().out
    assert main(["tags", str(first)]) == 0
    assert "natural-science" in capsys.readouterr().out

    assert main(["tag", "merge", "natural-science", "science"]) == 0
    assert "Merged tag into 'science' across 2 file(s)." in capsys.readouterr().out
    assert main(["tags", str(second)]) == 0
    output = capsys.readouterr().out
    assert "science" in output
    assert "natural-science" not in output


def test_cli_collections_roundtrip(tmp_path: Path, monkeypatch, capsys) -> None:
    _initialize(tmp_path, monkeypatch)
    capsys.readouterr()
    assert (
        main(
            [
                "collection",
                "save",
                "Physics Review",
                "--tag",
                "physics",
                "--tag",
                "exam",
                "--category",
                "lecture notes",
                "--mode",
                "hybrid",
                "entropy",
                "notes",
            ]
        )
        == 0
    )
    assert "Saved collection: Physics Review" in capsys.readouterr().out

    assert main(["collection", "list"]) == 0
    output = capsys.readouterr().out
    assert "Physics Review" in output
    assert "query='entropy notes'" in output
    assert "tags=[physics, exam]" in output
    assert "mode=hybrid" in output

    assert main(["collection", "show", "physics review"]) == 0
    output = capsys.readouterr().out
    assert "Category: lecture notes" in output
    assert "Mode: hybrid" in output

    assert main(["collection", "delete", "Physics Review"]) == 0
    assert main(["collection", "list"]) == 0
    assert "No saved searches." in capsys.readouterr().out


def test_cli_related_documents(tmp_path: Path, monkeypatch, capsys) -> None:
    docs = _initialize(tmp_path, monkeypatch)
    docs.mkdir()
    source = docs / "source.txt"
    related = docs / "related.txt"
    source.write_text("entropy and the second law", encoding="utf-8")
    related.write_text("thermodynamics lecture", encoding="utf-8")
    assert main(["scan", str(docs)]) == 0
    assert main(["tag", "add", str(source), "physics"]) == 0
    assert main(["tag", "add", str(related), "physics"]) == 0
    capsys.readouterr()

    assert main(["related", str(source)]) == 0
    output = capsys.readouterr().out
    assert str(related) in output
    assert "shared tags:" in output
    assert "physics" in output

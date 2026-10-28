from __future__ import annotations

from pathlib import Path

import pytest

from smartfs.config import Settings
from smartfs.extractor import (
    CorruptFile,
    EmptyDocument,
    ExtractionError,
    excerpt,
    extract,
    supported_extension,
)


def test_txt_utf8_and_latin1(tmp_path: Path) -> None:
    settings = Settings()
    utf8 = tmp_path / "notes.txt"
    utf8.write_text("héllo entropy", encoding="utf-8")
    document = extract(utf8, settings)
    assert "héllo entropy" in document.text
    assert document.metadata["encoding"] == "utf-8"

    latin = tmp_path / "latin.txt"
    latin.write_bytes("café".encode("latin-1"))
    document = extract(latin, settings)
    assert "café" in document.text
    assert document.metadata["encoding"] in {"latin-1", "utf-8"}


def test_empty_binary_and_unsupported(tmp_path: Path) -> None:
    settings = Settings()
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(EmptyDocument):
        extract(empty, settings)

    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"hello\x00world")
    with pytest.raises(CorruptFile):
        extract(binary, settings)

    unknown = tmp_path / "file.xyz"
    unknown.write_text("x", encoding="utf-8")
    with pytest.raises(ExtractionError):
        extract(unknown, settings)


def test_truncation_and_excerpt(tmp_path: Path) -> None:
    settings = Settings(max_text_chars=1000)
    settings.validate()
    file_path = tmp_path / "long.txt"
    file_path.write_text("a" * 2000, encoding="utf-8")
    document = extract(file_path, settings)
    assert document.text == "a" * 1000
    assert document.truncated is True
    assert excerpt("a" * 5000, 20) == "a" * 20


def test_pdf_extraction_optional(tmp_path: Path) -> None:
    fitz = pytest.importorskip("pymupdf")
    settings = Settings()
    pdf_path = tmp_path / "sample.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Thermodynamics lecture notes")
    document.save(pdf_path)
    document.close()
    extracted = extract(pdf_path, settings)
    assert "Thermodynamics" in extracted.text
    assert extracted.page_count == 1


def test_docx_extraction_optional(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    settings = Settings()
    path = tmp_path / "sample.docx"
    document = docx.Document()
    document.add_heading("Lecture notes", level=1)
    document.add_paragraph("Entropy and heat engines")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Topic"
    table.cell(0, 1).text = "Physics"
    document.save(path)
    extracted = extract(path, settings)
    assert "Entropy" in extracted.text
    assert "Physics" in extracted.text


def test_supported_extension() -> None:
    assert supported_extension("a.TXT")
    assert supported_extension("a.pdf")
    assert supported_extension("a.docx")
    assert not supported_extension("a.png")

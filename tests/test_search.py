from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smartfs import db
from smartfs.config import Settings
from smartfs.embeddings import pack_embedding
from smartfs.scanner import scan_directory
from smartfs.search import SearchError, search_files
from smartfs.tags import add_tag


class _QueryEmbedding:
    model = "fake-model"

    def __init__(self, vector: list[float] | None) -> None:
        self.vector = vector
        self.calls = 0

    def embed_query(self, query: str) -> list[float] | None:
        self.calls += 1
        assert query
        return self.vector


def _scan(tmp_path: Path, connection: sqlite3.Connection, settings: Settings, docs: dict[str, str]):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    for name, content in docs.items():
        (root / name).write_text(content, encoding="utf-8")
    return scan_directory(root, connection, settings), root


def _add_embedding(
    connection: sqlite3.Connection,
    record_path: str,
    values: list[float],
    model: str = "fake-model",
) -> None:
    record = db.get_file_by_path(connection, record_path)
    assert record is not None and record.sha256
    blob, dimension = pack_embedding(values)
    db.upsert_embedding(connection, record.sha256, model, dimension, blob)


def test_fts_searches_full_body_and_reports_snippet(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    settings = Settings()
    filler = "background " * 500
    stats, root = _scan(
        tmp_path,
        connection,
        settings,
        {
            "deep.txt": filler + "uniqueterm appears beyond the stored excerpt",
            "other.txt": "sourdough bread and cooking notes",
        },
    )
    assert stats.errors == 0
    record = db.get_file_by_path(connection, str(root / "deep.txt"))
    assert record is not None
    assert record.text_excerpt and "uniqueterm" not in record.text_excerpt

    report = search_files(connection, settings, "uniqueterm", mode="text")
    assert [hit.record.path for hit in report.hits] == [str(root / "deep.txt")]
    assert report.used_mode == "text"
    assert report.text_candidates == 1
    hit = report.hits[0]
    assert hit.text_rank == 1
    assert hit.text_score is not None and hit.text_score > 0
    assert hit.text_snippet and "uniqueterm" in hit.text_snippet


def test_fts_and_or_and_path_matching(tmp_path: Path, connection: sqlite3.Connection) -> None:
    settings = Settings()
    _, root = _scan(
        tmp_path,
        connection,
        settings,
        {
            "both.txt": "thermodynamics and entropy notes",
            "entropy.txt": "entropy only notes",
            "bread.txt": "sourdough bread recipe",
        },
    )
    both = search_files(connection, settings, "thermodynamics entropy", mode="text")
    assert [hit.record.path for hit in both.hits] == [str(root / "both.txt")]
    assert both.fts_query == '"thermodynamics" AND "entropy"'

    broad = search_files(connection, settings, "thermodynamics bread", mode="text")
    assert {hit.record.path for hit in broad.hits} == {
        str(root / "both.txt"),
        str(root / "bread.txt"),
    }

    by_path = search_files(connection, settings, "entropy.txt", mode="text")
    assert str(root / "entropy.txt") in {hit.record.path for hit in by_path.hits}


def test_query_filters_tags_and_category(tmp_path: Path, connection: sqlite3.Connection) -> None:
    from smartfs.analysis import AnalysisService

    settings = Settings()
    analysis = AnalysisService(settings)
    root = tmp_path / "docs"
    root.mkdir()
    lecture = root / "lecture.txt"
    lecture.write_text(
        "Thermodynamics lecture notes. Entropy increases in an isolated system.",
        encoding="utf-8",
    )
    report_file = root / "report.txt"
    report_file.write_text("Quarterly report with revenue and budget metrics.", encoding="utf-8")
    scan_directory(root, connection, settings, analysis_service=analysis)
    lecture_record = db.get_file_by_path(connection, str(lecture))
    assert lecture_record is not None and lecture_record.id is not None
    add_tag(connection, lecture_record.id, "physics")

    filtered = search_files(
        connection,
        settings,
        "entropy",
        tags=["physics"],
        category="lecture notes",
        mode="text",
    )
    assert [hit.record.path for hit in filtered.hits] == [str(lecture)]
    assert filtered.hits[0].record.category == "lecture notes"

    excluded = search_files(connection, settings, "entropy", tags=["recipe"], mode="text")
    assert excluded.hits == []

    listed = search_files(connection, settings, "", tags=["physics"])
    assert listed.used_mode == "filters"
    assert [hit.record.path for hit in listed.hits] == [str(lecture)]


def test_semantic_search_uses_cosine_ranking(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    settings = Settings(embeddings_enabled=True)
    _, root = _scan(
        tmp_path,
        connection,
        settings,
        {"vector-a.txt": "first document", "vector-b.txt": "second document"},
    )
    _add_embedding(connection, str(root / "vector-a.txt"), [1.0, 0.0])
    _add_embedding(connection, str(root / "vector-b.txt"), [0.0, 1.0])
    service = _QueryEmbedding([1.0, 0.0])

    report = search_files(
        connection,
        settings,
        "anything",
        mode="semantic",
        embedding_service=service,
    )
    assert report.used_mode == "semantic"
    assert service.calls == 1
    assert [hit.record.path for hit in report.hits] == [str(root / "vector-a.txt")]
    assert report.hits[0].semantic_rank == 1
    assert report.hits[0].semantic_similarity == pytest.approx(1.0)


def test_hybrid_rrf_fuses_both_rankings(tmp_path: Path, connection: sqlite3.Connection) -> None:
    settings = Settings(embeddings_enabled=True)
    _, root = _scan(
        tmp_path,
        connection,
        settings,
        {
            "text.txt": "entropy thermodynamics lecture",
            "vector.txt": "sourdough bread recipe",
        },
    )
    _add_embedding(connection, str(root / "text.txt"), [0.1, 1.0])
    _add_embedding(connection, str(root / "vector.txt"), [1.0, 0.0])
    service = _QueryEmbedding([1.0, 0.0])

    report = search_files(
        connection,
        settings,
        "entropy",
        mode="hybrid",
        embedding_service=service,
    )
    assert report.used_mode == "hybrid"
    assert report.text_candidates == 1
    assert report.semantic_candidates == 2
    assert {hit.record.path for hit in report.hits} == {
        str(root / "text.txt"),
        str(root / "vector.txt"),
    }
    text_hit = next(hit for hit in report.hits if hit.record.path == str(root / "text.txt"))
    vector_hit = next(hit for hit in report.hits if hit.record.path == str(root / "vector.txt"))
    assert text_hit.text_rank == 1
    assert text_hit.semantic_rank == 2
    assert vector_hit.text_rank is None
    assert vector_hit.semantic_rank == 1
    assert text_hit.rrf_score == pytest.approx(1 / 61 + 1 / 62)
    assert vector_hit.rrf_score == pytest.approx(1 / 61)
    assert report.hits[0].record.path == str(root / "text.txt")


def test_auto_falls_back_to_text_and_hybrid_reports_reason(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    settings = Settings()
    _, root = _scan(tmp_path, connection, settings, {"note.txt": "entropy notes"})

    auto = search_files(connection, settings, "entropy")
    assert auto.used_mode == "text"
    assert auto.note is None
    assert [hit.record.path for hit in auto.hits] == [str(root / "note.txt")]

    hybrid = search_files(connection, settings, "entropy", mode="hybrid")
    assert hybrid.used_mode == "text"
    assert hybrid.note and "requires embeddings" in hybrid.note


def test_semantic_errors_are_explicit(tmp_path: Path, connection: sqlite3.Connection) -> None:
    settings = Settings()
    _, root = _scan(tmp_path, connection, settings, {"note.txt": "entropy notes"})
    with pytest.raises(SearchError, match="requires embeddings"):
        search_files(connection, settings, "entropy", mode="semantic")

    enabled = Settings(embeddings_enabled=True)
    with pytest.raises(SearchError, match="no stored embeddings"):
        search_files(
            connection,
            enabled,
            "entropy",
            mode="semantic",
            embedding_service=_QueryEmbedding([1.0, 0.0]),
        )

    _add_embedding(connection, str(root / "note.txt"), [1.0, 0.0])
    with pytest.raises(SearchError, match="could not embed"):
        search_files(
            connection,
            enabled,
            "entropy",
            mode="semantic",
            embedding_service=_QueryEmbedding(None),
        )


def test_reindex_replaces_fts_row(tmp_path: Path, connection: sqlite3.Connection) -> None:
    settings = Settings()
    _, root = _scan(tmp_path, connection, settings, {"note.txt": "alpha token content"})
    assert search_files(connection, settings, "alpha", mode="text").hits
    (root / "note.txt").write_text("beta token content", encoding="utf-8")
    scan_directory(root, connection, settings, force=True)
    assert not search_files(connection, settings, "alpha", mode="text").hits
    assert search_files(connection, settings, "beta", mode="text").hits

    renamed = root / "renamed.txt"
    (root / "note.txt").rename(renamed)
    scan_directory(root, connection, settings)
    by_name = search_files(connection, settings, "renamed", mode="text")
    assert [hit.record.path for hit in by_name.hits] == [str(renamed)]


def test_literal_fallback_for_unusual_query(tmp_path: Path, connection: sqlite3.Connection) -> None:
    settings = Settings()
    _, root = _scan(tmp_path, connection, settings, {"percent.txt": "discount 100% table"})
    report = search_files(connection, settings, "%", mode="auto")
    assert report.used_mode == "literal"
    assert [hit.record.path for hit in report.hits] == [str(root / "percent.txt")]


def test_search_limit_is_applied_to_single_source_modes(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    settings = Settings()
    _, root = _scan(
        tmp_path,
        connection,
        settings,
        {"one.txt": "shared token one", "two.txt": "shared token two"},
    )
    report = search_files(connection, settings, "shared", mode="text", limit=1)
    assert len(report.hits) == 1
    assert report.hits[0].record.path.startswith(str(root))

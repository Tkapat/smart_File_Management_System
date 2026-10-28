"""Hybrid FTS5 and local-semantic search with reciprocal-rank fusion."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from smartfs import db
from smartfs.config import Settings
from smartfs.embeddings import EmbeddingError, EmbeddingService, unpack_embedding
from smartfs.models import FileRecord, FileStatus

DEFAULT_RRF_K = 60
CANDIDATE_MULTIPLIER = 4
MIN_CANDIDATE_POOL = 50
_TEXT_TOKEN = re.compile(r"[^\W_][\w+#.'-]*", re.UNICODE)


class SearchMode(StrEnum):
    """Search strategies exposed by the CLI and API."""

    AUTO = "auto"
    TEXT = "text"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class SearchError(RuntimeError):
    """Raised when an explicitly requested search mode is unavailable."""


@dataclass(slots=True)
class SearchHit:
    """One fused search result with optional source-specific evidence."""

    record: FileRecord
    rrf_score: float = 0.0
    text_rank: int | None = None
    text_score: float | None = None
    text_snippet: str | None = None
    semantic_rank: int | None = None
    semantic_similarity: float | None = None

    @property
    def sources(self) -> list[str]:
        sources: list[str] = []
        if self.text_rank is not None:
            sources.append("text")
        if self.semantic_rank is not None:
            sources.append("semantic")
        return sources


@dataclass(slots=True)
class SearchReport:
    """Search results plus a non-secret description of what ran."""

    hits: list[SearchHit]
    requested_mode: SearchMode
    used_mode: str
    text_candidates: int = 0
    semantic_candidates: int = 0
    fts_query: str | None = None
    note: str | None = None


@dataclass(slots=True)
class _Candidate:
    record: FileRecord
    text_score: float | None = None
    text_snippet: str | None = None
    semantic_similarity: float | None = None


@dataclass(slots=True)
class _FilterSql:
    clauses: list[str] = field(default_factory=list)
    parameters: list[object] = field(default_factory=list)


def search_files(
    connection: sqlite3.Connection,
    settings: Settings,
    query: str,
    *,
    tags: Sequence[str] = (),
    category: str | None = None,
    limit: int = 100,
    mode: SearchMode | str = SearchMode.AUTO,
    embedding_service: EmbeddingService | None = None,
    rrf_k: int = DEFAULT_RRF_K,
) -> SearchReport:
    """Search by filters plus fused FTS5 and semantic rankings."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if rrf_k < 1:
        raise ValueError("rrf_k must be positive")
    requested = SearchMode(mode)
    text = " ".join((query or "").replace("\x00", " ").split())
    if not text:
        rows = db.query_files(
            connection,
            tags=tags,
            category=category,
            status=FileStatus.DONE,
            limit=limit,
        )
        hits = [SearchHit(record=row) for row in rows]
        return SearchReport(hits, requested, "filters")

    filters = _build_filters(tags=tags, category=category)
    candidate_limit = max(limit * CANDIDATE_MULTIPLIER, MIN_CANDIDATE_POOL)

    terms = _query_terms(text)
    if not terms:
        rows = db.query_files(
            connection,
            tags=tags,
            category=category,
            text=text,
            status=FileStatus.DONE,
            limit=limit,
        )
        return SearchReport(
            [SearchHit(record=row) for row in rows],
            requested,
            "literal",
            note="query had no tokenizable terms; used literal filtering",
        )

    if requested == SearchMode.SEMANTIC:
        text_candidates: list[_Candidate] = []
        fts_query = None
    else:
        text_candidates, fts_query, note = _text_candidates(
            connection,
            terms,
            filters,
            candidate_limit,
        )
    semantic_candidates: list[_Candidate] = []
    semantic_note: str | None = None
    if requested in {SearchMode.AUTO, SearchMode.SEMANTIC, SearchMode.HYBRID}:
        semantic_candidates, semantic_note = _semantic_candidates(
            connection,
            settings,
            text,
            filters,
            candidate_limit,
            embedding_service,
            force=requested != SearchMode.AUTO,
        )

    if requested == SearchMode.TEXT:
        if not text_candidates:
            return SearchReport([], requested, "text", fts_query=fts_query)
        return SearchReport(
            _single_source_hits(text_candidates, source="text", rrf_k=rrf_k)[:limit],
            requested,
            "text",
            text_candidates=len(text_candidates),
            fts_query=fts_query,
        )

    if requested == SearchMode.SEMANTIC:
        if semantic_note and not semantic_candidates:
            raise SearchError(semantic_note)
        return SearchReport(
            _single_source_hits(semantic_candidates, source="semantic", rrf_k=rrf_k)[:limit],
            requested,
            "semantic",
            semantic_candidates=len(semantic_candidates),
            note=semantic_note,
        )

    if not semantic_candidates:
        note_parts = [value for value in (note, semantic_note) if value]
        return SearchReport(
            _single_source_hits(text_candidates, source="text", rrf_k=rrf_k)[:limit],
            requested,
            "text",
            text_candidates=len(text_candidates),
            fts_query=fts_query,
            note="; ".join(note_parts) or None,
        )

    hits = _fuse(text_candidates, semantic_candidates, rrf_k=rrf_k)
    note_parts = [value for value in (note, semantic_note) if value]
    return SearchReport(
        hits[:limit],
        requested,
        "hybrid",
        text_candidates=len(text_candidates),
        semantic_candidates=len(semantic_candidates),
        fts_query=fts_query,
        note="; ".join(note_parts) or None,
    )


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _TEXT_TOKEN.finditer(query):
        term = match.group(0).strip("._'-")[:64]
        if not term:
            continue
        folded = term.casefold()
        if folded not in seen:
            seen.add(folded)
            terms.append(term)
    return terms


def _fts_query(terms: Sequence[str], operator: str) -> str:
    return f" {operator} ".join(f'"{term}"' for term in terms)


def _build_filters(*, tags: Sequence[str], category: str | None) -> _FilterSql:
    filters = _FilterSql()
    filters.clauses.append("f.status = ?")
    filters.parameters.append(FileStatus.DONE.value)
    if category is not None:
        filters.clauses.append("f.category = ?")
        filters.parameters.append(category.strip().lower())
    for tag in tags:
        filters.clauses.append(
            "EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id = ft.tag_id "
            "WHERE ft.file_id = f.id AND t.name = ?)"
        )
        filters.parameters.append(tag)
    return filters


def _text_candidates(
    connection: sqlite3.Connection,
    terms: Sequence[str],
    filters: _FilterSql,
    limit: int,
) -> tuple[list[_Candidate], str | None, str | None]:
    operators = ("AND", "OR") if len(terms) > 1 else ("AND",)
    for operator in operators:
        fts_query = _fts_query(terms, operator)
        sql = (
            "SELECT f.*, bm25(files_fts, 8.0, 5.0, 1.0, 2.0, 2.0, 1.0) AS text_score, "
            "snippet(files_fts, 5, '[', ']', ' … ', 20) AS text_snippet "
            "FROM files_fts JOIN files f ON f.id = files_fts.rowid "
            f"WHERE files_fts MATCH ? AND {' AND '.join(filters.clauses)} "
            "ORDER BY text_score ASC, f.mtime_ns DESC, f.path LIMIT ?"
        )
        parameters: list[object] = [fts_query, *filters.parameters, limit]
        try:
            rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            return [], fts_query, f"full-text query failed: {type(exc).__name__}"
        if rows:
            candidates = [
                _Candidate(
                    record=FileRecord.model_validate(dict(row)),
                    text_score=-float(row["text_score"]),
                    text_snippet=_clean_snippet(row["text_snippet"]),
                )
                for row in rows
            ]
            return candidates, fts_query, None
    return [], _fts_query(terms, operators[-1]), None


def _semantic_candidates(
    connection: sqlite3.Connection,
    settings: Settings,
    query: str,
    filters: _FilterSql,
    limit: int,
    embedding_service: EmbeddingService | None,
    *,
    force: bool,
) -> tuple[list[_Candidate], str | None]:
    if not settings.embeddings_enabled:
        return [], (
            "semantic search requires embeddings; run 'smartfs ai enable --embeddings'"
            if force
            else None
        )
    if embedding_service is None:
        return [], "semantic search is unavailable: embedding service was not initialized"
    query_vector = embedding_service.embed_query(query)
    if not query_vector:
        return [], "semantic search is unavailable: could not embed the query"
    model = embedding_service.model
    sql = (
        "SELECT f.*, e.vector AS embedding_vector, e.dimension AS embedding_dimension "
        "FROM embeddings e JOIN files f ON f.sha256 = e.sha256 "
        f"WHERE e.model = ? AND {' AND '.join(filters.clauses)}"
    )
    parameters: list[object] = [model, *filters.parameters]
    rows = connection.execute(sql, parameters).fetchall()
    if not rows:
        return [], f"no stored embeddings for model {model}; run a scan with embeddings enabled"

    query_norm = math.sqrt(sum(value * value for value in query_vector))
    if query_norm == 0:
        return [], "semantic search is unavailable: query embedding has zero length"
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        try:
            vector = unpack_embedding(row["embedding_vector"], int(row["embedding_dimension"]))
        except (EmbeddingError, TypeError, ValueError):
            continue
        if len(vector) != len(query_vector):
            continue
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            continue
        similarity = sum(a * b for a, b in zip(query_vector, vector, strict=True)) / (
            query_norm * norm
        )
        if similarity > 0:
            scored.append((similarity, row))
    scored.sort(key=lambda item: (-item[0], -int(item[1]["mtime_ns"]), str(item[1]["path"])))
    candidates = [
        _Candidate(
            record=FileRecord.model_validate(dict(row)),
            semantic_similarity=float(similarity),
        )
        for similarity, row in scored[:limit]
    ]
    if not candidates:
        return [], "no positive semantic matches for the query"
    return candidates, None


def _single_source_hits(
    candidates: Sequence[_Candidate],
    *,
    source: str,
    rrf_k: int,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for rank, candidate in enumerate(candidates, start=1):
        hit = SearchHit(record=candidate.record)
        if source == "text":
            hit.text_rank = rank
            hit.text_score = candidate.text_score
            hit.text_snippet = candidate.text_snippet
        else:
            hit.semantic_rank = rank
            hit.semantic_similarity = candidate.semantic_similarity
        hit.rrf_score = 1.0 / (rrf_k + rank)
        hits.append(hit)
    return hits


def _fuse(
    text_candidates: Sequence[_Candidate],
    semantic_candidates: Sequence[_Candidate],
    *,
    rrf_k: int,
) -> list[SearchHit]:
    hits: dict[int, SearchHit] = {}
    for rank, candidate in enumerate(text_candidates, start=1):
        file_id = _file_id(candidate.record)
        hit = hits.setdefault(file_id, SearchHit(record=candidate.record))
        hit.text_rank = rank
        hit.text_score = candidate.text_score
        hit.text_snippet = candidate.text_snippet
        hit.rrf_score += 1.0 / (rrf_k + rank)
    for rank, candidate in enumerate(semantic_candidates, start=1):
        file_id = _file_id(candidate.record)
        hit = hits.setdefault(file_id, SearchHit(record=candidate.record))
        hit.semantic_rank = rank
        hit.semantic_similarity = candidate.semantic_similarity
        hit.rrf_score += 1.0 / (rrf_k + rank)
    ordered = sorted(
        hits.values(),
        key=lambda hit: (
            -hit.rrf_score,
            -int(hit.record.mtime_ns),
            hit.record.path,
        ),
    )
    return ordered


def _file_id(record: FileRecord) -> int:
    if record.id is None:
        raise SearchError("search candidate is missing a database id")
    return int(record.id)


def _clean_snippet(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.replace("\x00", " ").split())
    return cleaned or None

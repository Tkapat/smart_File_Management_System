"""Related-document discovery from shared tags and local embedding similarity."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from smartfs import db
from smartfs.config import Settings
from smartfs.embeddings import EmbeddingError, unpack_embedding
from smartfs.models import FileRecord, FileStatus


class RelatedError(RuntimeError):
    """Raised when related documents cannot be computed."""


@dataclass(slots=True)
class RelatedHit:
    """One related file with the reasons it was selected."""

    record: FileRecord
    shared_tags: list[str]
    similarity: float | None = None
    score: float = 0.0

    @property
    def reason(self) -> str:
        parts: list[str] = []
        if self.shared_tags:
            parts.append("shared tags: " + ", ".join(self.shared_tags))
        if self.similarity is not None:
            parts.append(f"semantic cosine {self.similarity:.3f}")
        return "; ".join(parts) or "related metadata"


def related_files(
    connection: sqlite3.Connection,
    settings: Settings,
    file_id: int,
    *,
    limit: int = 10,
) -> list[RelatedHit]:
    """Return related indexed files ranked by shared tags and local cosine."""
    if limit < 1:
        raise ValueError("limit must be positive")
    source = db.get_file_by_id(connection, file_id)
    if source is None:
        raise RelatedError("file is not indexed")
    if source.status != FileStatus.DONE:
        raise RelatedError("related documents require a fully indexed file")

    records: dict[int, FileRecord] = {}
    shared: dict[int, list[str]] = {}
    source_tags = set(db.tag_union(connection, file_id))
    if source_tags:
        rows = connection.execute(
            "SELECT f.*, COUNT(DISTINCT t.name) AS shared_count "
            "FROM files f "
            "JOIN file_tags ft ON ft.file_id = f.id "
            "JOIN tags t ON t.id = ft.tag_id "
            "WHERE f.status = 'done' AND f.id != ? "
            "AND ft.tag_id IN (SELECT tag_id FROM file_tags WHERE file_id = ?) "
            "GROUP BY f.id ORDER BY shared_count DESC, f.mtime_ns DESC, f.path LIMIT ?",
            (file_id, file_id, max(limit * 4, 40)),
        ).fetchall()
        for row in rows:
            record = FileRecord.model_validate(dict(row))
            if record.id is None or record.sha256 == source.sha256:
                continue
            records[record.id] = record
            shared[record.id] = sorted(
                source_tags.intersection(db.tag_union(connection, record.id))
            )

    similarity: dict[int, float] = {}
    if settings.embeddings_enabled and source.sha256:
        source_row = db.get_embedding(connection, source.sha256, settings.embedding_model)
        if source_row is not None:
            try:
                source_vector = unpack_embedding(source_row["vector"], int(source_row["dimension"]))
            except (EmbeddingError, TypeError, ValueError):
                source_vector = []
            if source_vector:
                rows = connection.execute(
                    "SELECT f.*, e.vector AS embedding_vector, "
                    "e.dimension AS embedding_dimension "
                    "FROM embeddings e JOIN files f ON f.sha256 = e.sha256 "
                    "WHERE e.model = ? AND f.status = 'done' AND f.id != ? "
                    "AND f.sha256 != ?",
                    (settings.embedding_model, file_id, source.sha256),
                ).fetchall()
                source_norm = math.sqrt(sum(value * value for value in source_vector))
                for row in rows:
                    try:
                        vector = unpack_embedding(
                            row["embedding_vector"], int(row["embedding_dimension"])
                        )
                    except (EmbeddingError, TypeError, ValueError):
                        continue
                    if len(vector) != len(source_vector):
                        continue
                    norm = math.sqrt(sum(value * value for value in vector))
                    if norm == 0 or source_norm == 0:
                        continue
                    cosine = sum(a * b for a, b in zip(source_vector, vector, strict=True)) / (
                        source_norm * norm
                    )
                    if cosine <= 0:
                        continue
                    record = FileRecord.model_validate(dict(row))
                    if record.id is None:
                        continue
                    records.setdefault(record.id, record)
                    similarity[record.id] = cosine

    results: list[RelatedHit] = []
    for candidate_id, record in records.items():
        shared_tags = shared.get(candidate_id, [])
        cosine = similarity.get(candidate_id)
        if not shared_tags and cosine is None:
            continue
        score = float(len(shared_tags)) + (max(cosine or 0.0, 0.0) * 2.0)
        results.append(
            RelatedHit(
                record=record,
                shared_tags=shared_tags,
                similarity=cosine,
                score=score,
            )
        )
    results.sort(
        key=lambda item: (
            -item.score,
            -item.record.mtime_ns,
            item.record.path,
        )
    )
    return results[:limit]

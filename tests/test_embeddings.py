from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smartfs import db
from smartfs.config import Settings
from smartfs.embeddings import (
    EmbeddingError,
    EmbeddingService,
    embedding_label,
    pack_embedding,
    unpack_embedding,
)


class _FakeProvider:
    model_name = "fake-model"

    def __init__(self, vector: list[float], error: Exception | None = None) -> None:
        self.vector = vector
        self.error = error
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert text
        return self.vector


def _settings(**overrides) -> Settings:
    values = {"embeddings_enabled": True}
    values.update(overrides)
    return Settings(**values)


def test_pack_and_unpack_embedding() -> None:
    blob, dimension = pack_embedding([0.5, -1.25, 2.0])
    assert dimension == 3
    assert unpack_embedding(blob, dimension) == pytest.approx([0.5, -1.25, 2.0])
    with pytest.raises(EmbeddingError):
        pack_embedding([])
    with pytest.raises(EmbeddingError):
        pack_embedding([float("nan")])
    with pytest.raises(EmbeddingError):
        unpack_embedding(blob, 2)


def test_disabled_embeddings_do_not_call_provider(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    provider = _FakeProvider([1.0, 2.0])
    service = EmbeddingService(Settings(embeddings_enabled=False), tmp_path, provider=provider)
    assert service.embed(connection, "a" * 64, text="hello") is None
    assert provider.calls == 0


def test_embedding_service_stores_and_caches(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    provider = _FakeProvider([0.1, 0.2, 0.3])
    service = EmbeddingService(_settings(), tmp_path, provider=provider)
    first = service.embed(connection, "b" * 64, text="document body", title="Title")
    assert first is not None
    assert first.cached is False
    assert first.dimension == 3
    assert first.vector is not None
    db.upsert_embedding(connection, "b" * 64, "fake-model", first.dimension, first.vector)
    stored = db.get_embedding(connection, "b" * 64, "fake-model")
    assert stored is not None
    second = service.embed(connection, "b" * 64, text="document body")
    assert second is not None
    assert second.cached is True
    assert provider.calls == 1


def test_embedding_failure_is_contained(tmp_path: Path, connection: sqlite3.Connection) -> None:
    provider = _FakeProvider([1.0], error=RuntimeError("model missing"))
    service = EmbeddingService(_settings(), tmp_path, provider=provider)
    assert service.embed(connection, "c" * 64, text="hello") is None
    assert service.embed(connection, "d" * 64, text="again") is None
    assert provider.calls == 1


def test_embedding_label(tmp_path: Path) -> None:
    assert embedding_label(Settings(embeddings_enabled=False)) == "disabled"
    label = embedding_label(_settings(embedding_model="test-model"))
    assert label.startswith("test-model (")

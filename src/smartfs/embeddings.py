"""Local dense embeddings with fastembed and SQLite persistence helpers."""

from __future__ import annotations

import importlib.util
import logging
import math
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from smartfs import db
from smartfs.config import Settings

LOGGER = logging.getLogger("smartfs.embeddings")


class EmbeddingError(RuntimeError):
    """Raised when a local embedding cannot be generated or decoded."""


class EmbeddingProvider(Protocol):
    """Minimal provider contract used by the indexing pipeline."""

    model_name: str

    def embed(self, text: str) -> list[float]:
        """Return one dense vector for the supplied text."""


@dataclass(slots=True)
class EmbeddingOutcome:
    """A newly generated or already cached dense vector."""

    provider: str
    model: str
    dimension: int
    vector: bytes | None = None
    cached: bool = False
    reason: str | None = None


def fastembed_available() -> bool:
    """Return True when the optional fastembed package is installed."""
    return importlib.util.find_spec("fastembed") is not None


class FastEmbedProvider:
    """Lazy fastembed wrapper that never downloads during object construction."""

    def __init__(self, model_name: str, cache_dir: str | Path) -> None:
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise EmbeddingError(
                    "fastembed is not installed; install the ai extra with 'uv sync --extra ai'"
                ) from exc
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._model = TextEmbedding(
                    model_name=self.model_name,
                    cache_dir=str(self.cache_dir),
                )
            except Exception as exc:
                raise EmbeddingError(
                    f"Could not load embedding model {self.model_name}: {type(exc).__name__}"
                ) from exc
            return self._model

    def embed(self, text: str) -> list[float]:
        model = self._ensure_model()
        try:
            with self._lock:
                vector = next(iter(model.embed([text])))
        except Exception as exc:
            raise EmbeddingError(f"Embedding generation failed: {type(exc).__name__}") from exc
        values = [float(value) for value in vector]
        if not values:
            raise EmbeddingError("Embedding model returned an empty vector")
        return values


class EmbeddingService:
    """Generate and cache local embeddings without failing file indexing."""

    def __init__(
        self,
        settings: Settings,
        cache_dir: str | Path,
        *,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider or FastEmbedProvider(
            settings.embedding_model,
            cache_dir,
        )
        self.model = self.provider.model_name
        self._warned = False
        self._disabled_reason: str | None = None
        self._lock = threading.Lock()

    def embed(
        self,
        connection: sqlite3.Connection,
        sha256: str,
        *,
        text: str,
        title: str | None = None,
    ) -> EmbeddingOutcome | None:
        """Return a vector outcome, or ``None`` when disabled/unavailable."""
        if not self.settings.embeddings_enabled:
            return None
        with self._lock:
            if self._disabled_reason is not None:
                return None
        existing = db.get_embedding(connection, sha256, self.model)
        if existing is not None:
            return EmbeddingOutcome(
                "fastembed",
                self.model,
                int(existing["dimension"]),
                cached=True,
            )
        payload = _embedding_input(
            text,
            title=title,
            max_chars=self.settings.embedding_max_chars,
        )
        if not payload:
            return None
        try:
            vector = self.provider.embed(payload)
        except Exception as exc:
            with self._lock:
                self._disabled_reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                if not self._warned:
                    self._warned = True
                    LOGGER.warning(
                        "Local embeddings unavailable; continuing without vectors",
                        extra={
                            "event": "embedding.unavailable",
                            "provider": "fastembed",
                            "model": self.model,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:300],
                        },
                    )
            return None
        blob, dimension = pack_embedding(vector)
        return EmbeddingOutcome(
            "fastembed",
            self.model,
            dimension,
            vector=blob,
        )

    def embed_query(self, query: str) -> list[float] | None:
        """Embed a query without persisting it; return None when unavailable."""
        if not self.settings.embeddings_enabled:
            return None
        text = " ".join((query or "").replace("\x00", " ").split())
        if not text:
            return None
        with self._lock:
            if self._disabled_reason is not None:
                return None
        try:
            vector = self.provider.embed(text[: self.settings.embedding_max_chars])
        except Exception as exc:
            with self._lock:
                self._disabled_reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                if not self._warned:
                    self._warned = True
                    LOGGER.warning(
                        "Local embeddings unavailable; semantic search is disabled",
                        extra={
                            "event": "embedding.query_unavailable",
                            "provider": "fastembed",
                            "model": self.model,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:300],
                        },
                    )
            return None
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            return None
        return values


def _embedding_input(
    text: str,
    *,
    title: str | None,
    max_chars: int,
) -> str:
    parts = [value for value in (title,) if value]
    body = text.replace("\x00", " ").strip()
    if body:
        parts.append(body[:max_chars])
    return "\n".join(parts).strip()


def pack_embedding(values: list[float] | tuple[float, ...]) -> tuple[bytes, int]:
    """Pack a dense vector as little-endian float32 bytes."""
    if not values:
        raise EmbeddingError("Cannot pack an empty embedding")
    if len(values) > 65_536:
        raise EmbeddingError("Embedding dimension is unreasonably large")
    for value in values:
        if not math.isfinite(float(value)):
            raise EmbeddingError("Embedding contains a non-finite value")
    dimension = len(values)
    return struct.pack(f"<{dimension}f", *(float(value) for value in values)), dimension


def unpack_embedding(blob: bytes, dimension: int) -> list[float]:
    """Decode float32 little-endian bytes and verify the expected dimension."""
    if dimension < 1:
        raise EmbeddingError("Embedding dimension must be positive")
    if len(blob) != dimension * 4:
        raise EmbeddingError("Embedding byte length does not match its dimension")
    return list(struct.unpack(f"<{dimension}f", blob))


def embedding_label(settings: Settings) -> str:
    """Return a short status label for configured embeddings."""
    if not settings.embeddings_enabled:
        return "disabled"
    installed = "installed" if fastembed_available() else "ai extra not installed"
    return f"{settings.embedding_model} ({installed})"

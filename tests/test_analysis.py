from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from smartfs import db
from smartfs.analysis import (
    GEMINI_PROVIDER,
    LOCAL_PROVIDER,
    AnalysisError,
    AnalysisService,
    _parse_gemini_json,
    local_analyze,
)
from smartfs.config import DEFAULT_GEMINI_MODEL, Settings
from smartfs.models import Category, ExtractedDocument


def _document() -> ExtractedDocument:
    return ExtractedDocument(
        text=(
            "Thermodynamics lecture notes. Entropy always increases in an isolated system. "
            "The second law is central to this course."
        ),
        title="Physics lecture",
    )


def test_local_analysis_is_structured_and_deterministic() -> None:
    document = _document()
    first = local_analyze(document, "/tmp/physics-lecture.txt")
    second = local_analyze(document, "/tmp/physics-lecture.txt")
    assert first == second
    assert first.category == Category.LECTURE_NOTES
    assert 3 <= len(first.tags) <= 8
    assert "physics" in first.tags
    assert "Thermodynamics lecture notes." in first.summary
    assert first.confidence > 0


def test_local_analysis_service_caches_without_network(
    connection: sqlite3.Connection,
) -> None:
    settings = Settings()
    service = AnalysisService(settings)
    first = service.analyze(connection, _document(), "/tmp/notes.txt", "a" * 64)
    assert first is not None
    assert first.provider == LOCAL_PROVIDER
    assert first.cached is False
    second = service.analyze(connection, _document(), "/tmp/notes.txt", "a" * 64)
    assert second is not None
    assert second.provider == LOCAL_PROVIDER
    assert second.cached is True
    assert db.get_cached_analysis(connection, "a" * 64, LOCAL_PROVIDER, "heuristic-v1", 1)


def test_cloud_without_key_falls_back_locally(connection: sqlite3.Connection) -> None:
    settings = Settings(consent=True, local_only=False)
    service = AnalysisService(settings, key_getter=lambda: None)
    outcome = service.analyze(connection, _document(), "/tmp/notes.txt", "b" * 64)
    assert outcome is not None
    assert outcome.provider == LOCAL_PROVIDER
    assert outcome.fallback_reason == "no Gemini API key configured"


class _FakeModels:
    def __init__(self, response_text: str | None = None, error: Exception | None = None) -> None:
        self.response_text = response_text
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.response_text)


class _FakeClient:
    def __init__(self, models: _FakeModels) -> None:
        self.models = models


def test_gemini_provider_and_cache(connection: sqlite3.Connection) -> None:
    payload = {
        "category": "lecture notes",
        "tags": ["physics", "entropy", "thermodynamics"],
        "summary": "Lecture notes on entropy.",
        "topics": ["physics"],
        "confidence": 0.9,
    }
    models = _FakeModels(json.dumps(payload))
    service = AnalysisService(
        Settings(consent=True, local_only=False),
        key_getter=lambda: "fake-key",
        client_factory=lambda _key, _timeout: _FakeClient(models),
    )
    outcome = service.analyze(connection, _document(), "/tmp/notes.txt", "c" * 64)
    assert outcome is not None
    assert outcome.provider == GEMINI_PROVIDER
    assert outcome.result.tags == ["physics", "entropy", "thermodynamics"]
    assert len(models.calls) == 1
    assert "Document excerpt" in str(models.calls[0]["contents"])
    assert models.calls[0]["model"] == DEFAULT_GEMINI_MODEL

    cached = service.analyze(connection, _document(), "/tmp/notes.txt", "c" * 64)
    assert cached is not None
    assert cached.cached is True
    assert len(models.calls) == 1


def test_gemini_failure_falls_back_to_local(connection: sqlite3.Connection) -> None:
    models = _FakeModels(error=RuntimeError("network down"))
    service = AnalysisService(
        Settings(consent=True, local_only=False),
        key_getter=lambda: "fake-key",
        client_factory=lambda _key, _timeout: _FakeClient(models),
    )
    outcome = service.analyze(connection, _document(), "/tmp/notes.txt", "d" * 64)
    assert outcome is not None
    assert outcome.provider == LOCAL_PROVIDER
    assert outcome.fallback_reason == "network down"


def test_parse_gemini_json_accepts_fences_and_rejects_invalid() -> None:
    body = {
        "category": "report",
        "tags": ["metrics", "revenue", "budget"],
        "summary": "Quarterly results.",
        "topics": [],
        "confidence": 0.7,
    }
    parsed = _parse_gemini_json(f"```json\n{json.dumps(body)}\n```")
    assert parsed.category == Category.REPORT
    with pytest.raises(AnalysisError):
        _parse_gemini_json("not json")


def test_disabled_analysis_returns_none(connection: sqlite3.Connection) -> None:
    service = AnalysisService(Settings(analysis_enabled=False))
    assert service.analyze(connection, _document(), "/tmp/notes.txt", "e" * 64) is None


def test_cloud_credential_lookup_failure_falls_back(
    connection: sqlite3.Connection,
) -> None:
    def broken_key_getter():
        raise RuntimeError("key backend exploded")

    service = AnalysisService(
        Settings(consent=True, local_only=False),
        key_getter=broken_key_getter,
    )
    outcome = service.analyze(connection, _document(), "/tmp/notes.txt", "f" * 64)
    assert outcome is not None
    assert outcome.provider == LOCAL_PROVIDER
    assert outcome.fallback_reason == "Credential lookup failed: RuntimeError"

"""Local heuristic and Gemini-backed structured document analysis."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from smartfs import db
from smartfs.config import ConfigError, Settings, redact_secrets
from smartfs.credentials import CredentialError, get_api_key
from smartfs.models import AnalysisResult, Category, ExtractedDocument
from smartfs.tags import TagError, normalize_tag

LOGGER = logging.getLogger("smartfs.analysis")

LOCAL_PROVIDER = "local"
LOCAL_MODEL = "heuristic-v1"
LOCAL_PROMPT_VERSION = 1
GEMINI_PROVIDER = "gemini"
GEMINI_PROMPT_VERSION = 1

_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "among",
    "and",
    "are",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "can",
    "could",
    "does",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "here",
    "how",
    "into",
    "its",
    "just",
    "more",
    "most",
    "not",
    "now",
    "only",
    "other",
    "our",
    "out",
    "over",
    "same",
    "should",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "you",
    "your",
    "document",
    "file",
}

_CATEGORY_KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.REPORT: (
        "quarterly",
        "annual report",
        "revenue",
        "findings",
        "metrics",
        "budget",
        "summary",
        "report",
    ),
    Category.TODO_LIST: (
        "todo",
        "to-do",
        "checklist",
        "task list",
        "tasks",
        "pending",
        "deadline",
        "action items",
    ),
    Category.LECTURE_NOTES: (
        "lecture",
        "lecture notes",
        "course",
        "professor",
        "chapter",
        "theorem",
        "entropy",
        "thermodynamics",
        "exam",
    ),
    Category.RESEARCH_PAPER: (
        "abstract",
        "references",
        "methodology",
        "hypothesis",
        "experiment",
        "results",
        "doi",
        "journal",
        "et al",
    ),
    Category.INVOICE: (
        "invoice",
        "amount due",
        "subtotal",
        "total",
        "tax",
        "payment",
        "receipt",
        "billing",
    ),
    Category.MEETING_NOTES: (
        "meeting",
        "minutes",
        "attendees",
        "agenda",
        "discussed",
        "decisions",
        "action items",
        "next steps",
    ),
}

_CATEGORY_LABELS = {
    Category.REPORT: "report",
    Category.TODO_LIST: "checklist",
    Category.LECTURE_NOTES: "lecture",
    Category.RESEARCH_PAPER: "research",
    Category.INVOICE: "invoice",
    Category.MEETING_NOTES: "meeting",
    Category.OTHER: "notes",
}

_TOKEN_PATTERN = re.compile(r"[^\W_][\w+#.'-]*", re.UNICODE)
_SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")
_CATEGORY_NAMES = ", ".join(category.value for category in Category)


class AnalysisError(RuntimeError):
    """Raised when an analysis provider cannot produce a valid result."""


class GeminiClient(Protocol):
    """Minimal client surface used by the Gemini provider."""

    models: Any


ClientFactory = Callable[[str, float], GeminiClient]
KeyGetter = Callable[[], str | None]


@dataclass(slots=True)
class AnalysisOutcome:
    """Validated analysis and the provider identity that produced it."""

    result: AnalysisResult
    provider: str
    model: str
    prompt_version: int
    cached: bool = False
    fallback_reason: str | None = None


class RateLimiter:
    """Small thread-safe sliding-window limiter for cloud requests."""

    def __init__(
        self, calls_per_minute: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if calls_per_minute < 1:
            raise ValueError("calls_per_minute must be positive")
        self.calls_per_minute = calls_per_minute
        self.clock = clock
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until another request is allowed in the current window."""
        with self._lock:
            now = self.clock()
            cutoff = now - 60.0
            self._timestamps = [stamp for stamp in self._timestamps if stamp > cutoff]
            if len(self._timestamps) >= self.calls_per_minute:
                wait = max(0.0, self._timestamps[0] + 60.0 - now)
                if wait:
                    time.sleep(wait)
                now = self.clock()
                cutoff = now - 60.0
                self._timestamps = [stamp for stamp in self._timestamps if stamp > cutoff]
            self._timestamps.append(now)


class AnalysisService:
    """Select cached Gemini or deterministic local analysis for one document."""

    def __init__(
        self,
        settings: Settings,
        *,
        key_getter: KeyGetter = get_api_key,
        client_factory: ClientFactory | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.key_getter = key_getter
        self.client_factory = client_factory or _default_gemini_client
        self.rate_limiter = rate_limiter or RateLimiter(settings.rate_limit_per_minute)
        self._client: GeminiClient | None = None
        self._client_checked = False
        self._client_error: str | None = None
        self._lock = threading.Lock()

    @property
    def cloud_requested(self) -> bool:
        return (
            self.settings.analysis_enabled
            and self.settings.consent
            and not self.settings.local_only
        )

    def analyze(
        self,
        connection: sqlite3.Connection,
        document: ExtractedDocument,
        path: str,
        sha256: str,
    ) -> AnalysisOutcome | None:
        """Return cached or fresh analysis; cloud failures fall back locally."""
        if not self.settings.analysis_enabled:
            return None
        if self.cloud_requested:
            cached = self._cached(connection, sha256, GEMINI_PROVIDER, self.settings.gemini_model)
            if cached is not None:
                return cached
            error = self._ensure_client()
            if error is None:
                try:
                    self.rate_limiter.acquire()
                    result = self._analyze_gemini(document, path)
                except Exception as exc:
                    message = redact_secrets(str(exc))[:300]
                    LOGGER.warning(
                        "Gemini analysis failed; using local fallback",
                        extra={
                            "event": "analysis.gemini_failed",
                            "path": path,
                            "error_type": type(exc).__name__,
                            "error": message,
                        },
                    )
                    if isinstance(exc, db.DatabaseError):
                        raise
                    return self._local(connection, document, path, sha256, message)
                else:
                    db.cache_analysis(
                        connection,
                        sha256,
                        GEMINI_PROVIDER,
                        self.settings.gemini_model,
                        GEMINI_PROMPT_VERSION,
                        result,
                    )
                    return AnalysisOutcome(
                        result,
                        GEMINI_PROVIDER,
                        self.settings.gemini_model,
                        GEMINI_PROMPT_VERSION,
                    )
            return self._local(connection, document, path, sha256, error)
        return self._local(connection, document, path, sha256, None)

    def _cached(
        self,
        connection: sqlite3.Connection,
        sha256: str,
        provider: str,
        model: str,
    ) -> AnalysisOutcome | None:
        version = GEMINI_PROMPT_VERSION if provider == GEMINI_PROVIDER else LOCAL_PROMPT_VERSION
        result = db.get_cached_analysis(connection, sha256, provider, model, version)
        if result is None:
            return None
        return AnalysisOutcome(result, provider, model, version, cached=True)

    def _ensure_client(self) -> str | None:
        with self._lock:
            if self._client_checked:
                return self._client_error
            self._client_checked = True
            try:
                key = self.key_getter()
            except CredentialError as exc:
                self._client_error = str(exc)
                return self._client_error
            except Exception as exc:
                self._client_error = f"Credential lookup failed: {type(exc).__name__}"
                LOGGER.warning(
                    "Gemini key lookup failed; using local fallback",
                    extra={
                        "event": "analysis.credential_lookup_failed",
                        "error_type": type(exc).__name__,
                    },
                )
                return self._client_error
            if not key:
                self._client_error = "no Gemini API key configured"
                LOGGER.warning(
                    "Gemini consent is enabled but no API key is configured; using local fallback",
                    extra={"event": "analysis.no_api_key"},
                )
                return self._client_error
            try:
                self._client = self.client_factory(key, self.settings.analysis_timeout_seconds)
            except Exception as exc:
                self._client_error = f"Gemini client unavailable: {type(exc).__name__}"
                LOGGER.warning(
                    "Gemini client could not be created; using local fallback",
                    extra={
                        "event": "analysis.client_unavailable",
                        "error_type": type(exc).__name__,
                    },
                )
                return self._client_error
            return None

    def _analyze_gemini(self, document: ExtractedDocument, path: str) -> AnalysisResult:
        if self._client is None:
            raise AnalysisError("Gemini client is not initialized")
        prompt = _build_gemini_prompt(document, path, self.settings.analysis_max_chars)
        config = _gemini_generation_config()
        response = self._client.models.generate_content(
            model=self.settings.gemini_model,
            contents=prompt,
            config=config,
        )
        text = getattr(response, "text", None)
        if not text:
            raise AnalysisError("Gemini returned an empty response")
        return _parse_gemini_json(str(text))

    def _local(
        self,
        connection: sqlite3.Connection,
        document: ExtractedDocument,
        path: str,
        sha256: str,
        fallback_reason: str | None,
    ) -> AnalysisOutcome:
        cached = self._cached(connection, sha256, LOCAL_PROVIDER, LOCAL_MODEL)
        if cached is not None:
            cached.fallback_reason = fallback_reason
            return cached
        result = local_analyze(document, path)
        db.cache_analysis(
            connection,
            sha256,
            LOCAL_PROVIDER,
            LOCAL_MODEL,
            LOCAL_PROMPT_VERSION,
            result,
        )
        return AnalysisOutcome(
            result,
            LOCAL_PROVIDER,
            LOCAL_MODEL,
            LOCAL_PROMPT_VERSION,
            fallback_reason=fallback_reason,
        )


def local_analyze(document: ExtractedDocument, path: str) -> AnalysisResult:
    """Deterministically classify and summarize a document without network use."""
    text = " ".join(document.text.replace("\x00", " ").split())
    title = _clean_short(document.title) or os.path.splitext(os.path.basename(path))[0]
    combined = f"{title} {title} {text}".strip()
    tokens = _tokens(combined)
    counts = Counter(tokens)
    category, category_score = _classify(text, title)
    tags = _generate_tags(counts, title, category)
    summary = _summary(text, title, os.path.basename(path))
    topics = [token for token, _count in counts.most_common(12) if token not in tags][:6]
    confidence = min(0.9, 0.45 + min(category_score, 6) * 0.06 + min(len(tokens), 60) / 600)
    return AnalysisResult(
        category=category,
        tags=tags,
        summary=summary,
        topics=topics,
        confidence=round(confidence, 2),
    )


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    values: list[str] = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        token = match.group(0).strip("._'-")
        if (
            len(token) >= 3
            and token not in _STOPWORDS
            and not token.isdigit()
            and not token.startswith("http")
        ):
            values.append(token[:64])
    return values


def _classify(text: str, title: str) -> tuple[Category, int]:
    normalized = unicodedata.normalize("NFKC", f"{title} {text}").casefold()
    scores: dict[Category, int] = {}
    for category, phrases in _CATEGORY_KEYWORDS.items():
        score = 0
        for phrase in phrases:
            occurrences = normalized.count(phrase)
            if occurrences:
                score += occurrences * (3 if phrase in title.casefold() else 1)
        scores[category] = score
    best = max(scores, key=lambda item: scores[item])
    if scores[best] == 0:
        return Category.OTHER, 0
    return best, scores[best]


def _generate_tags(counts: Counter[str], title: str, category: Category) -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        try:
            normalized = normalize_tag(value, max_words=3)
        except TagError:
            return
        if normalized not in candidates:
            candidates.append(normalized)

    for token, _count in counts.most_common(12):
        add(token)
        if len(candidates) >= 5:
            break
    add(_CATEGORY_LABELS[category])
    for token in _tokens(title):
        add(token)
    for fallback in ("content", "indexed", "document"):
        if len(candidates) >= 3:
            break
        add(fallback)
    return candidates[:8] if len(candidates) >= 3 else ["content", "indexed", "document"]


def _summary(text: str, title: str, filename: str) -> str:
    if text:
        first_sentences = " ".join(_SENTENCE_PATTERN.split(text)[:2]).strip()
        if first_sentences:
            return _truncate_summary(first_sentences)
    fallback = _clean_short(title) or _clean_short(filename) or "Indexed document"
    return _truncate_summary(fallback)


def _truncate_summary(value: str) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= 300:
        return cleaned
    shortened = cleaned[:300]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(".,;: ") or cleaned[:300]


def _clean_short(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.replace("\x00", " ").split())[:300]


def _build_gemini_prompt(document: ExtractedDocument, path: str, max_chars: int) -> str:
    excerpt_text = document.text[:max_chars]
    metadata = {
        "filename": os.path.basename(path),
        "mime": document.metadata.get("mime"),
        "title": document.title,
        "author": document.author,
        "page_count": document.page_count,
        "truncated": document.truncated,
    }
    return (
        "Analyze this file for a private local index.\n"
        f"Allowed categories: {_CATEGORY_NAMES}.\n"
        "Return ONLY a JSON object with keys: category, tags, summary, topics, confidence.\n"
        "tags must contain 3-8 lowercase short tags; summary at most 300 characters; "
        "topics at most 12; confidence between 0 and 1.\n"
        f"Metadata: {json.dumps(metadata, ensure_ascii=False)}\n"
        "Document excerpt begins after the marker.\n---\n"
        f"{excerpt_text}\n---"
    )


def _gemini_generation_config() -> Any:
    try:
        from google.genai import types
    except ImportError as exc:
        raise AnalysisError(
            "google-genai is not installed; install the ai extra with 'uv sync --extra ai'"
        ) from exc
    try:
        return types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=1024,
            response_mime_type="application/json",
        )
    except (TypeError, AttributeError) as exc:
        raise AnalysisError(f"Unsupported google-genai API: {type(exc).__name__}") from exc


def _default_gemini_client(api_key: str, timeout_seconds: float) -> GeminiClient:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise AnalysisError(
            "google-genai is not installed; install the ai extra with 'uv sync --extra ai'"
        ) from exc
    try:
        http_options = types.HttpOptions(timeout=int(timeout_seconds * 1000))
        return genai.Client(api_key=api_key, http_options=http_options)
    except (TypeError, AttributeError) as exc:
        raise AnalysisError(f"Unsupported google-genai API: {type(exc).__name__}") from exc


def _parse_gemini_json(text: str) -> AnalysisResult:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise AnalysisError("Gemini response did not contain a JSON object")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AnalysisError("Gemini returned invalid JSON") from exc
    try:
        return AnalysisResult.model_validate(payload)
    except ValueError as exc:
        raise AnalysisError(f"Gemini returned invalid analysis fields: {exc}") from exc


def analysis_label(settings: Settings) -> str:
    """Return a short non-secret description of the configured analysis mode."""
    if not settings.analysis_enabled:
        return "disabled"
    if settings.consent and not settings.local_only:
        return f"Gemini ({settings.gemini_model}) with local fallback"
    return "local heuristic (cloud disabled)"


def validate_settings_for_cloud(settings: Settings) -> None:
    """Raise when settings are inconsistent for requested cloud analysis."""
    if settings.consent and settings.local_only:
        raise ConfigError("Cloud consent requires local_only = false.")

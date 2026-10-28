"""Typed data models shared by SmartFS components."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Category(StrEnum):
    """Allowed AI-generated document categories."""

    REPORT = "report"
    TODO_LIST = "todo list"
    LECTURE_NOTES = "lecture notes"
    RESEARCH_PAPER = "research paper"
    INVOICE = "invoice"
    MEETING_NOTES = "meeting notes"
    OTHER = "other"


class FileStatus(StrEnum):
    """Lifecycle state of a file in the SQLite index."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


class TagSource(StrEnum):
    """Origin of a file-tag association."""

    USER = "user"
    AI = "ai"


class JobType(StrEnum):
    """Kinds of work accepted by the persistent queue."""

    INDEX = "index"
    DELETE = "delete"


class JobState(StrEnum):
    """Persistent lifecycle state of a queue job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCELLED = "cancelled"


class AnalysisResult(BaseModel):
    """Validated structured output from a local or cloud analysis provider."""

    model_config = ConfigDict(extra="forbid")

    category: Category
    tags: list[str] = Field(min_length=3, max_length=8)
    summary: str = Field(min_length=1, max_length=300)
    topics: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, values: list[str]) -> list[str]:
        from smartfs.tags import normalize_tag

        cleaned: list[str] = []
        for value in values:
            normalized = normalize_tag(value, max_words=3)
            if normalized not in cleaned:
                cleaned.append(normalized)
        if not 3 <= len(cleaned) <= 8:
            raise ValueError("analysis requires between 3 and 8 unique tags")
        return cleaned

    @field_validator("summary")
    @classmethod
    def _clean_summary(cls, value: str) -> str:
        cleaned = " ".join(value.replace("\x00", " ").split())
        if not cleaned:
            raise ValueError("summary must not be empty")
        if len(cleaned) > 300:
            raise ValueError("summary must be at most 300 characters")
        return cleaned

    @field_validator("topics")
    @classmethod
    def _clean_topics(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            normalized = " ".join(str(value).replace("\x00", " ").split())
            if normalized and len(normalized) <= 80 and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned[:12]


class ExtractedDocument(BaseModel):
    """Text and metadata produced by the extraction stage."""

    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    page_count: int | None = None
    title: str | None = None
    author: str | None = None
    truncated: bool = False


class FileRecord(BaseModel):
    """A file row as stored in SQLite."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    path: str
    inode: int | None = None
    device: int | None = None
    size: int = 0
    mtime_ns: int = 0
    sha256: str | None = None
    mime: str | None = None
    status: FileStatus = FileStatus.PENDING
    error_kind: str | None = None
    error_msg: str | None = None
    indexed_at: datetime | str | None = None
    index_version: int = 1
    analysis_provider: str | None = None
    analysis_model: str | None = None
    category: str | None = None
    summary: str | None = None
    text_excerpt: str | None = None
    page_count: int | None = None
    title: str | None = None
    author: str | None = None
    first_seen_at: datetime | str | None = None
    last_seen_at: datetime | str | None = None
    deleted_at: datetime | str | None = None


class FileTag(BaseModel):
    """A tag attached to a file, including its provenance."""

    name: str
    source: TagSource
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    created_at: datetime | str | None = None


class JobRecord(BaseModel):
    """A durable queue job."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    path: str
    type: JobType
    file_id: int | None = None
    state: JobState
    attempts: int = 0
    max_attempts: int = 3
    next_run_at: datetime | str
    last_error: str | None = None
    rerun_requested: bool = False
    cancel_requested: bool = False
    created_at: datetime | str
    updated_at: datetime | str


class XattrMetadata(BaseModel):
    """The SmartFS values mirrored into Linux extended attributes."""

    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    sha256: str | None = None
    version: str | None = None


class Collection(BaseModel):
    """A named saved search (query plus filters and search mode)."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    mode: str = "auto"
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


class DuplicateGroup(BaseModel):
    """Files that share the same SHA-256 content hash."""

    sha256: str
    size: int = 0
    files: list[FileRecord] = Field(default_factory=list)


class TagStat(BaseModel):
    """A tag plus its file and provenance counts."""

    name: str
    file_count: int = 0
    ai_count: int = 0
    user_count: int = 0

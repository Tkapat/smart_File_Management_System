"""Application paths, configuration, and structured logging for SmartFS."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_log_dir

APP_NAME = "smartfs"
CONFIG_VERSION = 1
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_MAX_FILE_SIZE = 25 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 250_000
DEFAULT_PDF_MAX_PAGES = 100

DEFAULT_EXCLUSIONS = [
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".cache",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".env",
    ".env.*",
    "*.kdbx",
    "*.kdb",
    "*.gpg",
    "*.pgp",
]

_SECRET_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


class ConfigError(RuntimeError):
    """Raised when configuration cannot be read or validated."""


def redact_secrets(text: str) -> str:
    """Replace API keys and API-key-shaped strings with a redaction marker."""
    redacted = _SECRET_PATTERN.sub("[REDACTED]", text)
    for variable in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env_key = os.environ.get(variable, "").strip()
        if env_key:
            redacted = redacted.replace(env_key, "[REDACTED]")
    return redacted


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Filesystem locations used by SmartFS."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "smartfs.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "smartfs.log"

    @classmethod
    def discover(cls) -> AppPaths:
        """Return paths from ``SMARTFS_HOME`` or platform-appropriate defaults."""
        override = os.environ.get("SMARTFS_HOME", "").strip()
        if override:
            return cls.for_root(Path(override).expanduser())
        return cls(
            config_dir=Path(user_config_dir(APP_NAME, appauthor=False)),
            data_dir=Path(user_data_dir(APP_NAME, appauthor=False)),
            cache_dir=Path(user_cache_dir(APP_NAME, appauthor=False)),
            log_dir=Path(user_log_dir(APP_NAME, appauthor=False)),
        )

    @classmethod
    def for_root(cls, root: Path) -> AppPaths:
        """Return an isolated layout rooted at ``root`` (used by tests and demos)."""
        root = root.expanduser().absolute()
        return cls(
            config_dir=root / "config",
            data_dir=root / "data",
            cache_dir=root / "cache",
            log_dir=root / "logs",
        )

    def ensure(self) -> None:
        """Create all SmartFS directories with owner-only permissions."""
        for directory in (self.config_dir, self.data_dir, self.cache_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass(slots=True)
class Settings:
    """Mutable application settings persisted as TOML."""

    config_version: int = CONFIG_VERSION
    watched_dirs: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUSIONS))
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    pdf_max_pages: int = DEFAULT_PDF_MAX_PAGES
    debounce_seconds: float = 1.5
    stability_seconds: float = 1.5
    stability_timeout_seconds: float = 120.0
    worker_count: int = 2
    job_queue_max: int = 100
    job_max_attempts: int = 3
    reconciliation_interval_seconds: int = 600
    missing_grace_seconds: int = 7 * 24 * 3600
    self_write_suppression_seconds: float = 5.0
    xattr_enabled: bool = True
    local_only: bool = True
    consent: bool = False
    analysis_enabled: bool = True
    analysis_max_chars: int = 12_000
    analysis_timeout_seconds: float = 30.0
    gemini_model: str = DEFAULT_GEMINI_MODEL
    rate_limit_per_minute: int = 30
    embeddings_enabled: bool = False
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_max_chars: int = 4_000

    def validate(self) -> None:
        """Validate settings in place and normalize watched directory paths."""
        if self.config_version != CONFIG_VERSION:
            raise ConfigError(
                f"Unsupported config version {self.config_version}; expected {CONFIG_VERSION}."
            )
        if not 1024 <= self.max_file_size_bytes <= 1024 * 1024 * 1024:
            raise ConfigError("max_file_size_bytes must be between 1 KiB and 1 GiB.")
        if not 1000 <= self.max_text_chars <= 50_000_000:
            raise ConfigError("max_text_chars must be between 1,000 and 50,000,000.")
        if not 1 <= self.pdf_max_pages <= 10_000:
            raise ConfigError("pdf_max_pages must be between 1 and 10,000.")
        if not 0.1 <= self.debounce_seconds <= 60:
            raise ConfigError("debounce_seconds must be between 0.1 and 60.")
        if not 0.1 <= self.stability_seconds <= 60:
            raise ConfigError("stability_seconds must be between 0.1 and 60.")
        if not 1 <= self.stability_timeout_seconds <= 3600:
            raise ConfigError("stability_timeout_seconds must be between 1 and 3600.")
        if not 1 <= self.worker_count <= 32:
            raise ConfigError("worker_count must be between 1 and 32.")
        if not 1 <= self.job_queue_max <= 10_000:
            raise ConfigError("job_queue_max must be between 1 and 10,000.")
        if not 1 <= self.job_max_attempts <= 20:
            raise ConfigError("job_max_attempts must be between 1 and 20.")
        if not 10 <= self.reconciliation_interval_seconds <= 7 * 24 * 3600:
            raise ConfigError("reconciliation_interval_seconds must be between 10 and 604800.")
        if not 0 <= self.missing_grace_seconds <= 365 * 24 * 3600:
            raise ConfigError("missing_grace_seconds must be between 0 and one year.")
        if not 0.1 <= self.self_write_suppression_seconds <= 120:
            raise ConfigError("self_write_suppression_seconds must be between 0.1 and 120.")
        if not 1 <= self.rate_limit_per_minute <= 600:
            raise ConfigError("rate_limit_per_minute must be between 1 and 600.")
        if not 1_000 <= self.analysis_max_chars <= 100_000:
            raise ConfigError("analysis_max_chars must be between 1,000 and 100,000.")
        if not 5 <= self.analysis_timeout_seconds <= 300:
            raise ConfigError("analysis_timeout_seconds must be between 5 and 300.")
        if not self.gemini_model.strip():
            raise ConfigError("gemini_model must not be empty.")
        if not 500 <= self.embedding_max_chars <= 100_000:
            raise ConfigError("embedding_max_chars must be between 500 and 100,000.")
        if not self.embedding_model.strip():
            raise ConfigError("embedding_model must not be empty.")
        normalized: list[str] = []
        for raw in self.watched_dirs:
            path = raw.strip()
            if not path:
                continue
            absolute = str(Path(path).expanduser().absolute())
            if absolute not in normalized:
                normalized.append(absolute)
        self.watched_dirs = normalized
        cleaned_exclusions = []
        for pattern in self.exclusions:
            value = pattern.strip()
            if value and "\x00" not in value:
                cleaned_exclusions.append(value)
        self.exclusions = cleaned_exclusions


def _expect_type(mapping: dict[str, Any], key: str, expected: type, default: Any) -> Any:
    if key not in mapping:
        return default
    value = mapping[key]
    if expected is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise ConfigError(f"Config key {key!r} must be {expected.__name__}.")
    return value


def _settings_from_mapping(mapping: dict[str, Any]) -> Settings:
    defaults = Settings()
    watched = _expect_type(mapping, "watched_dirs", list, defaults.watched_dirs)
    exclusions = _expect_type(mapping, "exclusions", list, defaults.exclusions)
    if any(not isinstance(item, str) for item in watched):
        raise ConfigError("watched_dirs must contain only strings.")
    if any(not isinstance(item, str) for item in exclusions):
        raise ConfigError("exclusions must contain only strings.")

    settings = Settings(
        config_version=_expect_type(mapping, "config_version", int, defaults.config_version),
        watched_dirs=list(watched),
        exclusions=list(exclusions),
        max_file_size_bytes=_expect_type(
            mapping, "max_file_size_bytes", int, defaults.max_file_size_bytes
        ),
        max_text_chars=_expect_type(mapping, "max_text_chars", int, defaults.max_text_chars),
        pdf_max_pages=_expect_type(mapping, "pdf_max_pages", int, defaults.pdf_max_pages),
        debounce_seconds=_expect_type(
            mapping, "debounce_seconds", float, defaults.debounce_seconds
        ),
        stability_seconds=_expect_type(
            mapping, "stability_seconds", float, defaults.stability_seconds
        ),
        stability_timeout_seconds=_expect_type(
            mapping,
            "stability_timeout_seconds",
            float,
            defaults.stability_timeout_seconds,
        ),
        worker_count=_expect_type(mapping, "worker_count", int, defaults.worker_count),
        job_queue_max=_expect_type(mapping, "job_queue_max", int, defaults.job_queue_max),
        job_max_attempts=_expect_type(mapping, "job_max_attempts", int, defaults.job_max_attempts),
        reconciliation_interval_seconds=_expect_type(
            mapping,
            "reconciliation_interval_seconds",
            int,
            defaults.reconciliation_interval_seconds,
        ),
        missing_grace_seconds=_expect_type(
            mapping, "missing_grace_seconds", int, defaults.missing_grace_seconds
        ),
        self_write_suppression_seconds=_expect_type(
            mapping,
            "self_write_suppression_seconds",
            float,
            defaults.self_write_suppression_seconds,
        ),
        xattr_enabled=_expect_type(mapping, "xattr_enabled", bool, defaults.xattr_enabled),
        local_only=_expect_type(mapping, "local_only", bool, defaults.local_only),
        consent=_expect_type(mapping, "consent", bool, defaults.consent),
        analysis_enabled=_expect_type(mapping, "analysis_enabled", bool, defaults.analysis_enabled),
        analysis_max_chars=_expect_type(
            mapping, "analysis_max_chars", int, defaults.analysis_max_chars
        ),
        analysis_timeout_seconds=_expect_type(
            mapping,
            "analysis_timeout_seconds",
            float,
            defaults.analysis_timeout_seconds,
        ),
        gemini_model=_expect_type(mapping, "gemini_model", str, defaults.gemini_model),
        rate_limit_per_minute=_expect_type(
            mapping, "rate_limit_per_minute", int, defaults.rate_limit_per_minute
        ),
        embeddings_enabled=_expect_type(
            mapping, "embeddings_enabled", bool, defaults.embeddings_enabled
        ),
        embedding_model=_expect_type(mapping, "embedding_model", str, defaults.embedding_model),
        embedding_max_chars=_expect_type(
            mapping, "embedding_max_chars", int, defaults.embedding_max_chars
        ),
    )
    settings.validate()
    return settings


def load_settings(paths: AppPaths | None = None) -> Settings:
    """Load settings from TOML, returning defaults when no config exists."""
    paths = paths or AppPaths.discover()
    if not paths.config_file.exists():
        settings = Settings()
        settings.validate()
        return settings
    try:
        with paths.config_file.open("rb") as handle:
            mapping = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read {paths.config_file}: {exc}") from exc
    return _settings_from_mapping(mapping)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _toml_array(values: list[str]) -> str:
    if not values:
        return "[]"
    lines = ["["]
    lines.extend(f"  {_toml_string(value)}," for value in values)
    lines.append("]")
    return "\n".join(lines)


def settings_to_toml(settings: Settings) -> str:
    """Serialize settings as deterministic, readable TOML."""
    return "\n".join(
        [
            f"config_version = {settings.config_version}",
            f"local_only = {'true' if settings.local_only else 'false'}",
            f"consent = {'true' if settings.consent else 'false'}",
            f"gemini_model = {_toml_string(settings.gemini_model)}",
            f"rate_limit_per_minute = {settings.rate_limit_per_minute}",
            f"max_file_size_bytes = {settings.max_file_size_bytes}",
            f"max_text_chars = {settings.max_text_chars}",
            f"pdf_max_pages = {settings.pdf_max_pages}",
            f"analysis_enabled = {'true' if settings.analysis_enabled else 'false'}",
            f"analysis_max_chars = {settings.analysis_max_chars}",
            f"analysis_timeout_seconds = {settings.analysis_timeout_seconds}",
            f"embeddings_enabled = {'true' if settings.embeddings_enabled else 'false'}",
            f"embedding_model = {_toml_string(settings.embedding_model)}",
            f"embedding_max_chars = {settings.embedding_max_chars}",
            f"debounce_seconds = {settings.debounce_seconds}",
            f"stability_seconds = {settings.stability_seconds}",
            f"stability_timeout_seconds = {settings.stability_timeout_seconds}",
            f"worker_count = {settings.worker_count}",
            f"job_queue_max = {settings.job_queue_max}",
            f"job_max_attempts = {settings.job_max_attempts}",
            f"reconciliation_interval_seconds = {settings.reconciliation_interval_seconds}",
            f"missing_grace_seconds = {settings.missing_grace_seconds}",
            f"self_write_suppression_seconds = {settings.self_write_suppression_seconds}",
            f"xattr_enabled = {'true' if settings.xattr_enabled else 'false'}",
            "",
            f"watched_dirs = {_toml_array(settings.watched_dirs)}",
            "",
            f"exclusions = {_toml_array(settings.exclusions)}",
            "",
        ]
    )


def save_settings(settings: Settings, paths: AppPaths | None = None) -> Path:
    """Validate and atomically write settings; return the config path."""
    paths = paths or AppPaths.discover()
    settings.validate()
    paths.ensure()
    content = settings_to_toml(settings)
    fd, temp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".toml", dir=paths.config_dir, text=True
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, paths.config_file)
        os.chmod(paths.config_file, 0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return paths.config_file


def set_watched_dirs(
    settings: Settings, watched_dirs: list[str], paths: AppPaths | None = None
) -> None:
    """Replace watched directories and persist settings."""
    settings.watched_dirs = watched_dirs
    save_settings(settings, paths)


class _JsonFormatter(logging.Formatter):
    _reserved = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in self._reserved and not key.startswith("_"):
                payload[key] = redact_secrets(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    paths: AppPaths | None = None,
    *,
    verbose: bool = False,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure the ``smartfs`` logger with rotating JSON file logging."""
    paths = paths or AppPaths.discover()
    paths.ensure()
    logger = logging.getLogger("smartfs")
    logger.setLevel(logging.DEBUG if verbose else level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = _JsonFormatter()
    file_handler = RotatingFileHandler(
        paths.log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG if verbose else level)
    logger.addHandler(file_handler)

    if verbose:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.setLevel(logging.DEBUG)
        logger.addHandler(stream)
    return logger


def logging_fields() -> list[str]:
    """Return the persisted settings field names (useful for diagnostics)."""
    return [item.name for item in fields(Settings)]

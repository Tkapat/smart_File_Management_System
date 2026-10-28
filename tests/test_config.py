from __future__ import annotations

from pathlib import Path

import pytest

from smartfs.config import (
    AppPaths,
    ConfigError,
    Settings,
    load_settings,
    save_settings,
    settings_to_toml,
)


def test_settings_roundtrip(tmp_path: Path) -> None:
    paths = AppPaths.for_root(tmp_path / "home")
    settings = Settings(
        watched_dirs=[str(tmp_path / "docs")],
        exclusions=["*.secret", ".git"],
        max_file_size_bytes=1024 * 1024,
        local_only=False,
        consent=True,
        gemini_model="gemini-test",
    )
    save_settings(settings, paths)
    loaded = load_settings(paths)
    assert loaded.watched_dirs == [str((tmp_path / "docs").absolute())]
    assert loaded.exclusions == ["*.secret", ".git"]
    assert loaded.local_only is False
    assert loaded.consent is True
    assert loaded.gemini_model == "gemini-test"
    assert paths.config_file.stat().st_mode & 0o777 == 0o600


def test_invalid_config_rejected(tmp_path: Path) -> None:
    paths = AppPaths.for_root(tmp_path / "home")
    paths.ensure()
    paths.config_file.write_text(
        settings_to_toml(Settings()).replace(
            "max_file_size_bytes = 26214400", "max_file_size_bytes = 10"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_settings(paths)


def test_missing_config_returns_defaults(tmp_path: Path) -> None:
    paths = AppPaths.for_root(tmp_path / "home")
    loaded = load_settings(paths)
    assert loaded.local_only is True
    assert loaded.consent is False
    assert loaded.analysis_enabled is True
    assert loaded.embeddings_enabled is False
    assert loaded.embedding_model == "BAAI/bge-small-en-v1.5"
    assert loaded.watched_dirs == []
    assert loaded.exclusions


def test_phase3_settings_roundtrip_and_validation(tmp_path: Path) -> None:
    paths = AppPaths.for_root(tmp_path / "home")
    settings = Settings(
        analysis_enabled=False,
        analysis_max_chars=8_000,
        analysis_timeout_seconds=12.5,
        embeddings_enabled=True,
        embedding_model="test/embedding-model",
        embedding_max_chars=2_000,
    )
    save_settings(settings, paths)
    loaded = load_settings(paths)
    assert loaded.analysis_enabled is False
    assert loaded.analysis_max_chars == 8_000
    assert loaded.analysis_timeout_seconds == 12.5
    assert loaded.embeddings_enabled is True
    assert loaded.embedding_model == "test/embedding-model"
    assert loaded.embedding_max_chars == 2_000

    for invalid in (
        Settings(analysis_max_chars=999),
        Settings(analysis_timeout_seconds=4.9),
        Settings(embedding_model="  "),
        Settings(embedding_max_chars=499),
    ):
        with pytest.raises(ConfigError):
            invalid.validate()

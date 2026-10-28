from __future__ import annotations

import io
from pathlib import Path

from smartfs import cli
from smartfs.cli import main
from smartfs.config import AppPaths, load_settings
from smartfs.credentials import CredentialStatus


def _initialize(tmp_path: Path, monkeypatch) -> AppPaths:
    monkeypatch.setenv("SMARTFS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--non-interactive"]) == 0
    return AppPaths.discover()


def test_ai_status_defaults_to_local(tmp_path: Path, monkeypatch, capsys) -> None:
    _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "credential_status",
        lambda: CredentialStatus(False, None, "no API key configured"),
    )
    capsys.readouterr()
    assert main(["ai", "status"]) == 0
    output = capsys.readouterr().out
    assert "local heuristic (cloud disabled)" in output
    assert "Consent: disabled" in output
    assert "Embeddings: disabled" in output
    assert "API key: missing" in output


def test_ai_enable_requires_a_key(tmp_path: Path, monkeypatch, capsys) -> None:
    _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "credential_status",
        lambda: CredentialStatus(False, None, "no API key configured"),
    )
    capsys.readouterr()
    assert main(["ai", "enable"]) == 1
    assert "No Gemini API key" in capsys.readouterr().err


def test_ai_enable_disable_roundtrip(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "credential_status",
        lambda: CredentialStatus(True, "environment:GEMINI_API_KEY"),
    )
    capsys.readouterr()
    assert main(["ai", "enable", "--embeddings"]) == 0
    settings = load_settings(paths)
    assert settings.consent is True
    assert settings.local_only is False
    assert settings.embeddings_enabled is True

    assert main(["ai", "disable"]) == 0
    settings = load_settings(paths)
    assert settings.consent is False
    assert settings.local_only is True


def test_ai_setup_noninteractive_requires_key(tmp_path: Path, monkeypatch, capsys) -> None:
    _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "credential_status",
        lambda: CredentialStatus(False, None, "no API key configured"),
    )
    capsys.readouterr()
    assert main(["ai", "setup", "--yes"]) == 1
    assert "No Gemini API key" in capsys.readouterr().err


def test_ai_setup_with_existing_key(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "credential_status",
        lambda: CredentialStatus(True, "environment:GEMINI_API_KEY"),
    )
    capsys.readouterr()
    assert main(["ai", "setup", "--yes", "--no-embeddings"]) == 0
    settings = load_settings(paths)
    assert settings.consent is True
    assert settings.local_only is False
    assert settings.embeddings_enabled is False


def test_ai_set_key_and_forget_key(tmp_path: Path, monkeypatch, capsys) -> None:
    _initialize(tmp_path, monkeypatch)
    stored: list[str] = []
    monkeypatch.setattr(cli, "set_api_key", stored.append)
    monkeypatch.setattr(cli, "environment_api_key", lambda: (None, None))
    monkeypatch.setattr("sys.stdin", io.StringIO("secret-key\n"))
    capsys.readouterr()
    assert main(["ai", "set-key", "--stdin"]) == 0
    assert stored == ["secret-key"]
    assert "stored in the system keyring" in capsys.readouterr().out

    monkeypatch.setattr(cli, "delete_api_key", lambda: True)
    assert main(["ai", "forget-key"]) == 0
    assert "Removed the Gemini API key" in capsys.readouterr().out


def test_ai_commands_reject_empty_key(tmp_path: Path, monkeypatch, capsys) -> None:
    _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    capsys.readouterr()
    assert main(["ai", "set-key", "--stdin"]) == 1
    assert "No API key provided" in capsys.readouterr().err


def test_ai_embeddings_toggle_without_api_key(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "credential_status",
        lambda: CredentialStatus(False, None, "no API key configured"),
    )
    capsys.readouterr()
    assert main(["ai", "embeddings", "enable"]) == 0
    settings = load_settings(paths)
    assert settings.embeddings_enabled is True
    assert settings.consent is False
    assert settings.local_only is True

    assert main(["ai", "embeddings", "disable"]) == 0
    assert load_settings(paths).embeddings_enabled is False

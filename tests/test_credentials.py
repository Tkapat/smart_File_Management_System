from __future__ import annotations

import pytest

from smartfs import credentials


class _Errors:
    class KeyringError(Exception):
        pass


class _MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.values[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


@pytest.fixture(autouse=True)
def _clear_environment(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def test_environment_key_takes_priority(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    status = credentials.credential_status()
    assert status.available is True
    assert status.source == "environment:GEMINI_API_KEY"
    assert credentials.get_api_key() == "env-key"


def test_keyring_store_read_delete(monkeypatch) -> None:
    store = _MemoryKeyring()
    monkeypatch.setattr(credentials, "_import_keyring", lambda: (store, _Errors))
    credentials.set_api_key(" secret-key ")
    assert credentials.get_api_key() == "secret-key"
    assert credentials.credential_status().source == "keyring"
    assert credentials.delete_api_key() is True
    assert credentials.delete_api_key() is False
    assert credentials.get_api_key() is None


def test_keyring_unavailable_is_reported(monkeypatch) -> None:
    def unavailable():
        raise credentials.CredentialError("no keyring backend")

    monkeypatch.setattr(credentials, "_import_keyring", unavailable)
    assert credentials.get_api_key() is None
    status = credentials.credential_status()
    assert status.available is False
    assert status.detail == "no keyring backend"
    with pytest.raises(credentials.CredentialError):
        credentials.set_api_key("secret")
    with pytest.raises(credentials.CredentialError):
        credentials.delete_api_key()


@pytest.mark.parametrize("value", ["", "   ", "bad key", "a\nb"])
def test_invalid_keys_are_rejected(value: str, monkeypatch) -> None:
    monkeypatch.setattr(credentials, "_import_keyring", lambda: (_MemoryKeyring(), _Errors))
    with pytest.raises(credentials.CredentialError):
        credentials.set_api_key(value)

"""Gemini API-key storage through the environment or the system keyring."""

from __future__ import annotations

import os
from dataclasses import dataclass

KEYRING_SERVICE = "smartfs"
KEYRING_USERNAME = "gemini-api-key"
ENV_VARIABLES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


class CredentialError(RuntimeError):
    """Raised when an API key cannot be read, stored, or removed safely."""


@dataclass(slots=True)
class CredentialStatus:
    """Non-secret description of the configured Gemini credential."""

    available: bool
    source: str | None
    detail: str | None = None


def _clean_key(key: str) -> str:
    value = key.strip()
    if not value:
        raise CredentialError("API key must not be empty.")
    if len(value) > 512:
        raise CredentialError("API key is unexpectedly long.")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise CredentialError("API key must not contain whitespace or control characters.")
    return value


def _import_keyring():
    try:
        import keyring
        from keyring import errors
    except ImportError as exc:
        raise CredentialError(
            "Python keyring support is not installed; use GEMINI_API_KEY instead."
        ) from exc
    return keyring, errors


def environment_api_key() -> tuple[str | None, str | None]:
    """Return the first configured key from the environment and its variable name."""
    for variable in ENV_VARIABLES:
        value = os.environ.get(variable, "").strip()
        if value:
            return _clean_key(value), variable
    return None, None


def keyring_api_key() -> str | None:
    """Read the Gemini key from the system keyring, if a backend is usable."""
    keyring, errors = _import_keyring()
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except errors.KeyringError as exc:
        raise CredentialError(f"Keyring read failed: {type(exc).__name__}") from exc
    except Exception as exc:
        raise CredentialError(f"Keyring backend unavailable: {type(exc).__name__}") from exc
    if value:
        return _clean_key(value)
    return None


def get_api_key() -> str | None:
    """Resolve the Gemini key, preferring environment variables over keyring."""
    value, _variable = environment_api_key()
    if value:
        return value
    try:
        return keyring_api_key()
    except CredentialError:
        return None


def credential_status() -> CredentialStatus:
    """Describe where the key comes from without returning the secret."""
    value, variable = environment_api_key()
    if value:
        return CredentialStatus(True, f"environment:{variable}")
    try:
        value = keyring_api_key()
    except CredentialError as exc:
        return CredentialStatus(False, None, str(exc))
    if value:
        return CredentialStatus(True, "keyring")
    return CredentialStatus(False, None, "no API key configured")


def set_api_key(key: str) -> None:
    """Validate and store the key in the system keyring only."""
    cleaned = _clean_key(key)
    keyring, errors = _import_keyring()
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, cleaned)
    except errors.KeyringError as exc:
        raise CredentialError(
            f"Could not store the key in the system keyring: {type(exc).__name__}"
        ) from exc
    except Exception as exc:
        raise CredentialError(f"Keyring backend unavailable: {type(exc).__name__}") from exc


def delete_api_key() -> bool:
    """Remove the stored key; return False when none was present."""
    keyring, errors = _import_keyring()
    try:
        if keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME) is None:
            return False
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except errors.KeyringError as exc:
        raise CredentialError(
            f"Could not remove the key from the system keyring: {type(exc).__name__}"
        ) from exc
    except Exception as exc:
        raise CredentialError(f"Keyring backend unavailable: {type(exc).__name__}") from exc
    return True

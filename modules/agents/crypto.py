"""Fernet at-rest encryption for BYOK Gemini keys (ADR 0046, decision 5).

Threat model, stated plainly: this protects the key against a DB-only
compromise (e.g. a leaked backup) - not against an app-server compromise,
since the app process holds the Fernet key in its own env. Standard
at-rest encryption, not a substitute for secrets-manager-grade isolation.
"""
from cryptography.fernet import Fernet, InvalidToken

from config import settings


class ByokKeyError(Exception):
    """Raised when AGENT_BYOK_ENCRYPTION_KEY is unset/invalid, or a stored
    ciphertext fails to decrypt (corrupt blob or key rotated out from under
    it)."""


def _fernet() -> Fernet:
    key = settings.AGENT_BYOK_ENCRYPTION_KEY
    if not key:
        raise ByokKeyError("AGENT_BYOK_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except ValueError as exc:
        raise ByokKeyError("AGENT_BYOK_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_api_key(raw: str) -> bytes:
    return _fernet().encrypt(raw.encode())


def decrypt_api_key(blob: bytes) -> str:
    try:
        return _fernet().decrypt(blob).decode()
    except InvalidToken as exc:
        raise ByokKeyError("stored Gemini API key could not be decrypted") from exc

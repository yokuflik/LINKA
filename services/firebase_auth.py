"""
Firebase Phone Auth ID-token verification (ADR 0009).

The SMS send + code check happen entirely client-side (Firebase JS SDK +
invisible reCAPTCHA). The server's only job is to verify the Firebase-issued
ID token and pull the verified `phone_number` claim out of it.

Verification is manual against Google's public x509 certs - no `firebase-admin`
dependency, no service-account file. The certs are cached in memory and
refetched on `Cache-Control: max-age` expiry (or once on an unknown `kid`,
for key rotation).
"""
import logging
import time

import httpx
import jwt
from cryptography.x509 import load_pem_x509_certificate

from config import FIREBASE_PROJECT_ID

logger = logging.getLogger(__name__)

# Google's Secure Token Service signing certs for Firebase ID tokens.
_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
_ISSUER = f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}"

_MIN_CACHE_SECONDS = 3600

_certs: dict[str, object] = {}  # kid -> public key
_certs_expiry: float = 0.0


class FirebaseAuthError(Exception):
    """Raised for any token that fails verification. Mapped to HTTP 401 by the caller."""


async def _load_certs(force: bool = False) -> None:
    global _certs, _certs_expiry
    if not force and _certs and time.time() < _certs_expiry:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(_CERTS_URL)
        resp.raise_for_status()
        raw = resp.json()  # { kid: "-----BEGIN CERTIFICATE----- ..." }

    _certs = {kid: _pubkey_from_cert(pem) for kid, pem in raw.items()}

    max_age = _MIN_CACHE_SECONDS
    cache_control = resp.headers.get("Cache-Control", "")
    for part in cache_control.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                max_age = max(_MIN_CACHE_SECONDS, int(part.split("=", 1)[1]))
            except ValueError:
                pass
    _certs_expiry = time.time() + max_age


def _pubkey_from_cert(pem: str):
    return load_pem_x509_certificate(pem.encode()).public_key()


async def verify_id_token(id_token: str) -> dict:
    """
    Returns the verified claim set. Raises FirebaseAuthError on any problem.

    Checks: RS256 signature against a current Google cert, `aud` == project id,
    `iss` == securetoken.google.com/<project id>, not expired, non-empty `sub`,
    and `auth_time` not in the future.
    """
    if not FIREBASE_PROJECT_ID:
        raise FirebaseAuthError("Firebase auth is not configured on this server")

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise FirebaseAuthError(f"Malformed token: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise FirebaseAuthError("Token has no key id")

    await _load_certs()
    if kid not in _certs:
        # Key rotation: force one refetch before giving up.
        await _load_certs(force=True)
    key = _certs.get(kid)
    if key is None:
        raise FirebaseAuthError("Token signed with an unknown key")

    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=FIREBASE_PROJECT_ID,
            issuer=_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise FirebaseAuthError(f"Token verification failed: {exc}") from exc

    if not claims.get("sub"):
        raise FirebaseAuthError("Token has no subject")
    auth_time = claims.get("auth_time")
    if auth_time is not None and auth_time > time.time() + 60:
        raise FirebaseAuthError("Token auth_time is in the future")

    return claims

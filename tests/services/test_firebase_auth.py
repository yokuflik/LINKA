import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from services import auth_service, firebase_auth

pytestmark = pytest.mark.asyncio

_PROJECT_ID = "linka-test"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _wire_firebase(monkeypatch, rsa_key):
    """Point the verifier at our test project id + a single in-memory signing key."""
    monkeypatch.setattr(firebase_auth, "FIREBASE_PROJECT_ID", _PROJECT_ID)
    monkeypatch.setattr(firebase_auth, "_ISSUER", f"https://securetoken.google.com/{_PROJECT_ID}")
    monkeypatch.setattr(firebase_auth, "_certs", {"testkid": rsa_key.public_key()})
    monkeypatch.setattr(firebase_auth, "_certs_expiry", time.time() + 3600)

    async def _noop(force: bool = False):
        return None

    monkeypatch.setattr(firebase_auth, "_load_certs", _noop)


def _make_token(rsa_key, *, aud=_PROJECT_ID, iss=None, phone="+972500000123", exp_offset=3600, sub="firebase-uid-1"):
    now = int(time.time())
    payload = {
        "iss": iss or f"https://securetoken.google.com/{_PROJECT_ID}",
        "aud": aud,
        "sub": sub,
        "iat": now,
        "auth_time": now,
        "exp": now + exp_offset,
        "phone_number": phone,
    }
    return jwt.encode(payload, rsa_key, algorithm="RS256", headers={"kid": "testkid"})


async def test_valid_token_returns_claims(rsa_key):
    claims = await firebase_auth.verify_id_token(_make_token(rsa_key))
    assert claims["phone_number"] == "+972500000123"


async def test_wrong_audience_rejected(rsa_key):
    with pytest.raises(firebase_auth.FirebaseAuthError):
        await firebase_auth.verify_id_token(_make_token(rsa_key, aud="some-other-project"))


async def test_wrong_issuer_rejected(rsa_key):
    with pytest.raises(firebase_auth.FirebaseAuthError):
        await firebase_auth.verify_id_token(_make_token(rsa_key, iss="https://evil.example/"))


async def test_expired_token_rejected(rsa_key):
    with pytest.raises(firebase_auth.FirebaseAuthError):
        await firebase_auth.verify_id_token(_make_token(rsa_key, exp_offset=-10))


async def test_token_signed_by_unknown_key_rejected(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(firebase_auth.FirebaseAuthError):
        await firebase_auth.verify_id_token(_make_token(other))


async def test_empty_subject_rejected(rsa_key):
    with pytest.raises(firebase_auth.FirebaseAuthError):
        await firebase_auth.verify_id_token(_make_token(rsa_key, sub=""))


async def test_verify_firebase_and_login_creates_user(db_session, redis_db, rsa_key):
    token = _make_token(rsa_key, phone="+972500000456")
    user, access_token, _ = await auth_service.verify_firebase_and_login(db_session, token)
    assert user.phone_number == "+972500000456"
    assert auth_service.verify_access_token(access_token) == user.id


async def test_verify_firebase_and_login_missing_phone_claim(db_session, redis_db, rsa_key):
    token = _make_token(rsa_key, phone=None)
    with pytest.raises(auth_service.InvalidOTPError):
        await auth_service.verify_firebase_and_login(db_session, token)


async def test_verify_firebase_and_login_rejects_bad_token(db_session, redis_db):
    with pytest.raises(auth_service.InvalidOTPError):
        await auth_service.verify_firebase_and_login(db_session, "not-a-jwt")

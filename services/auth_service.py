import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    DEV_AUTH_WHITELIST,
    OTP_REQUEST_RATE_LIMIT_MAX,
    OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    OTP_VERIFY_MAX_ATTEMPTS,
)
from database.crud.crud_user import create_user, get_user_by_phone
from database.models.user import User
from services import firebase_auth, rate_limit_service
from services.redis_client import redis_client
from utils.snowflake import next_id

logger = logging.getLogger(__name__)

_OTP_TTL_SECONDS = 300
_OTP_KEY_PREFIX = "otp:"
_REFRESH_JTI_KEY_PREFIX = "refresh_jti:"  # refresh_jti:{user_id} -> set of valid jti's


class InvalidOTPError(Exception):
    pass


class InvalidRefreshTokenError(Exception):
    pass


class OTPRequestRateLimitedError(Exception):
    pass


class PhoneAlreadyRegisteredError(Exception):
    pass


class PhoneNotRegisteredError(Exception):
    pass


def _otp_key(phone_number: str) -> str:
    return f"{_OTP_KEY_PREFIX}{phone_number}"


async def request_otp(phone_number: str, intent: str | None = None, session: AsyncSession | None = None) -> None:
    """
    Generates a one-time login code and hands it to the SMS provider.

    When ``intent`` and ``session`` are supplied, the phone number is checked
    against the users table first: 'register' fails if the number already has
    an account, 'login' fails if it doesn't - so the caller can't silently
    register by "logging in", or hit "verify & create account" on a number
    that's already taken.

    Rate-limited per phone number - otherwise this endpoint alone is an open
    invitation to SMS-bomb any number (cost abuse against the SMS provider,
    and a real annoyance/attack vector against the phone's owner), since
    nothing about it requires an account or a token yet.
    """
    # Dev whitelist (ADR 0009): a handful of non-real test phone strings ("1".."5")
    # skip SMS entirely - no code is stored, and verify accepts any input.
    if phone_number in DEV_AUTH_WHITELIST:
        return

    if intent and session is not None:
        existing = await get_user_by_phone(session, phone_number)
        if intent == "register" and existing is not None:
            raise PhoneAlreadyRegisteredError("This phone number is already registered - log in instead")
        if intent == "login" and existing is None:
            raise PhoneNotRegisteredError("No account found for this phone number - sign up first")

    allowed = await rate_limit_service.check_and_increment(
        phone_number, "otp_request", max_per_window=OTP_REQUEST_RATE_LIMIT_MAX, window_seconds=OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS
    )
    if not allowed:
        raise OTPRequestRateLimitedError(f"Too many OTP requests for {phone_number}")

    code = f"{random.randint(0, 999999):06d}"
    await redis_client.set(_otp_key(phone_number), code, ex=_OTP_TTL_SECONDS)
    await _deliver_otp(phone_number, code)


async def _deliver_otp(phone_number: str, code: str) -> None:
    #for now the opt is in the command line
    print(f"[STUB] Would SMS OTP {code} to {phone_number}")


async def verify_otp_and_login(session: AsyncSession, phone_number: str, code: str) -> tuple[User, str, str]:
    """
    Verifies the code, creates the user on first login, and issues a fresh
    access/refresh token pair. Returns (user, access_token, refresh_token).

    A 6-digit code is only as safe as the number of guesses an attacker gets
    to make against it - capping verification attempts per phone number is
    what actually makes the OTP_TTL_SECONDS window meaningful; without it,
    1,000,000 possibilities is well within brute-force range for the 5
    minutes the code is valid.
    """
    # Dev whitelist (ADR 0009): non-real test numbers log straight in.
    if phone_number in DEV_AUTH_WHITELIST:
        return await _find_or_create_and_issue(session, phone_number)

    attempts_allowed = await rate_limit_service.check_and_increment(
        phone_number, "otp_verify", max_per_window=OTP_VERIFY_MAX_ATTEMPTS, window_seconds=_OTP_TTL_SECONDS
    )
    if not attempts_allowed:
        raise InvalidOTPError("Too many attempts - request a new code")

    stored_code = await redis_client.get(_otp_key(phone_number))
    if stored_code is None or stored_code != code:
        raise InvalidOTPError("Invalid or expired code")

    # One-time: consume the code so it can't be replayed
    await redis_client.delete(_otp_key(phone_number))

    return await _find_or_create_and_issue(session, phone_number)


async def verify_firebase_and_login(session: AsyncSession, id_token: str) -> tuple[User, str, str]:
    """
    Trades a verified Firebase Phone Auth ID token for our own access/refresh
    pair (ADR 0009). The SMS + code check already happened client-side; here we
    only verify the token against Google's JWKS and trust its `phone_number`
    claim for find-or-create.

    Unlike the OTP path there is no register/login `intent` pre-check - Firebase
    sends the SMS before the server is involved, so this is always find-or-create.
    """
    try:
        claims = await firebase_auth.verify_id_token(id_token)
    except firebase_auth.FirebaseAuthError as exc:
        raise InvalidOTPError(str(exc)) from exc

    phone_number = claims.get("phone_number")
    if not phone_number:
        raise InvalidOTPError("Firebase token has no verified phone number")

    # Cheap replay/abuse guard even though Firebase already gates the SMS send.
    attempts_allowed = await rate_limit_service.check_and_increment(
        phone_number, "firebase_verify", max_per_window=OTP_VERIFY_MAX_ATTEMPTS, window_seconds=_OTP_TTL_SECONDS
    )
    if not attempts_allowed:
        raise InvalidOTPError("Too many attempts - try again shortly")

    return await _find_or_create_and_issue(session, phone_number)


async def _find_or_create_and_issue(session: AsyncSession, phone_number: str) -> tuple[User, str, str]:
    """Shared login tail: find-or-create the user, mint an access/refresh pair."""
    user = await get_user_by_phone(session, phone_number)
    if user is None:
        user = await create_user(session, user_id=next_id(), phone_number=phone_number)
        if user is None:
            # Two concurrent first-time logins for the same phone (e.g. a
            # retried request) can both reach here: one create_user call wins
            # the unique constraint, the other gets None back. Re-fetch
            # instead of crashing on user.id below.
            user = await get_user_by_phone(session, phone_number)

    access_token = _create_access_token(user.id)
    refresh_token = await _issue_refresh_token(user.id)

    return user, access_token, refresh_token


def _create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def _issue_refresh_token(user_id: int) -> str:
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    }
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    # Tracking valid jti's (instead of trusting the JWT alone) is what makes
    # logout/rotation possible - a bare JWT can't be revoked before it expires.
    await redis_client.sadd(f"{_REFRESH_JTI_KEY_PREFIX}{user_id}", jti)
    await redis_client.expire(f"{_REFRESH_JTI_KEY_PREFIX}{user_id}", REFRESH_TOKEN_EXPIRE_DAYS * 86400)

    return token


def verify_access_token(token: str) -> int:
    """
    Returns the user_id. Used as a FastAPI dependency on every REST route and
    on the WebSocket handshake. Raises jwt.PyJWTError (expired/invalid) on failure.
    """
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return int(payload["sub"])


async def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """
    Validates the refresh token, rotates it (old jti invalidated, new one
    issued), and returns a new (access_token, refresh_token) pair.
    """
    try:
        payload = jwt.decode(refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise InvalidRefreshTokenError(str(e))

    if payload.get("type") != "refresh":
        raise InvalidRefreshTokenError("Not a refresh token")

    user_id = int(payload["sub"])
    jti = payload["jti"]

    jti_key = f"{_REFRESH_JTI_KEY_PREFIX}{user_id}"

    # A single atomic SREM (instead of a SISMEMBER check followed by a
    # separate SREM) is what makes rotation safe under concurrency: two
    # simultaneous refreshes with the same token both hitting SISMEMBER
    # before either SREM ran would otherwise both pass. SREM's return value
    # (1 if it actually removed the member, 0 if it was already gone) is the
    # check.
    removed_count = await redis_client.srem(jti_key, jti)
    if removed_count == 0:
        raise InvalidRefreshTokenError("Refresh token has been revoked or already rotated")

    new_access_token = _create_access_token(user_id)
    new_refresh_token = await _issue_refresh_token(user_id)
    return new_access_token, new_refresh_token


async def logout(refresh_token: str) -> None:
    """Revokes a single refresh token (e.g. "log out this device")."""
    try:
        payload = jwt.decode(refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return  # already invalid/expired - nothing to revoke

    user_id = int(payload["sub"])
    jti = payload.get("jti")
    if jti:
        await redis_client.srem(f"{_REFRESH_JTI_KEY_PREFIX}{user_id}", jti)

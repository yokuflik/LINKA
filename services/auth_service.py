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
)
from database.crud.crud_user import create_user, get_user_by_phone
from database.models.user import User
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


def _otp_key(phone_number: str) -> str:
    return f"{_OTP_KEY_PREFIX}{phone_number}"


async def request_otp(phone_number: str) -> None:
    """
    Generates a one-time login code and hands it to the SMS provider.
    """
    code = f"{random.randint(0, 999999):06d}"
    await redis_client.set(_otp_key(phone_number), code, ex=_OTP_TTL_SECONDS)
    await _deliver_otp(phone_number, code)


async def _deliver_otp(phone_number: str, code: str) -> None:
    #for now the opt is in the command line
    logger.info(f"[STUB] Would SMS OTP {code} to {phone_number}")


async def verify_otp_and_login(session: AsyncSession, phone_number: str, code: str) -> tuple[User, str, str]:
    """
    Verifies the code, creates the user on first login, and issues a fresh
    access/refresh token pair. Returns (user, access_token, refresh_token).
    """
    stored_code = await redis_client.get(_otp_key(phone_number))
    if stored_code is None or stored_code != code:
        raise InvalidOTPError("Invalid or expired code")

    # One-time: consume the code so it can't be replayed
    await redis_client.delete(_otp_key(phone_number))

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

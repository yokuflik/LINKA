import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.auth.limits import AuthPolicy, DEFAULT_AUTH_POLICY
from modules.users.crud import create_user
from modules.users.crud import get_user_by_phone
from modules.users.models import User
from modules.auth import firebase as firebase_auth
from infra.ratelimit import service as rate_limit_service
from modules.users import service as user_service
from infra.redis.client import redis_client
from infra.ids.client import next_id

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


class AccountCreationRateLimitedError(Exception):
    pass


def _otp_key(phone_number: str) -> str:
    return f"{_OTP_KEY_PREFIX}{phone_number}"


async def request_otp(
    phone_number: str,
    intent: str | None = None,
    session: AsyncSession | None = None,
    *,
    policy: AuthPolicy = DEFAULT_AUTH_POLICY,
) -> None:
    """
    Generates a one-time login code and hands it to the SMS provider.

    ADR 0017: there is one flow (phone -> OTP); an unknown phone is
    find-or-create. The legacy ``intent`` pre-check is gone - ``intent`` is
    accepted for wire compatibility but no longer gates anything.

    Rate-limited per phone number - otherwise this endpoint alone is an open
    invitation to SMS-bomb any number (cost abuse against the SMS provider,
    and a real annoyance/attack vector against the phone's owner), since
    nothing about it requires an account or a token yet.
    """
    # Dev whitelist (ADR 0009): a handful of non-real test phone strings ("1".."5")
    # skip SMS entirely - no code is stored, and verify accepts any input.
    if phone_number in settings.DEV_AUTH_WHITELIST:
        return

    allowed = await rate_limit_service.check_and_increment(
        phone_number, "otp_request",
        max_per_window=policy.otp_request_max, window_seconds=policy.otp_request_window_s,
    )
    if not allowed:
        raise OTPRequestRateLimitedError(f"Too many OTP requests for {phone_number}")

    code = f"{random.randint(0, 999999):06d}"
    await redis_client.set(_otp_key(phone_number), code, ex=_OTP_TTL_SECONDS)
    await _deliver_otp(phone_number, code)


async def _deliver_otp(phone_number: str, code: str) -> None:
    #for now the opt is in the command line
    print(f"[STUB] Would SMS OTP {code} to {phone_number}")


async def verify_otp_and_login(
    session: AsyncSession, phone_number: str, code: str, client_ip: str | None = None,
    *, policy: AuthPolicy = DEFAULT_AUTH_POLICY,
) -> tuple[User, str, str, bool]:
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
    if phone_number in settings.DEV_AUTH_WHITELIST:
        return await _find_or_create_and_issue(session, phone_number, client_ip, policy=policy)

    attempts_allowed = await rate_limit_service.check_and_increment(
        phone_number, "otp_verify",
        max_per_window=policy.otp_verify_max_attempts, window_seconds=_OTP_TTL_SECONDS,
    )
    if not attempts_allowed:
        raise InvalidOTPError("Too many attempts - request a new code")

    stored_code = await redis_client.get(_otp_key(phone_number))
    if stored_code is None or stored_code != code:
        raise InvalidOTPError("Invalid or expired code")

    # One-time: consume the code so it can't be replayed
    await redis_client.delete(_otp_key(phone_number))

    return await _find_or_create_and_issue(session, phone_number, client_ip, policy=policy)


async def verify_firebase_and_login(
    session: AsyncSession, id_token: str, client_ip: str | None = None,
    *, policy: AuthPolicy = DEFAULT_AUTH_POLICY,
) -> tuple[User, str, str, bool]:
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
        phone_number, "firebase_verify",
        max_per_window=policy.otp_verify_max_attempts, window_seconds=_OTP_TTL_SECONDS,
    )
    if not attempts_allowed:
        raise InvalidOTPError("Too many attempts - try again shortly")

    return await _find_or_create_and_issue(session, phone_number, client_ip, policy=policy)


async def _find_or_create_and_issue(
    session: AsyncSession, phone_number: str, client_ip: str | None = None,
    *, policy: AuthPolicy = DEFAULT_AUTH_POLICY,
) -> tuple[User, str, str, bool]:
    """Shared login tail: find-or-create the user, mint an access/refresh pair.
    Returns ``(user, access, refresh, is_new_user)`` - the frontend uses the
    flag to open the post-signup welcome form (ADR 0017).

    Creating a brand-new account is gated per client IP (5/day, strict) so a
    single host can't mass-register - an existing number logging back in is
    never blocked by this.
    """
    user = await get_user_by_phone(session, phone_number)
    is_new_user = user is None
    if user is None:
        if client_ip:
            allowed = await rate_limit_service.check_and_increment(
                client_ip,
                "acct_create",
                max_per_window=policy.account_create_ip_max,
                window_seconds=policy.account_create_ip_window_s,
            )
            if not allowed:
                raise AccountCreationRateLimitedError("Too many new accounts from this network - try again later")
        # Auto-assign a free handle (ADR 0017). Retry on the unique-index race:
        # a lost candidate comes back as None from create_user.
        for _ in range(3):
            handle = await user_service.generate_free_username(session)
            user = await create_user(
                session, user_id=await next_id(), phone_number=phone_number, username=handle
            )
            if user is not None:
                break
        if user is None:
            # Two concurrent first-time logins for the same phone (e.g. a
            # retried request) can both reach here: one create_user call wins
            # the unique constraint, the other gets None back. Re-fetch
            # instead of crashing on user.id below.
            user = await get_user_by_phone(session, phone_number)
            is_new_user = False

    access_token = _create_access_token(user.id, policy=policy)
    refresh_token = await _issue_refresh_token(user.id)

    return user, access_token, refresh_token, is_new_user


def _create_access_token(user_id: int, *, policy: AuthPolicy = DEFAULT_AUTH_POLICY) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=policy.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


async def _issue_refresh_token(user_id: int) -> str:
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    # Tracking valid jti's (instead of trusting the JWT alone) is what makes
    # logout/rotation possible - a bare JWT can't be revoked before it expires.
    await redis_client.sadd(f"{_REFRESH_JTI_KEY_PREFIX}{user_id}", jti)
    await redis_client.expire(f"{_REFRESH_JTI_KEY_PREFIX}{user_id}", settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)

    return token


def verify_access_token(token: str) -> int:
    """
    Returns the user_id. Used as a FastAPI dependency on every REST route and
    on the WebSocket handshake. Raises jwt.PyJWTError (expired/invalid) on failure.
    """
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return int(payload["sub"])


async def refresh_access_token(
    refresh_token: str, *, policy: AuthPolicy = DEFAULT_AUTH_POLICY
) -> tuple[str, str]:
    """
    Validates the refresh token, rotates it (old jti invalidated, new one
    issued), and returns a new (access_token, refresh_token) pair.
    """
    try:
        payload = jwt.decode(refresh_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise InvalidRefreshTokenError(str(e))

    if payload.get("type") != "refresh":
        raise InvalidRefreshTokenError("Not a refresh token")

    user_id = int(payload["sub"])
    jti = payload["jti"]

    # Per-token throttle: even a valid (not-yet-rotated) refresh token can only
    # be exchanged a bounded number of times per hour, so a leaked token can't
    # be spun into an unlimited stream of access tokens before it's noticed.
    jti_allowed = await rate_limit_service.check_and_increment(
        jti,
        "refresh_jti",
        max_per_window=policy.refresh_jti_max,
        window_seconds=policy.refresh_jti_window_s,
    )
    if not jti_allowed:
        raise InvalidRefreshTokenError("This refresh token is being used too frequently")

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

    new_access_token = _create_access_token(user_id, policy=policy)
    new_refresh_token = await _issue_refresh_token(user_id)
    return new_access_token, new_refresh_token


async def logout(refresh_token: str) -> None:
    """Revokes a single refresh token (e.g. "log out this device")."""
    try:
        payload = jwt.decode(refresh_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return  # already invalid/expired - nothing to revoke

    user_id = int(payload["sub"])
    jti = payload.get("jti")
    if jti:
        await redis_client.srem(f"{_REFRESH_JTI_KEY_PREFIX}{user_id}", jti)

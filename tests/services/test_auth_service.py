import asyncio

import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services import auth_service

pytestmark = pytest.mark.asyncio


async def _get_code(redis_db, phone_number: str) -> str:
    code = await redis_db.get(auth_service._otp_key(phone_number))
    assert code is not None, "expected an OTP to have been stored"
    return code


async def test_request_otp_stores_a_6_digit_code(redis_db):
    phone = "+972500000001"
    await auth_service.request_otp(phone)

    code = await _get_code(redis_db, phone)
    assert len(code) == 6
    assert code.isdigit()


async def test_verify_otp_wrong_code_raises(db_session: AsyncSession, redis_db):
    phone = "+972500000002"
    await auth_service.request_otp(phone)

    with pytest.raises(auth_service.InvalidOTPError):
        await auth_service.verify_otp_and_login(db_session, phone, "000000")


async def test_verify_otp_expired_or_never_requested_raises(db_session: AsyncSession, redis_db):
    with pytest.raises(auth_service.InvalidOTPError):
        await auth_service.verify_otp_and_login(db_session, "+972500000003", "123456")


async def test_verify_otp_creates_user_on_first_login(db_session: AsyncSession, redis_db):
    phone = "+972500000004"
    await auth_service.request_otp(phone)
    code = await _get_code(redis_db, phone)

    user, access_token, refresh_token = await auth_service.verify_otp_and_login(db_session, phone, code)

    assert user.phone_number == phone
    assert auth_service.verify_access_token(access_token) == user.id


async def test_verify_otp_code_is_single_use(db_session: AsyncSession, redis_db):
    phone = "+972500000005"
    await auth_service.request_otp(phone)
    code = await _get_code(redis_db, phone)

    await auth_service.verify_otp_and_login(db_session, phone, code)

    # Replaying the exact same (now-consumed) code must fail
    with pytest.raises(auth_service.InvalidOTPError):
        await auth_service.verify_otp_and_login(db_session, phone, code)


async def test_verify_otp_second_login_reuses_existing_user(db_session: AsyncSession, redis_db):
    phone = "+972500000006"

    await auth_service.request_otp(phone)
    code1 = await _get_code(redis_db, phone)
    user1, _, _ = await auth_service.verify_otp_and_login(db_session, phone, code1)

    await auth_service.request_otp(phone)
    code2 = await _get_code(redis_db, phone)
    user2, _, _ = await auth_service.verify_otp_and_login(db_session, phone, code2)

    assert user1.id == user2.id


async def test_concurrent_first_login_same_phone_does_not_crash(session_factory, redis_db):
    # Two devices/tabs submitting the same still-valid OTP at once (or a
    # network retry) can both reach the "create the user" branch together.
    # This used to crash with AttributeError ('NoneType'.id) when the loser
    # of the unique-constraint race got None back from create_user.
    phone = "+972500000007"
    await auth_service.request_otp(phone)
    code = await _get_code(redis_db, phone)

    async def attempt():
        async with session_factory() as session:
            return await auth_service.verify_otp_and_login(session, phone, code)

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    crashes = [r for r in results if isinstance(r, Exception) and not isinstance(r, auth_service.InvalidOTPError)]
    assert crashes == [], f"unexpected crash(es): {crashes}"

    # Whichever attempt(s) succeeded must agree on a single user
    user_ids = {r[0].id for r in results if not isinstance(r, Exception)}
    assert len(user_ids) == 1


async def test_verify_access_token_rejects_expired_token(db_session: AsyncSession, redis_db, monkeypatch):
    monkeypatch.setattr(auth_service, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)
    token = auth_service._create_access_token(user_id=42)

    with pytest.raises(jwt.ExpiredSignatureError):
        auth_service.verify_access_token(token)


async def test_verify_access_token_rejects_a_refresh_token(redis_db):
    refresh_token = await auth_service._issue_refresh_token(user_id=42)

    with pytest.raises(jwt.InvalidTokenError):
        auth_service.verify_access_token(refresh_token)


async def test_verify_access_token_rejects_garbage():
    with pytest.raises(jwt.PyJWTError):
        auth_service.verify_access_token("not-a-real-token")


async def test_refresh_rotates_and_invalidates_the_old_token(redis_db):
    refresh_token = await auth_service._issue_refresh_token(user_id=77)

    new_access, new_refresh = await auth_service.refresh_access_token(refresh_token)

    assert auth_service.verify_access_token(new_access) == 77
    assert new_refresh != refresh_token

    # The rotated-away token must not work a second time
    with pytest.raises(auth_service.InvalidRefreshTokenError):
        await auth_service.refresh_access_token(refresh_token)


async def test_refresh_rejects_garbage_token(redis_db):
    with pytest.raises(auth_service.InvalidRefreshTokenError):
        await auth_service.refresh_access_token("not-a-real-token")


async def test_refresh_rejects_an_access_token(redis_db):
    access_token = auth_service._create_access_token(user_id=88)

    with pytest.raises(auth_service.InvalidRefreshTokenError):
        await auth_service.refresh_access_token(access_token)


async def test_logout_revokes_the_refresh_token(redis_db):
    refresh_token = await auth_service._issue_refresh_token(user_id=99)

    await auth_service.logout(refresh_token)

    with pytest.raises(auth_service.InvalidRefreshTokenError):
        await auth_service.refresh_access_token(refresh_token)


async def test_concurrent_refresh_with_the_same_token_only_one_wins(redis_db):
    # Two requests racing to refresh with the exact same (stolen, replayed,
    # or just double-fired) refresh token must not both succeed - that would
    # silently hand out two independent valid sessions from one token.
    refresh_token = await auth_service._issue_refresh_token(user_id=100)

    results = await asyncio.gather(
        auth_service.refresh_access_token(refresh_token),
        auth_service.refresh_access_token(refresh_token),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, auth_service.InvalidRefreshTokenError)]

    assert len(successes) == 1
    assert len(failures) == 1

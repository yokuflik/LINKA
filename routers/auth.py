from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    OTP_REQUEST_IP_RATE_LIMIT_MAX,
    OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS,
    OTP_VERIFY_IP_RATE_LIMIT_MAX,
    OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS,
    REFRESH_IP_RATE_LIMIT_MAX,
    REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS,
)
from database.connection import get_db
from routers.schemas import FirebaseVerifyIn, LoginOut, OTPRequestIn, OTPVerifyIn, RefreshTokenIn, TokenPairOut
from services import auth_service, rate_limit_service
from services.rate_limit_service import RateLimited

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return rate_limit_service.client_ip(request)


async def _enforce_ip(ip: str, action: str, max_per_window: int, window_seconds: int) -> None:
    """Coarse per-IP fixed-window gate, run before the service. One host must
    not be able to spray OTPs / refresh calls across many identities."""
    allowed = await rate_limit_service.check_and_increment(
        ip, action, max_per_window=max_per_window, window_seconds=window_seconds
    )
    if not allowed:
        raise RateLimited(action, retry_after=window_seconds)


@router.post("/otp/request", status_code=status.HTTP_204_NO_CONTENT)
async def request_otp(body: OTPRequestIn, request: Request, session: AsyncSession = Depends(get_db)):
    ip = _client_ip(request)
    await _enforce_ip(
        ip, "otp_request_ip", OTP_REQUEST_IP_RATE_LIMIT_MAX, OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS
    )
    await auth_service.request_otp(body.phone_number, intent=body.intent, session=session)


@router.post("/otp/verify", response_model=LoginOut)
async def verify_otp(body: OTPVerifyIn, request: Request, session: AsyncSession = Depends(get_db)):
    ip = _client_ip(request)
    await _enforce_ip(
        ip, "otp_verify_ip", OTP_VERIFY_IP_RATE_LIMIT_MAX, OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS
    )
    user, access_token, refresh_token = await auth_service.verify_otp_and_login(
        session, body.phone_number, body.code, client_ip=ip
    )
    return LoginOut(user=user, access_token=access_token, refresh_token=refresh_token)


@router.post("/firebase/verify", response_model=LoginOut)
async def firebase_verify(body: FirebaseVerifyIn, request: Request, session: AsyncSession = Depends(get_db)):
    ip = _client_ip(request)
    await _enforce_ip(
        ip, "otp_verify_ip", OTP_VERIFY_IP_RATE_LIMIT_MAX, OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS
    )
    user, access_token, refresh_token = await auth_service.verify_firebase_and_login(
        session, body.id_token, client_ip=ip
    )
    return LoginOut(user=user, access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(body: RefreshTokenIn, request: Request):
    ip = _client_ip(request)
    await _enforce_ip(
        ip, "refresh_ip", REFRESH_IP_RATE_LIMIT_MAX, REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS
    )
    access_token, refresh_token = await auth_service.refresh_access_token(body.refresh_token)
    return TokenPairOut(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshTokenIn):
    await auth_service.logout(body.refresh_token)

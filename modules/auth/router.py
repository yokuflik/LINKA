from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.connection import get_db
from modules.auth.limits import AuthPolicy, DEFAULT_AUTH_POLICY
from modules.auth.schemas import FirebaseVerifyIn
from modules.auth.schemas import LoginOut
from modules.auth.schemas import OTPRequestIn
from modules.auth.schemas import OTPVerifyIn
from modules.auth.schemas import RefreshTokenIn
from modules.auth.schemas import TokenPairOut
from modules.auth import service as auth_service
from infra.ratelimit import service as rate_limit_service
from infra.ratelimit.service import RateLimited

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_policy() -> AuthPolicy:
    """FastAPI dependency (ADR 0033). Tests override this with
    `app.dependency_overrides[get_auth_policy]` to shrink a limit/window."""
    return DEFAULT_AUTH_POLICY


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
async def request_otp(
    body: OTPRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_db),
    policy: AuthPolicy = Depends(get_auth_policy),
):
    ip = _client_ip(request)
    await _enforce_ip(ip, "otp_request_ip", policy.otp_request_ip_max, policy.otp_request_ip_window_s)
    await auth_service.request_otp(body.phone_number, intent=body.intent, session=session, policy=policy)


@router.post("/otp/verify", response_model=LoginOut)
async def verify_otp(
    body: OTPVerifyIn,
    request: Request,
    session: AsyncSession = Depends(get_db),
    policy: AuthPolicy = Depends(get_auth_policy),
):
    ip = _client_ip(request)
    await _enforce_ip(ip, "otp_verify_ip", policy.otp_verify_ip_max, policy.otp_verify_ip_window_s)
    user, access_token, refresh_token, is_new_user = await auth_service.verify_otp_and_login(
        session, body.phone_number, body.code, client_ip=ip, policy=policy
    )
    return LoginOut(
        user=user, access_token=access_token, refresh_token=refresh_token, is_new_user=is_new_user
    )


@router.post("/firebase/verify", response_model=LoginOut)
async def firebase_verify(
    body: FirebaseVerifyIn,
    request: Request,
    session: AsyncSession = Depends(get_db),
    policy: AuthPolicy = Depends(get_auth_policy),
):
    ip = _client_ip(request)
    await _enforce_ip(ip, "otp_verify_ip", policy.otp_verify_ip_max, policy.otp_verify_ip_window_s)
    user, access_token, refresh_token, is_new_user = await auth_service.verify_firebase_and_login(
        session, body.id_token, client_ip=ip, policy=policy
    )
    return LoginOut(
        user=user, access_token=access_token, refresh_token=refresh_token, is_new_user=is_new_user
    )


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(
    body: RefreshTokenIn,
    request: Request,
    policy: AuthPolicy = Depends(get_auth_policy),
):
    ip = _client_ip(request)
    await _enforce_ip(ip, "refresh_ip", policy.refresh_ip_max, policy.refresh_ip_window_s)
    access_token, refresh_token = await auth_service.refresh_access_token(body.refresh_token, policy=policy)
    return TokenPairOut(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshTokenIn):
    await auth_service.logout(body.refresh_token)

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from routers.schemas import FirebaseVerifyIn, LoginOut, OTPRequestIn, OTPVerifyIn, RefreshTokenIn, TokenPairOut
from services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/otp/request", status_code=status.HTTP_204_NO_CONTENT)
async def request_otp(body: OTPRequestIn, session: AsyncSession = Depends(get_db)):
    await auth_service.request_otp(body.phone_number, intent=body.intent, session=session)


@router.post("/otp/verify", response_model=LoginOut)
async def verify_otp(body: OTPVerifyIn, session: AsyncSession = Depends(get_db)):
    user, access_token, refresh_token = await auth_service.verify_otp_and_login(session, body.phone_number, body.code)
    return LoginOut(user=user, access_token=access_token, refresh_token=refresh_token)


@router.post("/firebase/verify", response_model=LoginOut)
async def firebase_verify(body: FirebaseVerifyIn, session: AsyncSession = Depends(get_db)):
    user, access_token, refresh_token = await auth_service.verify_firebase_and_login(session, body.id_token)
    return LoginOut(user=user, access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(body: RefreshTokenIn):
    access_token, refresh_token = await auth_service.refresh_access_token(body.refresh_token)
    return TokenPairOut(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshTokenIn):
    await auth_service.logout(body.refresh_token)

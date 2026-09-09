"""Auth / OTP / token API models (ADR 0030)."""

from pydantic import BaseModel

from modules.users.schemas import UserOut


class OTPRequestIn(BaseModel):
    phone_number: str
    # 'login' | 'register' - lets the server reject "register an existing
    # number" / "log in with an unknown number" before an OTP is even sent.
    # Optional so existing callers keep working (no intent = no pre-check).
    intent: str | None = None


class OTPVerifyIn(BaseModel):
    phone_number: str
    code: str


class FirebaseVerifyIn(BaseModel):
    # Firebase Phone Auth ID token, issued client-side after the SMS code check.
    id_token: str


class RefreshTokenIn(BaseModel):
    refresh_token: str


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str


class LoginOut(BaseModel):
    user: UserOut
    access_token: str
    refresh_token: str
    # ADR 0017: true only when this verify call just created the account, so the
    # client opens the post-signup welcome form (username pre-filled).
    is_new_user: bool = False


__all__ = [
    "OTPRequestIn",
    "OTPVerifyIn",
    "FirebaseVerifyIn",
    "RefreshTokenIn",
    "TokenPairOut",
    "LoginOut",
]

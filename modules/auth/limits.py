"""Injectable auth tunables (ADR 0033).

`AuthPolicy` is the single place the auth rate-limit / token knobs are named.
Service functions take `policy: AuthPolicy = DEFAULT_AUTH_POLICY`; the router
exposes `get_auth_policy` as a FastAPI dependency so tests override it with
`app.dependency_overrides` (route tests) or pass `policy=AuthPolicy(...)`
directly (service-unit tests) instead of `monkeypatch.setattr`-ing a
`from config import NAME` module global.

The field defaults are read from `config` once, at class-definition time -
that is fine: a test never patches a dataclass default, it constructs a new
`AuthPolicy(otp_request_max=...)`.
"""

from dataclasses import dataclass

from config import settings


@dataclass(frozen=True)
class AuthPolicy:
    # Per-phone OTP request / verify caps (enforced in the service).
    otp_request_max: int = settings.OTP_REQUEST_RATE_LIMIT_MAX
    otp_request_window_s: int = settings.OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS
    otp_verify_max_attempts: int = settings.OTP_VERIFY_MAX_ATTEMPTS
    # Per-IP account-creation cap (enforced in the service login tail).
    account_create_ip_max: int = settings.ACCOUNT_CREATE_IP_RATE_LIMIT_MAX
    account_create_ip_window_s: int = settings.ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS
    # Per-jti refresh throttle (enforced in the service).
    refresh_jti_max: int = settings.REFRESH_JTI_RATE_LIMIT_MAX
    refresh_jti_window_s: int = settings.REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS
    # Token lifetime (not a rate limit - why this object is a "Policy").
    access_token_expire_minutes: int = settings.ACCESS_TOKEN_EXPIRE_MINUTES
    # Per-IP coarse gates enforced in the router before the service runs.
    otp_request_ip_max: int = settings.OTP_REQUEST_IP_RATE_LIMIT_MAX
    otp_request_ip_window_s: int = settings.OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS
    otp_verify_ip_max: int = settings.OTP_VERIFY_IP_RATE_LIMIT_MAX
    otp_verify_ip_window_s: int = settings.OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS
    refresh_ip_max: int = settings.REFRESH_IP_RATE_LIMIT_MAX
    refresh_ip_window_s: int = settings.REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS


DEFAULT_AUTH_POLICY = AuthPolicy()

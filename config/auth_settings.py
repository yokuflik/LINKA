import os

# --- JWT / Auth ---
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

# --- OTP abuse protection ---
# A 6-digit code (1M possibilities) with no attempt cap is brute-forceable
# well within its own TTL by any reasonably fast script - these limits
# are what actually make that TTL meaningful.
#
# Two axes (COMMS_SECURITY_PLAN step 4): per-PHONE limits protect a single
# number's owner from SMS-bombing; per-IP limits stop one host spraying OTPs
# at thousands of different numbers. The per-phone request limit was retuned
# from 20/10min down to 5/30min.
OTP_REQUEST_RATE_LIMIT_MAX = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_MAX", "5"))
OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS", "1800"))
OTP_VERIFY_MAX_ATTEMPTS = int(os.environ.get("OTP_VERIFY_MAX_ATTEMPTS", "5"))

# Per-IP OTP ceilings.
OTP_REQUEST_IP_RATE_LIMIT_MAX = int(os.environ.get("OTP_REQUEST_IP_RATE_LIMIT_MAX", "15"))
OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS", "3600"))
OTP_VERIFY_IP_RATE_LIMIT_MAX = int(os.environ.get("OTP_VERIFY_IP_RATE_LIMIT_MAX", "30"))
OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS", "3600"))

# Per-IP /auth/refresh ceiling, plus a per-refresh-token-jti throttle so a
# single leaked refresh token can't be spun into unlimited access tokens.
REFRESH_IP_RATE_LIMIT_MAX = int(os.environ.get("REFRESH_IP_RATE_LIMIT_MAX", "60"))
REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS", "3600"))
REFRESH_JTI_RATE_LIMIT_MAX = int(os.environ.get("REFRESH_JTI_RATE_LIMIT_MAX", "10"))
REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS", "3600"))

# New accounts (first successful verify for a previously-unknown phone) per IP
# per day. Strict on purpose (business answer 5, 2026-09-06): no relaxation.
ACCOUNT_CREATE_IP_RATE_LIMIT_MAX = int(os.environ.get("ACCOUNT_CREATE_IP_RATE_LIMIT_MAX", "5"))
ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS", "86400"))

# --- Firebase Phone Auth (ADR 0009) ---
# Real phone verification runs client-side (Firebase JS SDK + reCAPTCHA); the
# server only verifies the resulting ID token against Google's public JWKS.
# FIREBASE_PROJECT_ID doubles as the expected token `aud` / `iss` suffix.
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_AUTH_ENABLED = bool(FIREBASE_PROJECT_ID)
# Exact phone-number strings that skip all verification (dev/PoC only). These
# are not real numbers - the client routes them through the legacy OTP stub.
DEV_AUTH_WHITELIST = set(
    x.strip() for x in os.environ.get("DEV_AUTH_WHITELIST", "1,2,3,4,5").split(",") if x.strip()
)

__all__ = [
    "JWT_SECRET_KEY",
    "JWT_ALGORITHM",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "REFRESH_TOKEN_EXPIRE_DAYS",
    "OTP_REQUEST_RATE_LIMIT_MAX",
    "OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS",
    "OTP_VERIFY_MAX_ATTEMPTS",
    "OTP_REQUEST_IP_RATE_LIMIT_MAX",
    "OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS",
    "OTP_VERIFY_IP_RATE_LIMIT_MAX",
    "OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS",
    "REFRESH_IP_RATE_LIMIT_MAX",
    "REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS",
    "REFRESH_JTI_RATE_LIMIT_MAX",
    "REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS",
    "ACCOUNT_CREATE_IP_RATE_LIMIT_MAX",
    "ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_AUTH_ENABLED",
    "DEV_AUTH_WHITELIST",
]

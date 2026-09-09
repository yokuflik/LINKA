from sqlalchemy import Column, BigInteger, String, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from infra.db.base import Base

class User(Base):
    __tablename__ = "users"

    # Using BigInteger to support Snowflake IDs for massive scale
    id = Column(BigInteger, primary_key=True, index=True)

    # Phone number is the only unique identifier (e.g., +972501234567)
    # Indexed for fast lookups during OTP login
    phone_number = Column(String(20), unique=True, index=True, nullable=False)

    # Unique, lowercase-canonical handle (ADR 0017). Always stored lowercase;
    # the plain UNIQUE index is the sole race-safe uniqueness authority.
    # NOT NULL - every account is auto-assigned one at creation
    # (user_service.generate_free_username).
    username = Column(String(32), unique=True, index=True, nullable=False)

    # Timestamp of the last *user-initiated* username change. NULL until the
    # user changes their auto-assigned handle themselves. Kept for the
    # released-handle grace-hold reasoning; no longer the quota authority.
    username_changed_at = Column(DateTime(timezone=True), nullable=True)

    # Rolling log of user-initiated username-change timestamps (ISO-8601 UTC
    # strings, newest last), capped at config.USERNAME_CHANGE_MAX_PER_WINDOW
    # entries. NULL/absent = never changed. Authority for the change quota
    # (ADR 0023): up to N changes per rolling USERNAME_CHANGE_WINDOW_DAYS.
    username_change_log = Column(JSONB, nullable=True)

    # Optional, free-form, multi-language nickname (ADR 0024). No uniqueness,
    # no index, never searchable. Shown instead of `username` when set
    # (fallback: display_name || username || phone_number). Sanitised in the
    # service layer (control/bidi/zero-width stripped, NFC, capped at
    # config.DISPLAY_NAME_MAX_LEN). Column is 80 to leave headroom over the cap.
    display_name = Column(String(80), nullable=True)

    # Short bio or status text
    about_text = Column(String(150), nullable=True)

    # URL pointing to the image file stored in AWS S3 / MinIO
    profile_pic_url = Column(Text, nullable=True)

    # Tiny inline thumbnail (a ~64px JPEG data: URI) computed by the uploader's
    # browser - ADR 0016. Rendered directly as the avatar; the full-res image
    # loads only when the avatar is tapped.
    profile_pic_preview = Column(Text, nullable=True)

    # Running total of the sizes of media messages this user has sent, in bytes
    # (ADR 0028). Authority for the per-user hard storage quota
    # (config.STORAGE_QUOTA_BYTES). Counted per send / per ref - dedup (ADR 0010)
    # does NOT discount it. Incremented on confirmed send, decremented (floored
    # at 0) on an irreversible message purge (ADR 0021); a soft delete does not
    # refund. Existing accounts start at 0 - no backfill.
    storage_bytes_used = Column(BigInteger, nullable=False, server_default="0", default=0)

    # Audit timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class UserPublicKey(Base):
    """Client-side E2E encryption (ADR 0026): the user's long-term ECDH P-256
    *public* key, uploaded by their browser. The server stores and hands it out
    as an opaque blob - it never holds the matching private key or any
    plaintext. One current key per user; the table shape (not a column on
    ``users``) leaves room for per-device keys later without a migration."""

    __tablename__ = "user_public_keys"

    user_id = Column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    # JWK of the public key, e.g. {"kty":"EC","crv":"P-256","x":...,"y":...}.
    # Validated in the service layer only for "is a public EC/P-256 JWK with no
    # private `d` component" - the bytes are otherwise opaque to the server.
    public_key = Column(JSONB, nullable=False)

    algo = Column(String(32), nullable=False, default="ECDH-P256")

    # SHA-256 hex of the canonical JWK - the basis for a future safety-number
    # UI (ADR 0026 defers the UI, keeps the value).
    fingerprint = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
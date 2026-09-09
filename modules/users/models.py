from sqlalchemy import Column, BigInteger, String, Text, DateTime
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

    # Short bio or status text
    about_text = Column(String(150), nullable=True)

    # URL pointing to the image file stored in AWS S3 / MinIO
    profile_pic_url = Column(Text, nullable=True)

    # Tiny inline thumbnail (a ~64px JPEG data: URI) computed by the uploader's
    # browser - ADR 0016. Rendered directly as the avatar; the full-res image
    # loads only when the avatar is tapped.
    profile_pic_preview = Column(Text, nullable=True)

    # Audit timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
from sqlalchemy import Column, BigInteger, ForeignKey, DateTime, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from database.base import Base


class UserSettings(Base):
    """
    Per-user persistent settings (privacy, and whatever we add later).

    One row per user, 1:1 with `users`. All actual settings live in a single
    JSONB blob rather than one column per setting so that adding a new
    setting is a code-only change (extend DEFAULT_USER_SETTINGS + the
    validator) with no migration - see docs/adr/0002-user-settings-jsonb.md.
    Stored values are sparse: only keys the user has explicitly changed are
    persisted; the service layer merges them over DEFAULT_USER_SETTINGS on
    read.
    """

    __tablename__ = "user_settings"

    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    settings = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

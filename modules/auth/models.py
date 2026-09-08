"""
Released-username grace hold (ADR 0017).

When a user changes their username, the old handle is not instantly grabbable:
a row is inserted here and, while it is live (``expires_at > now()``), nobody
else may take that handle (``grace_hold``) but the original owner may reclaim
it. Anti-impersonation.

One row per released handle, keyed by the lowercase username. Tiny table
(one row per username change, not per user); not partitioned. Expiry is
checked passively - a prune of dead rows can be added to
scripts/partition_maintenance.py later but is not required for correctness.
"""

from sqlalchemy import Column, BigInteger, String, DateTime

from infra.db.base import Base


class ReservedUsername(Base):
    __tablename__ = "reserved_usernames"

    # Lowercase handle being held.
    username = Column(String(32), primary_key=True)

    # The user who released it and may reclaim it during the grace window.
    reserved_for_user_id = Column(BigInteger, nullable=False)

    released_at = Column(DateTime(timezone=True), nullable=False)
    # released_at + config.USERNAME_RESERVED_GRACE_DAYS. A row with
    # expires_at <= now() is dead and ignored by username_is_free.
    expires_at = Column(DateTime(timezone=True), nullable=False)

from sqlalchemy import Column, BigInteger, SMALLINT, Text, Boolean, DateTime, ForeignKey, Index, PrimaryKeyConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from database.base import Base

class Message(Base):
    __tablename__ = "messages"

    # Snowflake ID, generated at the application layer (same generator as User/Chat).
    # Sortable by creation time, so ORDER BY id == ORDER BY created_at.
    id = Column(BigInteger, nullable=False)

    # Audit + partition key. Postgres requires the partition key to be part
    # of every primary/unique key on a range-partitioned table, which is why
    # it's in the PK below instead of only living as a plain timestamp column.
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)

    # Nullable + SET NULL: deleting a user must not erase their message
    # history for the other participants (mirrors WhatsApp's "deleted user" behavior).
    sender_id = Column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # 1=text, 2=image, 3=video, 4=audio, 5=file, 6=system. SMALLINT for the same
    # memory-efficiency reason as Participant.role.
    type = Column(SMALLINT, nullable=False, default=1)

    content = Column(Text, nullable=True)

    # Loosely-referenced on purpose: a strict FK here would need to include
    # the partition key (created_at) of the replied-to row, which is awkward
    # across partitions at this scale. Validated at the application layer instead.
    reply_to_message_id = Column(BigInteger, nullable=True)

    is_edited = Column(Boolean, nullable=False, default=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)

    # Soft delete: physically deleting rows in a table this size is expensive
    # and unnecessary; a NULL check is enough to hide the message.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # No back_populates collections on Chat/User: with tens of billions of rows,
    # an ORM relationship like chat.messages would silently try to load an
    # unbounded result set. Always query messages explicitly with pagination.
    chat = relationship("Chat", viewonly=True)
    sender = relationship("User", viewonly=True)

    __table_args__ = (
        # Composite PK: (id, created_at) satisfies Postgres's partition-key
        # requirement while id alone remains effectively unique (Snowflake).
        PrimaryKeyConstraint("id", "created_at"),

        # Covers the hot-path query: paginate a single chat's messages by id.
        Index("ix_messages_chat_id_id", "chat_id", "id"),

        {"postgresql_partition_by": "RANGE (created_at)"},
    )

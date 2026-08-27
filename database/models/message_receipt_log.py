from sqlalchemy import BigInteger, Column, DateTime, Index, PrimaryKeyConstraint, SMALLINT
from sqlalchemy.sql import func

from database.base import Base


class MessageReceiptLog(Base):
    """
    Append-only history of every per-user receipt-watermark advance
    (delivered / read / played) in a chat, with the timestamp it happened.

    NOT a row per (message, user): one row per *acknowledgement action* - a
    client marking "read up to message X" - regardless of how many messages
    that action covered. This is what keeps it affordable at a billion users:
    opening a chat with 500 unread messages is one row, not 500.

    Two questions the O(1) watermark model on Chat/Participant cannot answer,
    and that this table exists for:
      - "when exactly did user U read message X" -> occurred_at of U's
        earliest row for this chat with kind=read and up_to_message_id >= X
        (the instant U's watermark crossed X - same semantics as WhatsApp's
        read timestamp).
      - "who in this group has read / played message X" -> every current
        participant that has such a row.

    The fast-path sent/delivered/read/played tick (the chat list, the check
    mark under a bubble) is still the watermark rollup on Chat - see
    database/models/message.py MessageStatus - and never reads this table.

    Writes land here batched, through a Redis Stream + background worker
    (services/receipts), so a large-group acknowledgement is a single
    multi-row INSERT. RANGE-partitioned by occurred_at; partitions older than
    config.RECEIPT_LOG_RETENTION_DAYS are dropped whole
    (scripts/prune_receipt_log.py). No FK to messages/chats/users for the
    same cross-partition reason Message.reply_to_message_id has none.
    """

    __tablename__ = "message_receipt_log"

    # Snowflake id, minted by the worker at insert time. Time-ordered, so
    # ORDER BY id is ORDER BY insertion order.
    id = Column(BigInteger, nullable=False)

    # Partition key. Same (id, occurred_at) composite-PK shape as Message,
    # for the same Postgres range-partitioning requirement.
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    chat_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)

    # config.RECEIPT_KIND_* : 2=delivered, 3=read, 4=played. Mirrors the
    # MessageStatus integer values for the same states.
    kind = Column(SMALLINT, nullable=False)

    # The participant's watermark position right after this acknowledgement.
    up_to_message_id = Column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at"),
        # "who has crossed message X" for a kind, and - ordered by
        # up_to_message_id asc - each user's *earliest* crossing of it.
        Index("ix_receipt_log_chat_kind_upto", "chat_id", "kind", "up_to_message_id"),
        # one user's own receipt history within a chat (a "my activity" view).
        Index("ix_receipt_log_chat_user_kind_id", "chat_id", "user_id", "kind", "id"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

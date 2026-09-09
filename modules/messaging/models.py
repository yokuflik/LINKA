import enum

from sqlalchemy import Column, BigInteger, SMALLINT, Text, Boolean, DateTime, ForeignKey, Index, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from infra.db.base import Base


# type == 4. Voice recordings are the only message kind that can additionally
# reach the PLAYED receipt state below ("נשמעה" - the recipient actually
# listened to it), on top of the sent/delivered/read a text message can have.
AUDIO_MESSAGE_TYPE = 4


class MessageStatus(enum.IntEnum):
    """
    A message's receipt state from the sender's point of view - WhatsApp's
    one grey check / two grey checks / two blue checks, plus PLAYED for a
    voice recording the recipient(s) have actually listened to.

    Deliberately not a column on Message: it's derived on the fly by
    crud_message.compute_message_status(), by comparing this message's id
    against its chat's all_delivered_up_to_message_id/all_read_up_to_message_id
    (see database/models/chat.py). Storing it directly would need a write to
    this row every time it crosses a threshold - and in a group, that
    threshold is "every one of up to ~1000 participants has acknowledged it",
    on a table already sized for tens of billions of rows. The chat-level
    watermark columns turn that into a single indexed integer comparison
    per message instead, at read time, with zero extra writes to `messages`.
    """
    SENT = 1
    DELIVERED = 2
    READ = 3

    # Voice recordings only (AUDIO_MESSAGE_TYPE). Ranks above READ: playing a
    # voice note implies having seen it. Derived exactly like the others, by
    # comparing the message id against Chat.all_played_up_to_message_id
    # (MIN(Participant.last_played_message_id) across the chat's participants),
    # so a group where every member has listened rolls up to PLAYED with no
    # per-message, per-recipient write.
    PLAYED = 4


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

    # --- Client-side E2E encryption (ADR 0026) ---
    # is_encrypted true => `content` is opaque base64 AES-256-GCM ciphertext the
    # server never decrypts, and enc_header carries the per-message header:
    # {v, alg, iv, eph_pub (ephemeral ECDH public JWK), wraps: {user_id: {ek,iv}}}.
    # Both default to the plaintext path so pre-feature messages are untouched.
    is_encrypted = Column(Boolean, nullable=False, default=False)
    enc_header = Column(JSONB, nullable=True)

    # --- Media attachment (image/video/audio/file messages) ---
    # The app server never touches file bytes: the client uploads directly to
    # object storage via a presigned PUT, then sends the message carrying the
    # resulting object key here. media_key is the private-media-bucket key;
    # media_mime / media_size are the client's declared values (authoritative
    # type/size come from a storage HEAD, off the hot path). media_name is the
    # original filename (for `file` messages). media_duration_seconds is for
    # audio/video. All NULL for a plain text or system message.
    media_key = Column(Text, nullable=True)
    media_mime = Column(Text, nullable=True)
    media_size = Column(BigInteger, nullable=True)
    media_name = Column(Text, nullable=True)
    media_duration_seconds = Column(BigInteger, nullable=True)

    # Tiny blurred placeholder (ThumbHash, base64) computed by the sender's
    # browser at send time - image thumbnail, or a video's first frame. The
    # client renders this instantly on chat open and only fetches the real
    # object bytes on an explicit tap (ADR 0014). NULL for text/system/file
    # messages and for anything sent by a pre-feature client.
    media_blur_hash = Column(Text, nullable=True)

    # Loosely-referenced on purpose: a strict FK here would need to include
    # the partition key (created_at) of the replied-to row, which is awkward
    # across partitions at this scale. Validated at the application layer instead.
    reply_to_message_id = Column(BigInteger, nullable=True)

    is_edited = Column(Boolean, nullable=False, default=False)
    edited_at = Column(DateTime(timezone=True), nullable=True)

    # Soft delete: physically deleting rows in a table this size is expensive
    # and unnecessary; a NULL check is enough to hide the message.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # Hard "delete forever" (ADR 0021): stamped when the sender purges an
    # already-soft-deleted message. content / media_* are wiped to NULL, the
    # media blob is dereffed (object removed from S3 on the last deref), and
    # restore is permanently blocked. The row itself is never physically
    # removed (partition PK). NULL = not purged.
    purged_at = Column(DateTime(timezone=True), nullable=True)

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


class ScheduledMessageStatus(enum.IntEnum):
    """Lifecycle of a ScheduledMessage row (ADR 0031)."""
    PENDING = 0
    SENT = 1
    CANCELLED = 2
    FAILED = 3


class ScheduledMessage(Base):
    """
    A message the user composed now to be delivered at `scheduled_for` (ADR 0031).

    Deliberately NOT a `messages` row and NOT partitioned: it is not a real
    message yet, has no Snowflake-id/created_at partition semantics, and is
    low-volume (bounded by SCHEDULED_MAX_PENDING_PER_USER per user). At fire
    time an in-process poll worker turns it into a real message via the normal
    async send path, reusing `client_message_id` as the idempotency key.
    """
    __tablename__ = "scheduled_messages"

    # Snowflake id, minted via infra.ids.client.next_id. Not a partition key,
    # so there is no created_at-window constraint on it.
    id = Column(BigInteger, primary_key=True)

    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Absolute UTC fire time; the client converts from local time.
    scheduled_for = Column(DateTime(timezone=True), nullable=False)

    # 1=text, 2=image, 3=video, 4=audio, 5=file. 6 (system) is rejected.
    type = Column(SMALLINT, nullable=False, default=1)

    content = Column(Text, nullable=True)

    # Same shape as Message, captured at schedule time.
    media_key = Column(Text, nullable=True)
    media_mime = Column(Text, nullable=True)
    media_size = Column(BigInteger, nullable=True)
    media_name = Column(Text, nullable=True)
    media_duration_seconds = Column(BigInteger, nullable=True)
    media_blur_hash = Column(Text, nullable=True)

    # FK-less, same as Message.reply_to_message_id.
    reply_to_message_id = Column(BigInteger, nullable=True)

    # Generated at schedule time, reused as the send idempotency key when the
    # message fires so a worker crash/retry can't double-send.
    client_message_id = Column(Text, nullable=False)

    # ScheduledMessageStatus: 0=pending, 1=sent, 2=cancelled, 3=failed.
    status = Column(SMALLINT, nullable=False, default=0)

    # Set when status=3.
    last_error = Column(Text, nullable=True)

    # Number of times the worker has tried and transiently failed to fire this
    # row; capped by SCHEDULED_MAX_FIRE_ATTEMPTS.
    fire_attempts = Column(SMALLINT, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_scheduled_messages_sender_scheduled", "sender_id", "scheduled_for"),
        Index("ix_scheduled_messages_status_scheduled", "status", "scheduled_for"),
    )

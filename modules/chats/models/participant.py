from sqlalchemy import Column, BigInteger, ForeignKey, DateTime, SMALLINT
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from database.base import Base

class Participant(Base):
    __tablename__ = "participants"

    # Composite Primary Key ensures a user can only join a specific chat once
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    
    # Secondary index on user_id for fast O(log N) lookups of a user's chats
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)

    # 1 for Member, 2 for Admin, 3 for Owner
    role = Column(SMALLINT, nullable=False, default=1)

    # Watermark pattern: the id of the last message this participant has
    # read, not a per-message "read" flag. It exists so an unread badge is a
    # single O(1) comparison - chat.last_message_id != participant.
    # last_read_message_id (see chat_service.get_chat_list) - instead of a
    # COUNT(*)/flag lookup across a chat's full message history, or a
    # written row per (message, participant) pair, which in a 1000-person
    # group would mean up to 1000 writes per message.
    last_read_message_id = Column(BigInteger, nullable=True)

    # Same watermark pattern, one step earlier in the pipeline: the id of
    # the last message that reached this participant's device, whether or
    # not they've actually opened/read it yet (WhatsApp's grey vs. blue
    # double-check). See Chat.all_delivered_up_to_message_id / MessageStatus
    # for how these per-participant cursors roll up into a message's
    # sent/delivered/read status without ever writing to the message itself.
    last_delivered_message_id = Column(BigInteger, nullable=True)

    # Same watermark pattern again, one step *past* read and only meaningful
    # for voice recordings: the id of the last message this participant has
    # actually listened to ("נשמעה"). A client bumps it via the mark_played
    # WebSocket action when a voice note finishes / is scrubbed to the end.
    # Rolls up into Chat.all_played_up_to_message_id (see MessageStatus.PLAYED)
    # so a group's "everyone heard it" is one O(1) comparison, not a row per
    # (recording, listener). NULL until the participant plays their first one.
    last_played_message_id = Column(BigInteger, nullable=True)

    # Coarse "when did this participant last acknowledge anything" timestamps,
    # bumped alongside the *_message_id watermarks above. Unlike
    # message_receipt_log (the detailed, time-partitioned, 30-day history),
    # these are single columns that never expire - so "last read at" still has
    # an answer for a message older than the log's retention window. Not used
    # for the per-message detail view; that reads the log.
    last_delivered_at = Column(DateTime(timezone=True), nullable=True)
    last_read_at = Column(DateTime(timezone=True), nullable=True)
    last_played_at = Column(DateTime(timezone=True), nullable=True)

    # Per-user chat pinning. NULL = not pinned. When set, the chat is
    # sorted above all un-pinned chats in this user's chat list, ordered
    # by pinned_at DESC (most recently pinned first). Lives on Participant
    # so every user pins independently; no limit on how many a user pins.
    pinned_at = Column(DateTime(timezone=True), nullable=True)

    # Per-user chat mute. NULL = not muted. A future timestamp = muted
    # until then; "mute forever" is just a far-future timestamp. The client
    # owns the duration menu (8h / 1d / 1w / forever) and sends the absolute
    # expiry; the server stores it verbatim. Server-side this is consulted
    # in exactly one place - the offline push path in
    # services/messaging/send.py drops recipients whose muted_until > now()
    # before sending an FCM push. Everything else about muting (hiding the
    # unread badge, silencing the in-app notification) is the client's job.
    # See ADR 0004.
    muted_until = Column(DateTime(timezone=True), nullable=True)

    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    # SQLAlchemy ORM relationships
    # Using strings prevents circular import errors
    chat = relationship("Chat", back_populates="participants")
    user = relationship("User")
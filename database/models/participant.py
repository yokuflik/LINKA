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

    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    # SQLAlchemy ORM relationships
    # Using strings prevents circular import errors
    chat = relationship("Chat", back_populates="participants")
    user = relationship("User")
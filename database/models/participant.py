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

    # High-performance unread message counter (Watermark pattern)
    last_read_message_id = Column(BigInteger, nullable=True)

    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    # SQLAlchemy ORM relationships
    # Using strings prevents circular import errors
    chat = relationship("Chat", back_populates="participants")
    user = relationship("User")
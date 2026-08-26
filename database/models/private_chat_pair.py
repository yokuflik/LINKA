from sqlalchemy import Column, BigInteger, ForeignKey

from database.base import Base

class PrivateChatPair(Base):
    __tablename__ = "private_chat_pairs"

    # Both columns are part of the PK, always stored normalized
    # (user_low_id < user_high_id) so a given pair of users can only ever be
    # represented one way. That's what turns "does a private chat between
    # these two users already exist" into a single unique-constrained INSERT
    # instead of a check-then-act race - the same guarantee a single-column
    # PK gives create_user/create_chat, just over two columns.
    user_low_id = Column(BigInteger, primary_key=True)
    user_high_id = Column(BigInteger, primary_key=True)

    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, unique=True)

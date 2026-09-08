from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typing import Optional
import logging

from database.models.private_chat_pair import PrivateChatPair

logger = logging.getLogger(__name__)


def _normalize(user_a_id: int, user_b_id: int) -> tuple[int, int]:
    return (user_a_id, user_b_id) if user_a_id < user_b_id else (user_b_id, user_a_id)


async def get_pair_chat_id(session: AsyncSession, user_a_id: int, user_b_id: int) -> Optional[int]:
    """
    Time Complexity: O(log N)
    Explanation: Direct hit on the (user_low_id, user_high_id) Primary Key.
    """
    user_low_id, user_high_id = _normalize(user_a_id, user_b_id)
    stmt = select(PrivateChatPair.chat_id).where(
        PrivateChatPair.user_low_id == user_low_id, PrivateChatPair.user_high_id == user_high_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_pair(session: AsyncSession, user_a_id: int, user_b_id: int, chat_id: int) -> bool:
    """
    Reserves the (user_a_id, user_b_id) pair for chat_id.

    Returns True if this call won the reservation, False if a concurrent
    call already claimed this pair first (Primary Key violation) - the
    caller should then discard whatever it was about to create and use the
    winner's chat_id instead.
    """
    user_low_id, user_high_id = _normalize(user_a_id, user_b_id)
    session.add(PrivateChatPair(user_low_id=user_low_id, user_high_id=user_high_id, chat_id=chat_id))
    try:
        await session.commit()
        return True
    except IntegrityError as e:
        await session.rollback()
        logger.info(f"Lost the race to pair ({user_a_id}, {user_b_id}) with chat {chat_id}: {e}")
        return False

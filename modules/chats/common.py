"""Shared chat-domain primitives: role constants and the two small helpers
(_require_role, _display_name_for) used across the group-mutation modules."""

from sqlalchemy.ext.asyncio import AsyncSession

from database.models.participant import Participant
from database.models.user import User
from services.chats.errors import PermissionDeniedError

ROLE_MEMBER = 1
ROLE_ADMIN = 2
ROLE_OWNER = 3


async def _require_role(session: AsyncSession, chat_id: int, user_id: int, min_role: int) -> None:
    stmt_participant = await session.get(Participant, {"chat_id": chat_id, "user_id": user_id})
    if stmt_participant is None or stmt_participant.role < min_role:
        raise PermissionDeniedError(f"User {user_id} lacks the required role in chat {chat_id}")


async def _display_name_for(session: AsyncSession, user_id: int) -> str:
    """
    System-message text is plain content, not a structured field a client
    could resolve an id against post-hoc (unlike sender_id on a normal
    message) - so it has to already contain a human-readable name/phone
    number by the time it's written.
    """
    user = await session.get(User, user_id)
    if user is None:
        return str(user_id)
    return user.username or user.phone_number

from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import create_chat, delete_chat, update_chat_details
from database.crud.crud_participant import (
    add_participant_to_chat,
    get_user_chats,
    remove_participant,
    update_participant_role,
)
from database.crud.crud_private_chat_pair import create_pair, get_pair_chat_id
from database.models.chat import Chat
from database.models.participant import Participant
from services import message_service
from utils.snowflake import next_id

ROLE_MEMBER = 1
ROLE_ADMIN = 2
ROLE_OWNER = 3


class PermissionDeniedError(Exception):
    pass


async def get_or_create_private_chat(session: AsyncSession, user_a_id: int, user_b_id: int) -> Chat:
    """
    Private chats must be idempotent: two users should never end up with two
    separate 1-on-1 chats just because they both tapped "message" at once.

    The lookup-then-create below is *not* by itself race-free (two
    concurrent calls can both miss the lookup and both proceed to create).
    What actually closes the race is create_pair()'s unique constraint on
    the user pair - one of the two concurrent create_pair() calls always
    loses, and the loser discards its unused candidate chat and adopts the
    winner's instead.
    """
    existing_chat_id = await get_pair_chat_id(session, user_a_id, user_b_id)
    if existing_chat_id is not None:
        chat = await session.get(Chat, existing_chat_id)
        if chat is not None:
            return chat

    candidate_chat = await create_chat(session, chat_id=next_id(), is_group=False)
    # Captured now, before create_pair(): on a lost race it rolls back,
    # which expires every object in this session - candidate_chat included.
    # Accessing candidate_chat.id afterwards would then trigger an implicit
    # refresh-from-DB outside of a valid async context (MissingGreenlet).
    candidate_chat_id = candidate_chat.id

    won = await create_pair(session, user_a_id, user_b_id, candidate_chat_id)

    if not won:
        await delete_chat(session, candidate_chat_id)
        winning_chat_id = await get_pair_chat_id(session, user_a_id, user_b_id)
        return await session.get(Chat, winning_chat_id)

    await add_participant_to_chat(session, chat_id=candidate_chat_id, user_id=user_a_id, role=ROLE_MEMBER)
    await add_participant_to_chat(session, chat_id=candidate_chat_id, user_id=user_b_id, role=ROLE_MEMBER)
    return candidate_chat


async def create_group_chat(
    session: AsyncSession,
    creator_id: int,
    title: str,
    initial_member_ids: Sequence[int] = (),
    about_text: Optional[str] = None,
) -> Chat:
    chat = await create_chat(session, chat_id=next_id(), is_group=True, title=title, about_text=about_text)
    await add_participant_to_chat(session, chat_id=chat.id, user_id=creator_id, role=ROLE_OWNER)

    for member_id in initial_member_ids:
        if member_id != creator_id:
            await add_participant_to_chat(session, chat_id=chat.id, user_id=member_id, role=ROLE_MEMBER)

    return chat


async def get_chat_list(
    session: AsyncSession,
    user_id: int,
    before=None,
    limit: int = 30,
) -> Sequence[Participant]:
    """
    Home screen. Each returned Participant has its Chat eagerly loaded, so
    callers can also compute an unread count from
    `chat.last_message_id != participant.last_read_message_id` without an
    extra query.
    """
    return await get_user_chats(session, user_id, before=before, limit=limit)


async def _require_role(session: AsyncSession, chat_id: int, user_id: int, min_role: int) -> None:
    stmt_participant = await session.get(Participant, {"chat_id": chat_id, "user_id": user_id})
    if stmt_participant is None or stmt_participant.role < min_role:
        raise PermissionDeniedError(f"User {user_id} lacks the required role in chat {chat_id}")


async def update_group_details(
    session: AsyncSession,
    actor_id: int,
    chat_id: int,
    title: Optional[str] = None,
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None,
) -> Chat:
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    return await update_chat_details(
        session, chat_id=chat_id, title=title, about_text=about_text, profile_pic_url=profile_pic_url
    )


async def add_member(session: AsyncSession, actor_id: int, chat_id: int, new_user_id: int) -> Participant:
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    participant = await add_participant_to_chat(session, chat_id=chat_id, user_id=new_user_id, role=ROLE_MEMBER)

    await message_service.send_system_message(session, chat_id=chat_id, content=f"{new_user_id} joined the group")
    return participant


async def remove_member(session: AsyncSession, actor_id: int, chat_id: int, target_user_id: int) -> bool:
    if actor_id != target_user_id:
        # Removing someone else requires admin/owner; leaving yourself never needs a role check
        await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)

    removed = await remove_participant(session, chat_id=chat_id, user_id=target_user_id)
    if removed:
        verb = "left" if actor_id == target_user_id else "was removed from"
        await message_service.send_system_message(session, chat_id=chat_id, content=f"{target_user_id} {verb} the group")
    return removed


async def change_member_role(session: AsyncSession, actor_id: int, chat_id: int, target_user_id: int, new_role: int) -> Participant:
    """Only an Owner may promote/demote members."""
    await _require_role(session, chat_id, actor_id, min_role=ROLE_OWNER)
    return await update_participant_role(session, chat_id=chat_id, user_id=target_user_id, role=new_role)

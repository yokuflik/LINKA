from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import create_chat, delete_chat, update_chat_details
from database.crud.crud_participant import (
    add_participant_to_chat,
    get_chat_participants_with_users,
    get_user_chats,
    is_participant,
    remove_participant,
    update_participant_role,
)
from database.crud.crud_private_chat_pair import create_pair, get_pair_chat_id
from database.models.chat import Chat
from database.models.participant import Participant
from config import MAX_INITIAL_GROUP_MEMBERS
from services import message_service, realtime_service
from utils.snowflake import next_id

ROLE_MEMBER = 1
ROLE_ADMIN = 2
ROLE_OWNER = 3


class PermissionDeniedError(Exception):
    pass


class TooManyMembersError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


async def _notify_added_to_chat(user_id: int, chat_id: int) -> None:
    """
    Tells connection_manager (via the user's personal channel) to bring this
    user's already-open connections into the new chat's live subscription
    immediately, instead of only at their next reconnect - see
    ConnectionManager._handle_user_channel_event. Also reaches the client
    itself, to refresh its chat list / show a notification.
    """
    await realtime_service.publish_user_event(user_id, {"event": "added_to_chat", "chat_id": str(chat_id)})


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

    # add_participant_to_chat returns None on failure (most commonly: the
    # user_id doesn't actually exist, an FK violation) instead of raising -
    # this used to go unchecked, silently leaving a "private chat" with only
    # one participant in it and no way for the other side to ever see it.
    participant_a = await add_participant_to_chat(session, chat_id=candidate_chat_id, user_id=user_a_id, role=ROLE_MEMBER)
    participant_b = await add_participant_to_chat(session, chat_id=candidate_chat_id, user_id=user_b_id, role=ROLE_MEMBER)
    if participant_a is None or participant_b is None:
        # private_chat_pairs.chat_id cascades, so deleting the chat cleans up
        # the reservation too - nothing is left half-created.
        await delete_chat(session, candidate_chat_id)
        bad_id = user_a_id if participant_a is None else user_b_id
        raise UserNotFoundError(f"User {bad_id} does not exist")

    # Both sides, not just the other user: user_a_id's own connection also
    # never had this brand-new chat_id in its subscription snapshot, even
    # though they're the one who just created it.
    await _notify_added_to_chat(user_a_id, candidate_chat_id)
    await _notify_added_to_chat(user_b_id, candidate_chat_id)

    return candidate_chat


async def create_group_chat(
    session: AsyncSession,
    creator_id: int,
    title: str,
    initial_member_ids: Sequence[int] = (),
    about_text: Optional[str] = None,
) -> Chat:
    # Each member is its own sequential DB round trip below - an unbounded
    # list is an easy way to turn one call into millions of inserts.
    # Importing a huge membership list needs its own batched/background flow.
    if len(initial_member_ids) > MAX_INITIAL_GROUP_MEMBERS:
        raise TooManyMembersError(f"Cannot create a group with more than {MAX_INITIAL_GROUP_MEMBERS} initial members")

    chat = await create_chat(session, chat_id=next_id(), is_group=True, title=title, about_text=about_text)
    # Captured now: an add_participant_to_chat() failure below rolls back
    # (same reason as get_or_create_private_chat's candidate_chat_id above),
    # which expires every object in this session - chat included. Accessing
    # chat.id afterwards for cleanup would then hit the same MissingGreenlet
    # implicit-refresh-outside-async-context error.
    chat_id = chat.id

    owner = await add_participant_to_chat(session, chat_id=chat_id, user_id=creator_id, role=ROLE_OWNER)
    if owner is None:
        await delete_chat(session, chat_id)
        raise UserNotFoundError(f"User {creator_id} does not exist")

    for member_id in initial_member_ids:
        if member_id != creator_id:
            participant = await add_participant_to_chat(session, chat_id=chat_id, user_id=member_id, role=ROLE_MEMBER)
            if participant is None:
                await delete_chat(session, chat_id)
                raise UserNotFoundError(f"User {member_id} does not exist")

    await _notify_added_to_chat(creator_id, chat_id)
    for member_id in initial_member_ids:
        if member_id != creator_id:
            await _notify_added_to_chat(member_id, chat_id)

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


async def get_chat_members(session: AsyncSession, requester_id: int, chat_id: int) -> Sequence[Participant]:
    """
    Every participant of a chat, each with their User eagerly loaded - e.g.
    so a client can render a private chat's title as the other person's
    phone number instead of a raw chat id.
    """
    if not await is_participant(session, chat_id, requester_id):
        raise PermissionDeniedError(f"User {requester_id} is not a participant of chat {chat_id}")
    return await get_chat_participants_with_users(session, chat_id)


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


async def add_member(session: AsyncSession, actor_id: int, chat_id: int, new_user_id: int) -> Optional[Participant]:
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    participant = await add_participant_to_chat(session, chat_id=chat_id, user_id=new_user_id, role=ROLE_MEMBER)
    if participant is None:
        return None

    await message_service.send_system_message(session, chat_id=chat_id, content=f"{new_user_id} joined the group")
    await _notify_added_to_chat(new_user_id, chat_id)
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

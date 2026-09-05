"""Creating chats: the idempotent 1:1 get-or-create and group creation.

`MAX_INITIAL_GROUP_MEMBERS` is read off the `chat_service` facade at call time
(tests monkeypatch `chat_service.MAX_INITIAL_GROUP_MEMBERS`).
"""

from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import create_chat, delete_chat
from database.crud.crud_participant import add_participant_to_chat
from database.crud.crud_private_chat_pair import create_pair, get_pair_chat_id
from database.models.chat import Chat
from services import avatar_service
from services.chats.common import ROLE_MEMBER, ROLE_OWNER
from services.chats.errors import TooManyMembersError, UserNotFoundError
from services.chats.notifications import _notify_added_to_chat
from utils.id_client import next_id


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

    candidate_chat = await create_chat(session, chat_id=await next_id(), is_group=False)
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
    avatar_storage_key: Optional[str] = None,
) -> Chat:
    from services import chat_service

    # Each member is its own sequential DB round trip below - an unbounded
    # list is an easy way to turn one call into millions of inserts.
    # Importing a huge membership list needs its own batched/background flow.
    if len(initial_member_ids) > chat_service.MAX_INITIAL_GROUP_MEMBERS:
        raise TooManyMembersError(
            f"Cannot create a group with more than {chat_service.MAX_INITIAL_GROUP_MEMBERS} initial members"
        )

    chat = await create_chat(session, chat_id=await next_id(), is_group=True, title=title, about_text=about_text)
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

    # Optional group photo: the client uploaded the bytes straight to storage
    # via POST /chats/groups/avatar/upload-ticket and handed us the key.
    # avatar_service validates it (HEAD + avatar limits) before storing, so a
    # bad/forged key raises here rather than silently sticking. No system
    # message - a brand-new group has nobody to notify.
    if avatar_storage_key:
        updated = await avatar_service.set_group_avatar(session, chat_id, avatar_storage_key)
        if updated is not None:
            chat = updated

    await _notify_added_to_chat(creator_id, chat_id)
    for member_id in initial_member_ids:
        if member_id != creator_id:
            await _notify_added_to_chat(member_id, chat_id)

    return chat

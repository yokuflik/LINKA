import json
from datetime import datetime
from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import create_chat, delete_chat, get_chat_by_id, update_chat_details
from database.crud.crud_message import compute_message_status, count_unread_messages
from database.crud.crud_participant import (
    add_participant_to_chat,
    get_chat_participants,
    get_chat_participants_with_users,
    get_user_chats,
    is_participant,
    remove_participant,
    set_chat_muted as _crud_set_chat_muted,
    set_chat_pinned as _crud_set_chat_pinned,
    update_participant_role,
)
from database.crud.crud_private_chat_pair import create_pair, get_pair_chat_id
from database.models.chat import Chat
from database.models.participant import Participant
from database.models.user import User
from config import MAX_INITIAL_GROUP_MEMBERS
from services import avatar_service, message_service, realtime_service
from services.storage.media_service import public_avatar_url
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


class OwnershipTransferRequiredError(Exception):
    """
    Raised when the owner tries to leave a group that still has other
    members without naming who inherits ownership - a group can never be
    left ownerless while people remain in it.
    """
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


async def _notify_removed_from_chat(user_id: int, chat_id: int, actor_id: int, chat_title: Optional[str]) -> None:
    """
    Mirror of _notify_added_to_chat, for the opposite direction: tells
    connection_manager to drop this user's already-open connections from the
    chat's live subscription immediately (they're no longer a participant,
    so they shouldn't keep receiving its events), and reaches the client
    itself so it can drop the chat from its own list right away instead of
    only at the next reconnect/GET /chats. Fired both when someone else
    removes this user and when they leave on their own initiative, so every
    one of their connected devices stays in sync either way - actor_id is
    included so the client can tell the two cases apart (e.g. skip a "you
    were removed" toast on the device that did the leaving itself), and
    chat_title so that toast can name the group instead of just saying "a
    group" (the client's own list entry for it is about to disappear too).
    """
    await realtime_service.publish_user_event(
        user_id,
        {
            "event": "removed_from_chat",
            "chat_id": str(chat_id),
            "actor_id": str(actor_id),
            "chat_title": chat_title,
        },
    )


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
    avatar_storage_key: Optional[str] = None,
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


async def get_chat_list(
    session: AsyncSession,
    user_id: int,
    before=None,
    limit: int = 30,
) -> Sequence[Participant]:
    """
    Home screen. Each returned Participant has its Chat eagerly loaded and an
    `unread_count` attached (see below), so callers don't need a separate
    query per chat to render the WhatsApp-style unread badge.
    """
    participants = await get_user_chats(session, user_id, before=before, limit=limit)

    for participant in participants:
        chat = participant.chat
        # Attached rather than a stored column - same reasoning as
        # MessageStatus. The chat is already loaded, and this is a plain
        # comparison against its own last_message_id/
        # all_delivered_up_to_message_id/all_read_up_to_message_id - no
        # extra query per chat.
        chat.last_message_status = (
            compute_message_status(chat.last_message_id, chat) if chat.last_message_id is not None else None
        )
        # Asymmetric read-receipt privacy (ADR 0003): in a 1:1 chat, mask
        # READ/PLAYED -> DELIVERED when the *other* participant keeps their
        # own read receipts off. Only 1:1 chats pay the settings lookup.
        if chat.last_message_status is not None and not chat.is_group:
            from services.messaging.receipt_privacy import mask_status, read_receipts_hidden_for_message

            if await read_receipts_hidden_for_message(session, chat.id, sender_id=user_id, chat=chat):
                chat.last_message_status = mask_status(chat.last_message_status)
        # Unlike last_message_status (chat-wide), this is genuinely
        # per-viewer - how many messages *this* participant hasn't read yet
        # - so it's attached to the Participant, not the Chat. One indexed
        # COUNT query per chat (see count_unread_messages) - cheap via the
        # (chat_id, id) index, but still a real query per chat in the list.
        participant.unread_count = await count_unread_messages(session, chat.id, participant.last_read_message_id)

    return participants


async def set_chat_pinned(session: AsyncSession, user_id: int, chat_id: int, pinned: bool) -> bool:
    """
    Pin/unpin a chat for the calling user. Any participant may pin any of
    their own chats (no role check); pinning is purely a per-user chat-list
    ordering preference with no cap. Returns False if the user isn't a
    participant of the chat.
    """
    participant = await _crud_set_chat_pinned(session, chat_id=chat_id, user_id=user_id, pinned=pinned)
    if participant is None:
        return False

    # Pinning is per-user, so the only clients that care are this same
    # user's *other* open connections (a second browser tab / device).
    # Push it over their personal channel - connection_manager forwards any
    # user_events payload to every connection of that user - so each one
    # re-sorts its chat list live instead of only on the next GET /chats.
    # The acting connection gets the echo too; re-applying the same flag is
    # idempotent.
    await realtime_service.publish_user_event(
        user_id,
        {"event": "chat_pin_changed", "chat_id": str(chat_id), "pinned": pinned},
    )
    return True


async def set_chat_muted(
    session: AsyncSession, user_id: int, chat_id: int, muted_until: Optional[datetime]
) -> bool:
    """
    Mute/unmute a chat for the calling user. `muted_until` is an absolute
    expiry chosen by the client ("forever" = a far-future timestamp); None
    unmutes. No role check - a personal chat-list preference. Returns False
    if the user isn't a participant.

    Server-side, muting only suppresses offline push (see ADR 0004); the
    client does the rest. The mute state is pushed to the user's *other*
    connections so every device updates live (same pattern as pinning).
    """
    participant = await _crud_set_chat_muted(
        session, chat_id=chat_id, user_id=user_id, muted_until=muted_until
    )
    if participant is None:
        return False

    await realtime_service.publish_user_event(
        user_id,
        {
            "event": "chat_mute_changed",
            "chat_id": str(chat_id),
            "muted_until": muted_until.isoformat() if muted_until is not None else None,
        },
    )
    return True


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

    existing = await get_chat_by_id(session, chat_id)
    if existing is None:
        return None

    chat = await update_chat_details(
        session, chat_id=chat_id, title=title, about_text=about_text, profile_pic_url=profile_pic_url
    )
    if chat is None:
        return None

    # Mirror the other group mutations: a detail change is announced in-chat.
    actor_name = await _display_name_for(session, actor_id)
    if title is not None and title != existing.title:
        await message_service.send_system_message(
            session, chat_id=chat_id, content=f'{actor_name} changed the group name to "{title}"'
        )
    if about_text is not None and about_text != existing.about_text:
        await message_service.send_system_message(
            session, chat_id=chat_id, content=f"{actor_name} changed the group description"
        )
    await _broadcast_chat_update(session, chat_id)
    return chat


async def _broadcast_chat_update(session: AsyncSession, chat_id: int) -> None:
    """
    Tell every member of a group that its title / description / photo just
    changed, so open clients update the name+avatar they show in the sidebar
    and chat header without re-opening the chat or waiting for GET /chats.

    A **transient** chat-scoped event over the normal routing (same path as
    `typing`) - the accompanying system message ("X changed the group name")
    is the persisted record; this is only the live nudge that carries the new
    values so clients don't each have to re-fetch. Best-effort.
    """
    chat = await get_chat_by_id(session, chat_id)
    if chat is None:
        return
    key = chat.profile_pic_url
    resolved_pic = (
        key if (key and key.startswith(("http://", "https://"))) else (public_avatar_url(key) if key else None)
    )
    await realtime_service.publish_event(
        chat_id,
        {
            "event": "chat_updated",
            "title": chat.title,
            "about_text": chat.about_text,
            "profile_pic_url": resolved_pic,
        },
    )


async def ensure_can_manage_details(session: AsyncSession, actor_id: int, chat_id: int) -> None:
    """
    Public guard for endpoints that change group details (e.g. minting a
    group-avatar upload ticket) but don't go through update_group_details /
    set_group_avatar themselves. Raises PermissionDeniedError (-> 403).
    """
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)


async def set_group_avatar(session: AsyncSession, actor_id: int, chat_id: int, storage_key: str) -> Optional[Chat]:
    """
    Set a group's profile picture. Requires ROLE_ADMIN (same as any other
    group-detail change). The object-storage validation + old-object cleanup
    lives in avatar_service; this layer only owns the authorization and the
    "X changed the group photo" system message.
    """
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    chat = await avatar_service.set_group_avatar(session, chat_id, storage_key)
    if chat is None:
        return None
    actor_name = await _display_name_for(session, actor_id)
    await message_service.send_system_message(
        session, chat_id=chat_id, content=f"{actor_name} changed the group photo"
    )
    await _broadcast_chat_update(session, chat_id)
    return chat


async def clear_group_avatar(session: AsyncSession, actor_id: int, chat_id: int) -> Optional[Chat]:
    """Remove a group's profile picture. Requires ROLE_ADMIN."""
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    chat = await avatar_service.clear_group_avatar(session, chat_id)
    if chat is None:
        return None
    actor_name = await _display_name_for(session, actor_id)
    await message_service.send_system_message(
        session, chat_id=chat_id, content=f"{actor_name} removed the group photo"
    )
    await _broadcast_chat_update(session, chat_id)
    return chat


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
    return user.display_name or user.phone_number


async def add_member(session: AsyncSession, actor_id: int, chat_id: int, new_user_id: int) -> Optional[Participant]:
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    participant = await add_participant_to_chat(session, chat_id=chat_id, user_id=new_user_id, role=ROLE_MEMBER)
    if participant is None:
        return None

    actor_name = await _display_name_for(session, actor_id)
    new_member_name = await _display_name_for(session, new_user_id)
    await message_service.send_system_message(
        session, chat_id=chat_id, content=f"{actor_name} added {new_member_name} to the group"
    )
    await _notify_added_to_chat(new_user_id, chat_id)
    return participant


async def remove_member(
    session: AsyncSession,
    actor_id: int,
    chat_id: int,
    target_user_id: int,
    new_owner_id: Optional[int] = None,
) -> bool:
    chat = await session.get(Chat, chat_id)
    chat_title = chat.title if chat is not None else None

    if actor_id != target_user_id:
        # Removing someone else requires admin/owner; leaving yourself never needs a role check
        await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)

        # An admin may only remove a plain member - not another admin, and not
        # the owner. Only the owner outranks an admin and can remove one.
        actor = await session.get(Participant, {"chat_id": chat_id, "user_id": actor_id})
        target = await session.get(Participant, {"chat_id": chat_id, "user_id": target_user_id})
        if target is not None and target.role >= actor.role:
            raise PermissionDeniedError(
                f"User {actor_id} cannot remove user {target_user_id}: insufficient role"
            )
    else:
        # Self-leave. A group can never be left ownerless while other people
        # remain in it - the owner must name a successor first. If nobody
        # else is left, there's nothing to transfer, and the whole chat is
        # deleted below once this last participant is removed.
        actor = await session.get(Participant, {"chat_id": chat_id, "user_id": actor_id})
        if actor is not None and actor.role == ROLE_OWNER:
            other_participants = [p for p in await get_chat_participants(session, chat_id) if p.user_id != actor_id]
            if other_participants:
                if new_owner_id is None:
                    raise OwnershipTransferRequiredError(
                        f"Owner {actor_id} must name a new owner before leaving chat {chat_id}"
                    )
                if not any(p.user_id == new_owner_id for p in other_participants):
                    raise UserNotFoundError(f"User {new_owner_id} is not a member of chat {chat_id}")

                await update_participant_role(session, chat_id=chat_id, user_id=new_owner_id, role=ROLE_OWNER)
                actor_name = await _display_name_for(session, actor_id)
                new_owner_name = await _display_name_for(session, new_owner_id)
                await message_service.send_system_message(
                    session, chat_id=chat_id, content=f"{actor_name} made {new_owner_name} the group owner"
                )

    removed = await remove_participant(session, chat_id=chat_id, user_id=target_user_id)
    if removed:
        remaining = await get_chat_participants(session, chat_id)
        if remaining:
            verb = "left" if actor_id == target_user_id else "was removed from"
            name = await _display_name_for(session, target_user_id)
            await message_service.send_system_message(session, chat_id=chat_id, content=f"{name} {verb} the group")
        else:
            # Nobody left in the chat at all (last member left, or the owner
            # left with no one to hand it to) - no point sending a system
            # message nobody will ever read, just delete the chat outright.
            await delete_chat(session, chat_id)
        await _notify_removed_from_chat(target_user_id, chat_id, actor_id, chat_title)
    return removed


async def change_member_role(session: AsyncSession, actor_id: int, chat_id: int, target_user_id: int, new_role: int) -> Participant:
    """Only an Owner may promote/demote members."""
    await _require_role(session, chat_id, actor_id, min_role=ROLE_OWNER)
    participant = await update_participant_role(session, chat_id=chat_id, user_id=target_user_id, role=new_role)

    # Unlike "X joined/left the group", a role change is only meant to be
    # seen by the two people involved, not the whole chat - there's no
    # per-recipient system message, so this is fanned out to everyone (same
    # as any other system message) but as structured JSON content instead of
    # plain text. The client parses the "role_changed" kind and renders it
    # only when actor_id/target_id matches the viewer, building the
    # human-readable text itself (it already has name/phone resolution) -
    # this avoids a new Message column for something only two people ever see.
    await message_service.send_system_message(
        session,
        chat_id=chat_id,
        content=json.dumps({
            "kind": "role_changed",
            "actor_id": str(actor_id),
            "target_id": str(target_user_id),
            "new_role": new_role,
        }),
    )
    return participant

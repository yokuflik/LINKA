"""Adding, removing and re-roling group members - plus the ownerless-leave
guard and the last-member-out chat deletion."""

import json
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import delete_chat
from database.crud.crud_participant import (
    add_participant_to_chat,
    get_chat_participants,
    remove_participant,
    update_participant_role,
)
from database.models.chat import Chat
from database.models.participant import Participant
from services import message_service
from services.chats.common import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, _display_name_for, _require_role
from services.chats.errors import (
    OwnershipTransferRequiredError,
    PermissionDeniedError,
    UserNotFoundError,
)
from services.chats.notifications import _notify_added_to_chat, _notify_removed_from_chat


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

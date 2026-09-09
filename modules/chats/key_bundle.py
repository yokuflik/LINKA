"""Chat key-bundle: every participant's E2E public key in one call (ADR 0026).

The group send path needs all recipients' public keys to wrap the per-message
key for each of them. Exposed as GET /chats/{chat_id}/key-bundle; the requester
must be a participant.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_participant import get_chat_participants
from modules.chats.crud.crud_participant import is_participant
from modules.chats.errors import PermissionDeniedError
from modules.users import service as user_service


async def get_chat_key_bundle(session: AsyncSession, requester_id: int, chat_id: int):
    """Return the ``UserPublicKey`` rows for every member of ``chat_id``.
    Members with no published key are simply absent from the list."""
    if not await is_participant(session, chat_id, requester_id):
        raise PermissionDeniedError(f"User {requester_id} is not a participant of chat {chat_id}")

    participants = await get_chat_participants(session, chat_id)
    user_ids = [p.user_id for p in participants]
    return await user_service.get_public_keys(session, user_ids)

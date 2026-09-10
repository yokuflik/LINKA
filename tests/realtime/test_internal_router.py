"""
Coverage for `realtime/internal_router.py` — the endpoints the Rust ws_gateway
calls (ADR 0036). These carry the presence/typing authorisation rules that used
to live in the (now removed) Python `/ws` handler.
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from modules.auth import service as auth_service
from modules.chats import service as chat_service
from modules.messaging import service as message_service
from modules.settings import service as settings_service
from modules.users.crud import create_user
from realtime import internal_router

pytestmark = pytest.mark.asyncio


async def _make_group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for member_id in member_ids:
        await create_user(session, user_id=member_id, phone_number=f"+97250{member_id}")
    group = await chat_service.create_group_chat(
        session, creator_id=owner_id, title="Test", initial_member_ids=list(member_ids)
    )
    return group.id


# --- ws-bootstrap ---------------------------------------------------------

async def test_ws_bootstrap_returns_the_users_chats(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    token = auth_service._create_access_token(user_id=1)

    out = await internal_router.ws_bootstrap(token=token)

    assert out["user_id"] == "1"
    assert str(chat_id) in out["chat_ids"]


async def test_ws_bootstrap_rejects_a_bad_token(redis_db):
    with pytest.raises(HTTPException) as exc:
        await internal_router.ws_bootstrap(token="not-a-real-token")
    assert exc.value.status_code == 401


# --- presence-authorized -------------------------------------------------

async def _two_users(session: AsyncSession):
    await create_user(session, user_id=1, phone_number="+972501")
    await create_user(session, user_id=2, phone_number="+972502")


async def test_presence_authorized_everyone_by_default(db_session: AsyncSession, redis_db):
    await _two_users(db_session)
    out = await internal_router.presence_authorized_check(watcher_id=1, target_user_id=2)
    assert out == {"authorized": True}


async def test_presence_authorized_contacts_needs_a_private_chat(db_session: AsyncSession, redis_db):
    await _two_users(db_session)
    await settings_service.update_user_settings(db_session, 2, {"privacy": {"online": "contacts"}})

    assert (await internal_router.presence_authorized_check(watcher_id=1, target_user_id=2))["authorized"] is False

    await chat_service.get_or_create_private_chat(db_session, 1, 2)
    assert (await internal_router.presence_authorized_check(watcher_id=1, target_user_id=2))["authorized"] is True


async def test_presence_authorized_nobody(db_session: AsyncSession, redis_db):
    await _two_users(db_session)
    await chat_service.get_or_create_private_chat(db_session, 1, 2)
    await settings_service.update_user_settings(db_session, 2, {"privacy": {"online": "nobody"}})
    assert (await internal_router.presence_authorized_check(watcher_id=1, target_user_id=2))["authorized"] is False


# --- typing-allowed ----------------------------------------------------

async def test_typing_allowed_for_a_participant(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    assert (await internal_router.typing_allowed(chat_id=chat_id, sender_id=1))["allowed"] is True


async def test_typing_not_allowed_for_a_non_participant(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await create_user(db_session, user_id=3, phone_number="+972503")
    assert (await internal_router.typing_allowed(chat_id=chat_id, sender_id=3))["allowed"] is False


async def test_typing_suppressed_1to1_when_sender_hides_online(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])  # 2 participants -> 1:1
    await settings_service.update_user_settings(db_session, 1, {"privacy": {"online": "nobody"}})
    assert (await internal_router.typing_allowed(chat_id=chat_id, sender_id=1))["allowed"] is False


async def test_typing_allowed_in_group_regardless_of_privacy(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2, 3])  # group -> never gated
    await settings_service.update_user_settings(db_session, 1, {"privacy": {"online": "nobody"}})
    assert (await internal_router.typing_allowed(chat_id=chat_id, sender_id=1))["allowed"] is True


# --- message/* mutations (ADR 0038) -----------------------------------

async def _send(session, sender_id, chat_id, content="hi") -> int:
    msg = await message_service.process_outgoing(
        session, sender_id=sender_id, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content=content
    )
    return msg.id


async def test_message_edit_by_the_sender(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    mid = await _send(db_session, 1, chat_id)
    out = await internal_router.message_edit(
        internal_router._EditOp(user_id=1, chat_id=chat_id, message_id=mid, content="edited")
    )
    assert out["message_id"] == str(mid)


async def test_message_edit_by_a_non_sender_is_403(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    mid = await _send(db_session, 1, chat_id)
    with pytest.raises(HTTPException) as exc:
        await internal_router.message_edit(
            internal_router._EditOp(user_id=2, chat_id=chat_id, message_id=mid, content="nope")
        )
    assert exc.value.status_code == 403


async def test_message_delete_then_restore_then_purge(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    mid = await _send(db_session, 1, chat_id)
    op = internal_router._MessageOp(user_id=1, chat_id=chat_id, message_id=mid)

    assert (await internal_router.message_delete(op))["deleted"] is True
    assert (await internal_router.message_restore(op))["restored"] is True
    assert (await internal_router.message_delete(op))["deleted"] is True
    assert (await internal_router.message_purge(op))["purged"] is True

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from services import chat_service

pytestmark = pytest.mark.asyncio


async def _make_users(session: AsyncSession, *user_ids: int) -> None:
    for user_id in user_ids:
        await create_user(session, user_id=user_id, phone_number=f"+97250{user_id}")


async def test_get_or_create_private_chat_creates_one(db_session: AsyncSession):
    await _make_users(db_session, 1, 2)

    chat = await chat_service.get_or_create_private_chat(db_session, 1, 2)

    assert chat.is_group is False


async def test_get_or_create_private_chat_is_idempotent_regardless_of_argument_order(db_session: AsyncSession):
    await _make_users(db_session, 1, 2)

    chat1 = await chat_service.get_or_create_private_chat(db_session, 1, 2)
    chat2 = await chat_service.get_or_create_private_chat(db_session, 2, 1)

    assert chat1.id == chat2.id


async def test_get_or_create_private_chat_does_not_reuse_a_group_chat(db_session: AsyncSession):
    await _make_users(db_session, 1, 2)
    await chat_service.create_group_chat(db_session, creator_id=1, title="Not private", initial_member_ids=[2])

    private_chat = await chat_service.get_or_create_private_chat(db_session, 1, 2)

    assert private_chat.is_group is False


async def test_concurrent_private_chat_creation_between_the_same_two_users(session_factory):
    # Two devices/tabs both opening a DM with the same person at once must
    # never leave the two users with two separate private chats.
    async with session_factory() as setup_session:
        await _make_users(setup_session, 1, 2)

    async def attempt():
        async with session_factory() as session:
            return await chat_service.get_or_create_private_chat(session, 1, 2)

    chats = await asyncio.gather(attempt(), attempt())
    assert chats[0].id == chats[1].id


async def test_create_group_chat_makes_creator_the_owner(db_session: AsyncSession):
    await _make_users(db_session, 1, 2, 3)

    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2, 3])
    chats = await chat_service.get_chat_list(db_session, user_id=1)

    owner_participant = next(p for p in chats if p.chat_id == group.id)
    assert owner_participant.role == chat_service.ROLE_OWNER


async def test_create_group_chat_ignores_creator_in_initial_members(db_session: AsyncSession):
    # Passing the creator's own id in initial_member_ids (e.g. a sloppy
    # client) must not create a duplicate participant row / crash.
    await _make_users(db_session, 1, 2)

    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[1, 2])

    chats = await chat_service.get_chat_list(db_session, user_id=1, limit=10)
    assert len([p for p in chats if p.chat_id == group.id]) == 1


async def test_admin_can_add_a_member(db_session: AsyncSession):
    await _make_users(db_session, 1, 2, 3)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team")

    participant = await chat_service.add_member(db_session, actor_id=1, chat_id=group.id, new_user_id=2)

    assert participant.user_id == 2
    assert participant.role == chat_service.ROLE_MEMBER


async def test_add_member_generates_a_system_message(db_session: AsyncSession):
    from database.crud.crud_message import get_chat_messages

    await _make_users(db_session, 1, 2)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team")

    await chat_service.add_member(db_session, actor_id=1, chat_id=group.id, new_user_id=2)

    messages = await get_chat_messages(db_session, chat_id=group.id)
    assert any("joined" in (m.content or "") for m in messages)


async def test_plain_member_cannot_add_others(db_session: AsyncSession):
    await _make_users(db_session, 1, 2, 3)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2])

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.add_member(db_session, actor_id=2, chat_id=group.id, new_user_id=3)


async def test_non_participant_cannot_add_members(db_session: AsyncSession):
    await _make_users(db_session, 1, 2, 3)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team")

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.add_member(db_session, actor_id=3, chat_id=group.id, new_user_id=2)


async def test_user_can_leave_without_a_role_check(db_session: AsyncSession):
    await _make_users(db_session, 1, 2)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2])

    left = await chat_service.remove_member(db_session, actor_id=2, chat_id=group.id, target_user_id=2)
    assert left is True


async def test_plain_member_cannot_remove_someone_else(db_session: AsyncSession):
    await _make_users(db_session, 1, 2, 3)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2, 3])

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.remove_member(db_session, actor_id=2, chat_id=group.id, target_user_id=3)


async def test_admin_can_remove_someone_else(db_session: AsyncSession):
    await _make_users(db_session, 1, 2)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2])

    removed = await chat_service.remove_member(db_session, actor_id=1, chat_id=group.id, target_user_id=2)
    assert removed is True


async def test_only_owner_can_change_roles(db_session: AsyncSession):
    await _make_users(db_session, 1, 2, 3)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2, 3])

    # Promote 2 to admin (owner acting) - should succeed
    promoted = await chat_service.change_member_role(db_session, actor_id=1, chat_id=group.id, target_user_id=2, new_role=chat_service.ROLE_ADMIN)
    assert promoted.role == chat_service.ROLE_ADMIN

    # Now 2 (an admin, not owner) tries to promote 3 - must fail
    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.change_member_role(db_session, actor_id=2, chat_id=group.id, target_user_id=3, new_role=chat_service.ROLE_ADMIN)


async def test_only_admin_or_owner_can_update_group_details(db_session: AsyncSession):
    await _make_users(db_session, 1, 2)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2])

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.update_group_details(db_session, actor_id=2, chat_id=group.id, title="Hijacked")

    updated = await chat_service.update_group_details(db_session, actor_id=1, chat_id=group.id, title="Renamed")
    assert updated.title == "Renamed"


async def test_permission_check_on_a_nonexistent_chat_is_denied_not_crashed(db_session: AsyncSession):
    await _make_users(db_session, 1)

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.add_member(db_session, actor_id=1, chat_id=999999, new_user_id=1)


async def test_creating_many_groups_for_one_user_stays_paginated(db_session: AsyncSession):
    # Scale check: a power user in hundreds of groups must still only get
    # `limit` rows back per get_chat_list call, never the whole set.
    await _make_users(db_session, 1)
    for _ in range(150):
        await chat_service.create_group_chat(db_session, creator_id=1, title="Group")

    page = await chat_service.get_chat_list(db_session, user_id=1, limit=30)
    assert len(page) == 30


async def test_concurrent_add_member_calls_do_not_duplicate_or_crash(session_factory):
    # Two admins tapping "add member" for the same target at the same
    # instant - the underlying composite PK still guarantees only one
    # participant row, this just checks the service layer surfaces that as
    # a clean result rather than an unhandled exception.
    async with session_factory() as setup_session:
        await _make_users(setup_session, 1, 2, 3)
        group = await chat_service.create_group_chat(setup_session, creator_id=1, title="Team")
        group_id = group.id

    async def attempt():
        async with session_factory() as session:
            return await chat_service.add_member(session, actor_id=1, chat_id=group_id, new_user_id=2)

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    unexpected = [r for r in results if isinstance(r, Exception) and not isinstance(r, chat_service.PermissionDeniedError)]
    assert unexpected == [], f"unexpected crash(es): {unexpected}"

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


async def test_add_member_for_a_nonexistent_user_returns_none_without_a_system_message(db_session: AsyncSession):
    # Regression: this used to skip the None-check entirely, so a bad
    # new_user_id (FK violation, silently swallowed by add_participant_to_chat)
    # still produced a "X joined the group" system message for a join that
    # never actually happened.
    from database.crud.crud_message import get_chat_messages

    await _make_users(db_session, 1)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team")
    # Captured now: add_member's internal IntegrityError rollback (from the
    # bad new_user_id) expires every object in this session, group included.
    group_id = group.id

    result = await chat_service.add_member(db_session, actor_id=1, chat_id=group_id, new_user_id=999999)
    assert result is None

    messages = await get_chat_messages(db_session, chat_id=group_id)
    assert messages == []


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


async def test_create_group_chat_rejects_more_members_than_the_cap(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(chat_service, "MAX_INITIAL_GROUP_MEMBERS", 3)
    await _make_users(db_session, 1, 2, 3, 4, 5)

    with pytest.raises(chat_service.TooManyMembersError):
        await chat_service.create_group_chat(db_session, creator_id=1, title="Huge", initial_member_ids=[2, 3, 4, 5])


async def test_create_group_chat_allows_exactly_the_cap(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(chat_service, "MAX_INITIAL_GROUP_MEMBERS", 3)
    await _make_users(db_session, 1, 2, 3, 4)

    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Just fits", initial_member_ids=[2, 3, 4])
    assert group is not None


# ---------------------------------------------------------------------------
# Regression: a nonexistent participant used to fail silently (a swallowed FK
# violation), leaving a "chat" only some of its intended members could ever
# see - this is exactly the bug a corrupted client-side id (JS JSON-number
# precision loss - see routers/schemas.py's IdStr) actually triggered.
# get_or_create_private_chat/create_group_chat must now fail loudly and
# leave nothing behind instead.
# ---------------------------------------------------------------------------

async def test_private_chat_with_a_nonexistent_user_raises_and_leaves_nothing_behind(db_session: AsyncSession):
    await _make_users(db_session, 1)

    with pytest.raises(chat_service.UserNotFoundError):
        await chat_service.get_or_create_private_chat(db_session, 1, 999999)

    # No orphaned chat, and no dangling pair reservation, should remain
    chats = await chat_service.get_chat_list(db_session, user_id=1)
    assert chats == []


async def test_group_chat_with_a_nonexistent_member_raises_and_leaves_nothing_behind(db_session: AsyncSession):
    await _make_users(db_session, 1)

    with pytest.raises(chat_service.UserNotFoundError):
        await chat_service.create_group_chat(db_session, creator_id=1, title="Group", initial_member_ids=[999999])

    chats = await chat_service.get_chat_list(db_session, user_id=1)
    assert chats == []


# ---------------------------------------------------------------------------
# "added_to_chat" notifications - what makes a chat created after a user's
# WebSocket already connected still reach them live (see connection_manager's
# personal-channel handling). Verified here by actually subscribing to the
# real Redis channel, the same way a live connection_manager listener would.
# ---------------------------------------------------------------------------

async def _collect_one_user_event(user_id: int, timeout: float = 2.0):
    from services import realtime_service
    agen = realtime_service.subscribe_to_user(user_id)
    task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)  # let the subscribe() actually land before the caller publishes
        return agen, task
    except Exception:
        await agen.aclose()
        raise


async def test_private_chat_creation_notifies_both_sides(db_session: AsyncSession, redis_db):
    await _make_users(db_session, 1, 2)

    agen_a, task_a = await _collect_one_user_event(1)
    agen_b, task_b = await _collect_one_user_event(2)
    try:
        chat = await chat_service.get_or_create_private_chat(db_session, 1, 2)

        event_a = await asyncio.wait_for(task_a, timeout=2.0)
        event_b = await asyncio.wait_for(task_b, timeout=2.0)
    finally:
        await agen_a.aclose()
        await agen_b.aclose()

    assert event_a == {"event": "added_to_chat", "chat_id": str(chat.id)}
    assert event_b == {"event": "added_to_chat", "chat_id": str(chat.id)}


async def test_private_chat_reuse_does_not_renotify(db_session: AsyncSession, redis_db):
    await _make_users(db_session, 1, 2)
    await chat_service.get_or_create_private_chat(db_session, 1, 2)

    agen, task = await _collect_one_user_event(1)
    try:
        # Second call hits the existing-chat early return - no new notification expected
        await chat_service.get_or_create_private_chat(db_session, 1, 2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0.5)
    finally:
        task.cancel()
        await agen.aclose()


async def test_group_chat_creation_notifies_creator_and_every_member(db_session: AsyncSession, redis_db):
    await _make_users(db_session, 1, 2, 3)

    agen_2, task_2 = await _collect_one_user_event(2)
    agen_3, task_3 = await _collect_one_user_event(3)
    try:
        group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2, 3])
        event_2 = await asyncio.wait_for(task_2, timeout=2.0)
        event_3 = await asyncio.wait_for(task_3, timeout=2.0)
    finally:
        await agen_2.aclose()
        await agen_3.aclose()

    assert event_2 == {"event": "added_to_chat", "chat_id": str(group.id)}
    assert event_3 == {"event": "added_to_chat", "chat_id": str(group.id)}


async def test_add_member_notifies_the_new_member(db_session: AsyncSession, redis_db):
    await _make_users(db_session, 1, 2)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team")

    agen, task = await _collect_one_user_event(2)
    try:
        await chat_service.add_member(db_session, actor_id=1, chat_id=group.id, new_user_id=2)
        event = await asyncio.wait_for(task, timeout=2.0)
    finally:
        await agen.aclose()

    assert event == {"event": "added_to_chat", "chat_id": str(group.id)}

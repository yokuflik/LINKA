import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from database.models.user import User
from services import chat_service, user_service

pytestmark = pytest.mark.asyncio


async def test_get_profile_returns_none_for_a_nonexistent_user(db_session: AsyncSession):
    assert await user_service.get_profile(db_session, 999999) is None


async def test_get_profile_returns_the_user(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501")

    profile = await user_service.get_profile(db_session, 1)
    assert profile.phone_number == "+972501"


async def test_update_profile_only_touches_provided_fields(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501", username="old_handle")

    updated = await user_service.update_profile(db_session, user_id=1, about_text="new bio")

    assert updated.username == "old_handle"  # untouched
    assert updated.about_text == "new bio"


async def test_update_profile_for_a_nonexistent_user_returns_none(db_session: AsyncSession):
    assert await user_service.update_profile(db_session, user_id=999999, about_text="X") is None


async def test_broadcast_profile_update_fans_one_event_per_shared_chat(db_session: AsyncSession, monkeypatch):
    # A profile edit must reach everyone who shares a chat with the user, as a
    # transient per-chat event (never a persisted system message).
    await create_user(db_session, user_id=1, phone_number="+972501", username="alice")
    await create_user(db_session, user_id=2, phone_number="+972502")
    await create_user(db_session, user_id=3, phone_number="+972503")
    private = await chat_service.get_or_create_private_chat(db_session, 1, 2)
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[3])

    captured = []

    async def capture(cid, ev):
        captured.append((cid, ev))

    monkeypatch.setattr(user_service.realtime_service, "publish_event", capture)

    await user_service.broadcast_profile_update(db_session, 1)

    assert {cid for cid, _ in captured} == {private.id, group.id}
    for _, ev in captured:
        assert ev["event"] == "profile_updated"
        assert ev["user_id"] == str(1)
        assert ev["username"] == "alice"


async def test_broadcast_profile_update_for_a_nonexistent_user_is_a_noop(db_session: AsyncSession, monkeypatch):
    captured = []

    async def capture(cid, ev):
        captured.append((cid, ev))

    monkeypatch.setattr(user_service.realtime_service, "publish_event", capture)
    await user_service.broadcast_profile_update(db_session, 999999)
    assert captured == []


# --- Usernames (ADR 0017 steps 2, 4, 5) ---


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("ab", "too_short"),
        ("a" * 33, "too_long"),
        ("1abc", "must_start_letter"),
        ("bad-hyphen", "bad_chars"),
        ("admin", "reserved"),
    ],
)
async def test_validate_username_format_reason_codes(raw, reason):
    with pytest.raises(user_service.UsernameError) as exc:
        user_service.validate_username_format(raw)
    assert exc.value.reason == reason


async def test_validate_username_format_normalises():
    assert user_service.validate_username_format("  CoolName  ") == "coolname"


async def test_generate_free_username_is_valid_and_free(db_session: AsyncSession):
    handle = await user_service.generate_free_username(db_session)
    assert user_service.validate_username_format(handle) == handle
    assert await user_service.check_username_available(db_session, 999999, handle) == {
        "available": True,
        "reason": None,
    }


async def test_set_username_changes_and_reserves_old_handle(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501", username="alpha_one_123")
    await create_user(db_session, user_id=2, phone_number="+972502", username="beta_two_456")

    updated = await user_service.set_username(db_session, 1, "gamma_new_789")
    assert updated.username == "gamma_new_789"
    assert updated.username_changed_at is not None

    # The released handle sits in a grace hold - user 2 cannot take it.
    with pytest.raises(user_service.UsernameError) as exc:
        await user_service.set_username(db_session, 2, "alpha_one_123")
    assert exc.value.reason == "grace_hold"
    assert exc.value.http_status == 409


async def test_set_username_rejects_a_taken_handle(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501", username="alpha_one_123")
    await create_user(db_session, user_id=2, phone_number="+972502", username="beta_two_456")
    with pytest.raises(user_service.UsernameError) as exc:
        await user_service.set_username(db_session, 2, "ALPHA_one_123")
    assert exc.value.reason == "taken"


async def test_set_username_enforces_the_change_cooldown(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501", username="alpha_one_123")
    await db_session.execute(
        update(User)
        .where(User.id == 1)
        .values(username_changed_at=datetime.now(timezone.utc) - timedelta(days=1))
    )
    await db_session.commit()

    with pytest.raises(user_service.UsernameError) as exc:
        await user_service.set_username(db_session, 1, "delta_late_222")
    assert exc.value.reason == "cooldown"
    assert exc.value.http_status == 409

    check = await user_service.check_username_available(db_session, 1, "delta_late_222")
    assert check == {"available": False, "reason": "cooldown"}


async def test_set_username_first_user_change_is_free(db_session: AsyncSession):
    # Auto-assigned handle -> username_changed_at is NULL -> first change allowed.
    await create_user(db_session, user_id=1, phone_number="+972501", username="alpha_one_123")
    updated = await user_service.set_username(db_session, 1, "epsilon_ok_333")
    assert updated.username == "epsilon_ok_333"


async def test_concurrent_profile_updates_to_different_fields_do_not_clobber_each_other(session_factory):
    # Two devices for the same user saving different profile fields at once
    # (e.g. one screen edits the name, another edits the bio) - since each
    # is a targeted UPDATE ... SET <only the given columns>, neither should
    # be able to silently overwrite the other's field back to NULL.
    async with session_factory() as setup:
        await create_user(setup, user_id=1, phone_number="+972501")

    async def update_pic():
        async with session_factory() as session:
            await user_service.update_profile(session, user_id=1, profile_pic_url="k1")

    async def update_bio():
        async with session_factory() as session:
            await user_service.update_profile(session, user_id=1, about_text="New Bio")

    await asyncio.gather(update_pic(), update_bio())

    async with session_factory() as session:
        final = await user_service.get_profile(session, 1)

    assert final.profile_pic_url == "k1"
    assert final.about_text == "New Bio"

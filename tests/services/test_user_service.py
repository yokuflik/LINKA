import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from services import user_service

pytestmark = pytest.mark.asyncio


async def test_get_profile_returns_none_for_a_nonexistent_user(db_session: AsyncSession):
    assert await user_service.get_profile(db_session, 999999) is None


async def test_get_profile_returns_the_user(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501")

    profile = await user_service.get_profile(db_session, 1)
    assert profile.phone_number == "+972501"


async def test_update_profile_only_touches_provided_fields(db_session: AsyncSession):
    await create_user(db_session, user_id=1, phone_number="+972501", display_name="Old Name")

    updated = await user_service.update_profile(db_session, user_id=1, about_text="new bio")

    assert updated.display_name == "Old Name"  # untouched
    assert updated.about_text == "new bio"


async def test_update_profile_for_a_nonexistent_user_returns_none(db_session: AsyncSession):
    assert await user_service.update_profile(db_session, user_id=999999, display_name="X") is None


async def test_concurrent_profile_updates_to_different_fields_do_not_clobber_each_other(session_factory):
    # Two devices for the same user saving different profile fields at once
    # (e.g. one screen edits the name, another edits the bio) - since each
    # is a targeted UPDATE ... SET <only the given columns>, neither should
    # be able to silently overwrite the other's field back to NULL.
    async with session_factory() as setup:
        await create_user(setup, user_id=1, phone_number="+972501")

    async def update_name():
        async with session_factory() as session:
            await user_service.update_profile(session, user_id=1, display_name="New Name")

    async def update_bio():
        async with session_factory() as session:
            await user_service.update_profile(session, user_id=1, about_text="New Bio")

    await asyncio.gather(update_name(), update_bio())

    async with session_factory() as session:
        final = await user_service.get_profile(session, 1)

    assert final.display_name == "New Name"
    assert final.about_text == "New Bio"

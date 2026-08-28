import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from services.settings import service as settings_service
from services.settings.errors import SettingsValidationError

pytestmark = pytest.mark.asyncio


async def _user(session: AsyncSession, uid: int = 1) -> int:
    await create_user(session, user_id=uid, phone_number=f"+97250{uid}")
    return uid


async def test_defaults_for_a_user_with_no_stored_settings(db_session: AsyncSession):
    uid = await _user(db_session)
    settings = await settings_service.get_user_settings(db_session, uid)
    assert settings == {"privacy": {"last_seen": "everyone", "online": "everyone"}}


async def test_partial_patch_merges_over_defaults_and_persists(db_session: AsyncSession):
    uid = await _user(db_session)

    merged = await settings_service.update_user_settings(
        db_session, uid, {"privacy": {"online": "nobody"}}
    )
    assert merged == {"privacy": {"last_seen": "everyone", "online": "nobody"}}

    # A second read (fresh) sees the persisted value, last_seen untouched.
    again = await settings_service.get_user_settings(db_session, uid)
    assert again == {"privacy": {"last_seen": "everyone", "online": "nobody"}}


async def test_successive_patches_do_not_clobber_earlier_keys(db_session: AsyncSession):
    uid = await _user(db_session)
    await settings_service.update_user_settings(db_session, uid, {"privacy": {"online": "contacts"}})
    await settings_service.update_user_settings(db_session, uid, {"privacy": {"last_seen": "nobody"}})

    settings = await settings_service.get_user_settings(db_session, uid)
    assert settings["privacy"] == {"last_seen": "nobody", "online": "contacts"}


async def test_unknown_key_is_rejected(db_session: AsyncSession):
    uid = await _user(db_session)
    with pytest.raises(SettingsValidationError):
        await settings_service.update_user_settings(db_session, uid, {"privacy": {"bogus": "x"}})
    with pytest.raises(SettingsValidationError):
        await settings_service.update_user_settings(db_session, uid, {"nonsense": {}})


async def test_bad_enum_value_is_rejected(db_session: AsyncSession):
    uid = await _user(db_session)
    with pytest.raises(SettingsValidationError):
        await settings_service.update_user_settings(
            db_session, uid, {"privacy": {"online": "sometimes"}}
        )


async def test_rejected_patch_does_not_persist_anything(db_session: AsyncSession):
    uid = await _user(db_session)
    with pytest.raises(SettingsValidationError):
        await settings_service.update_user_settings(db_session, uid, {"privacy": {"online": "bad"}})
    assert await settings_service.get_user_settings(db_session, uid) == {
        "privacy": {"last_seen": "everyone", "online": "everyone"}
    }


async def test_get_online_visibility_shortcut(db_session: AsyncSession):
    uid = await _user(db_session)
    assert await settings_service.get_online_visibility(db_session, uid) == "everyone"
    await settings_service.update_user_settings(db_session, uid, {"privacy": {"online": "nobody"}})
    assert await settings_service.get_online_visibility(db_session, uid) == "nobody"

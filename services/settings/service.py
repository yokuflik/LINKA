"""
Service layer for per-user settings.

Callers see only fully-resolved settings dicts (every key present, defaults
filled in). Storage stays sparse. See docs/adr/0002-user-settings-jsonb.md.
"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user_settings import get_settings_blob, upsert_settings_blob
from services.settings.schema import apply_patch, merge_with_defaults, validate_patch
from services.settings.errors import SettingsError, SettingsValidationError  # noqa: F401 (re-export)


async def get_user_settings(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """Full settings for a user, defaults merged in."""
    stored = await get_settings_blob(session, user_id)
    return merge_with_defaults(stored)


async def get_online_visibility(session: AsyncSession, user_id: int) -> str:
    """
    Shortcut for the presence layer: the resolved `privacy.online` value
    (`everyone` | `contacts` | `nobody`) for a user, defaults included.
    """
    settings = await get_user_settings(session, user_id)
    return settings["privacy"]["online"]


async def update_user_settings(
    session: AsyncSession, user_id: int, patch: dict[str, Any]
) -> dict[str, Any]:
    """
    Validate `patch` against the settings schema, deep-merge it onto the
    user's stored blob, persist, and return the full resolved settings.

    Raises SettingsValidationError on an unknown key or bad value.
    """
    validate_patch(patch)
    stored = await get_settings_blob(session, user_id)
    new_blob = apply_patch(stored, patch)
    saved = await upsert_settings_blob(session, user_id, new_blob)
    return merge_with_defaults(saved)

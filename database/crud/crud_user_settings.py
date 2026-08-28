from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.user_settings import UserSettings


async def get_settings_blob(session: AsyncSession, user_id: int) -> Optional[dict[str, Any]]:
    """The raw sparse JSONB blob, or None if the user has never set anything."""
    stmt = select(UserSettings.settings).where(UserSettings.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_settings_blob(
    session: AsyncSession, user_id: int, blob: dict[str, Any]
) -> dict[str, Any]:
    """Insert or replace the user's sparse settings blob; returns what was stored."""
    stmt = (
        pg_insert(UserSettings)
        .values(user_id=user_id, settings=blob)
        .on_conflict_do_update(
            index_elements=[UserSettings.user_id],
            set_={"settings": blob},
        )
        .returning(UserSettings.settings)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.scalar_one()

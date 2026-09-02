"""CRUD for the content-addressed media blob index (ADR 0010)."""

from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.media_blob import MediaBlob


async def get_blob_by_hash(session: AsyncSession, sha256: str) -> Optional[MediaBlob]:
    result = await session.execute(select(MediaBlob).where(MediaBlob.sha256 == sha256))
    return result.scalar_one_or_none()


async def get_blob_by_key(session: AsyncSession, storage_key: str) -> Optional[MediaBlob]:
    result = await session.execute(
        select(MediaBlob).where(MediaBlob.storage_key == storage_key)
    )
    return result.scalar_one_or_none()


async def reserve_blob(
    session: AsyncSession,
    *,
    sha256: str,
    storage_key: str,
    bucket: str,
    kind: str,
    mime: str,
    size: int,
) -> MediaBlob:
    """
    Insert a blob row for a freshly minted upload ticket (uploaded_at NULL),
    or return the existing row if this hash is already known (race / retry).
    """
    stmt = (
        pg_insert(MediaBlob)
        .values(
            sha256=sha256,
            storage_key=storage_key,
            bucket=bucket,
            kind=kind,
            mime=mime,
            size=size,
        )
        .on_conflict_do_nothing(index_elements=[MediaBlob.sha256])
    )
    await session.execute(stmt)
    await session.commit()
    return await get_blob_by_hash(session, sha256)


async def confirm_and_ref(
    session: AsyncSession, *, storage_key: str, mime: str, size: int
) -> None:
    """
    Called from the send path once the object is HEAD-verified: stamp
    uploaded_at / authoritative mime+size on first use and bump ref_count.
    """
    await session.execute(
        update(MediaBlob)
        .where(MediaBlob.storage_key == storage_key)
        .values(
            mime=mime,
            size=size,
            uploaded_at=func.coalesce(MediaBlob.uploaded_at, func.now()),
            ref_count=MediaBlob.ref_count + 1,
        )
    )
    await session.commit()

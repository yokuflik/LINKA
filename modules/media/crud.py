"""CRUD for the content-addressed media blob index (ADR 0010)."""

from typing import Optional

from sqlalchemy import delete as sa_delete, func, select, update
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
    session: AsyncSession,
    *,
    storage_key: str,
    mime: str,
    size: int,
    blur_hash: Optional[str] = None,
) -> None:
    """
    Called from the send path once the object is HEAD-verified: stamp
    uploaded_at / authoritative mime+size on first use and bump ref_count.
    ``blur_hash`` (ADR 0014) is stored only if the blob doesn't have one yet.
    """
    values = {
        "mime": mime,
        "size": size,
        "uploaded_at": func.coalesce(MediaBlob.uploaded_at, func.now()),
        "ref_count": MediaBlob.ref_count + 1,
    }
    if blur_hash is not None:
        values["blur_hash"] = func.coalesce(MediaBlob.blur_hash, blur_hash)
    await session.execute(
        update(MediaBlob).where(MediaBlob.storage_key == storage_key).values(**values)
    )
    await session.commit()


async def deref_blob(session: AsyncSession, storage_key: str) -> Optional[int]:
    """
    Decrement ref_count for a blob (floored at 0) and return the new count, or
    None if there is no such blob. Called from the message-purge path (ADR 0021)
    - the caller deletes the object + this row when the count reaches 0.
    """
    stmt = (
        update(MediaBlob)
        .where(MediaBlob.storage_key == storage_key)
        .values(ref_count=func.greatest(MediaBlob.ref_count - 1, 0))
        .returning(MediaBlob.ref_count)
    )
    result = await session.execute(stmt)
    new_count = result.scalar_one_or_none()
    await session.commit()
    return new_count


async def delete_blob_row(session: AsyncSession, storage_key: str) -> None:
    """Drop a media_blob row once its object has been deleted from storage."""
    await session.execute(sa_delete(MediaBlob).where(MediaBlob.storage_key == storage_key))
    await session.commit()

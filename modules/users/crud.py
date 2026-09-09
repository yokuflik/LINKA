from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from typing import Optional
from datetime import datetime, timedelta, timezone
import logging

from modules.users.models import User
from modules.auth.models import ReservedUsername

logger = logging.getLogger(__name__)


def _normalize_username(username: str) -> str:
    """Canonical form: trimmed + lowercased. Every read/write goes through this
    so the plain UNIQUE index on users.username is case-insensitive."""
    return (username or "").strip().lower()


async def get_user_by_id(session: AsyncSession, user_id: int) -> Optional[User]:
    """
    Fetch a user by their Primary Key (id).
    """
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_phone(session: AsyncSession, phone_number: str) -> Optional[User]:
    """
    Fetch a user by their phone number.
    """
    stmt = select(User).where(User.phone_number == phone_number)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_username(session: AsyncSession, username: str) -> Optional[User]:
    """
    Fetch a user by their exact (case-insensitive) username - a single point
    lookup on ``ix_users_username``.

    ADR 0017: this is the ONLY username lookup. No prefix / substring / LIKE /
    trigram matching anywhere - that would either scan the users table or need
    an index that doubles as a scraping tool.
    """
    stmt = select(User).where(User.username == _normalize_username(username))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def username_is_free(
    session: AsyncSession, username: str, *, for_user_id: Optional[int] = None
) -> bool:
    """
    True if ``username`` can be taken: not held by another user, and not sitting
    in a live ``reserved_usernames`` grace hold owned by someone else.

    ``for_user_id`` is the caller - a username they already own, or one held in
    *their own* grace window, still counts as free for them.
    """
    norm = _normalize_username(username)

    owner = await get_user_by_username(session, norm)
    if owner is not None and owner.id != for_user_id:
        return False

    held = await session.execute(
        select(ReservedUsername.reserved_for_user_id).where(
            ReservedUsername.username == norm,
            ReservedUsername.expires_at > func.now(),
        )
    )
    reserved_for = held.scalar_one_or_none()
    if reserved_for is not None and reserved_for != for_user_id:
        return False

    return True


async def create_user(
    session: AsyncSession,
    user_id: int,
    phone_number: str,
    username: Optional[str] = None,
) -> Optional[User]:
    """
    Insert a new user. ``username`` (ADR 0017) is normally a handle from
    ``user_service.generate_free_username``; when omitted a random
    ``user_<base36>`` fallback is used so callers/tests that don't care about
    the handle still get a valid NOT NULL value.
    """
    if not username:
        import random

        tail = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=10))
        username = f"user_{tail}"
    new_user = User(
        id=user_id, # Assumes Snowflake ID is generated at the application layer
        phone_number=phone_number,
        username=_normalize_username(username),
    )

    session.add(new_user)
    try:
        await session.commit()
        await session.refresh(new_user)
        return new_user
    except IntegrityError as e:
        # Concurrent request racing on the same phone number OR the same
        # generated username - either way, back off and let the caller re-fetch
        # / retry with a fresh handle.
        await session.rollback()
        logger.error(f"Failed to create user (phone {phone_number} / username {username}). Error: {e}")
        return None


class UsernameTakenError(Exception):
    """Raised by set_username when the target handle lost the unique-index race."""


async def set_username(
    session: AsyncSession, user_id: int, username: str, *, is_initial: bool = False
) -> Optional[User]:
    """
    Change a user's username.

    ``is_initial=True`` is the signup auto-assignment path: it does NOT stamp
    ``username_changed_at`` / append to ``username_change_log`` (so the first
    user-chosen change is free) and does NOT reserve any old handle. A normal
    change stamps ``username_changed_at``, appends ``now()`` to the capped
    ``username_change_log`` ring (ADR 0023 quota), and drops the previous handle
    into ``reserved_usernames`` for the grace window.

    The DB unique index is the authority: a lost race raises UsernameTakenError.
    """
    from config import (
        USERNAME_RESERVED_GRACE_DAYS,
        USERNAME_CHANGE_WINDOW_DAYS,
        USERNAME_CHANGE_MAX_PER_WINDOW,
    )

    norm = _normalize_username(username)
    user = await get_user_by_id(session, user_id)
    if user is None:
        return None

    old_username = user.username
    if old_username == norm:
        return user

    if not is_initial and old_username:
        # Release the old handle into a grace hold. Upsert, not a plain insert:
        # a user who cycles handles (A->B->A->C) re-releases "A" and would
        # otherwise hit the reserved_usernames PRIMARY KEY, aborting the whole
        # change and surfacing as a bogus "taken" error.
        release = pg_insert(ReservedUsername).values(
            username=old_username,
            reserved_for_user_id=user_id,
            released_at=func.now(),
            expires_at=func.now() + func.make_interval(0, 0, 0, USERNAME_RESERVED_GRACE_DAYS),
        )
        await session.execute(
            release.on_conflict_do_update(
                index_elements=["username"],
                set_={
                    "reserved_for_user_id": user_id,
                    "released_at": func.now(),
                    "expires_at": func.now()
                    + func.make_interval(0, 0, 0, USERNAME_RESERVED_GRACE_DAYS),
                },
            )
        )

    # Reclaiming a handle currently sitting in our OWN grace hold: clear that
    # row so it isn't left as a stale reservation against ourselves. (A hold
    # owned by another user was already rejected upstream as `grace_hold`.)
    if not is_initial:
        await session.execute(
            delete(ReservedUsername).where(
                ReservedUsername.username == norm,
                ReservedUsername.reserved_for_user_id == user_id,
            )
        )

    values = {"username": norm}
    if not is_initial:
        values["username_changed_at"] = func.now()
        # Maintain the capped quota ring (ADR 0023): keep only entries inside
        # the rolling window, append this change, cap at the max.
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=USERNAME_CHANGE_WINDOW_DAYS)
        log = []
        for raw_ts in (user.username_change_log or []):
            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                log.append(ts.astimezone(timezone.utc).isoformat())
        log.append(now.isoformat())
        values["username_change_log"] = log[-USERNAME_CHANGE_MAX_PER_WINDOW:]

    stmt = update(User).where(User.id == user_id).values(**values).returning(User)
    try:
        result = await session.execute(stmt)
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise UsernameTakenError(norm) from e

    return result.scalar_one_or_none()


async def update_user_profile(
    session: AsyncSession,
    user_id: int,
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None,
    profile_pic_preview: Optional[str] = None,
    write_preview: bool = False,
    display_name: Optional[str] = None,
    write_display_name: bool = False,
) -> Optional[User]:
    """
    Update user profile fields.

    ``write_preview=True`` forces ``profile_pic_preview`` to be written
    even when it is ``None`` (a new avatar with no computable hash still
    replaces the old one) - ADR 0016.

    ``write_display_name=True`` forces ``display_name`` to be written even
    when it is ``None`` - that's how the user clears their nickname (ADR 0024).
    """
    update_data = {}
    if about_text is not None:
        update_data["about_text"] = about_text
    if profile_pic_url is not None:
        update_data["profile_pic_url"] = profile_pic_url
    if write_preview:
        update_data["profile_pic_preview"] = profile_pic_preview
    if write_display_name:
        update_data["display_name"] = display_name

    if not update_data:
        return await get_user_by_id(session, user_id)

    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(**update_data)
        .returning(User) # PostgreSQL specific: returns the updated row in the same query
    )

    result = await session.execute(stmt)
    await session.commit()

    return result.scalar_one_or_none()

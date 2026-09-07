import random
import re
from datetime import timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

import config
from database.crud.crud_participant import get_all_chat_ids_for_user
from database.crud import crud_user
from database.crud.crud_user import (
    UsernameTakenError,
    get_user_by_id,
    get_user_by_phone,
    set_username as _crud_set_username,
    update_user_profile,
    username_is_free,
)
from database.models.user import User
from services import realtime_service
from services.storage.media_service import public_avatar_url


class UsernameError(Exception):
    """A username write was rejected. ``reason`` is a machine code the client
    maps to a hint (ADR 0017): too_short / too_long / bad_chars /
    must_start_letter / reserved / taken / grace_hold / cooldown.
    ``http_status`` is 400 for a malformed value, 409 for a conflict."""

    def __init__(self, reason: str, http_status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def validate_username_format(raw: str) -> str:
    """Normalise + validate an untrusted username. Returns the canonical
    (lowercased, trimmed) form or raises ``UsernameError`` with a reason code."""
    norm = (raw or "").strip().lower()
    if len(norm) < config.USERNAME_MIN_LEN:
        raise UsernameError("too_short")
    if len(norm) > config.USERNAME_MAX_LEN:
        raise UsernameError("too_long")
    if not norm[0].isalpha():
        raise UsernameError("must_start_letter")
    if not re.match(config.USERNAME_REGEX, norm):
        raise UsernameError("bad_chars")
    if norm in config.USERNAME_RESERVED:
        raise UsernameError("reserved")
    return norm


_USERNAME_ADJECTIVES = (
    "brave", "calm", "clever", "cosmic", "eager", "fuzzy", "gentle", "happy",
    "jolly", "keen", "lively", "lucky", "mellow", "nimble", "quiet", "rapid",
    "shiny", "silent", "sunny", "swift", "tidy", "vivid", "witty", "zesty",
)
_USERNAME_NOUNS = (
    "otter", "falcon", "maple", "comet", "pixel", "harbor", "meadow", "cedar",
    "river", "ember", "pebble", "willow", "lark", "quartz", "orbit", "birch",
    "cove", "delta", "fern", "grove", "heron", "isle", "lynx", "reef",
)


async def generate_free_username(session: AsyncSession) -> str:
    """Mint a random, currently-free handle for a brand-new account (ADR 0017
    step 2). ``<adjective>_<noun>_<digits>``, retried against the unique index
    with a widening digit suffix; falls back to ``user_<base36>``. The returned
    handle is only *probably* still free - ``create_user`` treats the unique
    index as the authority and this is retried on collision."""
    digits = 3
    for attempt in range(config.USERNAME_GENERATE_ATTEMPTS):
        adj = random.choice(_USERNAME_ADJECTIVES)
        noun = random.choice(_USERNAME_NOUNS)
        num = random.randint(10 ** (digits - 1), 10 ** digits - 1)
        candidate = f"{adj}_{noun}_{num}"
        if await username_is_free(session, candidate):
            return candidate
        if attempt >= 1:
            digits = min(digits + 1, 6)
    # Fallback: a large random base36 tail is effectively collision-free.
    tail = "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=10))
    return f"user_{tail}"


async def _cooldown_active(user: User) -> Optional[str]:
    """Return an ISO timestamp of when the change cooldown lifts, or None if a
    change is allowed now. The initial auto-assignment leaves
    ``username_changed_at`` NULL, so the first user-chosen change is free."""
    changed_at = user.username_changed_at
    if changed_at is None:
        return None
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    ready_at = changed_at + timedelta(days=config.USERNAME_CHANGE_COOLDOWN_DAYS)
    from datetime import datetime

    if datetime.now(timezone.utc) >= ready_at:
        return None
    return ready_at.isoformat()


async def check_username_available(session: AsyncSession, user_id: int, raw: str) -> dict:
    """Advisory check for GET /users/username-available. ``{available, reason}``
    - the real authority is the unique-index write in ``set_username``."""
    try:
        norm = validate_username_format(raw)
    except UsernameError as e:
        return {"available": False, "reason": e.reason}

    user = await get_user_by_id(session, user_id)
    if user is not None and user.username == norm:
        return {"available": True, "reason": None}

    if user is not None and await _cooldown_active(user) is not None:
        return {"available": False, "reason": "cooldown"}

    if not await username_is_free(session, norm, for_user_id=user_id):
        owner = await crud_user.get_user_by_username(session, norm)
        reason = "taken" if owner is not None else "grace_hold"
        return {"available": False, "reason": reason}

    return {"available": True, "reason": None}


async def set_username(session: AsyncSession, user_id: int, raw: str) -> User:
    """Change a user's handle. Enforces format, the change cooldown, and the
    grace hold; drops the old handle into ``reserved_usernames``. Raises
    ``UsernameError`` (reason-coded) on any rejection."""
    norm = validate_username_format(raw)

    user = await get_user_by_id(session, user_id)
    if user is None:
        raise UsernameError("not_found", http_status=404)

    if user.username == norm:
        return user

    cooldown_until = await _cooldown_active(user)
    if cooldown_until is not None:
        raise UsernameError("cooldown", http_status=409)

    if not await username_is_free(session, norm, for_user_id=user_id):
        owner = await crud_user.get_user_by_username(session, norm)
        raise UsernameError("taken" if owner is not None else "grace_hold", http_status=409)

    try:
        updated = await _crud_set_username(session, user_id, norm, is_initial=False)
    except UsernameTakenError:
        raise UsernameError("taken", http_status=409)
    if updated is None:
        raise UsernameError("not_found", http_status=404)
    return updated


async def get_profile(session: AsyncSession, user_id: int) -> Optional[User]:
    return await get_user_by_id(session, user_id)


async def get_profile_by_phone(session: AsyncSession, phone_number: str) -> Optional[User]:
    return await get_user_by_phone(session, phone_number)


async def get_profile_by_username(session: AsyncSession, username: str) -> Optional[User]:
    """Exact-match only (ADR 0017): the sole username lookup. No prefix / LIKE /
    substring / trigram - anti-harvest. Returns None on a malformed username."""
    try:
        norm = validate_username_format(username)
    except UsernameError:
        return None
    return await crud_user.get_user_by_username(session, norm)


async def update_profile(
    session: AsyncSession,
    user_id: int,
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None,
) -> Optional[User]:
    return await update_user_profile(
        session,
        user_id=user_id,
        about_text=about_text,
        profile_pic_url=profile_pic_url,
    )


async def broadcast_profile_update(session: AsyncSession, user_id: int) -> None:
    """
    Tell everyone who shares a chat with this user that their profile
    (username / about / photo) just changed, so open clients can update
    the cached name+avatar they show in chat lists, headers and message
    bubbles without waiting to re-open that chat.

    A profile edit touches every chat the user is in (all private chats +
    all shared groups), so this is a *transient* fan-out event over the
    normal chat routing (same path as `typing`) - never a persisted system
    message: that would mean one INSERT into the partitioned `messages`
    table per shared chat, per edit, plus permanent history noise.

    Best-effort: a Redis hiccup here just means a client refreshes on its
    own next chat-open (see useChats.resolve* / the visibilitychange hook).
    """
    user = await get_user_by_id(session, user_id)
    if user is None:
        return
    key = user.profile_pic_url
    resolved_pic = (
        key if (key and key.startswith(("http://", "https://"))) else (public_avatar_url(key) if key else None)
    )
    event = {
        "event": "profile_updated",
        "user_id": str(user.id),
        "username": user.username,
        "about_text": user.about_text,
        "profile_pic_url": resolved_pic,
        "profile_pic_preview": user.profile_pic_preview,
    }
    chat_ids = await get_all_chat_ids_for_user(session, user_id)
    for chat_id in chat_ids:
        await realtime_service.publish_event(chat_id, event)

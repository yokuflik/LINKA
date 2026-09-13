"""
Fills the database with a dataset big enough to actually navigate: three
users, private chats between all of them plus one group chat, and a backdated
message history in every one of those chats (default: 1,000 messages total -
4 chats * 250 messages-per-chat, sized for scripts/seed_vector_data.py, ADR 0042).

Safe to re-run - users, private chats and groups are looked up before they're
created, and a second run just appends more history to the chats that exist.

Usage:
    python3 -m scripts.seed_mock_data
    python3 -m scripts.seed_mock_data --messages-per-chat 2000 --days 90

Reads DATABASE_URL from the environment (or .env), same as scripts.init_db -
which has to have been run at least once first, to create the schema.
"""
import argparse
import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Loads DATABASE_URL before database.connection is imported - it reads the
# environment once, at import time.
load_dotenv()

from sqlalchemy import insert, select, update

from infra.db.connection import dispose_engine
from infra.db.connection import session_scope
from modules.chats.crud.crud_chat import create_chat
from modules.messaging.crud import build_last_message_preview
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.chats.crud.crud_participant import recompute_chat_receipt_cursors
from modules.chats.crud.crud_private_chat_pair import create_pair
from modules.chats.crud.crud_private_chat_pair import get_pair_chat_id
from modules.users.crud import create_user
from modules.users.crud import get_user_by_phone
from modules.users.crud import update_user_profile
from modules.chats.models.chat import Chat
from modules.messaging.models import Message
from modules.receipts.models import MessageReceiptLog
from modules.chats.models.participant import Participant
from modules.users.models import User
from config import (
    RECEIPT_KIND_DELIVERED,
    RECEIPT_KIND_PLAYED,
    RECEIPT_KIND_READ,
    S3_BUCKET_AVATARS,
)
from modules.media.client import async_session as s3_async_session
from modules.media.client import build_object_key
from modules.media.client import client_kwargs
from modules.media.media_service import ensure_buckets
from infra.ids.snowflake import next_id

# mock_photos/p{N}.png (N = user's phone number "1".."10") for user avatars,
# mock_photos/g{N}.jpg for group photos (mapped per group in GROUP_CHATS).
MOCK_PHOTOS_DIR = Path(__file__).resolve().parent.parent / "mock_photos"

ROLE_MEMBER = 1
ROLE_OWNER = 3
TEXT_MESSAGE_TYPE = 1

# These phone numbers are what you type into the PoC's login screen. The OTP
# still prints to the server console like any other login - seeded users
# aren't special.
# (phone number, username, display_name). Phone numbers "1".."10" are the shape
# the PoC's login screen accepts. Usernames must match config.USERNAME_REGEX
# (^[a-z][a-z0-9_]{2,31}$). display_name is free-form (any language / emoji) or
# None - several users are left without one to exercise the
# display_name || username || phone_number fallback (ADR 0024).
USERS = [
    ("1", "lior_azoulay", "ליאור"),
    ("2", "hila_moreno", "Hila ✨"),
    ("3", "yossi_peretz", None),
]

# Indices into USERS. 3 users -> every pair, so there's still private-chat
# coverage even with the smaller cast.
PRIVATE_PAIRS = [
    (0, 1), (0, 2), (1, 2),
]

# (title, owner index, other member indices, group photo file in mock_photos/)
# One small group so `messages_per_chat` (default 250) * 4 chats = 1000
# messages total (ADR 0042 seed size).
GROUP_CHATS = [
    ("Product Standup", 0, [1, 2], "g1.jpg"),
]

# Nothing here matters beyond being varied in length - a few are long enough
# to run past the chat list's preview truncation on purpose.
_PHRASES = [
    "morning",
    "leaving now, be there in 20",
    "did the pipeline go green?",
    "works for me",
    "give me five minutes",
    "no worries, take your time",
    "can you resend the invite?",
    "just parked",
    "lol yeah",
    "honestly that thread could have been a single message but here we are, forty replies deep",
    "let's push it to next week",
    "handled",
    "not sure that's the right call, but let's try it and reassess on Friday",
    "who's picking up the cake",
    "great",
    "one sec",
    "same error on staging, so at least we can reproduce it",
    "cheers!",
    "why did the rollback take twenty minutes?",
    "I'll dig into it after standup",
    "ignore that, wrong group",
    "still good for Saturday?",
    "turned out to be a stale cache, half a day gone for a one-line flush",
    "night",
    "sent",
    "what's the door code again?",
    "let's just release it and patch forward",
    "ring me when you land",
    "sure",
    "actually that's a really good point",
    "about ten minutes out, go ahead and order",
    "anyone free to review a small PR?",
    "the place is booked solid, looking for another spot",
    "forwarding it to you now",
    "let's regroup Monday morning",
    "completely slipped my mind, sorry",
    "forecast looks clear for the weekend",
    "I've got the cooler if you bring the grill",
    "did the transfer land on your side?",
    "see you in a bit",
]


def _random_content() -> str:
    sentence_count = random.choices([1, 2, 3], weights=[7, 3, 1])[0]
    return " ".join(random.choice(_PHRASES) for _ in range(sentence_count))


async def _ensure_users(session) -> list[User]:
    """
    Lookup-then-create: these phone numbers ("1".."10") are also the shape the
    PoC's login screen accepts, so one of them may already exist from earlier
    manual testing. An existing user's auto-assigned username is left alone.
    """
    users = []
    for phone_number, username, display_name in USERS:
        user = await get_user_by_phone(session, phone_number)
        if user is None:
            user = await create_user(
                session, user_id=next_id(), phone_number=phone_number, username=username
            )
            if display_name is not None:
                user = await update_user_profile(
                    session, user_id=user.id,
                    display_name=display_name, write_display_name=True,
                )
            print(f"  created user {username} ({phone_number})")
        users.append(user)
    return users


async def _ensure_avatars(session, users: list[User]) -> None:
    """
    Give every seeded user a profile picture: upload mock_photos/p{N}.png
    (N = phone number) to the avatars bucket and point profile_pic_url at the
    resulting storage key - the same key shape the real upload flow produces
    (build_object_key), so UserOut resolves it exactly the same way.

    Re-runnable: a user who already has a profile_pic_url is left alone. The
    script writes bytes directly rather than through a presigned PUT - it's a
    dev fixture, not a client.
    """
    await ensure_buckets()
    async with s3_async_session().client("s3", **client_kwargs()) as s3:
        for user in users:
            if user.profile_pic_url:
                continue
            photo = MOCK_PHOTOS_DIR / f"p{user.phone_number}.png"
            if not photo.is_file():
                print(f"  no photo file {photo.name} for {user.username} - skipped")
                continue
            key = build_object_key("avatar", "image/png")
            await s3.put_object(
                Bucket=S3_BUCKET_AVATARS,
                Key=key,
                Body=photo.read_bytes(),
                ContentType="image/png",
            )
            await update_user_profile(session, user_id=user.id, profile_pic_url=key)
            print(f"  avatar for {user.username} ({photo.name}) -> {key}")


_GROUP_PHOTO_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


async def _ensure_group_avatar(session, s3, chat: Chat, photo_name: str) -> None:
    """
    Give a group a profile picture: upload mock_photos/<photo_name> to the
    avatars bucket and point Chat.profile_pic_url at the resulting storage key
    - same key shape (build_object_key) the real group-avatar upload flow
    produces, so ChatOut resolves it identically. Re-runnable: a group that
    already has a profile_pic_url is left alone.
    """
    if chat.profile_pic_url:
        return
    photo = MOCK_PHOTOS_DIR / photo_name
    if not photo.is_file():
        print(f"  no photo file {photo.name} for group '{chat.title}' - skipped")
        return
    mime = _GROUP_PHOTO_MIME.get(photo.suffix.lower(), "image/jpeg")
    key = build_object_key("avatar", mime)
    await s3.put_object(Bucket=S3_BUCKET_AVATARS, Key=key, Body=photo.read_bytes(), ContentType=mime)
    await session.execute(update(Chat).where(Chat.id == chat.id).values(profile_pic_url=key))
    await session.commit()
    chat.profile_pic_url = key
    print(f"  group photo for '{chat.title}' ({photo.name}) -> {key}")


async def _ensure_private_chat(session, user_a: User, user_b: User) -> Chat:
    """
    The DB-only half of chat_service.get_or_create_private_chat - no Redis,
    no live "added_to_chat" fan-out, since nothing is connected while seeding.
    """
    existing_chat_id = await get_pair_chat_id(session, user_a.id, user_b.id)
    if existing_chat_id is not None:
        return await session.get(Chat, existing_chat_id)

    chat = await create_chat(session, chat_id=next_id(), is_group=False)
    await create_pair(session, user_a.id, user_b.id, chat.id)
    await add_participant_to_chat(session, chat_id=chat.id, user_id=user_a.id, role=ROLE_MEMBER)
    await add_participant_to_chat(session, chat_id=chat.id, user_id=user_b.id, role=ROLE_MEMBER)
    print(f"  created private chat {user_a.username} <-> {user_b.username}")
    return chat


async def _ensure_group_chat(session, title: str, owner: User, members: list[User]) -> Chat:
    # Groups have no uniqueness constraint of their own (two real groups may
    # share a title), so re-runs match on "a group with this title that this
    # owner is already in" rather than creating a second one.
    stmt = (
        select(Chat)
        .join(Participant, Participant.chat_id == Chat.id)
        .where(Chat.is_group.is_(True), Chat.title == title, Participant.user_id == owner.id)
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing

    chat = await create_chat(session, chat_id=next_id(), is_group=True, title=title)
    await add_participant_to_chat(session, chat_id=chat.id, user_id=owner.id, role=ROLE_OWNER)
    for member in members:
        await add_participant_to_chat(session, chat_id=chat.id, user_id=member.id, role=ROLE_MEMBER)
    print(f"  created group '{title}' ({len(members) + 1} members)")
    return chat


def _history_window(chat: Chat, days: int) -> tuple[datetime, datetime]:
    """
    The [start, end] the new messages get spread across. On a re-run it starts
    just after whatever the chat's newest message is, so timestamps stay in
    step with the Snowflake ids minted below - message pagination orders by
    id, and history that zig-zags in time reads as a bug when scrolling.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    if chat.last_message_id is not None and chat.last_message_at > start:
        start = chat.last_message_at + timedelta(seconds=1)
    return start, end


async def _seed_messages(session, chat: Chat, sender_ids: list[int], count: int, days: int) -> None:
    start, end = _history_window(chat, days)
    step = (end - start) / count

    rows = []
    sender_id = random.choice(sender_ids)
    for i in range(count):
        # 30% chance of the turn passing to someone else, so the history
        # comes out in bursts per sender instead of a strict round-robin.
        if random.random() < 0.3:
            sender_id = random.choice(sender_ids)
        # One message per step, pulled back by up to 90% of a step so the
        # history clumps instead of ticking like a metronome. Staying under a
        # full step is what keeps the sequence strictly increasing; counting
        # from 1 is what puts the newest message at `end` (i.e. now) rather
        # than a whole step short of it.
        offset_in_window = step * (i + 1) - step * (random.random() * 0.9)
        rows.append(
            {
                # Ids are minted in the same order as the timestamps, which is
                # what keeps ORDER BY id (how messages are actually paginated)
                # equal to ORDER BY created_at.
                "id": next_id(),
                "created_at": start + offset_in_window,
                "chat_id": chat.id,
                "sender_id": sender_id,
                "type": TEXT_MESSAGE_TYPE,
                "content": _random_content(),
            }
        )

    # Inserted in chunks rather than one INSERT per message: this is the
    # difference between seeding thousands of rows in a second and in a
    # minute. crud_message.create_message isn't used for the same reason -
    # it commits (and bumps the chat) once per message.
    chunk_size = 500
    for offset in range(0, len(rows), chunk_size):
        await session.execute(insert(Message), rows[offset : offset + chunk_size])

    # The bump create_message would normally have done, once for the batch.
    last = rows[-1]
    await session.execute(
        update(Chat)
        .where(Chat.id == chat.id)
        .values(
            last_message_at=last["created_at"],
            last_message_id=last["id"],
            last_message_preview=build_last_message_preview(last["content"]),
        )
    )
    await session.commit()


async def _seed_receipts(session, chat: Chat) -> None:
    """
    Give each chat a plausible receipt state: some participants fully caught
    up, some partway, some who've never opened it. Writes the fast-path
    watermarks (+ coarse *_at) AND a few message_receipt_log rows per
    participant, so the per-message "info" view has real data to show.

    Idempotent-ish: skips a chat that already has receipt-log rows.
    """
    existing = await session.scalar(
        select(MessageReceiptLog.id).where(MessageReceiptLog.chat_id == chat.id).limit(1)
    )
    if existing is not None:
        return

    msg_rows = (
        await session.execute(
            select(Message.id, Message.created_at, Message.sender_id, Message.type)
            .where(Message.chat_id == chat.id)
            .order_by(Message.id)
        )
    ).all()
    if len(msg_rows) < 4:
        return

    participants = (
        await session.execute(select(Participant).where(Participant.chat_id == chat.id))
    ).scalars().all()

    log_rows = []
    for p in participants:
        # 0 = never opened, 1 = read all, else = somewhere in the middle.
        roll = random.random()
        if roll < 0.2:
            continue
        if roll > 0.75:
            cut = len(msg_rows) - 1
        else:
            cut = random.randint(len(msg_rows) // 3, len(msg_rows) - 2)

        read_msg = msg_rows[cut]
        deliv_idx = min(len(msg_rows) - 1, cut + random.randint(0, 2))
        deliv_msg = msg_rows[deliv_idx]
        read_at = read_msg.created_at + timedelta(minutes=random.randint(1, 240))
        deliv_at = deliv_msg.created_at + timedelta(seconds=random.randint(5, 300))

        await session.execute(
            update(Participant)
            .where(Participant.chat_id == chat.id, Participant.user_id == p.user_id)
            .values(
                last_delivered_message_id=deliv_msg.id,
                last_read_message_id=read_msg.id,
                last_delivered_at=deliv_at,
                last_read_at=read_at,
            )
        )
        log_rows.append(dict(id=next_id(), occurred_at=deliv_at, chat_id=chat.id,
                             user_id=p.user_id, kind=RECEIPT_KIND_DELIVERED, up_to_message_id=deliv_msg.id))
        log_rows.append(dict(id=next_id(), occurred_at=read_at, chat_id=chat.id,
                             user_id=p.user_id, kind=RECEIPT_KIND_READ, up_to_message_id=read_msg.id))

        # A played row for the last voice message at/under the read cut, if any.
        played = next(
            (m for m in reversed(msg_rows[: cut + 1]) if m.type == 4 and m.sender_id != p.user_id),
            None,
        )
        if played is not None and random.random() < 0.7:
            await session.execute(
                update(Participant)
                .where(Participant.chat_id == chat.id, Participant.user_id == p.user_id)
                .values(last_played_message_id=played.id,
                        last_played_at=played.created_at + timedelta(minutes=random.randint(1, 120)))
            )
            log_rows.append(dict(id=next_id(),
                                 occurred_at=played.created_at + timedelta(minutes=random.randint(1, 120)),
                                 chat_id=chat.id, user_id=p.user_id,
                                 kind=RECEIPT_KIND_PLAYED, up_to_message_id=played.id))

    if log_rows:
        await session.execute(insert(MessageReceiptLog), log_rows)
    await recompute_chat_receipt_cursors(session, chat.id)
    await session.commit()


async def main(messages_per_chat: int, days: int) -> None:
    async with session_scope() as session:
        print("Users:")
        users = await _ensure_users(session)

        print("Avatars:")
        await _ensure_avatars(session, users)

        print("Chats:")
        # (label, chat, sender_ids, message_count)
        chats: list[tuple[str, Chat, list[int], int]] = []

        for a, b in PRIVATE_PAIRS:
            chat = await _ensure_private_chat(session, users[a], users[b])
            label = f"{users[a].username} <-> {users[b].username}"
            chats.append((label, chat, [users[a].id, users[b].id], messages_per_chat))

        group_photo_jobs: list[tuple[Chat, str]] = []
        for title, owner_index, member_indices, photo_name in GROUP_CHATS:
            owner = users[owner_index]
            members = [users[i] for i in member_indices]
            chat = await _ensure_group_chat(session, title, owner, members)
            chats.append((title, chat, [owner.id] + [m.id for m in members], messages_per_chat))
            group_photo_jobs.append((chat, photo_name))

        print("Group photos:")
        await ensure_buckets()
        async with s3_async_session().client("s3", **client_kwargs()) as s3:
            for chat, photo_name in group_photo_jobs:
                await _ensure_group_avatar(session, s3, chat, photo_name)

        print(f"Messages ({messages_per_chat} per chat):")
        total_messages = 0
        for label, chat, sender_ids, count in chats:
            await _seed_messages(session, chat, sender_ids, count, days)
            await _seed_receipts(session, chat)
            total_messages += count
            print(f"  {count:>6} -> {label}")

    await dispose_engine()

    print(f"\nDone: {len(users)} users, {len(chats)} chats, {total_messages} messages inserted.")
    print("Log in from the PoC with any of these numbers (OTP prints to the server console):")
    for phone_number, username, _display_name in USERS:
        print(f"  {phone_number}  {username}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages-per-chat", type=int, default=250)
    parser.add_argument("--days", type=int, default=30, help="how far back the generated history starts")
    args = parser.parse_args()

    asyncio.run(main(args.messages_per_chat, args.days))

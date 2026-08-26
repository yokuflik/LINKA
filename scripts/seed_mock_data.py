"""
Fills the database with a dataset big enough to actually navigate: five users,
private chats between some of them, group chats containing some of them, and a
backdated message history in every one of those chats.

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

from dotenv import load_dotenv

# Loads DATABASE_URL before database.connection is imported - it reads the
# environment once, at import time.
load_dotenv()

from sqlalchemy import insert, select, update

from database.connection import dispose_engine, session_scope
from database.crud.crud_chat import create_chat
from database.crud.crud_message import build_last_message_preview
from database.crud.crud_participant import add_participant_to_chat
from database.crud.crud_private_chat_pair import create_pair, get_pair_chat_id
from database.crud.crud_user import create_user, get_user_by_phone
from database.models.chat import Chat
from database.models.message import Message
from database.models.participant import Participant
from database.models.user import User
from utils.snowflake import next_id

ROLE_MEMBER = 1
ROLE_OWNER = 3
TEXT_MESSAGE_TYPE = 1

# Numbers are fixed (not random) so they're the same on every run: these are
# what you type into the PoC's login screen. The OTP still prints to the
# server console like any other login - seeded users aren't special.
MOCK_USERS = [
    ("+972500000001", "Noa Peretz"),
    ("+972500000002", "Yossi Levi"),
    ("+972500000003", "Maya Cohen"),
    ("+972500000004", "Amit Bar"),
    ("+972500000005", "Dana Shani"),
]

# Indices into MOCK_USERS. Deliberately not every possible pair - a dataset
# where everyone has a chat with everyone hides bugs in the "no chat with
# this person yet" path.
PRIVATE_PAIRS = [(0, 1), (0, 2), (1, 2), (1, 3), (3, 4)]

# (title, owner index, other member indices)
GROUP_CHATS = [
    ("Linka Devs", 0, [1, 2, 3]),
    ("Weekend Trip", 3, [0, 4]),
    ("Cohen Family", 2, [1, 4]),
]

# Nothing here matters beyond being varied in length - a few are long enough
# to run past the chat list's preview truncation on purpose.
_PHRASES = [
    "hey",
    "on my way",
    "did you see the latest build?",
    "sounds good to me",
    "I'll call you in ten minutes",
    "no rush, whenever you get a chance",
    "can you send me that link again?",
    "just landed",
    "haha same",
    "that meeting could have been an email, and I say that as someone who genuinely likes meetings",
    "let's move it to tomorrow morning",
    "done",
    "I'm not sure that's what they meant, but let's go with it for now and see what breaks",
    "who's bringing the coffee",
    "perfect",
    "give me a sec",
    "it's failing on my machine too, so at least it's consistent",
    "thanks!",
    "any idea why the deploy took so long?",
    "I'll take a look after lunch",
    "wrong chat, sorry",
    "are we still on for Thursday?",
    "the whole thing turned out to be a missing index, three days of debugging for one line of SQL",
    "good night",
    "sending it now",
    "what's the address again?",
    "I think we should just ship it and iterate",
    "call me when you're free",
    "ok",
    "that's actually a great idea",
]


def _random_content() -> str:
    sentence_count = random.choices([1, 2, 3], weights=[7, 3, 1])[0]
    return " ".join(random.choice(_PHRASES) for _ in range(sentence_count))


async def _ensure_users(session) -> list[User]:
    users = []
    for phone_number, display_name in MOCK_USERS:
        user = await get_user_by_phone(session, phone_number)
        if user is None:
            user = await create_user(session, user_id=next_id(), phone_number=phone_number, display_name=display_name)
            print(f"  created user {display_name} ({phone_number})")
        users.append(user)
    return users


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
    print(f"  created private chat {user_a.display_name} <-> {user_b.display_name}")
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


async def main(messages_per_chat: int, days: int) -> None:
    async with session_scope() as session:
        print("Users:")
        users = await _ensure_users(session)

        print("Chats:")
        chats: list[tuple[str, Chat, list[int]]] = []

        for a, b in PRIVATE_PAIRS:
            chat = await _ensure_private_chat(session, users[a], users[b])
            label = f"{users[a].display_name} <-> {users[b].display_name}"
            chats.append((label, chat, [users[a].id, users[b].id]))

        for title, owner_index, member_indices in GROUP_CHATS:
            owner = users[owner_index]
            members = [users[i] for i in member_indices]
            chat = await _ensure_group_chat(session, title, owner, members)
            chats.append((title, chat, [owner.id] + [m.id for m in members]))

        print(f"Messages ({messages_per_chat} per chat, spread over the last {days} days):")
        for label, chat, sender_ids in chats:
            await _seed_messages(session, chat, sender_ids, messages_per_chat, days)
            print(f"  {messages_per_chat:>6} -> {label}")

    await dispose_engine()

    print(
        f"\nDone: {len(users)} users, {len(chats)} chats, "
        f"{len(chats) * messages_per_chat} messages inserted."
    )
    print("Log in from the PoC with any of these numbers (OTP prints to the server console):")
    for phone_number, display_name in MOCK_USERS:
        print(f"  {phone_number}  {display_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages-per-chat", type=int, default=250)
    parser.add_argument("--days", type=int, default=30, help="how far back the generated history starts")
    args = parser.parse_args()

    asyncio.run(main(args.messages_per_chat, args.days))

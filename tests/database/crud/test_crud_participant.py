import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from database.crud.crud_chat import create_chat, get_chat_by_id
from database.crud.crud_message import create_message
from database.crud.crud_participant import (
    add_participant_to_chat,
    get_user_chats,
    get_chat_participants,
    update_last_delivered_message,
    update_last_read_message,
    remove_participant
)

# Tells pytest to run all tests in this file asynchronously
pytestmark = pytest.mark.asyncio


async def test_add_participant_success(db_session: AsyncSession):
    # Arrange: PostgreSQL requires the User and Chat to exist first due to Foreign Key constraints
    user_id = 500
    chat_id = 600
    await create_user(db_session, user_id=user_id, phone_number="+972505555555")
    await create_chat(db_session, chat_id=chat_id, is_group=True, title="Test Group")
    
    # Act: Add the user to the chat
    participant = await add_participant_to_chat(db_session, chat_id, user_id, role=2)
    
    # Assert
    assert participant is not None
    assert participant.chat_id == chat_id
    assert participant.user_id == user_id
    assert participant.role == 2
    assert participant.last_read_message_id is None


async def test_get_user_chats(db_session: AsyncSession):
    # Arrange: Create a user and two chats
    user_id = 501
    await create_user(db_session, user_id=user_id, phone_number="+972501112222")
    
    await create_chat(db_session, chat_id=601, is_group=True)
    await create_chat(db_session, chat_id=602, is_group=True)
    
    await add_participant_to_chat(db_session, chat_id=601, user_id=user_id)
    await add_participant_to_chat(db_session, chat_id=602, user_id=user_id)
    
    # Act: Retrieve all chats for this specific user
    chats = await get_user_chats(db_session, user_id)
    
    # Assert
    assert len(chats) == 2
    chat_ids = [c.chat_id for c in chats]
    assert 601 in chat_ids
    assert 602 in chat_ids


async def test_update_watermark(db_session: AsyncSession):
    # Arrange
    user_id = 502
    chat_id = 603
    await create_user(db_session, user_id=user_id, phone_number="+972503334444")
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=user_id)
    
    # Act: Simulate the user reading messages up to ID 9999
    updated = await update_last_read_message(
        session=db_session, 
        chat_id=chat_id, 
        user_id=user_id, 
        message_id=9999
    )
    
    # Assert
    assert updated is not None
    assert updated.last_read_message_id == 9999


async def test_remove_participant(db_session: AsyncSession):
    # Arrange
    user_id = 503
    chat_id = 604
    await create_user(db_session, user_id=user_id, phone_number="+972509998888")
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=user_id)
    
    # Act: Remove the user, then attempt to fetch all participants for that chat
    is_removed = await remove_participant(db_session, chat_id, user_id)
    participants = await get_chat_participants(db_session, chat_id)
    
    # Assert
    assert is_removed is True
    assert len(participants) == 0


async def test_get_user_chats_orders_by_recency_and_paginates(db_session: AsyncSession):
    # Arrange: 610 gets a message, then 611 is created but never messaged,
    # then 612 gets a message. 611 (empty) should rank by its creation time -
    # between the two messaged chats - not get shoved to the end of the list.
    user_id = 510
    await create_user(db_session, user_id=user_id, phone_number="+972505100000")

    await create_chat(db_session, chat_id=610, is_group=True)
    await add_participant_to_chat(db_session, chat_id=610, user_id=user_id)
    await create_message(db_session, message_id=90100, chat_id=610, sender_id=user_id, content="hi")

    await create_chat(db_session, chat_id=611, is_group=True)  # no message, ever
    await add_participant_to_chat(db_session, chat_id=611, user_id=user_id)

    await create_chat(db_session, chat_id=612, is_group=True)
    await add_participant_to_chat(db_session, chat_id=612, user_id=user_id)
    await create_message(db_session, message_id=90101, chat_id=612, sender_id=user_id, content="hi")

    # Act: first page, most recently active chat first
    first_page = await get_user_chats(db_session, user_id, limit=2)

    # Assert: 612 (messaged last) is first; 611 (created after 610's message,
    # never messaged) ranks by its creation time, ahead of 610
    assert [p.chat_id for p in first_page] == [612, 611]

    # Act: next page, cursoring off the last row of the first page
    last = first_page[-1]
    next_before = (last.chat.last_message_at, last.chat_id)
    second_page = await get_user_chats(db_session, user_id, before=next_before, limit=2)

    # Assert
    assert [p.chat_id for p in second_page] == [610]


async def test_update_last_delivered_message_sets_watermark(db_session: AsyncSession):
    # Arrange
    user_id, chat_id = 520, 620
    await create_user(db_session, user_id=user_id, phone_number="+972505200000")
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=user_id)

    # Act
    updated = await update_last_delivered_message(db_session, chat_id=chat_id, user_id=user_id, message_id=4242)

    # Assert
    assert updated is not None
    assert updated.last_delivered_message_id == 4242


async def test_receipt_cursors_advance_only_once_every_participant_catches_up(db_session: AsyncSession):
    # Arrange: a 3-person group - the sender plus two recipients.
    chat_id = 621
    sender_id, recipient_a, recipient_b = 521, 522, 523
    for uid in (sender_id, recipient_a, recipient_b):
        await create_user(db_session, user_id=uid, phone_number=f"+97250{uid}")
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    for uid in (sender_id, recipient_a, recipient_b):
        await add_participant_to_chat(db_session, chat_id=chat_id, user_id=uid)

    message = await create_message(db_session, message_id=95200, chat_id=chat_id, sender_id=sender_id, content="hi all")

    # Act + Assert: sending bumps the sender's own watermark (they've
    # trivially "seen" their own message), but neither recipient has
    # delivered/read it yet, so the chat-wide cursors can't advance past it.
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.all_delivered_up_to_message_id in (None, 0)
    assert chat.all_read_up_to_message_id in (None, 0)

    # Act: only one of the two recipients acknowledges delivery
    await update_last_delivered_message(db_session, chat_id=chat_id, user_id=recipient_a, message_id=message.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.all_delivered_up_to_message_id in (None, 0)  # recipient_b hasn't yet

    # Act: the last recipient catches up too
    await update_last_delivered_message(db_session, chat_id=chat_id, user_id=recipient_b, message_id=message.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.all_delivered_up_to_message_id == message.id
    assert chat.all_read_up_to_message_id in (None, 0)  # delivered != read

    # Act: same story for read receipts
    await update_last_read_message(db_session, chat_id=chat_id, user_id=recipient_a, message_id=message.id)
    await update_last_read_message(db_session, chat_id=chat_id, user_id=recipient_b, message_id=message.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.all_read_up_to_message_id == message.id


async def test_new_participant_does_not_retroactively_block_receipt_cursors(db_session: AsyncSession):
    # Arrange: two people fully read a message before a third ever joins.
    chat_id = 622
    veteran_a, veteran_b, newcomer = 524, 525, 526
    await create_user(db_session, user_id=veteran_a, phone_number=f"+97250{veteran_a}")
    await create_user(db_session, user_id=veteran_b, phone_number=f"+97250{veteran_b}")
    await create_user(db_session, user_id=newcomer, phone_number=f"+97250{newcomer}")
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=veteran_a)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=veteran_b)

    message = await create_message(db_session, message_id=95300, chat_id=chat_id, sender_id=veteran_a, content="old news")
    await update_last_delivered_message(db_session, chat_id=chat_id, user_id=veteran_b, message_id=message.id)
    await update_last_read_message(db_session, chat_id=chat_id, user_id=veteran_b, message_id=message.id)

    chat_before = await get_chat_by_id(db_session, chat_id)
    assert chat_before.all_read_up_to_message_id == message.id

    # Act: a brand-new member joins after that message was already read by all
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=newcomer)

    # Assert: their arrival doesn't undo history they were never part of
    chat_after = await get_chat_by_id(db_session, chat_id)
    assert chat_after.all_read_up_to_message_id == message.id
    assert chat_after.all_delivered_up_to_message_id == message.id


async def test_remove_participant_unsticks_receipt_cursors(db_session: AsyncSession):
    # Arrange: a lagging participant is the only reason the chat isn't
    # "read by everyone" yet.
    chat_id = 623
    sender_id, laggard = 527, 528
    await create_user(db_session, user_id=sender_id, phone_number=f"+97250{sender_id}")
    await create_user(db_session, user_id=laggard, phone_number=f"+97250{laggard}")
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=sender_id)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=laggard)

    message = await create_message(db_session, message_id=95400, chat_id=chat_id, sender_id=sender_id, content="hello")
    chat_before = await get_chat_by_id(db_session, chat_id)
    assert chat_before.all_read_up_to_message_id in (None, 0)

    # Act: the laggard leaves without ever reading it
    await remove_participant(db_session, chat_id=chat_id, user_id=laggard)

    # Assert: the remaining participant(s) - just the sender, who already
    # implicitly read their own message - now define "read by everyone"
    chat_after = await get_chat_by_id(db_session, chat_id)
    assert chat_after.all_read_up_to_message_id == message.id


async def test_concurrent_add_same_participant_only_one_wins(session_factory):
    # Arrange: e.g. a double-tapped "join group" request racing itself,
    # each attempt on its own session/connection.
    user_id, chat_id = 504, 605
    async with session_factory() as setup_session:
        await create_user(setup_session, user_id=user_id, phone_number="+972501231234")
        await create_chat(setup_session, chat_id=chat_id, is_group=True)

    async def attempt():
        async with session_factory() as session:
            return await add_participant_to_chat(session, chat_id, user_id)

    # Act
    results = await asyncio.gather(attempt(), attempt())

    # Assert: the composite (chat_id, user_id) Primary Key lets only one succeed
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1

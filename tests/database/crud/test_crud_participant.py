import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from database.crud.crud_chat import create_chat
from database.crud.crud_participant import (
    add_participant_to_chat,
    get_user_chats,
    get_chat_participants,
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

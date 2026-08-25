import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import (
    create_chat,
    get_chat_by_id,
    update_chat_details,
    delete_chat
)

# Tells pytest to run all tests in this file asynchronously
pytestmark = pytest.mark.asyncio


async def test_create_group_chat_success(db_session: AsyncSession):
    # Arrange
    chat_id = 10001
    
    # Act
    chat = await create_chat(
        session=db_session,
        chat_id=chat_id,
        is_group=True,
        title="Backend Engineering",
        about_text="Architecture discussions",
        profile_pic_url="https://s3.aws.com/bucket/group.png"
    )
    
    # Assert
    assert chat is not None
    assert chat.id == chat_id
    assert chat.is_group is True
    assert chat.title == "Backend Engineering"
    assert chat.about_text == "Architecture discussions"
    assert chat.profile_pic_url == "https://s3.aws.com/bucket/group.png"


async def test_create_private_chat_ignores_title(db_session: AsyncSession):
    # Arrange
    chat_id = 10002
    
    # Act: Attempt to create a private chat but accidentally pass a title
    chat = await create_chat(
        session=db_session,
        chat_id=chat_id,
        is_group=False,
        title="This Should Be Ignored",
        about_text="Also ignored"
    )
    
    # Assert: The CRUD logic should enforce that private chats have no title or about_text
    assert chat is not None
    assert chat.is_group is False
    assert chat.title is None
    assert chat.about_text is None


async def test_create_chat_duplicate_id_fails(db_session: AsyncSession):
    # Arrange
    chat_id = 10003
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    
    # Act: Try to insert another chat with the exact same ID
    duplicate_chat = await create_chat(db_session, chat_id=chat_id, is_group=False)
    
    # Assert: Should gracefully return None due to IntegrityError handling
    assert duplicate_chat is None


async def test_get_chat_by_id(db_session: AsyncSession):
    # Arrange
    chat_id = 10004
    await create_chat(db_session, chat_id=chat_id, is_group=True, title="Test Get")
    
    # Act
    fetched_chat = await get_chat_by_id(db_session, chat_id)
    
    # Assert
    assert fetched_chat is not None
    assert fetched_chat.title == "Test Get"


async def test_update_chat_details(db_session: AsyncSession):
    # Arrange
    chat_id = 10005
    await create_chat(
        session=db_session, 
        chat_id=chat_id, 
        is_group=True, 
        title="Old Title"
    )
    
    # Act
    updated_chat = await update_chat_details(
        session=db_session,
        chat_id=chat_id,
        title="New Title",
        about_text="Updated rules"
    )
    
    # Assert
    assert updated_chat is not None
    assert updated_chat.title == "New Title"
    assert updated_chat.about_text == "Updated rules"


async def test_delete_chat(db_session: AsyncSession):
    # Arrange
    chat_id = 10006
    await create_chat(db_session, chat_id=chat_id, is_group=True)
    
    # Act
    is_deleted = await delete_chat(db_session, chat_id)
    fetched_chat = await get_chat_by_id(db_session, chat_id)
    
    # Assert
    assert is_deleted is True
    assert fetched_chat is None


async def test_concurrent_create_same_chat_id_only_one_wins(session_factory):
    # Arrange: a duplicate/retried request racing itself with the same
    # (client-generated) Snowflake chat_id, each on its own session.
    chat_id = 10007

    async def attempt(is_group: bool):
        async with session_factory() as session:
            return await create_chat(session, chat_id=chat_id, is_group=is_group)

    # Act
    results = await asyncio.gather(attempt(True), attempt(False))

    # Assert: the Primary Key uniqueness lets exactly one insert succeed
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1
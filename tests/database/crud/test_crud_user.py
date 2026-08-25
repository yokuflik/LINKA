# tests/database/crud/test_crud_user.py
import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import (
    create_user,
    get_user_by_id,
    get_user_by_phone,
    update_user_profile
)

# Tells pytest to run these tests asynchronously
pytestmark = pytest.mark.asyncio

async def test_create_user_success(db_session: AsyncSession):
    # Arrange
    user_id = 123456789
    phone_number = "+972501234567"
    
    # Act
    new_user = await create_user(
        session=db_session, 
        user_id=user_id, 
        phone_number=phone_number, 
        display_name="Test User"
    )
    
    # Assert
    assert new_user is not None
    assert new_user.id == user_id
    assert new_user.phone_number == phone_number
    assert new_user.display_name == "Test User"


async def test_create_user_duplicate_phone_fails(db_session: AsyncSession):
    # Arrange
    phone_number = "+972509999999"
    
    # Act: Create the first user successfully
    await create_user(db_session, user_id=1, phone_number=phone_number)
    
    # Act: Attempt to create a second user with the exact same phone number
    duplicate_user = await create_user(db_session, user_id=2, phone_number=phone_number)
    
    # Assert: Should return None due to the IntegrityError caught in our CRUD function
    assert duplicate_user is None


async def test_get_user_by_phone(db_session: AsyncSession):
    # Arrange
    phone_number = "+972501111111"
    await create_user(db_session, user_id=10, phone_number=phone_number)
    
    # Act
    fetched_user = await get_user_by_phone(db_session, phone_number)
    
    # Assert
    assert fetched_user is not None
    assert fetched_user.id == 10


async def test_update_user_profile(db_session: AsyncSession):
    # Arrange
    user_id = 42
    await create_user(db_session, user_id=user_id, phone_number="+972504242424")
    
    # Act
    updated_user = await update_user_profile(
        session=db_session,
        user_id=user_id,
        display_name="Updated Name",
        about_text="New bio description",
        profile_pic_url="https://s3.aws.com/mybucket/pic.jpg"
    )
    
    # Assert
    assert updated_user is not None
    assert updated_user.display_name == "Updated Name"
    assert updated_user.about_text == "New bio description"
    assert updated_user.profile_pic_url == "https://s3.aws.com/mybucket/pic.jpg"
    assert updated_user.phone_number == "+972504242424" # Remains unchanged


async def test_concurrent_registration_same_phone_only_one_wins(session_factory):
    # Arrange: two different IDs racing to register the exact same phone number
    # (e.g. two OTP-verification requests firing at once), each on its own
    # session/connection since a single AsyncSession isn't concurrency-safe.
    phone = "+972500000001"

    async def attempt(user_id: int):
        async with session_factory() as session:
            return await create_user(session, user_id=user_id, phone_number=phone)

    # Act
    results = await asyncio.gather(attempt(90001), attempt(90002))

    # Assert: the unique constraint on phone_number lets exactly one succeed
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1
import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from database.crud.crud_chat import create_chat, get_chat_by_id
from database.crud.crud_message import (
    create_message,
    get_message_by_id,
    get_chat_messages,
    edit_message_content,
    soft_delete_message,
    compute_message_status,
    count_unread_messages,
    build_last_message_preview,
    DELETED_MESSAGE_PREVIEW,
    MAX_PAGE_SIZE,
)
from database.crud.crud_participant import add_participant_to_chat, update_last_delivered_message, update_last_read_message
from database.models.message import MessageStatus
from database.models.participant import Participant
from utils.snowflake import next_id

# Tells pytest to run all tests in this file asynchronously
pytestmark = pytest.mark.asyncio

# crud_message derives partition-pruning bounds on `created_at` from the message
# id (its high bits decode to a wall-clock ms - see MESSAGE_PARTITION_QUERY_SKEW_HOURS).
# Fabricated small ints like 90010 decode to 2024-01-01 and get pruned out of a
# now() row's partition, so tests must mint ids off a real, current base.
_MID_BASE = next_id() & ~0x3FFFFF


def _mid(n: int) -> int:
    return _MID_BASE + n


async def _make_chat_with_sender(db_session: AsyncSession, chat_id: int, user_id: int):
    await create_user(db_session, user_id=user_id, phone_number=f"+97250{user_id}")
    await create_chat(db_session, chat_id=chat_id, is_group=True, title="Test Chat")


async def test_create_message_success(db_session: AsyncSession):
    # Arrange: Foreign Key constraints require the chat and sender to exist first
    chat_id, user_id, message_id = 700, 800, 90001
    await _make_chat_with_sender(db_session, chat_id, user_id)

    # Act
    message = await create_message(
        session=db_session,
        message_id=message_id,
        chat_id=chat_id,
        sender_id=user_id,
        content="hello world",
    )

    # Assert
    assert message is not None
    assert message.id == message_id
    assert message.chat_id == chat_id
    assert message.sender_id == user_id
    assert message.content == "hello world"
    assert message.type == 1
    assert message.is_edited is False
    assert message.deleted_at is None
    assert message.created_at is not None


async def test_create_message_without_sender_for_system_messages(db_session: AsyncSession):
    # Arrange
    chat_id, message_id = 701, 90002
    await create_chat(db_session, chat_id=chat_id, is_group=True, title="Test Chat")

    # Act: System messages (e.g. "X joined the group") have no sender
    message = await create_message(
        session=db_session,
        message_id=message_id,
        chat_id=chat_id,
        sender_id=None,
        type=6,
        content="X joined the group",
    )

    # Assert
    assert message is not None
    assert message.sender_id is None
    assert message.type == 6


async def test_create_message_invalid_chat_fails(db_session: AsyncSession):
    # Act: The chat_id does not exist, so the Foreign Key constraint should reject it
    message = await create_message(
        session=db_session,
        message_id=90003,
        chat_id=999999,
        content="orphan message",
    )

    # Assert: Should gracefully return None due to IntegrityError handling
    assert message is None


async def test_get_message_by_id(db_session: AsyncSession):
    # Arrange
    chat_id, user_id, message_id = 702, 801, _mid(4)
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=message_id, chat_id=chat_id, sender_id=user_id, content="find me")

    # Act
    fetched = await get_message_by_id(db_session, chat_id=chat_id, message_id=message_id)

    # Assert
    assert fetched is not None
    assert fetched.content == "find me"


async def test_get_chat_messages_orders_newest_first_and_paginates(db_session: AsyncSession):
    # Arrange: Insert three messages with strictly increasing (Snowflake-like) IDs
    chat_id, user_id = 703, 802
    await _make_chat_with_sender(db_session, chat_id, user_id)
    for message_id in (_mid(10), _mid(11), _mid(12)):
        await create_message(db_session, message_id=message_id, chat_id=chat_id, sender_id=user_id, content=str(message_id))

    # Act: First page
    first_page = await get_chat_messages(db_session, chat_id=chat_id, limit=2)

    # Assert: Newest first
    assert [m.id for m in first_page] == [_mid(12), _mid(11)]

    # Act: Next page, using the last message of the first page as the cursor
    second_page = await get_chat_messages(db_session, chat_id=chat_id, before_id=first_page[-1].id, limit=2)

    # Assert
    assert [m.id for m in second_page] == [_mid(10)]


async def test_get_chat_messages_excludes_soft_deleted_by_default(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 704, 803
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=90020, chat_id=chat_id, sender_id=user_id, content="visible")
    await create_message(db_session, message_id=90021, chat_id=chat_id, sender_id=user_id, content="hidden")
    await soft_delete_message(db_session, chat_id=chat_id, message_id=90021)

    # Act
    visible_only = await get_chat_messages(db_session, chat_id=chat_id)
    including_deleted = await get_chat_messages(db_session, chat_id=chat_id, include_deleted=True)

    # Assert
    assert [m.id for m in visible_only] == [90020]
    assert {m.id for m in including_deleted} == {90020, 90021}


async def test_edit_message_content(db_session: AsyncSession):
    # Arrange
    chat_id, user_id, message_id = 705, 804, 90030
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=message_id, chat_id=chat_id, sender_id=user_id, content="typo")

    # Act
    edited = await edit_message_content(db_session, chat_id=chat_id, message_id=message_id, new_content="fixed")

    # Assert
    assert edited is not None
    assert edited.content == "fixed"
    assert edited.is_edited is True
    assert edited.edited_at is not None


async def test_soft_delete_message(db_session: AsyncSession):
    # Arrange
    chat_id, user_id, message_id = 706, 805, _mid(40)
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=message_id, chat_id=chat_id, sender_id=user_id, content="to delete")

    # Act
    is_deleted = await soft_delete_message(db_session, chat_id=chat_id, message_id=message_id)
    fetched = await get_message_by_id(db_session, chat_id=chat_id, message_id=message_id)

    # Assert: The row still exists (soft delete), but is flagged
    assert is_deleted is True
    assert fetched is not None
    assert fetched.deleted_at is not None


async def test_soft_delete_message_twice_returns_false(db_session: AsyncSession):
    # Arrange
    chat_id, user_id, message_id = 707, 806, 90050
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=message_id, chat_id=chat_id, sender_id=user_id, content="to delete")
    await soft_delete_message(db_session, chat_id=chat_id, message_id=message_id)

    # Act: Deleting an already-deleted message should be a no-op
    is_deleted_again = await soft_delete_message(db_session, chat_id=chat_id, message_id=message_id)

    # Assert
    assert is_deleted_again is False


async def test_create_message_bumps_chat_recency(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 709, 808
    await _make_chat_with_sender(db_session, chat_id, user_id)
    chat_before = await get_chat_by_id(db_session, chat_id)
    # No message yet, so recency defaults to when the chat was created
    assert chat_before.last_message_at == chat_before.created_at
    assert chat_before.last_message_id is None

    # Act
    message = await create_message(db_session, message_id=90070, chat_id=chat_id, sender_id=user_id, content="hi")

    # Assert: last_message_at/id mirror the message that was just sent
    chat_after = await get_chat_by_id(db_session, chat_id)
    assert chat_after.last_message_at == message.created_at
    assert chat_after.last_message_id == message.id


async def test_create_message_bumps_senders_own_watermarks(db_session: AsyncSession):
    # Arrange: sending implies having seen the chat up to that point - a
    # lone sender is therefore, trivially, "everyone" who's read it so far.
    chat_id, user_id = 711, 810
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=user_id)

    # Act
    message = await create_message(db_session, message_id=90080, chat_id=chat_id, sender_id=user_id, content="hi")

    # Assert: the sender's own watermarks jumped to their new message...
    participant = await db_session.get(Participant, {"chat_id": chat_id, "user_id": user_id})
    assert participant.last_delivered_message_id == message.id
    assert participant.last_read_message_id == message.id

    # ...and the chat-wide receipt cursors reflect that immediately.
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.all_delivered_up_to_message_id == message.id
    assert chat.all_read_up_to_message_id == message.id


async def test_build_last_message_preview():
    # Text message: the content, truncated to the column width.
    assert build_last_message_preview("hello") == "hello"
    assert build_last_message_preview("x" * 500) == "x" * 120

    # Caption-less media messages fall back to a generic label per type.
    assert build_last_message_preview(None, type=2) == "\U0001F4F7 Photo"
    assert build_last_message_preview(None, type=3) == "\U0001F3A5 Video"
    assert build_last_message_preview(None, type=4) == "\U0001F3A4 Voice message"
    assert build_last_message_preview(None, type=5) == "\U0001F4CE File"

    # A media message *with* a caption shows the caption, not the label.
    assert build_last_message_preview("look at this", type=2) == "look at this"

    # A plain text message with no content maps to no preview.
    assert build_last_message_preview(None) is None


async def test_create_message_persists_media_columns(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 720, 820
    await _make_chat_with_sender(db_session, chat_id, user_id)

    # Act
    message = await create_message(
        db_session,
        message_id=90200,
        chat_id=chat_id,
        sender_id=user_id,
        type=2,
        media_key="ab/image/90200.jpg",
        media_mime="image/jpeg",
        media_size=12345,
        media_name="vacation.jpg",
        media_duration_seconds=None,
    )

    # Assert: every media part round-trips onto the row
    assert message.media_key == "ab/image/90200.jpg"
    assert message.media_mime == "image/jpeg"
    assert message.media_size == 12345
    assert message.media_name == "vacation.jpg"

    # ...and a caption-less media message sets the generic chat-list preview
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.last_message_preview == "\U0001F4F7 Photo"


async def test_create_message_persists_reply_to_message_id(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 721, 821
    await _make_chat_with_sender(db_session, chat_id, user_id)
    original = await create_message(db_session, message_id=90210, chat_id=chat_id, sender_id=user_id, content="original")

    # Act
    reply = await create_message(
        db_session, message_id=90211, chat_id=chat_id, sender_id=user_id,
        content="a reply", reply_to_message_id=original.id,
    )

    # Assert
    assert reply.reply_to_message_id == original.id


async def test_system_message_does_not_overwrite_last_message_preview(db_session: AsyncSession):
    # Arrange: a real user message sets the chat-list preview.
    chat_id, user_id = 722, 822
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=90220, chat_id=chat_id, sender_id=user_id, content="real message")

    # Act: a system message (no sender) lands afterwards
    sys_msg = await create_message(
        db_session, message_id=90221, chat_id=chat_id, sender_id=None, type=6, content="X joined the group",
    )

    # Assert: recency (id/at) advances to the system message...
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.last_message_id == sys_msg.id
    assert chat.last_message_at == sys_msg.created_at
    # ...but the preview line still shows the last real user message
    assert chat.last_message_preview == "real message"


async def test_compute_message_status_played_only_for_audio(db_session: AsyncSession):
    # Arrange: an audio message and a text message, both fully played/read by all.
    chat_id, sender_id, recipient_id = 723, 823, 824
    await _make_chat_with_sender(db_session, chat_id, sender_id)
    await create_user(db_session, user_id=recipient_id, phone_number=f"+97250{recipient_id}")
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=sender_id)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=recipient_id)

    text_msg = await create_message(db_session, message_id=90230, chat_id=chat_id, sender_id=sender_id, content="hi")
    audio_msg = await create_message(db_session, message_id=90231, chat_id=chat_id, sender_id=sender_id, type=4)

    from database.crud.crud_participant import update_last_played_message
    await update_last_delivered_message(db_session, chat_id=chat_id, user_id=recipient_id, message_id=audio_msg.id)
    await update_last_read_message(db_session, chat_id=chat_id, user_id=recipient_id, message_id=audio_msg.id)
    await update_last_played_message(db_session, chat_id=chat_id, user_id=recipient_id, message_id=audio_msg.id)

    chat = await get_chat_by_id(db_session, chat_id)

    # Assert: the audio message unlocks PLAYED when message_type is passed...
    assert compute_message_status(audio_msg.id, chat, message_type=4) == MessageStatus.PLAYED
    # ...but a non-audio message tops out at READ even with the played cursor past it
    assert compute_message_status(text_msg.id, chat, message_type=1) == MessageStatus.READ
    # ...and omitting message_type never yields PLAYED
    assert compute_message_status(audio_msg.id, chat) == MessageStatus.READ


async def test_count_unread_messages(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 724, 825
    await _make_chat_with_sender(db_session, chat_id, user_id)
    await create_message(db_session, message_id=_mid(240), chat_id=chat_id, sender_id=user_id, content="1")
    await create_message(db_session, message_id=_mid(241), chat_id=chat_id, sender_id=user_id, content="2")
    m3 = await create_message(db_session, message_id=_mid(242), chat_id=chat_id, sender_id=user_id, content="3")
    await create_message(db_session, message_id=_mid(243), chat_id=chat_id, sender_id=None, type=6, content="system")
    await create_message(db_session, message_id=_mid(244), chat_id=chat_id, sender_id=user_id, content="4")
    await soft_delete_message(db_session, chat_id=chat_id, message_id=_mid(244))

    # Act + Assert: NULL cursor counts every *real*, non-deleted message
    # (4 real messages, minus the soft-deleted one = 3; the system message never counts)
    assert await count_unread_messages(db_session, chat_id, None) == 3

    # Act + Assert: with a cursor, only messages strictly after it
    assert await count_unread_messages(db_session, chat_id, m3.id) == 0

    # Act + Assert: cursor before m3 -> just m3 (90243 system + 90244 deleted excluded)
    assert await count_unread_messages(db_session, chat_id, _mid(241)) == 1


async def test_edit_message_updates_preview_only_for_current_last_message(db_session: AsyncSession):
    # Arrange: two messages; the second is the chat's current last_message.
    chat_id, user_id = 725, 826
    await _make_chat_with_sender(db_session, chat_id, user_id)
    old_msg = await create_message(db_session, message_id=90250, chat_id=chat_id, sender_id=user_id, content="old")
    last_msg = await create_message(db_session, message_id=90251, chat_id=chat_id, sender_id=user_id, content="last")

    # Act: editing the *older* message must not touch the chat-list preview
    await edit_message_content(db_session, chat_id=chat_id, message_id=old_msg.id, new_content="old edited")
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.last_message_preview == "last"

    # Act: editing the current last message updates it
    await edit_message_content(db_session, chat_id=chat_id, message_id=last_msg.id, new_content="last edited")
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.last_message_preview == "last edited"


async def test_soft_delete_updates_preview_only_for_current_last_message(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 726, 827
    await _make_chat_with_sender(db_session, chat_id, user_id)
    old_msg = await create_message(db_session, message_id=90260, chat_id=chat_id, sender_id=user_id, content="old")
    last_msg = await create_message(db_session, message_id=90261, chat_id=chat_id, sender_id=user_id, content="last")

    # Act: deleting the older message leaves the preview alone
    await soft_delete_message(db_session, chat_id=chat_id, message_id=old_msg.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.last_message_preview == "last"

    # Act: deleting the current last message drops in the tombstone marker
    await soft_delete_message(db_session, chat_id=chat_id, message_id=last_msg.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert chat.last_message_preview == DELETED_MESSAGE_PREVIEW


async def test_compute_message_status(db_session: AsyncSession):
    # Arrange
    chat_id, sender_id, recipient_id = 712, 811, 812
    await _make_chat_with_sender(db_session, chat_id, sender_id)
    await create_user(db_session, user_id=recipient_id, phone_number=f"+97250{recipient_id}")
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=sender_id)
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=recipient_id)

    message = await create_message(db_session, message_id=90090, chat_id=chat_id, sender_id=sender_id, content="status check")

    # Assert: one grey check - sent, but the recipient hasn't gotten it yet
    chat = await get_chat_by_id(db_session, chat_id)
    assert compute_message_status(message.id, chat) == MessageStatus.SENT

    # Act + Assert: two grey checks once the recipient's device has it
    await update_last_delivered_message(db_session, chat_id=chat_id, user_id=recipient_id, message_id=message.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert compute_message_status(message.id, chat) == MessageStatus.DELIVERED

    # Act + Assert: two blue checks once the recipient has actually read it
    await update_last_read_message(db_session, chat_id=chat_id, user_id=recipient_id, message_id=message.id)
    chat = await get_chat_by_id(db_session, chat_id)
    assert compute_message_status(message.id, chat) == MessageStatus.READ


async def test_get_chat_messages_limit_is_capped(db_session: AsyncSession):
    # Arrange
    chat_id, user_id = 710, 809
    await _make_chat_with_sender(db_session, chat_id, user_id)
    for i in range(MAX_PAGE_SIZE + 10):
        await create_message(db_session, message_id=91000 + i, chat_id=chat_id, sender_id=user_id, content=str(i))

    # Act: ask for way more than the cap allows
    page = await get_chat_messages(db_session, chat_id=chat_id, limit=MAX_PAGE_SIZE + 10)

    # Assert: the server-side cap wins over whatever the caller requested
    assert len(page) == MAX_PAGE_SIZE


async def test_concurrent_create_same_message_id_is_not_deduplicated_by_the_db(session_factory):
    # Documents a deliberate, known limitation: unlike users/chats (single-column
    # PK), the messages PK is (id, created_at) because created_at is the partition
    # key. Two concurrent inserts with the same message_id get different
    # created_at values (microseconds apart), so the PK does NOT reject the
    # duplicate - uniqueness of message_id is trusted to the Snowflake generator,
    # not enforced by the database. If this ever starts failing, the DB started
    # rejecting duplicates and this test (and the assumption behind it) is stale.
    chat_id, user_id, message_id = 708, 807, 90060
    async with session_factory() as setup_session:
        await _make_chat_with_sender(setup_session, chat_id, user_id)

    async def attempt(content: str):
        async with session_factory() as session:
            return await create_message(session, message_id=message_id, chat_id=chat_id, sender_id=user_id, content=content)

    # Act
    results = await asyncio.gather(attempt("first"), attempt("second"))

    # Assert: both inserts go through - this is the accepted trade-off
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 2

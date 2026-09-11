"""crud + service coverage for message search (ADR 0040).

Query-level: FTS match / prefix / phrase, recency order, deleted/purged/system
excluded, cursor pagination, `messages_around`. Permission-level: the global
`participants` JOIN (only member chats, removed member drops out).
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message, soft_delete_message
from modules.messaging.errors import NotAParticipantError
from modules.search import service as search_service
from modules.search.errors import SearchQueryTooShortError
from infra.ids.snowflake import next_id

pytestmark = pytest.mark.asyncio

_MID_BASE = next_id() & ~0x3FFFFF


def _mid(n: int) -> int:
    return _MID_BASE + n


async def _user(session, uid):
    from modules.users.crud import create_user

    await create_user(session, user_id=uid, phone_number=f"+97250{uid}")


async def _chat(session, chat_id, *user_ids):
    await create_chat(session, chat_id=chat_id, is_group=True, title=f"c{chat_id}")
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)


async def _search_chat(session, uid, chat_id, q, cursor=None, limit=20):
    return await search_service.search_in_chat(
        session, user_id=uid, chat_id=chat_id, raw_query=q, cursor=cursor, limit=limit
    )


async def _search_global(session, uid, q, cursor=None, limit=20):
    return await search_service.search_global(
        session, user_id=uid, raw_query=q, cursor=cursor, limit=limit
    )


async def test_in_chat_search_matches_recency_first_with_snippet(db_session: AsyncSession):
    await _user(db_session, 801)
    await _chat(db_session, 9001, 801)
    await create_message(db_session, message_id=_mid(1), chat_id=9001, sender_id=801, content="the quick brown fox")
    await create_message(db_session, message_id=_mid(2), chat_id=9001, sender_id=801, content="nothing here")
    await create_message(db_session, message_id=_mid(3), chat_id=9001, sender_id=801, content="another quick note")

    resp = await _search_chat(db_session, 801, 9001, "quick")
    assert [r.id for r in resp.results] == [str(_mid(3)), str(_mid(1))]
    assert "quick" in resp.results[0].snippet.lower()
    assert resp.has_more is False


async def test_prefix_match_on_last_term(db_session: AsyncSession):
    await _user(db_session, 802)
    await _chat(db_session, 9002, 802)
    await create_message(db_session, message_id=_mid(10), chat_id=9002, sender_id=802, content="hello world")

    assert (await _search_chat(db_session, 802, 9002, "hel")).results
    assert (await _search_chat(db_session, 802, 9002, "hello wor")).results
    assert not (await _search_chat(db_session, 802, 9002, "xyz")).results


async def test_phrase_and_exclusion_query(db_session: AsyncSession):
    await _user(db_session, 803)
    await _chat(db_session, 9003, 803)
    await create_message(db_session, message_id=_mid(20), chat_id=9003, sender_id=803, content="pay the invoice today")
    await create_message(db_session, message_id=_mid(21), chat_id=9003, sender_id=803, content="invoice was cancelled")

    only_today = await _search_chat(db_session, 803, 9003, '"invoice today"')
    assert [r.id for r in only_today.results] == [str(_mid(20))]

    no_cancel = await _search_chat(db_session, 803, 9003, "invoice -cancelled")
    assert [r.id for r in no_cancel.results] == [str(_mid(20))]


async def test_deleted_purged_and_system_rows_are_excluded(db_session: AsyncSession):
    await _user(db_session, 804)
    await _chat(db_session, 9004, 804)
    await create_message(db_session, message_id=_mid(30), chat_id=9004, sender_id=804, content="keeper apple")
    await create_message(db_session, message_id=_mid(31), chat_id=9004, sender_id=804, content="deleted apple")
    await create_message(db_session, message_id=_mid(32), chat_id=9004, sender_id=None, type=6, content="system apple")
    await create_message(db_session, message_id=_mid(33), chat_id=9004, sender_id=804, content="purged apple")
    await soft_delete_message(db_session, chat_id=9004, message_id=_mid(31))
    await db_session.execute(
        text("UPDATE messages SET deleted_at = now(), purged_at = now() WHERE id = :i"), {"i": _mid(33)}
    )
    await db_session.commit()

    resp = await _search_chat(db_session, 804, 9004, "apple")
    assert [r.id for r in resp.results] == [str(_mid(30))]


async def test_non_participant_cannot_search_a_chat(db_session: AsyncSession):
    await _user(db_session, 805)
    await _user(db_session, 806)
    await _chat(db_session, 9005, 805)
    await create_message(db_session, message_id=_mid(40), chat_id=9005, sender_id=805, content="secret banana")

    with pytest.raises(NotAParticipantError):
        await _search_chat(db_session, 806, 9005, "banana")


async def test_global_search_only_returns_member_chats(db_session: AsyncSession):
    await _user(db_session, 807)
    await _user(db_session, 808)
    await _chat(db_session, 9006, 807, 808)
    await _chat(db_session, 9007, 808)  # 807 is NOT a member
    await create_message(db_session, message_id=_mid(50), chat_id=9006, sender_id=808, content="global cherry")
    await create_message(db_session, message_id=_mid(51), chat_id=9007, sender_id=808, content="global cherry")

    resp = await _search_global(db_session, 807, "cherry")
    assert [r.chat_id for r in resp.results] == [str(9006)]


async def test_removed_member_drops_out_of_global_search(db_session: AsyncSession):
    await _user(db_session, 809)
    await _chat(db_session, 9008, 809)
    await create_message(db_session, message_id=_mid(60), chat_id=9008, sender_id=809, content="melon report")
    assert (await _search_global(db_session, 809, "melon")).results

    await db_session.execute(
        text("DELETE FROM participants WHERE chat_id = 9008 AND user_id = 809")
    )
    await db_session.commit()
    assert not (await _search_global(db_session, 809, "melon")).results


async def test_cursor_pagination_walks_every_match_once(db_session: AsyncSession):
    await _user(db_session, 810)
    await _chat(db_session, 9009, 810)
    for n in range(5):
        await create_message(db_session, message_id=_mid(70 + n), chat_id=9009, sender_id=810, content=f"page grape {n}")

    seen, cursor = [], None
    for _ in range(10):
        resp = await _search_chat(db_session, 810, 9009, "grape", cursor=cursor, limit=2)
        seen.extend(r.id for r in resp.results)
        if not resp.has_more:
            break
        cursor = resp.next_cursor
    assert seen == [str(_mid(74 - n)) for n in range(5)]


async def test_query_too_short_raises_before_db(db_session: AsyncSession):
    with pytest.raises(SearchQueryTooShortError):
        await _search_chat(db_session, 1, 1, "a")
    with pytest.raises(SearchQueryTooShortError):
        await _search_chat(db_session, 1, 1, "   ")


async def test_messages_around_returns_context_window(db_session: AsyncSession):
    await _user(db_session, 811)
    await _chat(db_session, 9010, 811)
    for n in range(11):
        await create_message(db_session, message_id=_mid(80 + n), chat_id=9010, sender_id=811, content=f"m{n}")

    around = await search_service.messages_around(
        db_session, user_id=811, chat_id=9010, message_id=_mid(85), radius=2
    )
    assert [m.id for m in around] == [_mid(83), _mid(84), _mid(85), _mid(86), _mid(87)]

"""crud coverage for semantic vector search (ADR 0042) against a real
Postgres + pgvector instance: the cosine-distance query itself, the
participants-JOIN membership gate, the relevance floor, soft-delete/purge/
system-row exclusion, the optional chat_id scope, the start_at/end_at window,
and update_embeddings' write-back.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message
from modules.vector_search import crud as vector_crud
from infra.ids.snowflake import next_id

from tests.modules.vector_search._vectors import near, unit_vector

pytestmark = pytest.mark.asyncio

_MID_BASE = next_id() & ~0x3FFFFF


def _mid(n: int) -> int:
    return _MID_BASE + n


async def _user(session, uid):
    from modules.users.crud import create_user

    await create_user(session, user_id=uid, phone_number=f"+97260{uid}")


async def _chat(session, chat_id, *user_ids):
    await create_chat(session, chat_id=chat_id, is_group=True, title=f"c{chat_id}")
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)


async def _embed(session, message_id, vector):
    await vector_crud.update_embeddings(session, [(message_id, vector)])


async def test_nearest_match_ranks_above_unrelated_message(db_session: AsyncSession):
    await _user(db_session, 901)
    await _chat(db_session, 9101, 901)
    await create_message(db_session, message_id=_mid(1), chat_id=9101, sender_id=901, content="close one")
    await create_message(db_session, message_id=_mid(2), chat_id=9101, sender_id=901, content="far one")
    await _embed(db_session, _mid(1), near(0))
    await _embed(db_session, _mid(2), unit_vector(200))

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=901, query_embedding=unit_vector(0), limit=10, max_distance=0.5
    )
    assert [r["id"] for r in rows] == [_mid(1)]


async def test_max_distance_floor_excludes_unrelated_rows(db_session: AsyncSession):
    await _user(db_session, 902)
    await _chat(db_session, 9102, 902)
    await create_message(db_session, message_id=_mid(10), chat_id=9102, sender_id=902, content="close")
    await create_message(db_session, message_id=_mid(11), chat_id=9102, sender_id=902, content="orthogonal")
    await _embed(db_session, _mid(10), near(5))
    await _embed(db_session, _mid(11), unit_vector(300))  # cosine distance ~1.0 from unit_vector(5)

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=902, query_embedding=unit_vector(5), limit=10, max_distance=0.5
    )
    assert [r["id"] for r in rows] == [_mid(10)]


async def test_non_member_gets_no_results(db_session: AsyncSession):
    await _user(db_session, 903)
    await _user(db_session, 904)
    await _chat(db_session, 9103, 903)
    await create_message(db_session, message_id=_mid(20), chat_id=9103, sender_id=903, content="private")
    await _embed(db_session, _mid(20), unit_vector(10))

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=904, query_embedding=unit_vector(10), limit=10, max_distance=1.5
    )
    assert rows == []


async def test_removed_member_drops_out(db_session: AsyncSession):
    await _user(db_session, 905)
    await _chat(db_session, 9104, 905)
    await create_message(db_session, message_id=_mid(30), chat_id=9104, sender_id=905, content="note")
    await _embed(db_session, _mid(30), unit_vector(20))

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=905, query_embedding=unit_vector(20), limit=10, max_distance=1.5
    )
    assert [r["id"] for r in rows] == [_mid(30)]

    await db_session.execute(
        text("DELETE FROM participants WHERE chat_id = 9104 AND user_id = 905")
    )
    await db_session.commit()

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=905, query_embedding=unit_vector(20), limit=10, max_distance=1.5
    )
    assert rows == []


async def test_deleted_purged_and_system_rows_excluded(db_session: AsyncSession):
    await _user(db_session, 906)
    await _chat(db_session, 9105, 906)
    await create_message(db_session, message_id=_mid(40), chat_id=9105, sender_id=906, content="keeper")
    await create_message(db_session, message_id=_mid(41), chat_id=9105, sender_id=906, content="deleted")
    await create_message(db_session, message_id=_mid(42), chat_id=9105, sender_id=None, type=6, content="system")
    await create_message(db_session, message_id=_mid(43), chat_id=9105, sender_id=906, content="purged")
    for n in (40, 41, 42, 43):
        await _embed(db_session, _mid(n), unit_vector(30))

    await db_session.execute(text("UPDATE messages SET deleted_at = now() WHERE id = :i"), {"i": _mid(41)})
    await db_session.execute(text("UPDATE messages SET purged_at = now() WHERE id = :i"), {"i": _mid(43)})
    await db_session.commit()

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=906, query_embedding=unit_vector(30), limit=10, max_distance=1.5
    )
    assert [r["id"] for r in rows] == [_mid(40)]


async def test_messages_without_embedding_are_skipped(db_session: AsyncSession):
    await _user(db_session, 907)
    await _chat(db_session, 9106, 907)
    await create_message(db_session, message_id=_mid(50), chat_id=9106, sender_id=907, content="no embedding yet")

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=907, query_embedding=unit_vector(40), limit=10, max_distance=1.5
    )
    assert rows == []


async def test_chat_id_scopes_to_one_chat(db_session: AsyncSession):
    await _user(db_session, 908)
    await _chat(db_session, 9107, 908)
    await _chat(db_session, 9108, 908)
    await create_message(db_session, message_id=_mid(60), chat_id=9107, sender_id=908, content="in scope")
    await create_message(db_session, message_id=_mid(61), chat_id=9108, sender_id=908, content="other chat")
    await _embed(db_session, _mid(60), unit_vector(50))
    await _embed(db_session, _mid(61), unit_vector(50))

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=908, query_embedding=unit_vector(50), limit=10, max_distance=1.5, chat_id=9107
    )
    assert [r["id"] for r in rows] == [_mid(60)]


async def test_chat_id_still_enforces_membership(db_session: AsyncSession):
    await _user(db_session, 909)
    await _user(db_session, 910)
    await _chat(db_session, 9109, 910)  # 909 is not a member
    await create_message(db_session, message_id=_mid(70), chat_id=9109, sender_id=910, content="not yours")
    await _embed(db_session, _mid(70), unit_vector(60))

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=909, query_embedding=unit_vector(60), limit=10, max_distance=1.5, chat_id=9109
    )
    assert rows == []


async def test_start_at_end_at_window(db_session: AsyncSession):
    await _user(db_session, 911)
    await _chat(db_session, 9110, 911)
    await create_message(db_session, message_id=_mid(80), chat_id=9110, sender_id=911, content="old")
    await create_message(db_session, message_id=_mid(81), chat_id=9110, sender_id=911, content="new")
    await _embed(db_session, _mid(80), unit_vector(70))
    await _embed(db_session, _mid(81), unit_vector(70))
    await db_session.execute(
        text("UPDATE messages SET created_at = now() - interval '10 days' WHERE id = :i"), {"i": _mid(80)}
    )
    await db_session.commit()

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=911, query_embedding=unit_vector(70), limit=10, max_distance=1.5, start_at=cutoff
    )
    assert [r["id"] for r in rows] == [_mid(81)]

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=911, query_embedding=unit_vector(70), limit=10, max_distance=1.5, end_at=cutoff
    )
    assert [r["id"] for r in rows] == [_mid(80)]


async def test_limit_caps_result_count(db_session: AsyncSession):
    await _user(db_session, 912)
    await _chat(db_session, 9111, 912)
    for n in range(5):
        mid = _mid(90 + n)
        await create_message(db_session, message_id=mid, chat_id=9111, sender_id=912, content=f"m{n}")
        await _embed(db_session, mid, unit_vector(80))

    rows = await vector_crud.semantic_search_messages(
        db_session, user_id=912, query_embedding=unit_vector(80), limit=2, max_distance=1.5
    )
    assert len(rows) == 2


async def test_update_embeddings_noop_on_empty_rows(db_session: AsyncSession):
    # Must not raise / must not touch the DB when there is nothing to write.
    await vector_crud.update_embeddings(db_session, [])

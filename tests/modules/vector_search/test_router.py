"""HTTP surface for semantic vector search (ADR 0042): GET /search/semantic.

Covers auth, the 422 min-length gate, rate limiting, limit clamping, and the
error->status-code mapping for the embedding-provider failures (main.py's
exception handlers), with Gemini itself monkeypatched.
"""
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

import main as main_module
from modules.auth import service as auth_service
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message
from modules.vector_search import crud as vector_crud, gemini_client, query_cache
from modules.vector_search.errors import EmbeddingProviderError, EmbeddingProviderQuotaExceededError
from modules.vector_search.limits import VectorSearchLimits
from modules.vector_search.router import get_vector_search_limits
from infra.ids.snowflake import next_id

from tests.modules.vector_search._vectors import unit_vector

pytestmark = pytest.mark.asyncio

_MID_BASE = next_id() & ~0x3FFFFF


def _mid(n: int) -> int:
    return _MID_BASE + n


@pytest_asyncio.fixture(autouse=True)
async def _clear_overrides():
    yield
    main_module.app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
def _reset_query_cache():
    from cachetools import TTLCache

    query_cache._cache = TTLCache(maxsize=query_cache._MAXSIZE, ttl=query_cache._TTL_SECONDS)
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client, redis_db, phone: str):
    resp = await client.post("/auth/otp/request", json={"phone_number": phone})
    assert resp.status_code == 204
    code = await redis_db.get(auth_service._otp_key(phone))
    resp = await client.post("/auth/otp/verify", json={"phone_number": phone, "code": code})
    assert resp.status_code == 200
    body = resp.json()
    return body["user"], body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _embed_query_returning(vector):
    async def _fn(text):
        return vector

    return _fn


async def _seed_message(db_session, chat_id, uid, mid, content, vector):
    await create_chat(db_session, chat_id=chat_id, is_group=True, title=f"c{chat_id}")
    await add_participant_to_chat(db_session, chat_id=chat_id, user_id=uid)
    await create_message(db_session, message_id=mid, chat_id=chat_id, sender_id=uid, content=content)
    await vector_crud.update_embeddings(db_session, [(mid, vector)])


async def test_requires_auth(client):
    resp = await client.get("/search/semantic?q=hello")
    assert resp.status_code in (401, 403)


async def test_query_too_short_is_422(client, db_session: AsyncSession, redis_db):
    _, token = await _login(client, redis_db, "+972500400001")
    resp = await client.get("/search/semantic?q=a", headers=_auth(token))
    assert resp.status_code == 422


async def test_successful_search_round_trip(client, db_session: AsyncSession, redis_db, monkeypatch):
    user, token = await _login(client, redis_db, "+972500400002")
    uid = int(user["id"])
    await _seed_message(db_session, 5101, uid, _mid(1), "matching content", unit_vector(0))

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _embed_query_returning(unit_vector(0)))

    resp = await client.get("/search/semantic?q=matching", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body["results"]] == [str(_mid(1))]


async def test_limit_is_clamped_to_max(client, db_session: AsyncSession, redis_db, monkeypatch):
    user, token = await _login(client, redis_db, "+972500400003")
    uid = int(user["id"])
    await create_chat(db_session, chat_id=5102, is_group=True, title="c5102")
    await add_participant_to_chat(db_session, chat_id=5102, user_id=uid)
    for n in range(5):
        mid = _mid(10 + n)
        await create_message(db_session, message_id=mid, chat_id=5102, sender_id=uid, content=f"m{n}")
        await vector_crud.update_embeddings(db_session, [(mid, unit_vector(5))])

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _embed_query_returning(unit_vector(5)))
    main_module.app.dependency_overrides[get_vector_search_limits] = lambda: VectorSearchLimits(
        min_query_len=2, max_query_len=512, default_limit=10, max_limit=2,
        max_distance=1.5, max_distance_expanded=1.9, rate_max=99, rate_window_s=60,
        queue_flush_size=50, gemini_batch_size=90,
    )

    resp = await client.get("/search/semantic?q=matching&limit=999", headers=_auth(token))
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2


async def test_rate_limit_returns_429(client, db_session: AsyncSession, redis_db, monkeypatch):
    _, token = await _login(client, redis_db, "+972500400004")
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _embed_query_returning(unit_vector(0)))
    main_module.app.dependency_overrides[get_vector_search_limits] = lambda: VectorSearchLimits(
        min_query_len=2, max_query_len=512, default_limit=10, max_limit=30,
        max_distance=1.5, max_distance_expanded=1.9, rate_max=1, rate_window_s=60,
        queue_flush_size=50, gemini_batch_size=90,
    )

    first = await client.get("/search/semantic?q=hello", headers=_auth(token))
    second = await client.get("/search/semantic?q=hello", headers=_auth(token))
    assert first.status_code == 200
    assert second.status_code == 429


async def test_embedding_provider_error_maps_to_502(client, db_session: AsyncSession, redis_db, monkeypatch):
    _, token = await _login(client, redis_db, "+972500400005")

    async def _fail(text):
        raise EmbeddingProviderError("boom")

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fail)
    resp = await client.get("/search/semantic?q=hello", headers=_auth(token))
    assert resp.status_code == 502


async def test_quota_exceeded_maps_to_503(client, db_session: AsyncSession, redis_db, monkeypatch):
    _, token = await _login(client, redis_db, "+972500400006")

    async def _quota(text):
        raise EmbeddingProviderQuotaExceededError("quota")

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _quota)
    resp = await client.get("/search/semantic?q=hello", headers=_auth(token))
    assert resp.status_code == 503
    assert resp.json().get("reason") == "embedding_quota_exceeded"


async def test_chat_id_scopes_and_still_enforces_membership(client, db_session: AsyncSession, redis_db, monkeypatch):
    owner, owner_token = await _login(client, redis_db, "+972500400007")
    outsider, outsider_token = await _login(client, redis_db, "+972500400008")
    await _seed_message(db_session, 5103, int(owner["id"]), _mid(20), "owner only", unit_vector(9))

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _embed_query_returning(unit_vector(9)))

    resp = await client.get("/search/semantic?q=owner&chat_id=5103", headers=_auth(outsider_token))
    assert resp.status_code == 200
    assert resp.json()["results"] == []

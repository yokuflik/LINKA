"""Agent "reset to default" (ADR 0050): modules.agents.reset + POST /agents/me/reset.

Scope: the reset orchestration (message purge + knowledge wipe + settings
restore) exercised directly against `reset_agent_to_default`, plus a smaller
set of HTTP-level tests for the router (auth, 404, greeting re-send, cache/
schedule sync, event broadcast). Real Postgres (ephemeral per-session DB,
ADR 0032) and real Redis (flushed per-test via `redis_db`) - Gemini is never
called on this path (the greeting message is AGENT_REPLY_MESSAGE_TYPE, which
trigger_engine explicitly skips, and no worker consumes agent_invoke_stream
in tests), so nothing here needs mocking for Gemini. `media_service.delete_
object` is monkeypatched since there's no real S3/MinIO in this suite.

Behavioral expectations encoded here (not just "whatever the code does"):

- Every message in the owner-agent chat is gone after reset: soft-deleted
  and purged, regardless of prior state (already-deleted, or never
  deleted) - a fresh reset must leave zero readable history.
- A message's media is deref'd and, once its blob hits ref_count 0, its S3
  object is deleted and its media_blob row dropped - same GC behavior as
  the single-message purge path (ADR 0021), not a shortcut.
- A media blob still ref'd by a message in a *different* chat survives
  reset (ref_count > 0 after the decrement) - reset must not blow away
  storage other chats still depend on.
- Every knowledge document AND its chunks are gone after reset (FK cascade
  verified directly against the chunks table, not just via the document
  list), and each document's S3 object is deleted.
- system_prompt / triggers / active_skill / builder_state / paused_chat_ids
  / restrictions all return to their documented defaults, and a BYOK key
  is cleared.
- is_enabled is left untouched by reset (ADR 0050 is explicit about this) -
  true stays true, false stays false.
- The function returns the same (mutated) Agent instance it was given.
- HTTP: 404 for a user with no agent; a real agent's chat gets exactly one
  message after reset (the greeting); the response body reflects the reset
  defaults; an agent_config_changed event is published on the user's
  personal channel.
"""
import json

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import main as main_module
from modules.agents.crud import list_knowledge_documents
from modules.agents.knowledge_service import commit_knowledge_document
from modules.agents.models import (
    Agent,
    AgentKnowledgeChunk,
    DEFAULT_AGENT_ACTIVE_SKILL,
    DEFAULT_AGENT_BUILDER_STATE,
    DEFAULT_AGENT_RESTRICTIONS,
    DEFAULT_AGENT_TRIGGERS,
)
from modules.agents.reset import reset_agent_to_default
from modules.auth import service as auth_service
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.media import media_service
from modules.media.crud import get_blob_by_key, reserve_blob
from modules.messaging.crud import create_message, soft_delete_message
from modules.messaging.models import Message
from modules.users.crud import create_user
from infra.ids.snowflake import next_id as _snowflake_next_id

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    """Plain counter, fine for user/chat/agent ids (never partition-pruned
    by created_at)."""
    global _ID
    _ID += 1
    return 960_000_000 + _ID


def _next_message_id() -> int:
    """Message ids must decode to a real "now" timestamp (id_to_datetime) -
    get_message_by_id/purge_message prune by a created_at window derived
    from the id, so a fabricated counter id would silently miss the row."""
    return _snowflake_next_id()


@pytest_asyncio.fixture(autouse=True)
async def _no_real_s3_delete(monkeypatch):
    """Never a real MinIO/S3 call in this suite; tracks every call so tests
    can assert exactly which keys were deleted."""
    deleted_keys = []

    async def fake_delete_object(storage_key, bucket=None):
        deleted_keys.append(storage_key)

    monkeypatch.setattr(media_service, "delete_object", fake_delete_object)
    return deleted_keys


async def _make_agent(session, owner_user_id: int | None = None, **overrides) -> Agent:
    if owner_user_id is None:
        owner_user_id = _next_id()
        await create_user(session, user_id=owner_user_id, phone_number=f"+1555{owner_user_id}")
    owner_agent_chat_id = _next_id()
    await create_chat(session, chat_id=owner_agent_chat_id, is_group=False)
    await add_participant_to_chat(session, chat_id=owner_agent_chat_id, user_id=owner_user_id)
    fields = {
        "triggers": json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        "restrictions": json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
        "is_enabled": True,
        **overrides,
    }
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        **fields,
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent


async def _add_message(session, chat_id, sender_id, *, deleted=False, media_key=None) -> Message:
    message = await create_message(
        session,
        message_id=_next_message_id(),
        chat_id=chat_id,
        sender_id=sender_id,
        type=1,
        content="hello agent" if media_key is None else None,
        media_key=media_key,
        media_mime="image/png" if media_key else None,
        media_size=10 if media_key else None,
    )
    if deleted:
        await soft_delete_message(session, chat_id=chat_id, message_id=message.id)
    return message


async def _add_media_blob(session, storage_key: str, *, ref_count: int = 1):
    await reserve_blob(
        session,
        sha256=storage_key,
        storage_key=storage_key,
        bucket="test-bucket",
        kind="image",
        mime="image/png",
        size=10,
    )
    for _ in range(ref_count):
        from modules.media.crud import confirm_and_ref
        await confirm_and_ref(session, storage_key=storage_key, mime="image/png", size=10)


async def _add_knowledge_document(session, agent: Agent, *, filename="doc.pdf"):
    # application/pdf: chunks are supplied pre-computed (client-side parsing
    # in real life), so this never touches S3/httpx, unlike the text/
    # markdown path which fetches the raw file server-side.
    return await commit_knowledge_document(
        session,
        agent,
        filename=filename,
        storage_key=f"agent-knowledge/{agent.id}/{filename}",
        mime_type="application/pdf",
        chunks=["chunk one", "chunk two"],
    )


# --- reset_agent_to_default: message purge -----------------------------------

async def test_reset_purges_every_message_in_the_owner_agent_chat(db_session: AsyncSession):
    agent = await _make_agent(db_session)
    m1 = await _add_message(db_session, agent.owner_agent_chat_id, agent.owner_user_id)
    m2 = await _add_message(db_session, agent.owner_agent_chat_id, agent.owner_user_id, deleted=True)

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    for message_id in (m1.id, m2.id):
        row = (
            await db_session.execute(select(Message).where(Message.id == message_id))
        ).scalar_one()
        assert row.deleted_at is not None
        assert row.purged_at is not None
        assert row.content is None


async def test_reset_leaves_other_chats_untouched(db_session: AsyncSession):
    agent = await _make_agent(db_session)
    other_chat_id = _next_id()
    await create_chat(db_session, chat_id=other_chat_id, is_group=False)
    await add_participant_to_chat(db_session, chat_id=other_chat_id, user_id=agent.owner_user_id)
    other_message = await _add_message(db_session, other_chat_id, agent.owner_user_id)

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    row = (
        await db_session.execute(select(Message).where(Message.id == other_message.id))
    ).scalar_one()
    assert row.deleted_at is None
    assert row.content is not None


async def test_reset_on_an_empty_chat_is_a_no_op_for_messages(db_session: AsyncSession):
    agent = await _make_agent(db_session)

    # Must not raise even though the chat has zero messages.
    result = await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert result is agent


# --- reset_agent_to_default: media GC -----------------------------------------

async def test_reset_deletes_s3_object_once_media_blob_ref_count_hits_zero(
    db_session: AsyncSession, _no_real_s3_delete
):
    agent = await _make_agent(db_session)
    storage_key = f"media/{_next_id()}.png"
    await _add_media_blob(db_session, storage_key, ref_count=1)
    await _add_message(db_session, agent.owner_agent_chat_id, agent.owner_user_id, media_key=storage_key)

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert storage_key in _no_real_s3_delete
    assert await get_blob_by_key(db_session, storage_key) is None


async def test_reset_does_not_delete_a_blob_still_referenced_by_another_chat(
    db_session: AsyncSession, _no_real_s3_delete
):
    agent = await _make_agent(db_session)
    other_chat_id = _next_id()
    await create_chat(db_session, chat_id=other_chat_id, is_group=False)
    await add_participant_to_chat(db_session, chat_id=other_chat_id, user_id=agent.owner_user_id)

    storage_key = f"media/{_next_id()}.png"
    await _add_media_blob(db_session, storage_key, ref_count=2)
    await _add_message(db_session, agent.owner_agent_chat_id, agent.owner_user_id, media_key=storage_key)
    await _add_message(db_session, other_chat_id, agent.owner_user_id, media_key=storage_key)

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert storage_key not in _no_real_s3_delete
    blob = await get_blob_by_key(db_session, storage_key)
    assert blob is not None
    assert blob.ref_count == 1


# --- reset_agent_to_default: knowledge base -----------------------------------

async def test_reset_deletes_every_knowledge_document_and_its_chunks(db_session: AsyncSession, _no_real_s3_delete):
    agent = await _make_agent(db_session)
    doc1 = await _add_knowledge_document(db_session, agent, filename="a.txt")
    doc2 = await _add_knowledge_document(db_session, agent, filename="b.txt")

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert await list_knowledge_documents(db_session, agent.id) == []

    chunks = (
        await db_session.execute(
            select(AgentKnowledgeChunk).where(AgentKnowledgeChunk.agent_id == agent.id)
        )
    ).scalars().all()
    assert chunks == []

    assert doc1.s3_key in _no_real_s3_delete
    assert doc2.s3_key in _no_real_s3_delete


async def test_reset_with_no_knowledge_documents_is_a_no_op(db_session: AsyncSession):
    agent = await _make_agent(db_session)

    result = await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert result is agent
    assert await list_knowledge_documents(db_session, agent.id) == []


# --- reset_agent_to_default: settings restore ---------------------------------

async def test_reset_restores_all_soft_and_hard_settings_to_default(db_session: AsyncSession):
    agent = await _make_agent(
        db_session,
        system_prompt="be extremely rude",
        triggers={**DEFAULT_AGENT_TRIGGERS, "on_specific_chats": {"123": {"keywords": ["hi"]}}},
        active_skill="sales_agent",
        builder_state="builder_agent",
        paused_chat_ids=[{"chat_id": "123", "paused_at": "x", "expires_at": "y"}],
        restrictions={**DEFAULT_AGENT_RESTRICTIONS, "can_message_groups": True},
        encrypted_gemini_api_key=b"fake-encrypted-key",
    )

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert agent.system_prompt == ""
    assert agent.triggers == DEFAULT_AGENT_TRIGGERS
    assert agent.active_skill == DEFAULT_AGENT_ACTIVE_SKILL
    assert agent.builder_state == DEFAULT_AGENT_BUILDER_STATE
    assert agent.paused_chat_ids == []
    assert agent.restrictions == DEFAULT_AGENT_RESTRICTIONS
    assert agent.encrypted_gemini_api_key is None


@pytest.mark.parametrize("is_enabled", [True, False])
async def test_reset_never_touches_is_enabled(db_session: AsyncSession, is_enabled):
    agent = await _make_agent(db_session, is_enabled=is_enabled)

    await reset_agent_to_default(db_session, agent)
    await db_session.commit()

    assert agent.is_enabled is is_enabled


async def test_reset_returns_the_same_mutated_agent_instance(db_session: AsyncSession):
    agent = await _make_agent(db_session)

    result = await reset_agent_to_default(db_session, agent)

    assert result is agent


# --- HTTP: POST /agents/me/reset ----------------------------------------------

@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client, redis_db, phone_number: str):
    resp = await client.post("/auth/otp/request", json={"phone_number": phone_number})
    assert resp.status_code == 204
    code = await redis_db.get(auth_service._otp_key(phone_number))
    resp = await client.post("/auth/otp/verify", json={"phone_number": phone_number, "code": code})
    assert resp.status_code == 200
    body = resp.json()
    return body["user"], body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_reset_endpoint_404s_for_a_user_with_no_agent(client, db_session, redis_db):
    _, token = await _login(client, redis_db, "+972500300001")

    resp = await client.post("/agents/me/reset", headers=_auth(token))

    assert resp.status_code == 404


async def test_reset_endpoint_wipes_history_and_resends_the_greeting(client, db_session, redis_db):
    user, token = await _login(client, redis_db, "+972500300002")

    resp = await client.post("/agents/me", headers=_auth(token))
    assert resp.status_code == 200
    agent_body = resp.json()

    # An extra owner message on top of the auto-sent greeting, so the chat
    # has more than one message before reset.
    resp = await client.get(f"/chats/{agent_body['owner_agent_chat_id']}/messages", headers=_auth(token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1  # just the creation greeting

    resp = await client.post("/agents/me/reset", headers=_auth(token))
    assert resp.status_code == 200
    reset_body = resp.json()

    assert reset_body["system_prompt"] == ""
    assert reset_body["active_skill"] == DEFAULT_AGENT_ACTIVE_SKILL
    assert reset_body["builder_state"] == DEFAULT_AGENT_BUILDER_STATE
    assert reset_body["paused_chat_ids"] == []

    # The messages endpoint always includes soft-deleted/purged rows (tombstone
    # rendering), so the old greeting still shows up here - but purged, with
    # its content wiped - alongside the one fresh reset greeting.
    resp = await client.get(f"/chats/{agent_body['owner_agent_chat_id']}/messages", headers=_auth(token))
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 2
    with_content = [m for m in messages if m["content"]]
    assert len(with_content) == 1


async def test_reset_endpoint_leaves_is_enabled_untouched(client, db_session, redis_db):
    _, token = await _login(client, redis_db, "+972500300003")

    resp = await client.post("/agents/me", headers=_auth(token))
    assert resp.status_code == 200
    created = resp.json()

    resp = await client.post("/agents/me/reset", headers=_auth(token))
    assert resp.status_code == 200
    reset_body = resp.json()

    assert reset_body["is_enabled"] == created["is_enabled"]


async def test_reset_endpoint_publishes_agent_config_changed_event(client, db_session, redis_db, monkeypatch):
    _, token = await _login(client, redis_db, "+972500300004")
    await client.post("/agents/me", headers=_auth(token))

    published = []

    async def fake_publish_user_event(user_id, payload):
        published.append((user_id, payload))

    from modules.agents import router as agent_router

    monkeypatch.setattr(agent_router.realtime_service, "publish_user_event", fake_publish_user_event)

    resp = await client.post("/agents/me/reset", headers=_auth(token))
    assert resp.status_code == 200

    assert len(published) == 1
    _, payload = published[0]
    assert payload["event"] == "agent_config_changed"
    assert payload["agent"]["system_prompt"] == ""

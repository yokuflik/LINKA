"""Agent "reset to default" orchestration (ADR 0050): wipes the owner-agent
chat's message history, drops the knowledge base, and restores every soft +
hard setting to its default. Irreversible."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.crud import list_knowledge_documents
from modules.agents.knowledge_service import delete_knowledge_document
from modules.agents.models import (
    Agent,
    DEFAULT_AGENT_ACTIVE_SKILL,
    DEFAULT_AGENT_BUILDER_STATE,
    DEFAULT_AGENT_RESTRICTIONS,
    DEFAULT_AGENT_TRIGGERS,
)
from modules.media.crud import delete_blob_row, deref_blob
from modules.media import media_service
from modules.messaging.crud import purge_message as crud_purge_message
from modules.messaging.crud import soft_delete_message
from modules.messaging.models import Message

logger = logging.getLogger(__name__)


async def _purge_all_chat_messages(session: AsyncSession, chat_id: int) -> None:
    """Hard-deletes every message in `chat_id`, regardless of sender or
    current soft-delete state. Unlike `edit_delete.purge_message` (ADR 0021)
    this is not sender-scoped and doesn't require a prior soft-delete - it
    soft-deletes (if needed) then purges each row. Media is deref'd and its
    S3 object removed on last ref, same as the single-message path."""
    stmt = select(Message.id, Message.deleted_at).where(Message.chat_id == chat_id)
    rows = (await session.execute(stmt)).all()

    for message_id, deleted_at in rows:
        if deleted_at is None:
            await soft_delete_message(session, chat_id=chat_id, message_id=message_id)

        media_key = await crud_purge_message(session, chat_id=chat_id, message_id=message_id)
        if not media_key:
            continue

        try:
            remaining = await deref_blob(session, media_key)
            if remaining == 0:
                try:
                    await media_service.delete_object(media_key)
                except Exception as exc:
                    logger.error("agent reset: failed to delete S3 object %s: %s", media_key, exc)
                await delete_blob_row(session, media_key)
        except Exception as exc:
            logger.error("agent reset: blob deref failed for %s: %s", media_key, exc)


async def _delete_all_knowledge(session: AsyncSession, agent: Agent) -> None:
    for document in await list_knowledge_documents(session, agent.id):
        await delete_knowledge_document(session, agent, document.id)


async def reset_agent_to_default(session: AsyncSession, agent: Agent) -> Agent:
    """ADR 0050: wipes owner-agent chat history, deletes the knowledge base,
    and restores every soft + hard setting to its default. `is_enabled` is
    left untouched on purpose - resetting config shouldn't silently re-arm a
    disabled agent."""
    await _purge_all_chat_messages(session, agent.owner_agent_chat_id)
    await _delete_all_knowledge(session, agent)

    agent.system_prompt = ""
    agent.triggers = dict(DEFAULT_AGENT_TRIGGERS)
    agent.active_skill = DEFAULT_AGENT_ACTIVE_SKILL
    agent.builder_state = DEFAULT_AGENT_BUILDER_STATE
    agent.paused_chat_ids = []
    agent.encrypted_gemini_api_key = None
    agent.restrictions = dict(DEFAULT_AGENT_RESTRICTIONS)

    await session.flush()
    return agent

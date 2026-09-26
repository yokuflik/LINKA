"""Agent config API (ADR 0045, frontend step 5): GET/create/PATCH the
caller's own agent. No listing/admin endpoints - one agent per user, always
addressed as "mine"."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user_id
from infra.db.connection import get_db
from infra.ids.client import next_id
from typing import List

from modules.agents import crud as agent_crud
from modules.agents import knowledge_service
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import KnowledgeQuotaExceededError, ScheduleQuotaExceededError, resume_agent_chat
from modules.agents.crypto import ByokKeyError, encrypt_api_key
from modules.agents.knowledge_service import KnowledgeValidationError
from modules.agents.models import Agent
from modules.agents.reset import reset_agent_to_default
from modules.media.errors import MediaValidationError
from modules.agents.schedule import sync_schedule_zset
from modules.agents.schemas import AgentConfigPatchIn
from modules.agents.schemas import (
    AgentKnowledgeCommitIn,
    AgentKnowledgeDocumentOut,
    AgentKnowledgeUploadTicketIn,
    AgentKnowledgeUploadTicketOut,
)
from modules.agents.schemas import AgentOut, AgentTokenWindowOut, AgentUsageOut
from modules.agents.token_budget import peek_usage
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.chats.common import ROLE_MEMBER
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from realtime import realtime_service

_GREETING_TEXT = "שלום, אני סוכן ה-AI שלך. מה תרצה שאעשה היום?"

router = APIRouter(prefix="/agents", tags=["agents"])


async def _get_my_agent_or_404(session: AsyncSession, user_id: int) -> Agent:
    agent = await agent_crud.get_agent_by_owner(session, user_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No agent yet")
    return agent


def _agent_out(agent: Agent) -> AgentOut:
    """AgentOut.has_custom_key is derived, not a DB column - never echoes the
    key itself (ADR 0046 decision 5). paused_chat_ids on the row is now a
    list of {chat_id, paused_at, expires_at} objects (ADR 0054); the API
    still exposes a plain list of active chat_ids, so it's projected down
    before validation."""
    from modules.agents.crud import _active_pauses

    fields = {name: getattr(agent, name) for name in AgentOut.model_fields if hasattr(agent, name)}
    fields["paused_chat_ids"] = [entry["chat_id"] for entry in _active_pauses(agent)]
    out = AgentOut.model_validate(fields)
    out.has_custom_key = agent.encrypted_gemini_api_key is not None
    return out


@router.get("/me", response_model=AgentOut)
async def get_my_agent(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    return _agent_out(await _get_my_agent_or_404(session, user_id))


@router.post("/me", response_model=AgentOut)
async def create_my_agent(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Idempotent: returns the existing agent if the caller already has one
    (mirrors get_or_create_private_chat's shape) rather than 409ing, since
    the config UI just wants "make sure I have one" semantics."""
    existing = await agent_crud.get_agent_by_owner(session, user_id)
    if existing is not None:
        return _agent_out(existing)

    # Owner-agent chat: a permanent 1:1-shaped chat with the owner as its
    # only participant (the agent has no user_id of its own - it always acts
    # as the owner, ADR 0045). Created eagerly, not lazily, since the daily
    # time-budget notification depends on it existing.
    chat = await create_chat(session, chat_id=await next_id(), is_group=False)
    await add_participant_to_chat(session, chat_id=chat.id, user_id=user_id, role=ROLE_MEMBER)

    agent = Agent(id=await next_id(), owner_user_id=user_id, owner_agent_chat_id=chat.id)
    session.add(agent)
    await session.commit()
    await sync_agent_cache(agent)

    # Opening greeting: a real, persisted message (not a client-side-only
    # placeholder) so it survives reload / shows up on any device. Sent the
    # same way any agent reply is (process_outgoing, AGENT_REPLY_MESSAGE_TYPE)
    # so it's indistinguishable from a normal turn. process_outgoing
    # self-commits, so this runs after the chat/agent transaction above, not
    # inside it.
    await message_service.process_outgoing(
        session,
        sender_id=user_id,
        chat_id=chat.id,
        client_message_id=f"agent-greeting-{agent.id}",
        content=_GREETING_TEXT,
        type=AGENT_REPLY_MESSAGE_TYPE,
    )

    return _agent_out(agent)


@router.patch("/me", response_model=AgentOut)
async def patch_my_agent(
    body: AgentConfigPatchIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    agent = await _get_my_agent_or_404(session, user_id)

    patch = body.model_dump(exclude_unset=True)
    # Nested restrictions/triggers patches only carry the sub-fields the
    # client actually set (Optional[...] = None default) - drop the None
    # placeholders before the shallow-merge in update_agent_config, or a
    # deliberately-untouched field would get clobbered to null.
    if patch.get("restrictions") is not None:
        patch["restrictions"] = {k: v for k, v in patch["restrictions"].items() if v is not None}
    if patch.get("triggers") is not None:
        patch["triggers"] = {k: v for k, v in patch["triggers"].items() if v is not None}

    # BYOK (ADR 0046 decision 5): handled separately from update_agent_config
    # since it's not a JSONB shallow-merge field - "" or null clears the key
    # (falls back to the shared GEMINI_API_KEY), a non-empty string encrypts
    # and replaces it, and the key is simply absent from `patch` if the
    # client didn't send it at all (leaves the stored key untouched).
    gemini_key_patch = patch.pop("gemini_api_key", "__unset__")
    if gemini_key_patch != "__unset__":
        if gemini_key_patch:
            try:
                agent.encrypted_gemini_api_key = encrypt_api_key(gemini_key_patch)
            except ByokKeyError as exc:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
        else:
            agent.encrypted_gemini_api_key = None

    try:
        updated = await agent_crud.update_agent_config(session, agent, patch)
    except ScheduleQuotaExceededError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    # update_agent_config only flushes (shared with update_own_triggers,
    # which intentionally lets its caller batch further writes) - this
    # request's writes (including the BYOK key set above) end here, so
    # commit now rather than relying on get_db() (which never auto-commits).
    await session.commit()
    await sync_agent_cache(updated)
    if "triggers" in patch and "on_schedule" in patch["triggers"]:
        await sync_schedule_zset(updated)

    out = _agent_out(updated)
    # Cross-tab/cross-device sync (AGENT_DRAWER_UI_PLAN.md Wave 2): a second
    # open tab/device otherwise only learns of this PATCH on its next GET
    # /agents/me. Same personal-channel pattern as chat_pin_changed/
    # chat_mute_changed. The acting tab gets this echo too - applying the
    # same state it just received is a no-op (see
    # useAgentConfig.applyAgentConfigChanged's dirty-field guard).
    await realtime_service.publish_user_event(
        user_id,
        {"event": "agent_config_changed", "agent": out.model_dump(mode="json")},
    )
    return out


@router.post("/me/reset", response_model=AgentOut)
async def reset_my_agent(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """ADR 0050: irreversibly wipes owner-agent chat history + knowledge base
    and restores every soft/hard setting to default. Confirmation is a
    frontend-only concern (window.confirm) - this endpoint executes
    unconditionally once called."""
    agent = await _get_my_agent_or_404(session, user_id)
    agent = await reset_agent_to_default(session, agent)
    await session.commit()
    await sync_agent_cache(agent)
    await sync_schedule_zset(agent)

    # Re-send the opening greeting so the now-empty chat isn't blank, same
    # path as agent creation (a real persisted message, not a client-only
    # placeholder). Runs after the reset transaction commits, like the
    # greeting on creation.
    await message_service.process_outgoing(
        session,
        sender_id=user_id,
        chat_id=agent.owner_agent_chat_id,
        client_message_id=f"agent-reset-greeting-{await next_id()}",
        content=_GREETING_TEXT,
        type=AGENT_REPLY_MESSAGE_TYPE,
    )

    out = _agent_out(agent)
    await realtime_service.publish_user_event(
        user_id,
        {"event": "agent_config_changed", "agent": out.model_dump(mode="json")},
    )
    return out


# --- Token usage (ADR 0059) --------------------------------------------------

@router.get("/me/usage", response_model=AgentUsageOut)
async def get_my_agent_usage(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Read-only, no side effect - feeds the drawer's usage progress bars."""
    agent = await _get_my_agent_or_404(session, user_id)
    usage = await peek_usage(agent.id)
    return AgentUsageOut(
        window_5h=AgentTokenWindowOut(
            used=usage["5h"].used,
            limit=usage["5h"].limit,
            percent=usage["5h"].percent,
            resets_in_seconds=usage["5h"].resets_in_seconds,
            is_blocked=usage["5h"].is_blocked,
        ),
        window_7d=AgentTokenWindowOut(
            used=usage["7d"].used,
            limit=usage["7d"].limit,
            percent=usage["7d"].percent,
            resets_in_seconds=usage["7d"].resets_in_seconds,
            is_blocked=usage["7d"].is_blocked,
        ),
    )


# --- Knowledge base (ADR 0046 decision 4) ------------------------------------

@router.post("/me/knowledge/upload-ticket", response_model=AgentKnowledgeUploadTicketOut)
async def create_knowledge_upload_ticket(
    body: AgentKnowledgeUploadTicketIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    await _get_my_agent_or_404(session, user_id)
    try:
        ticket = knowledge_service.create_knowledge_upload_ticket(body.mime_type, body.size_bytes)
    except (KnowledgeValidationError, MediaValidationError) as exc:
        # 400, not 422 - a client-fixable bad request (unknown mime/oversize),
        # same convention as the message-media upload-ticket endpoint.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return AgentKnowledgeUploadTicketOut(
        storage_key=ticket.storage_key,
        upload_url=ticket.upload_url,
        required_headers=ticket.required_headers,
        expires_in=ticket.expires_in,
    )


@router.post("/me/knowledge", response_model=AgentKnowledgeDocumentOut)
async def commit_knowledge_document(
    body: AgentKnowledgeCommitIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    agent = await _get_my_agent_or_404(session, user_id)
    try:
        document = await knowledge_service.commit_knowledge_document(
            session,
            agent,
            filename=body.filename,
            storage_key=body.storage_key,
            mime_type=body.mime_type,
            chunks=body.chunks,
        )
    except KnowledgeValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except KnowledgeQuotaExceededError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    # commit_knowledge_document only flushes - get_db() never auto-commits.
    await session.commit()
    return document


@router.get("/me/knowledge", response_model=List[AgentKnowledgeDocumentOut])
async def list_knowledge_documents(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    agent = await _get_my_agent_or_404(session, user_id)
    return await agent_crud.list_knowledge_documents(session, agent.id)


@router.delete("/me/knowledge/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_document(
    document_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    agent = await _get_my_agent_or_404(session, user_id)
    deleted = await knowledge_service.delete_knowledge_document(session, agent, document_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document")
    # delete_knowledge_document only flushes - get_db() never auto-commits.
    await session.commit()


# --- Escalation (ADR 0047 decision 5) ---------------------------------------

@router.post("/me/resume-chat/{chat_id}", response_model=AgentOut)
async def resume_chat(
    chat_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Human-only un-pause: clears chat_id from paused_chat_ids so the agent
    can be woken there again. An agent has no tool that can call this itself
    (pause_and_escalate is one-way from the agent's side, per the ADR)."""
    agent = await _get_my_agent_or_404(session, user_id)
    updated = await resume_agent_chat(session, agent, chat_id)
    await session.commit()
    await sync_agent_cache(updated)
    return _agent_out(updated)

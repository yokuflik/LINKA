from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from routers.dependencies import get_current_user_id
from routers.schemas import (
    AddMemberIn,
    AvatarCommitIn,
    AvatarUploadTicketIn,
    AvatarUploadTicketOut,
    ChangeRoleIn,
    ChatListItemOut,
    ChatMemberOut,
    ChatOut,
    CreateGroupChatIn,
    CreatePrivateChatIn,
    ParticipantOut,
    UpdateGroupDetailsIn,
)
from services import avatar_service, chat_service

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("", response_model=list[ChatListItemOut])
async def list_my_chats(
    limit: int = 30,
    before_last_message_at: Optional[datetime] = None,
    before_chat_id: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    before = (before_last_message_at, before_chat_id) if before_last_message_at and before_chat_id else None
    participants = await chat_service.get_chat_list(session, user_id, before=before, limit=limit)
    return [
        ChatListItemOut(
            chat=p.chat, role=p.role, last_read_message_id=p.last_read_message_id, unread_count=p.unread_count
        )
        for p in participants
    ]


@router.post("/private", response_model=ChatOut)
async def create_private_chat(
    body: CreatePrivateChatIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    return await chat_service.get_or_create_private_chat(session, user_id, body.other_user_id)


@router.post("/groups/avatar/upload-ticket", response_model=AvatarUploadTicketOut)
async def create_new_group_avatar_upload_ticket(
    body: AvatarUploadTicketIn,
    user_id: int = Depends(get_current_user_id),
):
    """Presigned PUT for a photo to attach to a group that doesn't exist yet
    (the group-creation form). No role check - the returned key is inert until
    it's committed, which POST /chats/groups does (validating it first)."""
    ticket = avatar_service.request_upload(body.mime_type, body.size_bytes)
    return AvatarUploadTicketOut(
        storage_key=ticket.storage_key,
        upload_url=ticket.upload_url,
        required_headers=ticket.required_headers,
        expires_in=ticket.expires_in,
    )


@router.post("/groups", response_model=ChatOut)
async def create_group_chat(
    body: CreateGroupChatIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    return await chat_service.create_group_chat(
        session,
        creator_id=user_id,
        title=body.title,
        initial_member_ids=body.initial_member_ids,
        about_text=body.about_text,
        avatar_storage_key=body.avatar_storage_key,
    )


@router.patch("/{chat_id}", response_model=ChatOut)
async def update_group_details(
    chat_id: int,
    body: UpdateGroupDetailsIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    chat = await chat_service.update_group_details(
        session, actor_id=user_id, chat_id=chat_id, title=body.title, about_text=body.about_text
    )
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


@router.post("/{chat_id}/avatar/upload-ticket", response_model=AvatarUploadTicketOut)
async def create_group_avatar_upload_ticket(
    chat_id: int,
    body: AvatarUploadTicketIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Step 1: presigned PUT the client uploads the group photo directly to.
    Requires the caller to be an admin/owner of the group."""
    await chat_service.ensure_can_manage_details(session, actor_id=user_id, chat_id=chat_id)
    ticket = avatar_service.request_upload(body.mime_type, body.size_bytes)
    return AvatarUploadTicketOut(
        storage_key=ticket.storage_key,
        upload_url=ticket.upload_url,
        required_headers=ticket.required_headers,
        expires_in=ticket.expires_in,
    )


@router.put("/{chat_id}/avatar", response_model=ChatOut)
async def set_group_avatar(
    chat_id: int,
    body: AvatarCommitIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Step 2: commit the uploaded object as the group's photo."""
    chat = await chat_service.set_group_avatar(session, actor_id=user_id, chat_id=chat_id, storage_key=body.storage_key)
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


@router.delete("/{chat_id}/avatar", response_model=ChatOut)
async def delete_group_avatar(
    chat_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    chat = await chat_service.clear_group_avatar(session, actor_id=user_id, chat_id=chat_id)
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


@router.get("/{chat_id}/members", response_model=list[ChatMemberOut])
async def get_chat_members(
    chat_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    participants = await chat_service.get_chat_members(session, requester_id=user_id, chat_id=chat_id)
    return [ChatMemberOut(user=p.user, role=p.role) for p in participants]


@router.post("/{chat_id}/members", response_model=ParticipantOut)
async def add_member(
    chat_id: int,
    body: AddMemberIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    participant = await chat_service.add_member(session, actor_id=user_id, chat_id=chat_id, new_user_id=body.user_id)
    if participant is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member, or the chat doesn't exist")
    return participant


@router.delete("/{chat_id}/members/{target_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    chat_id: int,
    target_user_id: int,
    new_owner_id: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    # new_owner_id only matters when the owner is leaving on their own
    # (user_id == target_user_id) and the group still has other members -
    # see chat_service.remove_member for the full rule (including the
    # "nobody left to inherit it" -> delete-the-chat case).
    removed = await chat_service.remove_member(
        session, actor_id=user_id, chat_id=chat_id, target_user_id=target_user_id, new_owner_id=new_owner_id
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")


@router.patch("/{chat_id}/members/{target_user_id}", response_model=ParticipantOut)
async def change_member_role(
    chat_id: int,
    target_user_id: int,
    body: ChangeRoleIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    participant = await chat_service.change_member_role(
        session, actor_id=user_id, chat_id=chat_id, target_user_id=target_user_id, new_role=body.role
    )
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
    return participant

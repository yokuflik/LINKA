from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from routers.dependencies import get_current_user_id
from routers.schemas import MessageOut
from services import message_service

router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["messages"])


@router.get("", response_model=list[MessageOut])
async def get_message_history(
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    return await message_service.get_message_history(session, user_id=user_id, chat_id=chat_id, before_id=before_id, limit=limit)

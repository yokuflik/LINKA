"""
Apply one collapsed receipt-stream entry (ADR 0037): advance the coarse
watermark and, if it moved, decide the live event to publish.

Called only by `receipt_log.drain_once`. The detailed `message_receipt_log`
row and the actual `publish_event` are the worker's job — this module just
does the DB watermark write and the ADR 0003 privacy decision.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.chats.crud.crud_participant import update_last_delivered_message
from modules.chats.crud.crud_participant import update_last_played_message
from modules.chats.crud.crud_participant import update_last_read_message
from modules.messaging.crud import get_message_by_id
from modules.messaging.models import AUDIO_MESSAGE_TYPE
from modules.messaging.receipt_privacy import reader_hides_read_receipts

logger = logging.getLogger(__name__)

# kind -> (watermark updater, wire event name, is it privacy-gated)
_KINDS = {
    settings.RECEIPT_KIND_DELIVERED: (update_last_delivered_message, "delivery_receipt", False),
    settings.RECEIPT_KIND_READ: (update_last_read_message, "read_receipt", True),
    settings.RECEIPT_KIND_PLAYED: (update_last_played_message, "played_receipt", True),
}


@dataclass
class ApplyOutcome:
    """The watermark advanced. `event` is the payload to publish, or None if
    the ADR 0003 privacy gate suppressed it (the detailed-log row is still
    written by the caller either way)."""
    event: dict | None


async def apply_receipt(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    kind: int,
    up_to_message_id: int,
    occurred_at: datetime,
) -> ApplyOutcome | None:
    spec = _KINDS.get(kind)
    if spec is None:
        logger.warning("receipt apply: unknown kind %s (chat=%s user=%s)", kind, chat_id, user_id)
        return None
    update_fn, event_name, privacy_gated = spec

    # A played watermark may only ever be moved by a listen to an actual voice
    # message — protects Chat.all_played_up_to_message_id.
    if kind == settings.RECEIPT_KIND_PLAYED:
        message = await get_message_by_id(session, chat_id=chat_id, message_id=up_to_message_id)
        if message is None or message.type != AUDIO_MESSAGE_TYPE:
            return None

    participant = await update_fn(
        session, chat_id=chat_id, user_id=user_id, message_id=up_to_message_id, occurred_at=occurred_at
    )
    if participant is None:
        return None  # watermark already at/past this message — nothing happened

    if privacy_gated and await reader_hides_read_receipts(session, chat_id, reader_id=user_id):
        return ApplyOutcome(event=None)

    return ApplyOutcome(
        event={
            "event": event_name,
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "message_id": str(up_to_message_id),
            "occurred_at": occurred_at.isoformat(),
        }
    )

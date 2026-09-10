"""
Facade for the messaging domain. The implementation was split by responsibility
into services/messaging/ (see that package's __init__). This module re-exports
the public API so main.py, routers/*, chat_service, and the fan-out workers keep
importing from one stable place - no behavior change.
"""

# ADR 0033: size/limit caps are injected via modules.messaging.limits
# (MessagingLimits / ScheduledLimits), not read off this facade module.
from modules.messaging.common import SYSTEM_MESSAGE_TYPE
from modules.messaging.common import _check_content_length  # noqa: F401
from modules.messaging.errors import MessageAlreadySentError
from modules.messaging.errors import MessageNotFoundError
from modules.messaging.errors import MessageTooLongError
from modules.messaging.errors import NotAParticipantError
from modules.messaging.errors import NotAVoiceMessageError
from modules.messaging.errors import ScheduledLimitExceededError
from modules.messaging.errors import ScheduledMessageNotFoundError
from modules.messaging.errors import ScheduledTimeInvalidError
from modules.messaging.media_validation import MediaAttachment
from modules.messaging.media_validation import _validate_media  # noqa: F401
from modules.messaging.send import fan_out_message
from modules.messaging.send import process_outgoing
from modules.messaging.send import send_system_message
from modules.messaging.edit_delete import delete_message
from modules.messaging.edit_delete import edit_message
from modules.messaging.edit_delete import purge_message
from modules.messaging.edit_delete import restore_message
from modules.messaging.scheduled_service import cancel_scheduled
from modules.messaging.scheduled_service import list_scheduled
from modules.messaging.scheduled_service import reschedule
from modules.messaging.scheduled_service import schedule_message
from modules.messaging.read_api import get_message_history  # noqa: F401
from modules.messaging.receipts import get_message_receipts
from modules.messaging.receipts import mark_as_delivered
from modules.messaging.receipts import mark_as_played
from modules.messaging.receipts import mark_as_read

__all__ = [
    "SYSTEM_MESSAGE_TYPE",
    "MediaAttachment",
    "MessageAlreadySentError",
    "MessageNotFoundError",
    "MessageTooLongError",
    "NotAParticipantError",
    "NotAVoiceMessageError",
    "ScheduledLimitExceededError",
    "ScheduledMessageNotFoundError",
    "ScheduledTimeInvalidError",
    "cancel_scheduled",
    "list_scheduled",
    "reschedule",
    "schedule_message",
    "delete_message",
    "edit_message",
    "fan_out_message",
    "get_message_history",
    "get_message_receipts",
    "restore_message",
    "mark_as_delivered",
    "mark_as_played",
    "mark_as_read",
    "process_outgoing",
    "send_system_message",
]

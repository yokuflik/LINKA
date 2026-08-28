"""
Facade for the messaging domain. The implementation was split by responsibility
into services/messaging/ (see that package's __init__). This module re-exports
the public API so main.py, routers/*, chat_service, and the fan-out workers keep
importing from one stable place - no behavior change.
"""

# Config values kept importable here because tests monkeypatch them on this
# module (message_service.MAX_MESSAGE_CONTENT_LENGTH / RECEIPT_NAMED_LIST_MAX_MEMBERS);
# the messaging submodules read them back off this module at call time.
from config import MAX_MESSAGE_CONTENT_LENGTH, RECEIPT_NAMED_LIST_MAX_MEMBERS  # noqa: F401

from services.messaging.common import SYSTEM_MESSAGE_TYPE, _check_content_length  # noqa: F401
from services.messaging.errors import (  # noqa: F401
    MessageAlreadySentError,
    MessageNotFoundError,
    MessageTooLongError,
    NotAParticipantError,
    NotAVoiceMessageError,
)
from services.messaging.media_validation import MediaAttachment, _validate_media  # noqa: F401
from services.messaging.send import (  # noqa: F401
    fan_out_message,
    process_outgoing,
    send_system_message,
)
from services.messaging.edit_delete import delete_message, edit_message  # noqa: F401
from services.messaging.read_api import get_message_history  # noqa: F401
from services.messaging.receipts import (  # noqa: F401
    get_message_receipts,
    mark_as_delivered,
    mark_as_played,
    mark_as_read,
)

__all__ = [
    "SYSTEM_MESSAGE_TYPE",
    "MediaAttachment",
    "MessageAlreadySentError",
    "MessageNotFoundError",
    "MessageTooLongError",
    "NotAParticipantError",
    "NotAVoiceMessageError",
    "delete_message",
    "edit_message",
    "fan_out_message",
    "get_message_history",
    "get_message_receipts",
    "mark_as_delivered",
    "mark_as_played",
    "mark_as_read",
    "process_outgoing",
    "send_system_message",
]

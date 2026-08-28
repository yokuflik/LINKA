"""Small shared helpers for the messaging modules."""

from typing import Optional

from services.messaging.errors import MessageTooLongError

# System messages ("X joined the group", etc.) have no sender
SYSTEM_MESSAGE_TYPE = 6


def _check_content_length(content: Optional[str]) -> None:
    # Read the limit off the facade module at call time so a test that does
    # monkeypatch.setattr(message_service, "MAX_MESSAGE_CONTENT_LENGTH", ...)
    # still takes effect after the split into services/messaging/.
    from services import message_service

    limit = message_service.MAX_MESSAGE_CONTENT_LENGTH
    if content is not None and len(content) > limit:
        raise MessageTooLongError(f"Message content exceeds {limit} characters")

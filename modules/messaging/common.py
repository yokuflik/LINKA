"""Small shared helpers for the messaging modules."""

from typing import Optional

from modules.messaging.errors import MessageTooLongError

# System messages ("X joined the group", etc.) have no sender
SYSTEM_MESSAGE_TYPE = 6


def _check_content_length(content: Optional[str], max_length: Optional[int] = None) -> None:
    # ADR 0033: callers thread the cap in from their injected Limits object.
    # `max_length=None` falls back to the default so any not-yet-migrated
    # caller keeps working.
    if max_length is None:
        from modules.messaging.limits import DEFAULT_MESSAGING_LIMITS

        max_length = DEFAULT_MESSAGING_LIMITS.max_message_content_length
    if content is not None and len(content) > max_length:
        raise MessageTooLongError(f"Message content exceeds {max_length} characters")

"""Exception types shared across the messaging flow."""


class MessageTooLongError(Exception):
    pass


class NotAParticipantError(Exception):
    pass


class EncryptionRequiredError(Exception):
    """Raised by edit_message when a plaintext edit targets an already-
    encrypted message (ADR 0027) - blocks a silent downgrade to plaintext."""
    pass


class NotAVoiceMessageError(Exception):
    """Raised by mark_as_played for a missing message or a non-audio one."""
    pass


class MessageNotFoundError(Exception):
    """Raised by get_message_receipts when the message doesn't exist."""
    pass


class ScheduledTimeInvalidError(Exception):
    """`scheduled_for` is outside [now + MIN_LEAD, now + MAX_LEAD] (ADR 0031)."""
    pass


class ScheduledLimitExceededError(Exception):
    """The user already has SCHEDULED_MAX_PENDING_PER_USER pending rows (ADR 0031)."""
    pass


class ScheduledMessageNotFoundError(Exception):
    """No pending scheduled message with this id owned by the caller (ADR 0031)."""
    pass


class MessageAlreadySentError(Exception):
    """
    Raised by process_outgoing when the idempotency key already holds a real
    message id - a duplicate stream entry for a client_message_id that was
    already written. Carries the existing id so the worker can nudge the
    sender's client to reconcile its optimistic bubble.
    """

    def __init__(self, message_id: int):
        super().__init__(f"message {message_id} already sent")
        self.message_id = message_id

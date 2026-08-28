"""Exception types shared across the messaging flow."""


class MessageTooLongError(Exception):
    pass


class NotAParticipantError(Exception):
    pass


class NotAVoiceMessageError(Exception):
    """Raised by mark_as_played for a missing message or a non-audio one."""
    pass


class MessageNotFoundError(Exception):
    """Raised by get_message_receipts when the message doesn't exist."""
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

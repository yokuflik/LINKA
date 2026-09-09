"""Injectable messaging / scheduled-message tunables (ADR 0033).

`MessagingLimits` and `ScheduledLimits` are the single place these caps are
named. Service functions take `limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS`;
the router exposes `get_messaging_limits` / `get_scheduled_limits` as FastAPI
dependencies for `app.dependency_overrides`. Service-unit tests pass
`limits=MessagingLimits(...)` directly.
"""

from dataclasses import dataclass

from config import settings


@dataclass(frozen=True)
class MessagingLimits:
    # Size caps enforced in the service.
    max_message_content_length: int = settings.MAX_MESSAGE_CONTENT_LENGTH
    receipt_named_list_max_members: int = settings.RECEIPT_NAMED_LIST_MAX_MEMBERS
    # Page-size clamp + per-user rate buckets enforced in the router.
    msg_history_max_limit: int = settings.MSG_HISTORY_MAX_LIMIT
    msg_history_rate_max: int = settings.MSG_HISTORY_RATE_MAX
    msg_history_rate_window_s: int = settings.MSG_HISTORY_RATE_WINDOW_SECONDS
    detail_read_rate_max: int = settings.DETAIL_READ_RATE_MAX
    detail_read_rate_window_s: int = settings.DETAIL_READ_RATE_WINDOW_SECONDS
    upload_ticket_rate_max: int = settings.UPLOAD_TICKET_RATE_MAX
    upload_ticket_rate_window_s: int = settings.UPLOAD_TICKET_RATE_WINDOW_SECONDS
    upload_ticket_ip_rate_max: int = settings.UPLOAD_TICKET_IP_RATE_LIMIT_MAX
    upload_ticket_ip_rate_window_s: int = settings.UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS


@dataclass(frozen=True)
class ScheduledLimits:
    max_pending_per_user: int = settings.SCHEDULED_MAX_PENDING_PER_USER
    min_lead_seconds: int = settings.SCHEDULED_MIN_LEAD_SECONDS
    max_lead_days: int = settings.SCHEDULED_MAX_LEAD_DAYS
    write_rate_max: int = settings.SCHEDULED_WRITE_RATE_MAX
    write_rate_window_s: int = settings.SCHEDULED_WRITE_RATE_WINDOW_SECONDS
    # Scheduled create/reschedule also runs the shared content-length check.
    max_message_content_length: int = settings.MAX_MESSAGE_CONTENT_LENGTH


DEFAULT_MESSAGING_LIMITS = MessagingLimits()
DEFAULT_SCHEDULED_LIMITS = ScheduledLimits()

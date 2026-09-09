"""
Facade for the chat domain. The implementation was split by responsibility
into services/chats/ (see that package's __init__). This module re-exports the
public API so routers/*, other services, the fan-out workers and the tests keep
importing from one stable place - no behaviour change.
"""

# ADR 0033: the initial-group-member cap is injected via
# modules.chats.limits.ChatLimits, not read off this facade module.

# Kept importable here because services/chats/*.py reference
# `chat_service.realtime_service.publish_*` at call time - tests monkeypatch
# `chat_service.realtime_service.publish_event` (see test_chat_service.py).
from realtime import realtime_service  # noqa: F401

from modules.chats.common import ROLE_ADMIN
from modules.chats.common import ROLE_MEMBER
from modules.chats.common import ROLE_OWNER
from modules.chats.common import _display_name_for
from modules.chats.common import _require_role
from modules.chats.errors import OwnershipTransferRequiredError
from modules.chats.errors import PermissionDeniedError
from modules.chats.errors import TooManyMembersError
from modules.chats.errors import UserNotFoundError
from modules.chats.notifications import _broadcast_chat_update
from modules.chats.notifications import _notify_added_to_chat
from modules.chats.notifications import _notify_removed_from_chat
from modules.chats.creation import create_group_chat
from modules.chats.creation import get_or_create_private_chat  # noqa: F401
from modules.chats.listing import get_chat_list
from modules.chats.listing import get_chat_members  # noqa: F401
from modules.chats.preferences import set_chat_muted
from modules.chats.preferences import set_chat_pinned  # noqa: F401
from modules.chats.key_bundle import get_chat_key_bundle
from modules.chats.group_details import clear_group_avatar
from modules.chats.group_details import ensure_can_manage_details
from modules.chats.group_details import set_group_avatar
from modules.chats.group_details import update_group_details
from modules.chats.membership import add_member
from modules.chats.membership import change_member_role
from modules.chats.membership import remove_member  # noqa: F401

__all__ = [
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "ROLE_OWNER",
    "OwnershipTransferRequiredError",
    "PermissionDeniedError",
    "TooManyMembersError",
    "UserNotFoundError",
    "add_member",
    "change_member_role",
    "clear_group_avatar",
    "create_group_chat",
    "ensure_can_manage_details",
    "get_chat_key_bundle",
    "get_chat_list",
    "get_chat_members",
    "get_or_create_private_chat",
    "remove_member",
    "set_chat_muted",
    "set_chat_pinned",
    "set_group_avatar",
    "update_group_details",
]

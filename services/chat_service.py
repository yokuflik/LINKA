"""
Facade for the chat domain. The implementation was split by responsibility
into services/chats/ (see that package's __init__). This module re-exports the
public API so routers/*, other services, the fan-out workers and the tests keep
importing from one stable place - no behaviour change.
"""

# Config value kept importable here because tests monkeypatch it on this module
# (chat_service.MAX_INITIAL_GROUP_MEMBERS); services/chats/creation.py reads it
# back off this module at call time.
from config import MAX_INITIAL_GROUP_MEMBERS  # noqa: F401

# Kept importable here because services/chats/*.py reference
# `chat_service.realtime_service.publish_*` at call time - tests monkeypatch
# `chat_service.realtime_service.publish_event` (see test_chat_service.py).
from services import realtime_service  # noqa: F401

from services.chats.common import (  # noqa: F401
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    _display_name_for,
    _require_role,
)
from services.chats.errors import (  # noqa: F401
    OwnershipTransferRequiredError,
    PermissionDeniedError,
    TooManyMembersError,
    UserNotFoundError,
)
from services.chats.notifications import (  # noqa: F401
    _broadcast_chat_update,
    _notify_added_to_chat,
    _notify_removed_from_chat,
)
from services.chats.creation import create_group_chat, get_or_create_private_chat  # noqa: F401
from services.chats.listing import get_chat_list, get_chat_members  # noqa: F401
from services.chats.preferences import set_chat_muted, set_chat_pinned  # noqa: F401
from services.chats.group_details import (  # noqa: F401
    clear_group_avatar,
    ensure_can_manage_details,
    set_group_avatar,
    update_group_details,
)
from services.chats.membership import add_member, change_member_role, remove_member  # noqa: F401

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
    "get_chat_list",
    "get_chat_members",
    "get_or_create_private_chat",
    "remove_member",
    "set_chat_muted",
    "set_chat_pinned",
    "set_group_avatar",
    "update_group_details",
]

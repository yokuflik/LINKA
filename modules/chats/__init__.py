"""
Chat domain, split from the old monolithic services/chat_service.py by
responsibility. services.chat_service stays as a thin facade re-exporting the
public API so routers / other services / tests keep importing from one place -
no behaviour change.

Modules:
- errors        - the exception types raised across the chat/group flows
- common        - ROLE_* constants, _require_role, _display_name_for
- notifications - personal-channel + chat-scoped live nudges
                  (_notify_added_to_chat, _notify_removed_from_chat,
                  _broadcast_chat_update)
- creation      - get_or_create_private_chat, create_group_chat
- listing       - get_chat_list, get_chat_members
- preferences   - set_chat_pinned, set_chat_muted (per-user list prefs)
- group_details - update_group_details, ensure_can_manage_details,
                  set_group_avatar, clear_group_avatar
- membership    - add_member, remove_member, change_member_role
"""

"""
The presence-visibility rule, used by the internal endpoints the Rust
`ws_gateway` calls on `subscribe_presence` and every typing frame
(`realtime/internal_router.py`, ADR 0036).

Whether `watcher_id` may see `target_user_id`'s presence — both the live online
indicator AND "last seen", gated together by the target's `privacy.online`:
  - `nobody`   -> never.
  - `contacts` -> only if a PrivateChatPair between the two exists.
  - `everyone` (default) -> any authenticated user.
"""
from modules.chats.crud.crud_private_chat_pair import get_pair_chat_id
from modules.settings import service as settings_service


async def presence_authorized(session, watcher_id: int, target_user_id: int) -> bool:
    visibility = await settings_service.get_online_visibility(session, target_user_id)
    if visibility == "nobody":
        return False
    if visibility == "contacts":
        return await get_pair_chat_id(session, watcher_id, target_user_id) is not None
    return True

"""Injectable chat tunables (ADR 0033).

`ChatLimits` is the single place the chat caps are named. `create_group_chat`
takes `limits: ChatLimits = DEFAULT_CHAT_LIMITS`; the router exposes
`get_chat_limits` as a FastAPI dependency for `app.dependency_overrides`, and
service-unit tests pass `limits=ChatLimits(...)` directly.
"""

from dataclasses import dataclass

from config import settings


@dataclass(frozen=True)
class ChatLimits:
    max_initial_group_members: int = settings.MAX_INITIAL_GROUP_MEMBERS


DEFAULT_CHAT_LIMITS = ChatLimits()

import asyncio
import logging

from fastapi import WebSocket

from services import realtime_service

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Holds this process's local WebSocket state - nothing here is shared
    across instances (that's Redis's job, via presence_service and
    realtime_service). Two responsibilities live here together because
    they're tightly coupled: which local sockets belong to which user
    (multi-device), and which chat-event channels this instance actually
    needs a Redis subscription for.

    Channel subscriptions are ref-counted per chat_id by *connection*, not by
    user: two different local connections both caring about the same chat
    (two devices, or two different users in the same chat) must keep that
    chat's subscription alive until every one of them has disconnected.
    """

    def __init__(self):
        self._sockets_by_connection: dict[str, WebSocket] = {}
        self._user_by_connection: dict[str, int] = {}
        self._connections_by_user: dict[int, set[str]] = {}

        # chat_id -> connection_ids currently interested in it
        self._chat_subscribers: dict[int, set[str]] = {}
        # connection_id -> chat_ids it's interested in (needed to clean up on disconnect)
        self._chats_by_connection: dict[str, set[int]] = {}
        # chat_id -> the background task consuming realtime_service.subscribe_to_chat(chat_id)
        self._chat_listener_tasks: dict[int, asyncio.Task] = {}

    async def connect(self, user_id: int, connection_id: str, websocket: WebSocket, chat_ids: list[int]) -> None:
        self._sockets_by_connection[connection_id] = websocket
        self._user_by_connection[connection_id] = user_id
        self._connections_by_user.setdefault(user_id, set()).add(connection_id)
        self._chats_by_connection[connection_id] = set(chat_ids)

        for chat_id in chat_ids:
            await self._subscribe_connection_to_chat(connection_id, chat_id)

    async def disconnect(self, connection_id: str) -> None:
        user_id = self._user_by_connection.pop(connection_id, None)
        self._sockets_by_connection.pop(connection_id, None)

        if user_id is not None:
            connections = self._connections_by_user.get(user_id)
            if connections is not None:
                connections.discard(connection_id)
                if not connections:
                    del self._connections_by_user[user_id]

        for chat_id in self._chats_by_connection.pop(connection_id, set()):
            await self._unsubscribe_connection_from_chat(connection_id, chat_id)

    def get_local_user_ids(self) -> set[int]:
        """Every user with at least one connection to *this* process right now."""
        return set(self._connections_by_user.keys())

    async def _subscribe_connection_to_chat(self, connection_id: str, chat_id: int) -> None:
        subscribers = self._chat_subscribers.setdefault(chat_id, set())
        is_first_subscriber = len(subscribers) == 0
        subscribers.add(connection_id)

        if is_first_subscriber:
            self._chat_listener_tasks[chat_id] = asyncio.create_task(self._listen_to_chat(chat_id))

    async def _unsubscribe_connection_from_chat(self, connection_id: str, chat_id: int) -> None:
        subscribers = self._chat_subscribers.get(chat_id)
        if subscribers is None:
            return

        subscribers.discard(connection_id)
        if not subscribers:
            del self._chat_subscribers[chat_id]
            task = self._chat_listener_tasks.pop(chat_id, None)
            if task is not None:
                task.cancel()

    async def _listen_to_chat(self, chat_id: int) -> None:
        """
        One task per actively-subscribed chat, forwarding every event
        published to it to this chat's locally-connected sockets. Cancelled
        (by _unsubscribe_connection_from_chat) once the last local connection
        interested in this chat disconnects.
        """
        agen = realtime_service.subscribe_to_chat(chat_id)
        try:
            async for event in agen:
                await self._broadcast_to_chat(chat_id, event)
        except asyncio.CancelledError:
            pass
        finally:
            await agen.aclose()

    async def _broadcast_to_chat(self, chat_id: int, event: dict) -> None:
        dead_connections = []

        for connection_id in self._chat_subscribers.get(chat_id, set()):
            websocket = self._sockets_by_connection.get(connection_id)
            if websocket is None:
                continue
            try:
                await websocket.send_json(event)
            except Exception as e:
                logger.info(f"Dropping dead connection {connection_id}: {e}")
                dead_connections.append(connection_id)

        for connection_id in dead_connections:
            await self.disconnect(connection_id)


# One shared instance per process - the WebSocket route and the connection
# manager must agree on the same local state.
connection_manager = ConnectionManager()

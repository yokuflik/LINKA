import asyncio
import logging
from typing import Awaitable, Callable

from fastapi import WebSocket

from config import SERVER_ID
from services import realtime_service
from services.fanout import routing

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Holds this process's local WebSocket state - nothing here is shared
    across instances (that's Redis's job, via presence_service and
    realtime_service). Three responsibilities live here together because
    they're tightly coupled:
      - which local sockets belong to which user (multi-device)
      - which chats this instance currently serves (ref-counted per chat_id
        by *connection*); this drives the Redis routing table so the fan-out
        worker publishes a chat's events only to the processes that have a
        local member - see services/fanout/routing.py
      - each user's personal channel, used to react to a chat being created
        *after* their connection was already established - see connect()
        and _handle_user_channel_event() for why this exists at all.
    """

    def __init__(self):
        self._sockets_by_connection: dict[str, WebSocket] = {}
        self._user_by_connection: dict[str, int] = {}
        self._connections_by_user: dict[int, set[str]] = {}

        # chat_id -> connection_ids currently interested in it. Purely a local
        # routing table now: dispatch of an inbox event to the right sockets.
        self._chat_subscribers: dict[int, set[str]] = {}
        # connection_id -> chat_ids it's interested in (needed to clean up on disconnect)
        self._chats_by_connection: dict[str, set[int]] = {}

        # One Redis subscription per process (not per chat): every chat-scoped
        # event for any chat this process serves arrives on this instance's
        # inbox channel. Started lazily on the first connect, stopped when the
        # last connection goes away.
        self._instance_inbox_task: asyncio.Task | None = None

        # Same three-dict shape as above, but for each user's personal
        # channel instead of a chat's - keyed separately from the chat_id
        # dicts above on purpose: a chat_id and a user_id are both Snowflake
        # ids drawn from the same id space, so a shared dict would risk a
        # real (if rare) key collision between an actual chat and an actual user.
        self._user_channel_subscribers: dict[int, set[str]] = {}
        self._user_channel_listener_tasks: dict[int, asyncio.Task] = {}

        # Subscribe-on-demand presence (see CLAUDE.md): target_user_id ->
        # connection_ids currently watching that user's presence, populated
        # only by an explicit subscribe_presence WS action (never on connect)
        # and scoped to however long the watcher keeps the relevant 1:1 chat
        # open. Same three-dict shape as the chat/user-channel state above,
        # for the same reason.
        self._presence_subscribers: dict[int, set[str]] = {}
        self._presence_listener_tasks: dict[int, asyncio.Task] = {}
        self._presence_watches_by_connection: dict[str, set[int]] = {}

    async def connect(self, user_id: int, connection_id: str, websocket: WebSocket, chat_ids: list[int]) -> None:
        self._sockets_by_connection[connection_id] = websocket
        self._user_by_connection[connection_id] = user_id
        self._connections_by_user.setdefault(user_id, set()).add(connection_id)
        self._chats_by_connection[connection_id] = set(chat_ids)

        self._ensure_instance_inbox_listener()

        for chat_id in chat_ids:
            await self._subscribe_connection_to_chat(connection_id, chat_id)

        # Every connection also listens on its own user's personal channel,
        # for the rest of its lifetime - this is what lets a chat created
        # after connect() still reach it (see _handle_user_channel_event).
        await self._subscribe_connection_to_user_channel(connection_id, user_id)

    async def disconnect(self, connection_id: str) -> None:
        user_id = self._user_by_connection.pop(connection_id, None)
        self._sockets_by_connection.pop(connection_id, None)

        if user_id is not None:
            connections = self._connections_by_user.get(user_id)
            if connections is not None:
                connections.discard(connection_id)
                if not connections:
                    del self._connections_by_user[user_id]
            await self._unsubscribe_connection_from_user_channel(connection_id, user_id)

        for chat_id in self._chats_by_connection.pop(connection_id, set()):
            await self._unsubscribe_connection_from_chat(connection_id, chat_id)

        for target_user_id in self._presence_watches_by_connection.pop(connection_id, set()):
            await self._unsubscribe_connection_from_presence(connection_id, target_user_id)

        if not self._sockets_by_connection:
            self._stop_instance_inbox_listener()

    def get_local_user_ids(self) -> set[int]:
        """Every user with at least one connection to *this* process right now."""
        return set(self._connections_by_user.keys())

    # -----------------------------------------------------------------
    # Chat channels
    # -----------------------------------------------------------------

    async def _subscribe_connection_to_chat(self, connection_id: str, chat_id: int) -> None:
        subscribers = self._chat_subscribers.setdefault(chat_id, set())
        is_first_subscriber = len(subscribers) == 0
        subscribers.add(connection_id)

        if is_first_subscriber:
            # This process now serves this chat - register it so the fan-out
            # worker routes the chat's events to us.
            try:
                await routing.add_chat_for_instance(SERVER_ID, chat_id)
            except Exception:
                logger.exception("routing: add_chat_for_instance(%s) failed", chat_id)

    async def _unsubscribe_connection_from_chat(self, connection_id: str, chat_id: int) -> None:
        subscribers = self._chat_subscribers.get(chat_id)
        if subscribers is None:
            return

        subscribers.discard(connection_id)
        if not subscribers:
            del self._chat_subscribers[chat_id]
            try:
                await routing.remove_chat_for_instance(SERVER_ID, chat_id)
            except Exception:
                logger.exception("routing: remove_chat_for_instance(%s) failed", chat_id)

    # -----------------------------------------------------------------
    # Instance inbox - one Redis subscription per process
    # -----------------------------------------------------------------

    def _ensure_instance_inbox_listener(self) -> None:
        if self._instance_inbox_task is None or self._instance_inbox_task.done():
            self._instance_inbox_task = asyncio.create_task(self._listen_to_instance_inbox())

    def _stop_instance_inbox_listener(self) -> None:
        task = self._instance_inbox_task
        self._instance_inbox_task = None
        if task is not None:
            task.cancel()

    async def _listen_to_instance_inbox(self) -> None:
        await self._run_resilient_listener(
            label=f"instance inbox {SERVER_ID}",
            subscribe=lambda: realtime_service.subscribe_to_instance_inbox(SERVER_ID),
            on_event=lambda event: self._dispatch_inbox_event(event),
            still_wanted=lambda: self._instance_inbox_task is not None,
        )

    async def _dispatch_inbox_event(self, event: dict) -> None:
        chat_id_raw = event.get("chat_id")
        if chat_id_raw is None:
            return
        await self._broadcast_to_chat(int(chat_id_raw), event)

    async def _broadcast_to_chat(self, chat_id: int, event: dict) -> None:
        # Snapshotted, not iterated live: a concurrent disconnect from a
        # different connection in this same chat would otherwise mutate this
        # exact set out from under the loop and raise "Set changed size
        # during iteration".
        connection_ids = list(self._chat_subscribers.get(chat_id, ()))
        await self._send_to_connections(connection_ids, event)

    # -----------------------------------------------------------------
    # Per-user personal channel
    # -----------------------------------------------------------------

    async def _subscribe_connection_to_user_channel(self, connection_id: str, user_id: int) -> None:
        subscribers = self._user_channel_subscribers.setdefault(user_id, set())
        is_first_subscriber = len(subscribers) == 0
        subscribers.add(connection_id)

        if is_first_subscriber:
            self._user_channel_listener_tasks[user_id] = asyncio.create_task(self._listen_to_user_channel(user_id))

    async def _unsubscribe_connection_from_user_channel(self, connection_id: str, user_id: int) -> None:
        subscribers = self._user_channel_subscribers.get(user_id)
        if subscribers is None:
            return

        subscribers.discard(connection_id)
        if not subscribers:
            del self._user_channel_subscribers[user_id]
            task = self._user_channel_listener_tasks.pop(user_id, None)
            if task is not None:
                task.cancel()

    async def _listen_to_user_channel(self, user_id: int) -> None:
        await self._run_resilient_listener(
            label=f"user {user_id}",
            subscribe=lambda: realtime_service.subscribe_to_user(user_id),
            on_event=lambda event: self._handle_user_channel_event(user_id, event),
            still_wanted=lambda: user_id in self._user_channel_subscribers,
        )

    async def _handle_user_channel_event(self, user_id: int, event: dict) -> None:
        if event.get("event") == "added_to_chat" and event.get("chat_id") is not None:
            chat_id = int(event["chat_id"])
            # The actual fix: bring this user's already-open connections into
            # the new chat's live subscription *now*, instead of only at
            # their next reconnect (when get_all_chat_ids_for_user() would
            # have picked it up naturally).
            for connection_id in list(self._user_channel_subscribers.get(user_id, ())):
                self._chats_by_connection.setdefault(connection_id, set()).add(chat_id)
                await self._subscribe_connection_to_chat(connection_id, chat_id)

        if event.get("event") == "removed_from_chat" and event.get("chat_id") is not None:
            chat_id = int(event["chat_id"])
            # Mirror of the above: drop this user's already-open connections
            # from the chat's live subscription now, instead of leaving them
            # subscribed to events for a chat they're no longer part of until
            # their next reconnect.
            for connection_id in list(self._user_channel_subscribers.get(user_id, ())):
                self._chats_by_connection.get(connection_id, set()).discard(chat_id)
                await self._unsubscribe_connection_from_chat(connection_id, chat_id)

        # Forwarded to the client too, so the UI can react (refresh the chat
        # list, show a notification) without polling.
        connection_ids = list(self._user_channel_subscribers.get(user_id, ()))
        await self._send_to_connections(connection_ids, event)

    # -----------------------------------------------------------------
    # Presence (subscribe-on-demand)
    # -----------------------------------------------------------------

    async def subscribe_presence(self, connection_id: str, target_user_id: int) -> None:
        """
        Called only from the `subscribe_presence` WS action - after the
        caller (routers/websocket.py) has already verified the requester
        shares a private chat with target_user_id. Deliberately not called
        from connect(): presence has no "subscribe to everything I might
        care about at connect time" step the way chats do, by design.
        """
        subscribers = self._presence_subscribers.setdefault(target_user_id, set())
        is_first_subscriber = len(subscribers) == 0
        subscribers.add(connection_id)
        self._presence_watches_by_connection.setdefault(connection_id, set()).add(target_user_id)

        if is_first_subscriber:
            self._presence_listener_tasks[target_user_id] = asyncio.create_task(self._listen_to_presence(target_user_id))

    async def unsubscribe_presence(self, connection_id: str, target_user_id: int) -> None:
        """Called from the `unsubscribe_presence` WS action (chat closed/switched away from)."""
        self._presence_watches_by_connection.get(connection_id, set()).discard(target_user_id)
        await self._unsubscribe_connection_from_presence(connection_id, target_user_id)

    async def _unsubscribe_connection_from_presence(self, connection_id: str, target_user_id: int) -> None:
        subscribers = self._presence_subscribers.get(target_user_id)
        if subscribers is None:
            return

        subscribers.discard(connection_id)
        if not subscribers:
            del self._presence_subscribers[target_user_id]
            task = self._presence_listener_tasks.pop(target_user_id, None)
            if task is not None:
                task.cancel()

    async def _listen_to_presence(self, target_user_id: int) -> None:
        await self._run_resilient_listener(
            label=f"presence {target_user_id}",
            subscribe=lambda: realtime_service.subscribe_to_presence(target_user_id),
            on_event=lambda event: self._broadcast_to_presence_watchers(target_user_id, event),
            still_wanted=lambda: target_user_id in self._presence_subscribers,
        )

    async def _broadcast_to_presence_watchers(self, target_user_id: int, event: dict) -> None:
        # Snapshotted for the same reason as _broadcast_to_chat: a concurrent
        # unsubscribe must not mutate this set mid-iteration.
        connection_ids = list(self._presence_subscribers.get(target_user_id, ()))
        await self._send_to_connections(connection_ids, event)

    # -----------------------------------------------------------------
    # Shared plumbing
    # -----------------------------------------------------------------

    async def _run_resilient_listener(
        self,
        label: str,
        subscribe: Callable[[], object],
        on_event: Callable[[dict], Awaitable[None]],
        still_wanted: Callable[[], bool],
    ) -> None:
        """
        Resubscribes with a brief backoff on any *unexpected* failure (a
        dropped Redis connection, a pub/sub hiccup) instead of letting the
        task die: without this, a transient Redis blip would silently and
        permanently stop delivery, because nothing else ever re-checks a
        task that already finished. Only stops for real on cancellation (the
        normal path: the last local interest in this channel went away) or
        once `still_wanted()` says nobody's listening for this any more.
        """
        while still_wanted():
            agen = subscribe()
            try:
                async for event in agen:
                    await on_event(event)
                break  # the generator ended on its own - nothing left to listen to
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Listener for {label} failed, resubscribing: {e}")
                await asyncio.sleep(0.5)
            finally:
                await agen.aclose()

    async def _send_to_connections(self, connection_ids: list[str], event: dict) -> None:
        # Sent concurrently, not one at a time: with sequential sends, one
        # slow/stalled client (bad network, backpressure) would delay
        # delivery to every other recipient behind it in this same task.
        results = await asyncio.gather(
            *(self._send_to_connection(connection_id, event) for connection_id in connection_ids),
            return_exceptions=True,
        )

        dead_connections = [
            connection_id for connection_id, result in zip(connection_ids, results)
            if isinstance(result, Exception)
        ]
        for connection_id in dead_connections:
            await self.disconnect(connection_id)

    async def _send_to_connection(self, connection_id: str, event: dict) -> None:
        websocket = self._sockets_by_connection.get(connection_id)
        if websocket is None:
            return
        try:
            await websocket.send_json(event)
        except Exception as e:
            logger.info(f"Dropping dead connection {connection_id}: {e}")
            raise


# One shared instance per process - the WebSocket route and the connection
# manager must agree on the same local state.
connection_manager = ConnectionManager()

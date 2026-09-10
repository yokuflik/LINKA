// WebSocket lifecycle + the client half of the receipt watermark protocol.
// Owns the raw socket (kept private); other composables talk to it through
// ctx.sendRaw / ctx.wsIsOpen. Global `useWebsocket(ctx)` factory.
//
// Needs from ctx: wsBase, accessToken, log, logError, currentUser,
// chats, activeChatId, handleWsMessage, and (call-time) resubscribePresenceForActiveChat.
function useWebsocket(ctx) {
  const { ref } = Vue;
  const { log, logError } = ctx;

  const wsStatus = ref('disconnected'); // disconnected | connecting | connected | error
  let ws = null;
  let wsReconnectTimer = null;
  let heartbeatTimer = null;
  let reconnectFailures = 0;
  let sustainedOutageToasted = false;

  function wsIsOpen() {
    return !!ws && ws.readyState === WebSocket.OPEN;
  }

  // Send an arbitrary JSON frame; silently a no-op with no open connection.
  function sendRaw(obj) {
    if (!wsIsOpen()) return;
    ws.send(JSON.stringify(obj));
  }

  function connectWebSocket() {
    if (ws) { ws.onclose = null; ws.close(); }
    wsStatus.value = 'connecting';
    const url = `${ctx.wsBase.value}/ws?token=${encodeURIComponent(ctx.accessToken.value)}`;
    log('WS connecting →', url);
    ws = new WebSocket(url);

    ws.onopen = () => {
      wsStatus.value = 'connected';
      reconnectFailures = 0;
      sustainedOutageToasted = false;
      log('WS connected');
      heartbeatTimer = setInterval(() => {
        if (!wsIsOpen()) return;
        // The `heartbeat` frame's only server-side effect is refreshing our
        // own presence TTL (routing TTLs are refreshed by a server-side task,
        // not this frame). Only ping it while the tab is actually foreground,
        // so a backgrounded / switched-away tab stops being "online" for peers
        // once the 60s presence TTL lapses - even if the explicit
        // `presence_active:false` was missed (frozen tab, dropped frame,
        // reconnect race re-marking us online). ADR 0025.
        if (!ctx.windowIsActive || ctx.windowIsActive()) {
          ws.send(JSON.stringify({ type: 'heartbeat' }));
        }
        // Re-assert the presence subscription for the open private chat so
        // the server re-checks the other user's privacy.online setting -
        // a change on their side takes effect within one heartbeat, with
        // no per-push authorization check server-side.
        if (ctx.refreshPresenceSubscription) ctx.refreshPresenceSubscription();
      }, 30000);
      markAllChatsDelivered();

      // Resume the outbox drain loop (parked while the socket was down).
      if (ctx.kickDrain) ctx.kickDrain();

      // A fresh connection means the server-side presence subscription from
      // before (if any) is gone with the old connection - reset the local
      // bookkeeping and re-subscribe for the open chat, otherwise the
      // "already subscribed, no-op" guard would skip the new socket.
      ctx.resubscribePresenceForActiveChat();

      // If the open chat failed to load its history while we were offline
      // (empty pane + "Waiting for connection…"), retry that fetch now.
      if (ctx.reloadActiveChatIfUnloaded) ctx.reloadActiveChatIfUnloaded();

      // If the open chat IS loaded, revalidate it: a message that arrived while
      // the socket was down went to an offline push only and never hit the
      // live list - pull it in and re-fire delivered/read for the newest id.
      if (ctx.revalidateActiveChatOnReconnect) ctx.revalidateActiveChatOnReconnect();
    };

    ws.onmessage = (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (err) { logError('WS message was not valid JSON:', evt.data); return; }
      log('WS ←', msg);
      ctx.handleWsMessage(msg);
    };

    ws.onerror = (evt) => {
      wsStatus.value = 'error';
      logError('WS error', evt);
    };

    ws.onclose = (evt) => {
      log('WS closed, code =', evt.code, 'reason =', evt.reason || '(none)');
      wsStatus.value = 'disconnected';
      if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
      ws = null;
      // Drop stale peer online / typing state so it can't linger on reconnect
      // before the fresh presence_status pull (ADR 0025).
      if (ctx.resetTyping) ctx.resetTyping();
      if (ctx.resetPresence) ctx.resetPresence();
      if (!ctx.accessToken.value) return;

      // 4401 = the server rejected the token (expired mid-session, e.g. tab
      // idle > 15 min). Refresh it and reconnect at once instead of looping
      // the handshake with a dead token or bouncing to the login screen.
      if (evt.code === 4401 && ctx.tryRefresh) {
        log('WS closed 4401 - refreshing token before reconnect');
        ctx.tryRefresh().then((ok) => {
          if (ok && ctx.accessToken.value) connectWebSocket();
          // tryRefresh() calls ctx.logout() on failure - nothing to do here.
        });
        return;
      }

      // A single reconnect (e.g. uvicorn --reload) is normal and silent.
      // Only warn once the outage has persisted across a few attempts.
      reconnectFailures += 1;
      if (reconnectFailures >= 3 && !sustainedOutageToasted && ctx.showErrorToast) {
        sustainedOutageToasted = true;
        ctx.showErrorToast("You're offline. We'll keep trying to reconnect.");
      }
      log('reconnecting in 3s…');
      wsReconnectTimer = setTimeout(connectWebSocket, 3000);
    };
  }

  // The browser regained connectivity - don't wait out the 3s reconnect timer.
  window.addEventListener('online', () => {
    if (!ctx.accessToken.value || wsIsOpen()) return;
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    log('back online - reconnecting now');
    connectWebSocket();
  });

  function disconnectWebSocket() {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    wsStatus.value = 'disconnected';
  }

  // Sends a {chat_id, message_id} receipt action ("mark_delivered" /
  // "mark_read" / "mark_played") - the client-side half of the watermark
  // pattern the server maintains (crud_participant.recompute_chat_receipt_cursors).
  function sendReceipt(type, chatId, messageId) {
    if (!wsIsOpen() || messageId == null) return;
    ws.send(JSON.stringify({ type, chat_id: chatId, message_id: messageId }));
  }

  // The listener finished (or nearly finished) a voice message - tell the
  // server so it counts as "played" ("נשמעה"). Never for your own recording.
  function onVoicePlayed(message) {
    if (!message || message.sender_id === ctx.currentUser.value.id) return;
    sendReceipt('mark_played', ctx.activeChatId.value, message.id);
  }

  // Catches up "delivered" on every chat's latest message, not just the open
  // one - otherwise anything sent while this device was offline stays stuck
  // at "sent" until each chat is opened by hand.
  function markAllChatsDelivered() {
    for (const item of ctx.chats.value) {
      if (item.chat.last_message_id != null) {
        sendReceipt('mark_delivered', item.chat.id, item.chat.last_message_id);
      }
    }
  }

  return {
    wsStatus, wsIsOpen, sendRaw,
    connectWebSocket, disconnectWebSocket,
    sendReceipt, onVoicePlayed, markAllChatsDelivered,
  };
}

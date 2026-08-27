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
      log('WS connected');
      heartbeatTimer = setInterval(() => {
        if (wsIsOpen()) ws.send(JSON.stringify({ type: 'heartbeat' }));
      }, 30000);
      markAllChatsDelivered();

      // A fresh connection means the server-side presence subscription from
      // before (if any) is gone with the old connection - reset the local
      // bookkeeping and re-subscribe for the open chat, otherwise the
      // "already subscribed, no-op" guard would skip the new socket.
      ctx.resubscribePresenceForActiveChat();
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
      if (ctx.accessToken.value) {
        log('reconnecting in 3s…');
        wsReconnectTimer = setTimeout(connectWebSocket, 3000);
      }
    };
  }

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

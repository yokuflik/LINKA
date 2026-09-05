// Client-side outbound message queue + paced sender (front-end only, no
// backend change).
//
// ALL text sends go through this queue, online or not. A single drain loop
// pops frames one at a time, ~450ms apart, so a rapid burst never trips the
// server's per-user send limiter (send_message 3 / 1s primary, 40 / 60s
// burst). When the socket is down the loop just parks until the next
// reconnect. A `rate_limited` / transient error puts the frame back at the
// head and backs off; the server dedupes any accidental double-send by
// client_message_id (`message_already_sent`), so replay is always safe.
//
// The queue is IN-MEMORY ONLY: closing the tab genuinely loses anything still
// pending (by design). The matching optimistic bubbles are written through to
// the localStorage message cache with pending:true - on the next load
// `markStalePendingAsFailed()` turns those into "not sent" (⚠️) bubbles the
// user can retry.
//
// Needs from ctx: log, logError, wsIsOpen, sendRaw, messages.
function useOutbox(ctx) {
  const { ref, computed } = Vue;

  // Minimum gap between frames leaving the browser. Server primary limit is
  // 3 / 1s; 450ms => ~2.2/s, comfortably under it even with clock skew.
  const SEND_INTERVAL_MS = 450;
  // Backoff after a `rate_limited` reject. The primary window is 1s but the
  // burst window is 60s, so a large batch can keep bouncing - exponential
  // backoff per frame, capped, until the burst window drains.
  const RATE_LIMIT_BACKOFF_MS = 1500;
  const RATE_LIMIT_BACKOFF_MAX_MS = 20000;

  // Pending frames, oldest first. Each is the exact send_message frame.
  const outbox = ref([]);
  const pendingCount = computed(() => outbox.value.length);

  const isOffline = ref(!navigator.onLine);
  function refreshOnlineFlag() { isOffline.value = !navigator.onLine; }
  window.addEventListener('online', refreshOnlineFlag);
  window.addEventListener('offline', refreshOnlineFlag);

  // Banner above the composer: something queued AND we can't deliver right now.
  const showOutboxBanner = computed(() => outbox.value.length > 0 && !ctx.wsIsOpen());
  const outboxBannerText = computed(() => {
    const n = outbox.value.length;
    return `No connection — ${n} ${n === 1 ? 'message' : 'messages'} will send when you're back online.`;
  });

  // Frames whose ack we're still waiting on, so a `rate_limited` error can
  // find the original payload to requeue. client_message_id -> payload.
  const inFlight = new Map();

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Safety net: if a sent frame gets neither an echo (new_message /
  // message_already_sent) nor an error within this window, the send silently
  // failed (e.g. a server error frame that carried no client_message_id) -
  // flag the bubble ⚠️ so it isn't a clock forever. Long enough to outlast the
  // server's 60s burst window plus a couple of backoff retries; a frame that's
  // actively being requeued has its timer disarmed (see onSendError).
  const ACK_TIMEOUT_MS = 90000;
  const ackTimers = new Map();
  // Per-client_message_id count of consecutive rate_limited rejects, for
  // exponential backoff. Cleared once the frame finally resolves.
  const rejectCount = new Map();
  function armAckTimeout(clientMessageId) {
    clearAckTimeout(clientMessageId);
    ackTimers.set(clientMessageId, setTimeout(() => {
      ackTimers.delete(clientMessageId);
      if (!inFlight.has(clientMessageId)) return; // already resolved
      inFlight.delete(clientMessageId);
      rejectCount.delete(clientMessageId);
      const m = ctx.messages.value.find((x) => x.client_message_id === clientMessageId);
      if (m && m.pending) { m.pending = false; m.send_failed = true; }
      ctx.logError('outbox: no ack for', clientMessageId, '- marking failed');
    }, ACK_TIMEOUT_MS));
  }
  function clearAckTimeout(clientMessageId) {
    const t = ackTimers.get(clientMessageId);
    if (t) { clearTimeout(t); ackTimers.delete(clientMessageId); }
  }

  let draining = false;
  async function drainLoop() {
    if (draining) return;
    draining = true;
    try {
      while (outbox.value.length) {
        if (!ctx.wsIsOpen()) break; // park until reconnect kicks the loop
        const payload = outbox.value[0];
        outbox.value = outbox.value.slice(1);
        inFlight.set(payload.client_message_id, payload);
        ctx.log('outbox →', payload.client_message_id, `(remaining ${outbox.value.length})`);
        ctx.sendRaw(payload);
        armAckTimeout(payload.client_message_id);
        await sleep(SEND_INTERVAL_MS);
      }
    } finally {
      draining = false;
    }
  }

  // Public: enqueue a frame (caller already rendered the optimistic bubble).
  function enqueueOutgoing(payload) {
    outbox.value = [...outbox.value, payload];
    kickDrain();
  }

  // Start / resume the drain loop (called on enqueue and on ws reconnect).
  function kickDrain() {
    if (!draining) drainLoop();
  }

  // The server accepted (new_message / message_already_sent) or permanently
  // rejected (message_failed) this frame - stop tracking it.
  function removeFromOutbox(clientMessageId) {
    inFlight.delete(clientMessageId);
    clearAckTimeout(clientMessageId);
    rejectCount.delete(clientMessageId);
    outbox.value = outbox.value.filter((p) => p.client_message_id !== clientMessageId);
  }

  // The server sent {type:"error", code, client_message_id} for one of our
  // sends. rate_limited / internal_error => requeue at the head with
  // exponential backoff (the ack-timeout keeps running as the hard ceiling,
  // and the bubble stays on 🕓 while we retry). Anything else => fail the
  // bubble.
  async function onSendError(code, clientMessageId) {
    const payload = clientMessageId && inFlight.get(clientMessageId);
    if (!payload) return false; // not one of ours
    // Leave the ack-timeout armed - it's the ultimate give-up ceiling.
    inFlight.delete(clientMessageId);

    if (code === 'rate_limited' || code === 'internal_error') {
      const n = (rejectCount.get(clientMessageId) || 0) + 1;
      rejectCount.set(clientMessageId, n);
      const wait = Math.min(RATE_LIMIT_BACKOFF_MS * 2 ** (n - 1), RATE_LIMIT_BACKOFF_MAX_MS);
      ctx.logError(`send ${code} for ${clientMessageId} - retry ${n} in ${wait}ms`);
      // Keep the bubble on 🕓 (not ⚠️) - it hasn't failed, just delayed.
      const m = ctx.messages.value.find((x) => x.client_message_id === clientMessageId);
      if (m) { m.pending = true; m.send_failed = false; }
      // Requeue at the head so ordering is preserved.
      outbox.value = [payload, ...outbox.value];
      await sleep(wait);
      kickDrain();
      return true;
    }

    // Permanent (bad payload, not a participant, ...): flag the bubble.
    clearAckTimeout(clientMessageId);
    rejectCount.delete(clientMessageId);
    const m = ctx.messages.value.find((x) => x.client_message_id === clientMessageId);
    if (m) { m.pending = false; m.send_failed = true; }
    return true;
  }

  // On load: a cached bubble still marked pending was queued when the tab
  // closed and never sent - show it failed (⚠️, retryable), not a stuck clock.
  function markStalePendingAsFailed() {
    for (const m of ctx.messages.value) {
      if (m.pending && !m.send_failed) { m.pending = false; m.send_failed = true; }
    }
  }

  // Retry a failed bubble: rebuild its frame and requeue.
  function retryFailedMessage(m) {
    if (!m || !m.send_failed || !m.client_message_id) return;
    const payload = {
      type: 'send_message',
      chat_id: m.chat_id,
      client_message_id: m.client_message_id,
      content: m.content,
      message_type: 1,
    };
    if (m.reply_to_message_id) payload.reply_to_message_id = m.reply_to_message_id;
    m.send_failed = false;
    m.pending = true;
    enqueueOutgoing(payload);
  }

  // logout / explicit disconnect: nothing will be delivered - drop everything.
  function clearOutbox() {
    outbox.value = [];
    inFlight.clear();
    rejectCount.clear();
    for (const t of ackTimers.values()) clearTimeout(t);
    ackTimers.clear();
    markStalePendingAsFailed();
  }

  return {
    outbox, pendingCount, isOffline,
    showOutboxBanner, outboxBannerText,
    enqueueOutgoing, kickDrain, removeFromOutbox, onSendError,
    markStalePendingAsFailed, retryFailedMessage, clearOutbox,
  };
}

// Typing / recording-audio indicator - fully ephemeral (see CLAUDE.md), no DB,
// no "stopped" event: each received event (re)starts a ~5s client-side expiry
// for that (chat_id, user_id) pair. Global `useTyping(ctx)` factory.
//
// Needs from ctx: sendRaw, activeChatId, currentUser, userLabelById.
function useTyping(ctx) {
  const { ref, computed } = Vue;

  const TYPING_EXPIRY_MS = 5000;
  // How often we send our own event while the user keeps going without a
  // pause - matches the receiving side's expiry so a steady typist's
  // indicator never lapses on other clients.
  const TYPING_SEND_THROTTLE_MS = 3000;

  // chat_id -> Map<user_id, { handle, kind }>. "kind" is "typing" or
  // "recording_audio", from the server event's "kind" field (older servers
  // omit it -> defaults to "typing").
  const typingUsersByChatId = ref({});
  let lastTypingSentAt = 0;
  let lastRecordingSentAt = 0;

  function noteUserTyping(chatId, userId, kind) {
    kind = kind || 'typing';
    const existing = typingUsersByChatId.value[chatId] || {};
    if (existing[userId]) clearTimeout(existing[userId].handle);
    const handle = setTimeout(() => {
      const current = { ...(typingUsersByChatId.value[chatId] || {}) };
      delete current[userId];
      typingUsersByChatId.value = { ...typingUsersByChatId.value, [chatId]: current };
    }, TYPING_EXPIRY_MS);
    typingUsersByChatId.value = {
      ...typingUsersByChatId.value,
      [chatId]: { ...existing, [userId]: { handle, kind } },
    };
  }

  // Sending a message implies you stopped typing - clear the sender's entry
  // immediately instead of waiting for it to expire.
  function clearUserTyping(chatId, userId) {
    const existing = typingUsersByChatId.value[chatId];
    if (!existing || !existing[userId]) return;
    clearTimeout(existing[userId].handle);
    const current = { ...existing };
    delete current[userId];
    typingUsersByChatId.value = { ...typingUsersByChatId.value, [chatId]: current };
  }

  function notifyTyping() {
    if (!ctx.activeChatId.value) return;
    const now = Date.now();
    if (now - lastTypingSentAt < TYPING_SEND_THROTTLE_MS) return;
    lastTypingSentAt = now;
    ctx.sendRaw({ type: 'typing', chat_id: ctx.activeChatId.value });
  }

  // Same throttle/shape as notifyTyping, for the "recording_audio" kind -
  // called on a 1s tick while a mic recording is running.
  function notifyRecording() {
    if (!ctx.activeChatId.value) return;
    const now = Date.now();
    if (now - lastRecordingSentAt < TYPING_SEND_THROTTLE_MS) return;
    lastRecordingSentAt = now;
    ctx.sendRaw({ type: 'recording', chat_id: ctx.activeChatId.value });
  }

  // "X is typing…" / "N people are typing…", per kind. Rules (product
  // decision): exactly one of a kind -> their name; two or more -> a count.
  // Both kinds active -> the phrases joined with a comma. A plain function
  // (takes a chatId); reading typingUsersByChatId.value still tracks reactively.
  function typingLabelForChat(chatId) {
    const entries = typingUsersByChatId.value[chatId] || {};
    const byKind = { typing: [], recording_audio: [] };
    for (const id of Object.keys(entries)) {
      if (id === ctx.currentUser.value.id) continue;
      const kind = (entries[id] && entries[id].kind) || 'typing';
      (byKind[kind] || byKind.typing).push(id);
    }
    const parts = [];
    const phrase = (ids, verbOne, verbMany) => {
      if (ids.length === 1) return `${ctx.userLabelById(ids[0])} is ${verbOne}`;
      return `${ids.length} people are ${verbMany}`;
    };
    if (byKind.typing.length) parts.push(phrase(byKind.typing, 'typing', 'typing'));
    if (byKind.recording_audio.length) parts.push(phrase(byKind.recording_audio, 'recording', 'recording'));
    if (!parts.length) return '';
    return parts.join(', ') + '…';
  }

  const activeChatTypingLabel = computed(() => {
    if (!ctx.activeChatId.value) return '';
    return typingLabelForChat(ctx.activeChatId.value);
  });

  function resetTyping() {
    typingUsersByChatId.value = {};
  }

  return {
    typingUsersByChatId,
    noteUserTyping, clearUserTyping, notifyTyping, notifyRecording,
    typingLabelForChat, activeChatTypingLabel, resetTyping,
  };
}

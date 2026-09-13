// Browser system notifications for incoming messages while the tab is
// backgrounded/hidden. Frontend-only, no backend change - uses the standard
// Notification API. Global `useDesktopNotifications(ctx)` factory.
//
// Needs from ctx (call-time): windowIsActive, chatDisplayName, chatAvatarUrl,
// senderLabel, previewText, currentUser.
function useDesktopNotifications(ctx) {
  const supported = typeof window !== 'undefined' && 'Notification' in window;

  // Ask once the user is actually inside the app (not on the auth screen) -
  // browsers require a page already loaded, and asking right away on a cold
  // load tends to get auto-dismissed/ignored.
  function requestNotificationPermission() {
    if (!supported) return;
    // Chrome/Firefox silently refuse to even show the prompt on an insecure
    // origin (plain file:// or non-localhost http://) - no error, permission
    // just stays 'default' forever. Surface that instead of failing silently,
    // since this PoC is normally opened via file:// (see frontend.md "Running").
    if (!window.isSecureContext) {
      ctx.logError && ctx.logError('desktop notifications need a secure context (https, or http://localhost) - serve the PoC via "cd poc && python3 -m http.server 5500" instead of opening index.html directly');
      return;
    }
    if (Notification.permission === 'default') Notification.requestPermission();
  }

  let lastNotification = null;

  // Only for a message that isn't ours and while the tab is hidden/unfocused -
  // a visible foreground chat already shows the message in the pane.
  function notifyIncomingMessage(msg) {
    if (!supported || Notification.permission !== 'granted') return;
    if (msg.sender_id == null || msg.sender_id === ctx.currentUser.value.id) return;
    if (ctx.windowIsActive()) return;

    // chatDisplayName/chatAvatarUrl take the raw chat object, not the
    // ChatListItem wrapper ({chat, ...}) - unwrap it here.
    const chatItem = ctx.chats.value.find((c) => c.chat.id === msg.chat_id);
    const chat = chatItem ? chatItem.chat : null;
    const title = chat ? ctx.chatDisplayName(chat) : ctx.senderLabel(msg.sender_id);
    const senderPrefix = (chat && chat.is_group) ? `${ctx.senderLabel(msg.sender_id)}: ` : '';
    const body = senderPrefix + ctx.previewText(msg.content, msg.type);
    const icon = chat ? (ctx.chatAvatarUrl(chat) || undefined) : undefined;

    try {
      if (lastNotification) lastNotification.close();
      lastNotification = new Notification(title, { body, icon, tag: `linka-chat-${msg.chat_id}` });
      lastNotification.onclick = () => {
        window.focus();
        if (lastNotification) lastNotification.close();
      };
    } catch (err) {
      // Notification construction can throw on some platforms (e.g. no
      // permission granted yet, or an unsupported context) - never let a
      // notification failure break message handling.
      ctx.logError && ctx.logError('desktop notification failed:', err.message);
    }
  }

  return { requestNotificationPermission, notifyIncomingMessage };
}

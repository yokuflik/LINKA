// Browser system notifications for incoming messages the user isn't actually
// looking at. Frontend-only, no backend change - uses the standard Notification
// API. Global `useDesktopNotifications(ctx)` factory.
//
// "Not looking at it" = the tab isn't visible (document.hidden - switched to
// another tab, minimized, screen off), OR it's visible but the message is for
// a chat other than the one currently open. Deliberately does NOT require
// document.hasFocus() (unlike useChatOpen.windowIsActive, used for mark-read/
// presence): a Chrome window can be fully visible side-by-side with another
// app/window that holds OS focus, and the user is still plainly reading it -
// requiring focus there caused notifications to needlessly fire in that case.
//
// Needs from ctx (call-time): activeChatId, chatDisplayName, chatAvatarUrl,
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

  // Only for a message that isn't ours, and only when the user isn't already
  // looking at it (see the file-header comment for what that means here).
  function notifyIncomingMessage(msg) {
    if (!supported) { ctx.log && ctx.log('[notify] skipped: Notification API unsupported'); return; }
    if (Notification.permission !== 'granted') { ctx.log && ctx.log('[notify] skipped: permission is', Notification.permission); return; }
    if (msg.sender_id == null || msg.sender_id === ctx.currentUser.value.id) { ctx.log && ctx.log('[notify] skipped: own message or system'); return; }
    const tabVisible = document.visibilityState === 'visible';
    const chatOpen = msg.chat_id === ctx.activeChatId.value;
    if (tabVisible && chatOpen) { ctx.log && ctx.log('[notify] skipped: tab visible and this chat is open'); return; }
    ctx.log && ctx.log('[notify] firing for chat', msg.chat_id, 'from', msg.sender_id);

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

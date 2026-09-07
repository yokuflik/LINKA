// New chat / group creation: the private-chat form and the group-creation
// modal (including one-shot creation-with-photo). Global `useNewChat(ctx)`
// factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, privateChatTitles, privateChatOtherUserId,
// userById, loadChats, selectChat. (createGroupChat takes its payload from
// NewGroupModal.)
function useNewChat(ctx) {
  const { ref, computed } = Vue;

  const showNewPrivate = ref(false);
  const showNewChatModal = ref(false);
  const showNewGroupModal = ref(false);
  const newGroupBusy = ref(false);
  const newGroupError = ref('');
  const newPrivatePhone = ref('');
  const chatFormError = ref('');

  // --- New-chat modal: exact-match user search (username or phone) ---------
  const userSearchQuery = ref('');
  const userSearchBusy = ref(false);
  const userSearchError = ref('');
  const userSearchResult = ref(null); // a UserOut, or null
  let userSearchSeq = 0;
  let userSearchTimer = null;
  const USER_SEARCH_DEBOUNCE_MS = 1500;

  function resetUserSearch() {
    if (userSearchTimer) { clearTimeout(userSearchTimer); userSearchTimer = null; }
    userSearchQuery.value = '';
    userSearchBusy.value = false;
    userSearchError.value = '';
    userSearchResult.value = null;
  }

  function openNewChatModal() {
    resetUserSearch();
    showNewChatModal.value = true;
  }

  // Partial matches over EXISTING chats (groups + private peers), shown as a
  // scrollable list under the search row. Pure client-side substring match -
  // no request. Matches on the resolved display name and, for a private
  // chat, also on the peer's phone number (digits-only, so "+972 50" and
  // "97250" both hit). Empty query -> nothing.
  const existingChatMatches = computed(() => {
    const q = userSearchQuery.value.trim().toLowerCase().replace(/^@/, '');
    if (!q) return [];
    const qDigits = q.replace(/\D/g, '');
    return ctx.chats.value.filter((item) => {
      const chat = item.chat;
      if ((ctx.chatDisplayName(chat) || '').toLowerCase().includes(q)) return true;
      if (chat.is_group) return false;
      const otherId = ctx.privateChatOtherUserId.value[chat.id];
      const phone = otherId && ctx.userById.value[otherId] && ctx.userById.value[otherId].phone_number;
      const titlePhone = ctx.privateChatTitles.value[chat.id]; // often "username || phone"
      const hay = `${phone || ''} ${titlePhone || ''}`;
      return qDigits.length >= 2 && hay.replace(/\D/g, '').includes(qDigits);
    });
  });

  // Called on every keystroke (v-model). Debounce the network lookup: fire
  // it once the user has paused for USER_SEARCH_DEBOUNCE_MS.
  function onUserSearchInput() {
    userSearchError.value = '';
    userSearchResult.value = null;
    if (userSearchTimer) clearTimeout(userSearchTimer);
    userSearchSeq++; // cancel any in-flight response
    userSearchBusy.value = false;
    if (!userSearchQuery.value.trim()) return;
    userSearchTimer = setTimeout(() => { userSearchTimer = null; runUserSearch(); }, USER_SEARCH_DEBOUNCE_MS);
  }

  // Exact match only (ADR 0017): a "+"-prefixed / all-digit query hits
  // /users/by-phone, anything else /users/by-username. No prefix search.
  async function runUserSearch() {
    if (userSearchTimer) { clearTimeout(userSearchTimer); userSearchTimer = null; }
    const raw = userSearchQuery.value.trim();
    userSearchError.value = '';
    userSearchResult.value = null;
    if (!raw) return;
    const seq = ++userSearchSeq;
    userSearchBusy.value = true;
    try {
      const looksLikePhone = /^\+?\d[\d\s-]*$/.test(raw);
      const path = looksLikePhone
        ? `/users/by-phone?phone_number=${encodeURIComponent(raw.replace(/[\s-]/g, ''))}`
        : `/users/by-username?username=${encodeURIComponent(raw.replace(/^@/, ''))}`;
      const user = await ctx.apiFetch(path);
      if (seq !== userSearchSeq) return;
      userSearchResult.value = user;
    } catch (err) {
      if (seq !== userSearchSeq) return;
      userSearchError.value = err.status === 404 ? 'No user found — check the exact username or phone number.' : ctx.friendlyError(err, "We couldn't run that search. Please try again.");
    } finally {
      if (seq === userSearchSeq) userSearchBusy.value = false;
    }
  }

  // --- New-group modal: member picker (step 1) ---------------------------
  // Your private-chat peers, in sidebar (chat-list) order - shown first in
  // the picker list. Each entry is a UserOut (resolved from userById).
  const privateChatPeers = computed(() => {
    const out = [];
    const seen = new Set();
    for (const item of ctx.chats.value) {
      const chat = item.chat;
      if (chat.is_group) continue;
      const otherId = ctx.privateChatOtherUserId.value[chat.id];
      if (!otherId || seen.has(otherId)) continue;
      const user = ctx.userById.value[otherId];
      if (!user) continue;
      seen.add(otherId);
      out.push(user);
    }
    return out;
  });

  // Exact-match user lookup for the group picker (same logic as the
  // new-chat search: +digits -> /users/by-phone, else /users/by-username).
  const groupSearchQuery = ref('');
  const groupSearchBusy = ref(false);
  const groupSearchError = ref('');
  const groupSearchResult = ref(null); // a UserOut, or null
  let groupSearchSeq = 0;
  let groupSearchTimer = null;

  function resetGroupSearch() {
    if (groupSearchTimer) { clearTimeout(groupSearchTimer); groupSearchTimer = null; }
    groupSearchQuery.value = '';
    groupSearchBusy.value = false;
    groupSearchError.value = '';
    groupSearchResult.value = null;
  }

  function onGroupSearchInput() {
    groupSearchError.value = '';
    groupSearchResult.value = null;
    if (groupSearchTimer) clearTimeout(groupSearchTimer);
    groupSearchSeq++;
    groupSearchBusy.value = false;
    if (!groupSearchQuery.value.trim()) return;
    groupSearchTimer = setTimeout(() => { groupSearchTimer = null; runGroupSearch(); }, 1500);
  }

  async function runGroupSearch() {
    if (groupSearchTimer) { clearTimeout(groupSearchTimer); groupSearchTimer = null; }
    const raw = groupSearchQuery.value.trim();
    groupSearchError.value = '';
    groupSearchResult.value = null;
    if (!raw) return;
    const seq = ++groupSearchSeq;
    groupSearchBusy.value = true;
    try {
      const looksLikePhone = /^\+?\d[\d\s-]*$/.test(raw);
      const path = looksLikePhone
        ? `/users/by-phone?phone_number=${encodeURIComponent(raw.replace(/[\s-]/g, ''))}`
        : `/users/by-username?username=${encodeURIComponent(raw.replace(/^@/, ''))}`;
      const user = await ctx.apiFetch(path);
      if (seq !== groupSearchSeq) return;
      ctx.userById.value[user.id] = user;
      groupSearchResult.value = user;
    } catch (err) {
      if (seq !== groupSearchSeq) return;
      groupSearchError.value = err.status === 404 ? 'No user found — check the exact username or phone number.' : ctx.friendlyError(err, "We couldn't run that search. Please try again.");
    } finally {
      if (seq === groupSearchSeq) groupSearchBusy.value = false;
    }
  }

  function openNewGroupModal() {
    resetGroupSearch();
    newGroupError.value = '';
    showNewChatModal.value = false;
    showNewGroupModal.value = true;
  }

  // Pick an existing chat straight from the partial-match list.
  async function pickExistingChat(chatId) {
    showNewChatModal.value = false;
    await ctx.selectChat(chatId);
  }

  // Pick the found user: open a draft chat (or the existing one), same as
  // the old createPrivateChat tail.
  async function pickSearchedUser(user) {
    showNewChatModal.value = false;
    const existing = ctx.chats.value.find(
      (c) => !c.chat.is_group && ctx.privateChatOtherUserId.value[c.chat.id] === user.id
    );
    if (existing) { await ctx.selectChat(existing.chat.id); return; }
    ctx.userById.value[user.id] = user;
    ctx.openDraftChat(user);
  }

  // Opening a private chat no longer creates it: we resolve the phone number
  // to a user and open a *draft* pane. The chat row is created server-side
  // only when the first message is sent (useMessageSend.sendMessage). If an
  // actual chat with that user already exists, just open it.
  async function createPrivateChat() {
    chatFormError.value = '';
    const phone = newPrivatePhone.value.trim();
    if (!phone) { chatFormError.value = 'Enter a phone number.'; return; }
    try {
      const target = await ctx.apiFetch(`/users/by-phone?phone_number=${encodeURIComponent(phone)}`);
      newPrivatePhone.value = '';
      showNewPrivate.value = false;

      const existing = ctx.chats.value.find(
        (c) => !c.chat.is_group && ctx.privateChatOtherUserId.value[c.chat.id] === target.id
      );
      if (existing) { await ctx.selectChat(existing.chat.id); return; }

      ctx.openDraftChat(target);
    } catch (err) {
      chatFormError.value = err.status === 404 ? `No user with phone number ${phone}` : ctx.friendlyError(err, "We couldn't start that chat. Please try again.");
    }
  }

  // Commit the open draft chat: POST /chats/private, cache what
  // resolvePrivateChatTitle would have, refresh the list, make it active.
  // Returns the new chat id, or null if there's no draft.
  async function commitDraftChat() {
    const draft = ctx.draftChat.value;
    if (!draft) return null;
    const chat = await ctx.apiFetch('/chats/private', {
      method: 'POST',
      body: JSON.stringify({ other_user_id: draft.otherUserId }),
    });
    ctx.privateChatTitles.value[chat.id] = draft.phone;
    ctx.privateChatOtherUserId.value[chat.id] = draft.otherUserId;
    // selectChat() discards the draft (and its presence sub); re-subscribe
    // happens inside selectChat for the now-real chat.
    await ctx.loadChats();
    await ctx.selectChat(chat.id);
    return chat.id;
  }

  // Upload a picked photo straight to storage and return its object key.
  // `ticketPath` is the presigned-PUT endpoint (chat-less for group
  // creation, chat-scoped for an existing group). The app never sees the
  // bytes - it only mints the URL and, later, records the key.
  async function uploadAvatarBytes(ticketPath, file) {
    // Downscale/recompress in-browser if it's over the avatar cap.
    if (file.size > ctx.AVATAR_MAX_BYTES) {
      file = await ctx.shrinkImageToFit(file, ctx.AVATAR_MAX_BYTES, { maxDim: 512 });
    }
    const ticket = await ctx.apiFetch(ticketPath, {
      method: 'POST',
      body: JSON.stringify({ mime_type: file.type, size_bytes: file.size }),
    });
    // fetch() won't let JS set Content-Length, but the browser sets it
    // itself to the body's byte length - which matches the signed
    // size_bytes since we declared file.size. The header in the dict is a
    // harmless no-op.
    const putResp = await fetch(ticket.upload_url, {
      method: 'PUT',
      headers: ticket.required_headers || { 'Content-Type': file.type },
      body: file,
    });
    if (!putResp.ok) {
      const detail = await putResp.text().catch(() => '');
      throw new Error('storage rejected the upload (' + putResp.status + ') ' + detail.slice(0, 200));
    }
    return ticket.storage_key;
  }

  // payload: { title, about, memberUsers, photoFile } from NewGroupModal.
  // `memberUsers` is a list of already-resolved UserOut objects picked in
  // step 1 of the wizard (peers + exact-match search hits).
  async function createGroupChat(payload) {
    newGroupError.value = '';
    const title = (payload.title || '').trim();
    if (!title) { newGroupError.value = 'Enter a group name.'; return; }
    newGroupBusy.value = true;
    try {
      const targets = payload.memberUsers || [];
      const memberIds = targets.map((t) => t.id);
      for (const t of targets) ctx.userById.value[t.id] = t; // already known - skip a later round trip

      const body = { title, initial_member_ids: memberIds };
      const about = (payload.about || '').trim();
      if (about) body.about_text = about;

      // Upload the photo BEFORE creating the group, so the group is born
      // with its avatar in one atomic POST (the backend HEAD-validates the
      // key). A failed upload aborts creation - nothing half-made.
      if (payload.photoFile) {
        body.avatar_storage_key = await uploadAvatarBytes('/chats/groups/avatar/upload-ticket', payload.photoFile);
        // Inline avatar thumbnail (ADR 0016) - best-effort.
        try { body.avatar_preview = await ctx.encodeAvatarPreview(payload.photoFile); } catch (_) {}
      }

      const chat = await ctx.apiFetch('/chats/groups', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      showNewGroupModal.value = false;
      await ctx.loadChats();
      await ctx.selectChat(chat.id);
    } catch (err) {
      newGroupError.value = ctx.friendlyError(err, "We couldn't create the group. Please try again.");
    } finally {
      newGroupBusy.value = false;
    }
  }

  return {
    showNewPrivate, showNewChatModal, showNewGroupModal, newGroupBusy, newGroupError,
    newPrivatePhone, chatFormError,
    userSearchQuery, userSearchBusy, userSearchError, userSearchResult,
    existingChatMatches,
    openNewChatModal, resetUserSearch, runUserSearch, onUserSearchInput,
    pickSearchedUser, pickExistingChat,
    privateChatPeers,
    groupSearchQuery, groupSearchBusy, groupSearchError, groupSearchResult,
    onGroupSearchInput, runGroupSearch, openNewGroupModal,
    createPrivateChat, commitDraftChat, uploadAvatarBytes, createGroupChat,
  };
}

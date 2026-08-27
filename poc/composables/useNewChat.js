// New chat / group creation: the private-chat form and the group-creation
// modal (including one-shot creation-with-photo). Global `useNewChat(ctx)`
// factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, privateChatTitles, privateChatOtherUserId,
// userById, loadChats, selectChat. (createGroupChat takes its payload from
// NewGroupModal.)
function useNewChat(ctx) {
  const { ref } = Vue;

  const showNewPrivate = ref(false);
  const showNewGroupModal = ref(false);
  const newGroupBusy = ref(false);
  const newGroupError = ref('');
  const newPrivatePhone = ref('');
  const chatFormError = ref('');

  async function createPrivateChat() {
    chatFormError.value = '';
    const phone = newPrivatePhone.value.trim();
    if (!phone) { chatFormError.value = 'Enter a phone number.'; return; }
    try {
      // /users/by-phone resolves the number to a user id first - the
      // create-chat endpoint itself still takes other_user_id, it just
      // doesn't know about phone numbers.
      const target = await ctx.apiFetch(`/users/by-phone?phone_number=${encodeURIComponent(phone)}`);
      const chat = await ctx.apiFetch('/chats/private', {
        method: 'POST',
        body: JSON.stringify({ other_user_id: target.id }),
      });
      // Cache everything resolvePrivateChatTitle would have fetched - it
      // early-returns once the title is set, so without this the new chat
      // has no privateChatOtherUserId / userById entry and its avatar
      // (and presence) never resolve until the next full reload.
      ctx.privateChatTitles.value[chat.id] = target.phone_number;
      ctx.privateChatOtherUserId.value[chat.id] = target.id;
      ctx.userById.value[target.id] = target;
      newPrivatePhone.value = '';
      showNewPrivate.value = false;
      await ctx.loadChats();
      await ctx.selectChat(chat.id);
    } catch (err) {
      chatFormError.value = err.status === 404 ? `No user with phone number ${phone}` : err.message;
    }
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

  // payload: { title, about, memberPhones, photoFile } from NewGroupModal.
  async function createGroupChat(payload) {
    newGroupError.value = '';
    const title = (payload.title || '').trim();
    if (!title) { newGroupError.value = 'Enter a group name.'; return; }
    const phones = (payload.memberPhones || '')
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    newGroupBusy.value = true;
    try {
      // Resolve each member phone number to a user id first (/chats/groups
      // itself only knows ids), same as creating a private chat.
      const targets = await Promise.all(
        phones.map((phone) => ctx.apiFetch(`/users/by-phone?phone_number=${encodeURIComponent(phone)}`).catch((err) => {
          throw err.status === 404 ? new Error(`No user with phone number ${phone}`) : err;
        }))
      );
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
      }

      const chat = await ctx.apiFetch('/chats/groups', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      showNewGroupModal.value = false;
      await ctx.loadChats();
      await ctx.selectChat(chat.id);
    } catch (err) {
      newGroupError.value = err.message;
    } finally {
      newGroupBusy.value = false;
    }
  }

  return {
    showNewPrivate, showNewGroupModal, newGroupBusy, newGroupError,
    newPrivatePhone, chatFormError,
    createPrivateChat, uploadAvatarBytes, createGroupChat,
  };
}

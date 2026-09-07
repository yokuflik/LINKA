// Profile editing for the current user and for a group. Same direct-to-storage
// avatar model as sign-up (see useAuth.uploadPickedAvatar): ask for a presigned
// PUT ticket, PUT the bytes at MinIO, then commit the object key. Text fields
// go through PATCH /users/me and PATCH /chats/{id}.
// Global `useProfileEdit(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, log, logError, currentUser, activeChatId,
// activeChatItem, AVATAR_MAX_BYTES, AVATAR_MIME, shrinkImageToFit, loadChats,
// showToast, userById, groupChatMembers.
// Privacy settings live in the separate ⚙ Settings modal (useSettings.js).
function useProfileEdit(ctx) {
  const { ref } = Vue;
  const { AVATAR_MAX_BYTES, AVATAR_MIME, shrinkImageToFit } = ctx;

  // ---------------------------------------------------------------
  // Shared avatar-file picker - a picked file kept in memory until the
  // surrounding form is saved (mirrors useAuth's sign-up picker).
  // ---------------------------------------------------------------
  function makeAvatarPicker() {
    const file = ref(null);
    const previewUrl = ref(null);
    const error = ref('');
    // true once the user hit the "remove photo" control, so save() knows to
    // DELETE the existing avatar rather than leave it untouched.
    const cleared = ref(false);

    async function pick(f) {
      error.value = '';
      if (!AVATAR_MIME.includes(f.type)) { error.value = 'Use a JPEG, PNG or WebP image.'; return; }
      if (f.size <= 0) { error.value = 'That file looks empty.'; return; }
      if (f.size > AVATAR_MAX_BYTES) {
        f = await shrinkImageToFit(f, AVATAR_MAX_BYTES, { maxDim: 512 });
      }
      if (f.size > AVATAR_MAX_BYTES) { error.value = 'Profile picture must be 512 KB or smaller.'; return; }
      revoke();
      file.value = f;
      previewUrl.value = URL.createObjectURL(f);
      cleared.value = false;
    }

    function clear() {
      revoke();
      file.value = null;
      previewUrl.value = null;
      cleared.value = true;
      error.value = '';
    }

    function reset() {
      revoke();
      file.value = null;
      previewUrl.value = null;
      cleared.value = false;
      error.value = '';
    }

    function revoke() {
      if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
    }

    return { file, previewUrl, error, cleared, pick, clear, reset };
  }

  // Direct-to-storage upload against one of the two avatar endpoint pairs.
  // `base` is '/users/me' or `/chats/${id}` - both expose
  // {base}/avatar/upload-ticket (POST) and {base}/avatar (PUT/DELETE) with
  // the same request/response shape.
  async function commitAvatar(base, picker) {
    if (picker.file.value) {
      const f = picker.file.value;
      const ticket = await ctx.apiFetch(`${base}/avatar/upload-ticket`, {
        method: 'POST',
        body: JSON.stringify({ mime_type: f.type, size_bytes: f.size }),
      });
      const putResp = await fetch(ticket.upload_url, {
        method: 'PUT',
        headers: ticket.required_headers || { 'Content-Type': f.type },
        body: f,
      });
      if (!putResp.ok) throw new Error('avatar upload failed (' + putResp.status + ')');
      // Inline avatar thumbnail (ADR 0016) - best-effort.
      let preview = null;
      try { preview = await ctx.encodeAvatarPreview(f); } catch (_) {}
      return await ctx.apiFetch(`${base}/avatar`, {
        method: 'PUT',
        body: JSON.stringify({ storage_key: ticket.storage_key, preview }),
      });
    }
    if (picker.cleared.value) {
      return await ctx.apiFetch(`${base}/avatar`, { method: 'DELETE' });
    }
    return null;
  }

  // ---------------------------------------------------------------
  // Current-user profile
  // ---------------------------------------------------------------
  const showProfileModal = ref(false);
  const profileForm = ref({ about_text: '', username: '' });
  const profileBusy = ref(false);
  const profileError = ref('');
  const profileAvatar = makeAvatarPicker();
  // Live username availability for the edit form (same shape as the sign-up
  // welcome form). status: '' | 'checking' | 'ok' | 'bad'.
  const profileUsernameCheck = ref({ status: '', reason: '' });
  let _profUsernameTimer = null;

  function openProfileModal() {
    const u = ctx.currentUser.value || {};
    profileForm.value = {
      about_text: u.about_text || '',
      username: u.username || '',
    };
    profileUsernameCheck.value = { status: '', reason: '' };
    if (_profUsernameTimer) clearTimeout(_profUsernameTimer);
    profileAvatar.reset();
    profileError.value = '';
    showProfileModal.value = true;
  }

  function checkProfileUsername() {
    const raw = (profileForm.value.username || '').trim();
    const current = (ctx.currentUser.value && ctx.currentUser.value.username) || '';
    if (_profUsernameTimer) clearTimeout(_profUsernameTimer);
    if (!raw || raw === current) {
      profileUsernameCheck.value = { status: '', reason: '' };
      return;
    }
    profileUsernameCheck.value = { status: 'checking', reason: '' };
    _profUsernameTimer = setTimeout(async () => {
      try {
        const r = await ctx.apiFetch('/users/username-available?username=' + encodeURIComponent(raw));
        if ((profileForm.value.username || '').trim() !== raw) return; // stale
        profileUsernameCheck.value = r.available
          ? { status: 'ok', reason: '' }
          : { status: 'bad', reason: r.reason || '' };
      } catch (_) {
        profileUsernameCheck.value = { status: '', reason: '' };
      }
    }, 400);
  }

  async function saveProfile() {
    profileError.value = '';
    profileBusy.value = true;
    try {
      const u = ctx.currentUser.value || {};
      const patch = {};
      const about = (profileForm.value.about_text || '').trim();
      const uname = (profileForm.value.username || '').trim();
      // PATCH /users/me treats null-vs-value, not "" - only send changed fields.
      if (about !== (u.about_text || '')) patch.about_text = about;
      if (uname && uname !== (u.username || '')) patch.username = uname;
      if (Object.keys(patch).length) {
        ctx.currentUser.value = await ctx.apiFetch('/users/me', {
          method: 'PATCH',
          body: JSON.stringify(patch),
        });
      }
      const afterAvatar = await commitAvatar('/users/me', profileAvatar);
      if (afterAvatar) ctx.currentUser.value = afterAvatar;

      // currentUser (above) drives our own name/avatar in the app header. The
      // backend also fans a `profile_updated` event back to us over every
      // shared chat, and useWsRouter merges that into the shared caches
      // (userById + the per-group member rows) and into currentUser. We still
      // patch the caches here directly so the change is visible even if that
      // best-effort Redis fan-out never arrives.
      const me = ctx.currentUser.value || {};
      if (me.id) {
        // New avatar -> evict our own stale full-res image from the device cache.
        if (ctx.noteAvatarUrl) ctx.noteAvatarUrl('user:' + me.id, me.profile_pic_url || null);
        const existing = ctx.userById.value[me.id] || { id: me.id };
        ctx.userById.value[me.id] = {
          ...existing,
          username: me.username,
          about_text: me.about_text,
          profile_pic_url: me.profile_pic_url,
          profile_pic_preview: me.profile_pic_preview || null,
        };
        Object.values(ctx.groupChatMembers.value || {}).forEach((members) => {
          const row = (members || []).find((m) => m.user && m.user.id === me.id);
          if (row) row.user = ctx.userById.value[me.id];
        });
      }
      showProfileModal.value = false;
      ctx.showToast('Profile updated');
    } catch (err) {
      // Backend sends { detail, reason } for a rejected username (ADR 0017):
      // taken / cooldown / grace_hold / format codes.
      const reason = err && err.body && err.body.reason;
      if (reason) {
        const map = {
          too_short: 'Username too short (min 3)', too_long: 'Username too long (max 32)',
          bad_chars: 'Username: letters, digits and _ only',
          must_start_letter: 'Username must start with a letter',
          reserved: 'That username is reserved', taken: 'That username is already taken',
          grace_hold: 'That username was recently released and is not available yet',
          cooldown: 'You changed your username recently - try again later',
        };
        profileError.value = map[reason] || ('Username: ' + reason.replace(/_/g, ' '));
        profileUsernameCheck.value = { status: 'bad', reason };
      } else {
        profileError.value = err.message || String(err);
      }
    } finally {
      profileBusy.value = false;
    }
  }

  // ---------------------------------------------------------------
  // Group profile (title / description / photo) - admin/owner only, gated
  // both here and server-side (chat_service._require_role, ROLE_ADMIN).
  // ---------------------------------------------------------------
  const showGroupEditModal = ref(false);
  const groupForm = ref({ title: '', about_text: '' });
  const groupEditBusy = ref(false);
  const groupEditError = ref('');
  const groupAvatar = makeAvatarPicker();

  function openGroupEditModal() {
    const chat = ctx.activeChatItem.value && ctx.activeChatItem.value.chat;
    if (!chat) return;
    groupForm.value = {
      title: chat.title || '',
      about_text: chat.about_text || '',
    };
    groupAvatar.reset();
    groupEditError.value = '';
    showGroupEditModal.value = true;
  }

  async function saveGroupEdit() {
    const chat = ctx.activeChatItem.value && ctx.activeChatItem.value.chat;
    if (!chat) return;
    groupEditError.value = '';
    const title = (groupForm.value.title || '').trim();
    if (!title) { groupEditError.value = 'Group name cannot be empty.'; return; }
    groupEditBusy.value = true;
    try {
      const patch = {};
      const about = (groupForm.value.about_text || '').trim();
      if (title !== (chat.title || '')) patch.title = title;
      if (about !== (chat.about_text || '')) patch.about_text = about;
      if (Object.keys(patch).length) {
        await ctx.apiFetch(`/chats/${chat.id}`, {
          method: 'PATCH',
          body: JSON.stringify(patch),
        });
      }
      await commitAvatar(`/chats/${chat.id}`, groupAvatar);

      // The backend fans out a `chat_updated` event (+ a system message) that
      // this client also receives, so the sidebar/header update on that -
      // no explicit refresh needed here.
      showGroupEditModal.value = false;
      ctx.showToast('Group info updated');
    } catch (err) {
      if (err.status === 403) groupEditError.value = 'Only group admins can edit group info.';
      else groupEditError.value = err.message || String(err);
    } finally {
      groupEditBusy.value = false;
    }
  }

  return {
    showProfileModal, profileForm, profileBusy, profileError, profileAvatar,
    profileUsernameCheck, openProfileModal, saveProfile, checkProfileUsername,
    showGroupEditModal, groupForm, groupEditBusy, groupEditError, groupAvatar,
    openGroupEditModal, saveGroupEdit,
  };
}

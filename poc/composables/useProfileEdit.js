// Profile editing for the current user and for a group. Same direct-to-storage
// avatar model as sign-up (see useAuth.uploadPickedAvatar): ask for a presigned
// PUT ticket, PUT the bytes at MinIO, then commit the object key. Text fields
// go through PATCH /users/me and PATCH /chats/{id}.
// Global `useProfileEdit(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, log, logError, currentUser, activeChatId,
// activeChatItem, AVATAR_MAX_BYTES, AVATAR_MIME, shrinkImageToFit, loadChats,
// showToast, userById, groupChatMembers, userSettings, loadSettings,
// saveSettings.
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
      return await ctx.apiFetch(`${base}/avatar`, {
        method: 'PUT',
        body: JSON.stringify({ storage_key: ticket.storage_key }),
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
  // `privacy_online` is edited alongside the profile fields but saved through
  // the separate /users/me/settings endpoint (see saveProfile).
  const profileForm = ref({ display_name: '', about_text: '', privacy_online: 'everyone' });
  const profileBusy = ref(false);
  const profileError = ref('');
  const profileAvatar = makeAvatarPicker();

  async function openProfileModal() {
    const u = ctx.currentUser.value || {};
    if (!ctx.userSettings.value) await ctx.loadSettings();
    const s = ctx.userSettings.value || {};
    profileForm.value = {
      display_name: u.display_name || '',
      about_text: u.about_text || '',
      privacy_online: (s.privacy && s.privacy.online) || 'everyone',
    };
    profileAvatar.reset();
    profileError.value = '';
    showProfileModal.value = true;
  }

  async function saveProfile() {
    profileError.value = '';
    profileBusy.value = true;
    try {
      const u = ctx.currentUser.value || {};
      const patch = {};
      const name = (profileForm.value.display_name || '').trim();
      const about = (profileForm.value.about_text || '').trim();
      // PATCH /users/me treats null-vs-value, not "" - only send changed fields.
      if (name !== (u.display_name || '')) patch.display_name = name;
      if (about !== (u.about_text || '')) patch.about_text = about;
      if (Object.keys(patch).length) {
        ctx.currentUser.value = await ctx.apiFetch('/users/me', {
          method: 'PATCH',
          body: JSON.stringify(patch),
        });
      }
      const afterAvatar = await commitAvatar('/users/me', profileAvatar);
      if (afterAvatar) ctx.currentUser.value = afterAvatar;

      // Privacy settings go through a separate endpoint (partial patch).
      const s = ctx.userSettings.value || {};
      const currentOnline = (s.privacy && s.privacy.online) || 'everyone';
      if (profileForm.value.privacy_online !== currentOnline) {
        await ctx.saveSettings({ privacy: { online: profileForm.value.privacy_online } });
      }

      // currentUser (above) drives our own name/avatar in the app header. The
      // backend also fans a `profile_updated` event back to us over every
      // shared chat, and useWsRouter merges that into the shared caches
      // (userById + the per-group member rows) and into currentUser. We still
      // patch the caches here directly so the change is visible even if that
      // best-effort Redis fan-out never arrives.
      const me = ctx.currentUser.value || {};
      if (me.id) {
        const existing = ctx.userById.value[me.id] || { id: me.id };
        ctx.userById.value[me.id] = {
          ...existing,
          display_name: me.display_name,
          about_text: me.about_text,
          profile_pic_url: me.profile_pic_url,
        };
        Object.values(ctx.groupChatMembers.value || {}).forEach((members) => {
          const row = (members || []).find((m) => m.user && m.user.id === me.id);
          if (row) row.user = ctx.userById.value[me.id];
        });
      }
      showProfileModal.value = false;
      ctx.showToast('Profile updated');
    } catch (err) {
      profileError.value = err.message || String(err);
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
    openProfileModal, saveProfile,
    showGroupEditModal, groupForm, groupEditBusy, groupEditError, groupAvatar,
    openGroupEditModal, saveGroupEdit,
  };
}

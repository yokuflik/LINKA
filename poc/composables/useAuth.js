// Auth: OTP request/verify, the sign-up profile-photo picker + direct-to-storage
// upload, and logout (which resets state owned by other composables through ctx).
// Global `useAuth(ctx)` factory; needs ctx.apiFetch / ctx.log / ctx.logError /
// ctx.AVATAR_* from useCore, and ctx.connectWebSocket / ctx.disconnectWebSocket /
// ctx.loadChats / the per-domain reset hooks wired by the root setup().
function useAuth(ctx) {
  const { ref, computed } = Vue;
  const { apiFetch, log, logError, AVATAR_MAX_BYTES, AVATAR_MIME, shrinkImageToFit } = ctx;

  // ---------------------------------------------------------------
  // Auth state
  // ---------------------------------------------------------------
  const accessToken = ref(localStorage.getItem('linka_access_token') || '');
  const refreshToken = ref(localStorage.getItem('linka_refresh_token') || '');
  const currentUser = ref(null);
  const isAuthed = computed(() => !!accessToken.value && !!currentUser.value);

  const authMode = ref('login'); // 'login' | 'register'
  const authStage = ref('phone'); // 'phone' | 'otp'
  const phoneNumber = ref('');
  const otpCode = ref('');
  const profileDraft = ref({ display_name: '', about_text: '' });
  const authError = ref('');
  const authBusy = ref(false);

  const avatarFile = ref(null);
  const avatarPreviewUrl = ref(null);
  const avatarError = ref('');

  async function pickAvatar(file) {
    avatarError.value = '';
    if (!AVATAR_MIME.includes(file.type)) {
      avatarError.value = 'Use a JPEG, PNG or WebP image.';
      return;
    }
    if (file.size <= 0) {
      avatarError.value = 'That file looks empty.';
      return;
    }
    // Downscale/recompress in-browser if it's over the backend cap.
    if (file.size > AVATAR_MAX_BYTES) {
      file = await shrinkImageToFit(file, AVATAR_MAX_BYTES, { maxDim: 512 });
    }
    if (file.size > AVATAR_MAX_BYTES) {
      avatarError.value = 'Profile picture must be 512 KB or smaller.';
      return;
    }
    clearAvatar();
    avatarFile.value = file;
    avatarPreviewUrl.value = URL.createObjectURL(file);
  }

  function clearAvatar() {
    if (avatarPreviewUrl.value) URL.revokeObjectURL(avatarPreviewUrl.value);
    avatarFile.value = null;
    avatarPreviewUrl.value = null;
    avatarError.value = '';
  }

  // Direct-to-storage upload: ask the app for a presigned PUT ticket, PUT
  // the bytes straight at MinIO, then tell the app the object key.
  async function uploadPickedAvatar() {
    const file = avatarFile.value;
    if (!file) return;
    const ticket = await apiFetch('/users/me/avatar/upload-ticket', {
      method: 'POST',
      body: JSON.stringify({ mime_type: file.type, size_bytes: file.size }),
    });
    const putResp = await fetch(ticket.upload_url, {
      method: 'PUT',
      headers: ticket.required_headers || { 'Content-Type': file.type },
      body: file,
    });
    if (!putResp.ok) throw new Error('avatar upload failed (' + putResp.status + ')');
    currentUser.value = await apiFetch('/users/me/avatar', {
      method: 'PUT',
      body: JSON.stringify({ storage_key: ticket.storage_key }),
    });
  }

  // ---------------------------------------------------------------
  // Auth actions
  // ---------------------------------------------------------------
  async function requestOtp() {
    authError.value = '';
    authBusy.value = true;
    try {
      await apiFetch('/auth/otp/request', {
        method: 'POST',
        body: JSON.stringify({
          phone_number: phoneNumber.value,
          intent: authMode.value === 'register' ? 'register' : 'login',
        }),
      });
      authStage.value = 'otp';
      log('OTP requested for', phoneNumber.value, '- no SMS provider is wired up, check the SERVER console/log for the code');
    } catch (err) {
      authError.value = err.message || 'Failed to request code';
    } finally {
      authBusy.value = false;
    }
  }

  async function verifyOtp() {
    authError.value = '';
    authBusy.value = true;
    try {
      const body = await apiFetch('/auth/otp/verify', {
        method: 'POST',
        body: JSON.stringify({ phone_number: phoneNumber.value, code: otpCode.value }),
      });
      accessToken.value = body.access_token;
      refreshToken.value = body.refresh_token;
      currentUser.value = body.user;
      localStorage.setItem('linka_access_token', accessToken.value);
      localStorage.setItem('linka_refresh_token', refreshToken.value);
      log('logged in as', currentUser.value);

      // On register, push the profile fields the user filled in on the
      // sign-up form. Best-effort - a failure here shouldn't block login.
      if (authMode.value === 'register') {
        const patch = {};
        const name = (profileDraft.value.display_name || '').trim();
        const about = (profileDraft.value.about_text || '').trim();
        if (name) patch.display_name = name;
        if (about) patch.about_text = about;
        if (Object.keys(patch).length) {
          try {
            currentUser.value = await apiFetch('/users/me', {
              method: 'PATCH',
              body: JSON.stringify(patch),
            });
          } catch (err) {
            logError('failed to save profile on register:', err.message);
          }
        }
        try {
          await uploadPickedAvatar();
        } catch (err) {
          logError('failed to upload avatar on register:', err.message);
        }
      }

      ctx.connectWebSocket();
      await ctx.loadChats();
    } catch (err) {
      authError.value = err.message || 'Invalid code';
    } finally {
      authBusy.value = false;
    }
  }

  function logout() {
    ctx.disconnectWebSocket();
    accessToken.value = '';
    refreshToken.value = '';
    currentUser.value = null;
    ctx.chats.value = [];
    ctx.messages.value = [];
    ctx.activeChatId.value = null;
    ctx.resetPresence();
    ctx.resetTyping();
    ctx.resetSettings();
    ctx.unreadCountByChatId.value = {};
    ctx.contextMenuMessage.value = null;
    ctx.replyingToMessage.value = null;
    authStage.value = 'phone';
    otpCode.value = '';
    profileDraft.value = { display_name: '', about_text: '' };
    clearAvatar();
    phoneNumber.value = '';
    otpCode.value = '';
    localStorage.removeItem('linka_access_token');
    localStorage.removeItem('linka_refresh_token');
    log('logged out');
  }

  return {
    accessToken, refreshToken, currentUser, isAuthed,
    authMode, authStage, phoneNumber, otpCode, profileDraft, authError, authBusy,
    avatarFile, avatarPreviewUrl, avatarError,
    pickAvatar, clearAvatar, uploadPickedAvatar,
    requestOtp, verifyOtp, logout,
  };
}

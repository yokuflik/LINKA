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
  // Resolved phone value (E.164, or the raw "1".."5" dev token). Kept as a ref
  // so lifecycle code that still destructures `phoneNumber` from ctx keeps
  // working; synced from usePhoneInput's `resolvedPhone` at request time.
  const phoneNumber = ref('');
  const otpCode = ref('');
  // Firebase confirmationResult between requestOtp() and verifyOtp() for the
  // real-SMS path (ADR 0009). null on the dev-whitelist path.
  let firebaseConfirmation = null;
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
  // Fresh invisible reCAPTCHA verifier per request - Firebase consumes the
  // token on each signInWithPhoneNumber, so a resend needs a new one.
  function firebaseRecaptcha() {
    if (window._linkaRecaptcha) {
      try { window._linkaRecaptcha.clear(); } catch (e) { /* already gone */ }
    }
    window._linkaRecaptcha = new firebase.auth.RecaptchaVerifier('recaptcha-container', { size: 'invisible' });
    return window._linkaRecaptcha;
  }

  async function requestOtp() {
    authError.value = '';
    authBusy.value = true;
    phoneNumber.value = ctx.resolvedPhone.value;
    try {
      if (ctx.phoneIsWhitelisted.value) {
        // Dev-whitelist number (1..5): legacy OTP stub, code is anything.
        await apiFetch('/auth/otp/request', {
          method: 'POST',
          body: JSON.stringify({
            phone_number: phoneNumber.value,
            intent: authMode.value === 'register' ? 'register' : 'login',
          }),
        });
        log('OTP (dev stub) requested for', phoneNumber.value, '- any code works');
      } else {
        // Real number: Firebase sends the SMS entirely client-side.
        if (!window.firebaseAuth) throw new Error('Phone verification is unavailable (Firebase not loaded)');
        firebaseConfirmation = await window.firebaseAuth.signInWithPhoneNumber(phoneNumber.value, firebaseRecaptcha());
        log('Firebase SMS sent to', phoneNumber.value);
      }
      authStage.value = 'otp';
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
      let body;
      if (ctx.phoneIsWhitelisted.value) {
        body = await apiFetch('/auth/otp/verify', {
          method: 'POST',
          body: JSON.stringify({ phone_number: phoneNumber.value, code: otpCode.value }),
        });
      } else {
        // Confirm the SMS code with Firebase, then trade its ID token for our pair.
        if (!firebaseConfirmation) throw new Error('Request a code first');
        const cred = await firebaseConfirmation.confirm(otpCode.value);
        const idToken = await cred.user.getIdToken();
        body = await apiFetch('/auth/firebase/verify', {
          method: 'POST',
          body: JSON.stringify({ id_token: idToken }),
        });
        try { await window.firebaseAuth.signOut(); } catch (e) { /* our JWT is the truth */ }
        firebaseConfirmation = null;
      }
      await finishLogin(body);
    } catch (err) {
      authError.value = err.message || 'Invalid code';
    } finally {
      authBusy.value = false;
    }
  }

  // Shared post-verify tail for both the dev-stub and Firebase paths.
  async function finishLogin(body) {
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
  }

  function logout() {
    ctx.disconnectWebSocket();
    accessToken.value = '';
    refreshToken.value = '';
    currentUser.value = null;
    ctx.chats.value = [];
    ctx.messages.value = [];
    ctx.activeChatId.value = null;
    ctx.draftChat.value = null;
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
    firebaseConfirmation = null;
    if (ctx.resetPhoneInput) ctx.resetPhoneInput();
    ctx.clearAllMessageCache();
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

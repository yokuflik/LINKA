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

  // One flow (ADR 0017): phone -> otp -> (new accounts only) welcome.
  const authStage = ref('phone'); // 'phone' | 'otp' | 'welcome'
  // Resolved phone value (E.164, or the raw "1".."5" dev token). Kept as a ref
  // so lifecycle code that still destructures `phoneNumber` from ctx keeps
  // working; synced from usePhoneInput's `resolvedPhone` at request time.
  const phoneNumber = ref('');
  const otpCode = ref('');
  // Firebase confirmationResult between requestOtp() and verifyOtp() for the
  // real-SMS path (ADR 0009). null on the dev-whitelist path.
  let firebaseConfirmation = null;
  const profileDraft = ref({ about_text: '', username: '' });
  // Advisory username availability for the welcome form. status: '' | 'checking'
  // | 'ok' | 'bad'; reason is the backend machine code when status==='bad'.
  const usernameCheck = ref({ status: '', reason: '' });
  let _usernameCheckTimer = null;
  const authError = ref('');
  const authBusy = ref(false);

  // Connection-retry banner for the auth screen. While apiFetch is looping on
  // a network failure / rate-limit, connRetry holds { reason, secondsLeft }
  // and a 1s ticker counts it down; cleared on success / abandon.
  const connRetry = ref(null);
  let _connRetryTimer = null;
  let _authScreenLeft = false;
  function clearConnRetry() {
    connRetry.value = null;
    if (_connRetryTimer) { clearInterval(_connRetryTimer); _connRetryTimer = null; }
  }
  function connRetryMessage() {
    const r = connRetry.value;
    if (!r) return '';
    const s = r.secondsLeft;
    const unit = s === 1 ? 'second' : 'seconds';
    const when = s > 0 ? ('Retrying in ' + s + ' ' + unit + '…') : 'Retrying…';
    return r.reason === 'rate_limited'
      ? ('Too many attempts right now. ' + when)
      : ('Having trouble reaching the server. ' + when + " We'll keep trying.");
  }
  // Hand this to apiFetch as { onRetry, retryCancelled } for an auth call.
  function authRetryHooks() {
    _authScreenLeft = false;
    return {
      onRetry: ({ waitMs, reason }) => {
        if (_connRetryTimer) clearInterval(_connRetryTimer);
        connRetry.value = { reason, secondsLeft: Math.ceil(waitMs / 1000) };
        _connRetryTimer = setInterval(() => {
          if (!connRetry.value) return;
          connRetry.value = { ...connRetry.value, secondsLeft: Math.max(0, connRetry.value.secondsLeft - 1) };
        }, 1000);
      },
      retryCancelled: () => _authScreenLeft,
    };
  }
  // Called by the template when the user navigates away from the OTP/phone
  // step so an in-flight retry loop stops instead of running forever unseen.
  function abandonAuthRetry() {
    _authScreenLeft = true;
    clearConnRetry();
  }

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
    // Inline avatar thumbnail (ADR 0016) - best-effort, omitted on failure.
    let preview = null;
    try { preview = await ctx.encodeAvatarPreview(file); } catch (_) {}
    currentUser.value = await apiFetch('/users/me/avatar', {
      method: 'PUT',
      body: JSON.stringify({ storage_key: ticket.storage_key, preview }),
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
    clearConnRetry();
    phoneNumber.value = ctx.resolvedPhone.value;
    try {
      if (ctx.phoneIsWhitelisted.value) {
        // Dev-whitelist number (1..5): legacy OTP stub, code is anything.
        await apiFetch('/auth/otp/request', {
          method: 'POST',
          body: JSON.stringify({ phone_number: phoneNumber.value }),
          ...authRetryHooks(),
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
      authError.value = ctx.friendlyError(err, "We couldn't send your code. Please check the number and try again.");
    } finally {
      authBusy.value = false;
      clearConnRetry();
    }
  }

  async function verifyOtp() {
    authError.value = '';
    authBusy.value = true;
    clearConnRetry();
    try {
      let body;
      if (ctx.phoneIsWhitelisted.value) {
        body = await apiFetch('/auth/otp/verify', {
          method: 'POST',
          body: JSON.stringify({ phone_number: phoneNumber.value, code: otpCode.value }),
          ...authRetryHooks(),
        });
      } else {
        // Confirm the SMS code with Firebase, then trade its ID token for our pair.
        if (!firebaseConfirmation) throw new Error('Request a code first');
        const cred = await firebaseConfirmation.confirm(otpCode.value);
        const idToken = await cred.user.getIdToken();
        body = await apiFetch('/auth/firebase/verify', {
          method: 'POST',
          body: JSON.stringify({ id_token: idToken }),
          ...authRetryHooks(),
        });
        try { await window.firebaseAuth.signOut(); } catch (e) { /* our JWT is the truth */ }
        firebaseConfirmation = null;
      }

      accessToken.value = body.access_token;
      refreshToken.value = body.refresh_token;
      currentUser.value = body.user;
      localStorage.setItem('linka_access_token', accessToken.value);
      localStorage.setItem('linka_refresh_token', refreshToken.value);
      log('logged in as', currentUser.value);

      if (body.is_new_user) {
        // First sign-in: collect an about line / avatar and let the user keep
        // or replace the server-assigned username (ADR 0017 §2).
        profileDraft.value = {
          about_text: '',
          username: (body.user && body.user.username) || '',
        };
        usernameCheck.value = { status: '', reason: '' };
        authStage.value = 'welcome';
      } else {
        await enterApp();
      }
    } catch (err) {
      authError.value = ctx.friendlyError(err, "That code doesn't look right. Please check it and try again.");
    } finally {
      authBusy.value = false;
      clearConnRetry();
    }
  }

  // Advisory availability check for the welcome form, debounced.
  function checkUsername() {
    const raw = (profileDraft.value.username || '').trim();
    if (_usernameCheckTimer) clearTimeout(_usernameCheckTimer);
    if (!raw || raw === ((currentUser.value && currentUser.value.username) || '')) {
      usernameCheck.value = { status: '', reason: '' };
      return;
    }
    usernameCheck.value = { status: 'checking', reason: '' };
    _usernameCheckTimer = setTimeout(async () => {
      try {
        const r = await apiFetch('/users/username-available?username=' + encodeURIComponent(raw), { noRetry: true });
        // Ignore a stale response if the field changed while in flight.
        if ((profileDraft.value.username || '').trim() !== raw) return;
        usernameCheck.value = r.available
          ? { status: 'ok', reason: '' }
          : { status: 'bad', reason: r.reason || '' };
      } catch (err) {
        usernameCheck.value = { status: '', reason: '' };
      }
    }, 400);
  }

  // Submit the welcome form: best-effort PATCH of the changed fields + avatar,
  // then start the app. A rejected username keeps the user on the form.
  async function submitWelcome() {
    authError.value = '';
    authBusy.value = true;
    clearConnRetry();
    try {
      const patch = {};
      const about = (profileDraft.value.about_text || '').trim();
      const uname = (profileDraft.value.username || '').trim();
      if (about) patch.about_text = about;
      if (uname && uname !== ((currentUser.value && currentUser.value.username) || '')) {
        patch.username = uname;
      }
      if (Object.keys(patch).length) {
        currentUser.value = await apiFetch('/users/me', {
          method: 'PATCH',
          body: JSON.stringify(patch),
          ...authRetryHooks(),
        });
      }
      try {
        await uploadPickedAvatar();
      } catch (err) {
        logError('failed to upload avatar on sign-up:', err.message);
      }
      await enterApp();
    } catch (err) {
      // Backend sends { detail, reason } for a bad username.
      const reason = err && err.body && err.body.reason;
      authError.value = reason ? ('Username: ' + reason.replace(/_/g, ' ')) : ctx.friendlyError(err, "We couldn't save your profile. Please try again.");
      if (reason) usernameCheck.value = { status: 'bad', reason };
    } finally {
      authBusy.value = false;
      clearConnRetry();
    }
  }

  // Skip the welcome form - the random username and empty profile stand.
  async function skipWelcome() {
    authBusy.value = true;
    try { await enterApp(); } finally { authBusy.value = false; }
  }

  async function enterApp() {
    // Leave the 'welcome' stage so the main app view (gated on authStage) shows.
    authStage.value = 'phone';
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
    abandonAuthRetry();
    authStage.value = 'phone';
    otpCode.value = '';
    profileDraft.value = { about_text: '', username: '' };
    usernameCheck.value = { status: '', reason: '' };
    if (_usernameCheckTimer) clearTimeout(_usernameCheckTimer);
    clearAvatar();
    phoneNumber.value = '';
    otpCode.value = '';
    firebaseConfirmation = null;
    if (ctx.resetPhoneInput) ctx.resetPhoneInput();
    ctx.clearAllMessageCache();
    if (ctx.clearOutbox) ctx.clearOutbox();
    if (ctx.clearAvatarCache) ctx.clearAvatarCache();
    localStorage.removeItem('linka_access_token');
    localStorage.removeItem('linka_refresh_token');
    log('logged out');
  }

  return {
    accessToken, refreshToken, currentUser, isAuthed,
    authStage, phoneNumber, otpCode, profileDraft, usernameCheck, authError, authBusy,
    connRetry, connRetryMessage, abandonAuthRetry,
    avatarFile, avatarPreviewUrl, avatarError,
    pickAvatar, clearAvatar, uploadPickedAvatar,
    requestOtp, verifyOtp, checkUsername, submitWelcome, skipWelcome, logout,
  };
}

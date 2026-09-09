// Core PoC plumbing shared by every composable: config, logging, the REST
// helper (Bearer auth + one auto-refresh-and-retry on 401), and the client-side
// upload-size / MIME constants that mirror the backend's config.py.
//
// Loaded via <script src> before every use*.js module and before the root
// createApp script. Exposes a global `useCore(ctx)` factory returning a slice
// of state/methods; the root setup() merges that slice into the shared `ctx`.
function useCore(ctx) {
  const { ref, computed } = Vue;

  // Default to the origin the PoC itself was served from, so a fresh device
  // just works when Caddy serves both the PoC and the API from one host.
  // Falls back to localhost:8000 only when opened as a file:// (no origin).
  // Override for local dev with localStorage.setItem('linka_api_base', ...).
  const _servedOrigin = (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin
    : 'http://localhost:8000';
  const apiBase = ref(localStorage.getItem('linka_api_base') || _servedOrigin);
  const wsBase = computed(() => apiBase.value.replace(/^http/, 'ws'));

  function log(...args) { console.log('%c[Linka]', 'color:#0e7c90', ...args); }
  function logError(...args) { console.error('[Linka]', ...args); }

  // Sign-up profile photo. Client-side limits mirror the backend
  // (config.MAX_UPLOAD_BYTES_AVATAR = 0.5MB, avatar MIME set) so the user
  // gets an instant error instead of a 400 from the upload-ticket call.
  const AVATAR_MAX_BYTES = 512 * 1024;
  // Message media caps (config.MAX_UPLOAD_BYTES_IMAGE / _VIDEO) - mirrored
  // here so the PoC can reject an oversize file before requesting a ticket.
  const MEDIA_MAX_BYTES = { image: 5 * 1024 * 1024, audio: 5 * 1024 * 1024, video: 20 * 1024 * 1024, file: 20 * 1024 * 1024 };
  const AVATAR_MIME = ['image/jpeg', 'image/png', 'image/webp'];

  // ---------------------------------------------------------------
  // Client-side image downscale/recompress. When a picked image is
  // larger than the backend cap we redraw it on a <canvas> at a
  // bounded resolution and re-encode as JPEG, shrinking the byte size
  // with minimal visible quality loss (no server round-trip).
  // Returns a new File, or the original if it already fits / isn't a
  // raster image / anything goes wrong (caller still size-checks).
  // ---------------------------------------------------------------
  async function shrinkImageToFit(file, maxBytes, opts = {}) {
    const maxDim = opts.maxDim || 1600;
    if (!file || !file.type || !file.type.startsWith('image/')) return file;
    if (file.type === 'image/gif') return file; // canvas would drop animation
    if (file.size <= maxBytes) return file;
    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
    } catch (err) {
      logError('shrinkImageToFit: decode failed, sending original', err);
      return file;
    }
    let { width, height } = bitmap;
    const scale = Math.min(1, maxDim / Math.max(width, height));
    width = Math.round(width * scale);
    height = Math.round(height * scale);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const cctx = canvas.getContext('2d');
    cctx.drawImage(bitmap, 0, 0, width, height);
    bitmap.close && bitmap.close();

    for (const quality of [0.85, 0.75, 0.6, 0.45]) {
      const blob = await new Promise((res) => canvas.toBlob(res, 'image/jpeg', quality));
      if (blob && blob.size <= maxBytes) {
        const name = file.name.replace(/\.[^.]+$/, '') + '.jpg';
        log(`shrinkImageToFit: ${(file.size / 1024).toFixed(0)}KB → ${(blob.size / 1024).toFixed(0)}KB @ q${quality}`);
        return new File([blob], name, { type: 'image/jpeg' });
      }
    }
    logError('shrinkImageToFit: still over cap at lowest quality, sending original');
    return file;
  }

  // ---------------------------------------------------------------
  // REST helper - JSON in/out, Bearer auth, one auto-refresh-and-retry
  // on a 401, console logging on every request/response/error.
  //
  // Connection resilience: a network failure (server down / offline) or a
  // 429 / rate_limited is retried automatically, forever, until it succeeds
  // - a network failure every RETRY_NETWORK_MS (3s, fixed), a rate-limit
  // with an escalating backoff (3s, 6s, 12s, ... capped at RETRY_BACKOFF_MAX_MS).
  // Any other non-2xx (a real 4xx/5xx from our API) is thrown immediately.
  //
  // options.onRetry({ attempt, waitMs, reason })  - called before each wait so
  //   the caller can show a "trouble connecting - retrying in Ns" message.
  //   reason is 'network' | 'rate_limited'.
  // options.retryCancelled  - a () => boolean; return true to stop retrying
  //   (e.g. the user left the screen). The last error is then thrown.
  // options.noRetry: true  - opt out entirely (single attempt, old behaviour).
  // ---------------------------------------------------------------
  const RETRY_NETWORK_MS = 3000;
  const RETRY_BACKOFF_BASE_MS = 3000;
  const RETRY_BACKOFF_MAX_MS = 60000;
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

  async function apiFetch(path, options = {}) {
    const doFetch = () => {
      const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
      if (ctx.accessToken.value) headers['Authorization'] = `Bearer ${ctx.accessToken.value}`;
      log('→', options.method || 'GET', path);
      return fetch(`${apiBase.value}${path}`, { ...options, headers });
    };

    const onRetry = typeof options.onRetry === 'function' ? options.onRetry : null;
    const cancelled = typeof options.retryCancelled === 'function' ? options.retryCancelled : () => false;
    const canRetry = !options.noRetry;
    let attempt = 0;

    // Loop: one iteration = one full request attempt. `continue` after a wait
    // for a retryable failure; `return` / `throw` otherwise.
    // eslint-disable-next-line no-constant-condition
    while (true) {
      attempt += 1;
      let resp;
      try {
        resp = await doFetch();
      } catch (err) {
        logError('network error calling', path, err);
        err.isNetworkError = true; // consumed by friendlyError() for a graceful message
        if (!canRetry || cancelled()) throw err;
        if (onRetry) onRetry({ attempt, waitMs: RETRY_NETWORK_MS, reason: 'network' });
        log(`retrying ${path} in ${RETRY_NETWORK_MS}ms (network, attempt ${attempt})`);
        await sleep(RETRY_NETWORK_MS);
        if (cancelled()) throw err;
        continue;
      }

      if (resp.status === 401 && ctx.refreshToken.value) {
        log('401 on', path, '- attempting token refresh');
        if (await tryRefresh()) resp = await doFetch().catch((err) => {
          err.isNetworkError = true;
          throw err;
        });
      }

      if (!resp.ok) {
        let detail = resp.statusText;
        let body = null;
        try { body = await resp.json(); detail = body.detail || detail; } catch (_) {}
        logError('←', resp.status, path, detail);

        const isRateLimited = resp.status === 429 || (body && body.code === 'rate_limited');
        if (isRateLimited && canRetry && !cancelled()) {
          const waitMs = Math.min(RETRY_BACKOFF_BASE_MS * Math.pow(2, attempt - 1), RETRY_BACKOFF_MAX_MS);
          if (onRetry) onRetry({ attempt, waitMs, reason: 'rate_limited' });
          log(`retrying ${path} in ${waitMs}ms (rate_limited, attempt ${attempt})`);
          await sleep(waitMs);
          if (cancelled()) {
            const err = new Error(detail); err.status = resp.status; err.body = body; throw err;
          }
          continue;
        }

        const err = new Error(detail);
        err.status = resp.status;
        err.body = body;          // e.g. { detail, reason } for a bad username (ADR 0017)
        throw err;
      }

      log('←', resp.status, path);
      return resp.status === 204 ? null : resp.json();
    }
  }

  // ---------------------------------------------------------------
  // Turn any thrown error (a fetch/network failure, a non-2xx from
  // apiFetch, or a plain Error) into a short, non-technical sentence
  // that is safe to show in the UI. Never leaks stack traces, HTTP
  // status text, or browser strings like "Failed to fetch".
  // ---------------------------------------------------------------
  function friendlyError(err, fallback) {
    const generic = fallback || 'Something went wrong. Please try again.';
    if (!err) return generic;
    // fetch() rejects with a TypeError when the connection drops / is blocked.
    if (err.isNetworkError || err.name === 'TypeError') {
      return "We're having trouble reaching the server right now. Please check your connection and try again.";
    }
    const status = err.status;
    if (status === 429 || (err.body && err.body.code === 'rate_limited')) {
      return "You're going a little too fast — please wait a moment and try again.";
    }
    if (status === 401 || status === 403) {
      return 'Your session has expired. Please sign in again.';
    }
    // Per-user storage quota (ADR 0028): over the file-storage limit.
    if (status === 413 || (err.body && err.body.reason === 'storage_quota_exceeded')) {
      return 'Storage full — delete some files to upload more.';
    }
    if (typeof status === 'number' && status >= 500) {
      return 'The server ran into a problem. Please try again in a little while.';
    }
    // A 4xx from our own API carries a human-readable `detail` we can trust.
    if (typeof status === 'number' && status >= 400) {
      const detail = err.body && typeof err.body.detail === 'string' ? err.body.detail : null;
      if (detail && detail.length <= 140 && !/[<>{}]/.test(detail)) return detail;
    }
    return generic;
  }

  // De-dupes concurrent refreshes (a burst of 401s, or a 401 racing the
  // proactive timer) onto one in-flight /auth/refresh call.
  let refreshInFlight = null;

  async function tryRefresh() {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async () => {
      try {
        const resp = await fetch(`${apiBase.value}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: ctx.refreshToken.value }),
        });
        if (!resp.ok) throw new Error(`refresh rejected (${resp.status})`);
        const body = await resp.json();
        ctx.accessToken.value = body.access_token;
        ctx.refreshToken.value = body.refresh_token;
        localStorage.setItem('linka_access_token', ctx.accessToken.value);
        localStorage.setItem('linka_refresh_token', ctx.refreshToken.value);
        log('access token refreshed');
        scheduleTokenRefresh(); // re-arm the proactive timer off the new exp
        return true;
      } catch (err) {
        logError('refresh failed, logging out:', err.message);
        ctx.logout();
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
    return refreshInFlight;
  }

  // --- Proactive token refresh -----------------------------------------
  // The access token lives ~15 min. Rather than wait for a 401 (which
  // bounces WS reconnects and stalls in-flight requests), decode its `exp`
  // and refresh ~60s early, re-arming after each success. Keeps a tab that
  // sat idle for hours from ever being logged out.
  let proactiveRefreshTimer = null;
  const REFRESH_SKEW_MS = 60 * 1000;

  function decodeJwtExpMs(token) {
    try {
      const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      return typeof payload.exp === 'number' ? payload.exp * 1000 : null;
    } catch (_) {
      return null;
    }
  }

  function clearTokenRefresh() {
    if (proactiveRefreshTimer) { clearTimeout(proactiveRefreshTimer); proactiveRefreshTimer = null; }
  }

  function scheduleTokenRefresh() {
    clearTokenRefresh();
    const token = ctx.accessToken.value;
    if (!token || !ctx.refreshToken.value) return;
    const expMs = decodeJwtExpMs(token);
    // Unknown exp → conservative 10-min poll; otherwise 60s before expiry,
    // clamped so a near-expired token refreshes almost immediately.
    const delay = expMs == null
      ? 10 * 60 * 1000
      : Math.max(1000, expMs - Date.now() - REFRESH_SKEW_MS);
    log(`token refresh scheduled in ${Math.round(delay / 1000)}s`);
    proactiveRefreshTimer = setTimeout(() => { tryRefresh(); }, delay);
  }

  // A tab woken from background/sleep may have blown past the scheduled
  // fire time; check freshness on return to the foreground.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || !ctx.accessToken.value) return;
    const expMs = decodeJwtExpMs(ctx.accessToken.value);
    if (expMs != null && expMs - Date.now() <= REFRESH_SKEW_MS) tryRefresh();
    else scheduleTokenRefresh();
  });

  return {
    apiBase, wsBase, log, logError,
    AVATAR_MAX_BYTES, MEDIA_MAX_BYTES, AVATAR_MIME,
    apiFetch, tryRefresh, shrinkImageToFit, friendlyError,
    scheduleTokenRefresh, clearTokenRefresh,
  };
}

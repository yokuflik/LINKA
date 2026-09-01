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
  // ---------------------------------------------------------------
  async function apiFetch(path, options = {}) {
    const doFetch = () => {
      const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
      if (ctx.accessToken.value) headers['Authorization'] = `Bearer ${ctx.accessToken.value}`;
      log('→', options.method || 'GET', path);
      return fetch(`${apiBase.value}${path}`, { ...options, headers });
    };

    let resp;
    try {
      resp = await doFetch();
    } catch (err) {
      logError('network error calling', path, err);
      throw err;
    }

    if (resp.status === 401 && ctx.refreshToken.value) {
      log('401 on', path, '- attempting token refresh');
      if (await tryRefresh()) resp = await doFetch();
    }

    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch (_) {}
      logError('←', resp.status, path, detail);
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }

    log('←', resp.status, path);
    return resp.status === 204 ? null : resp.json();
  }

  async function tryRefresh() {
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
      return true;
    } catch (err) {
      logError('refresh failed, logging out:', err.message);
      ctx.logout();
      return false;
    }
  }

  return {
    apiBase, wsBase, log, logError,
    AVATAR_MAX_BYTES, MEDIA_MAX_BYTES, AVATAR_MIME,
    apiFetch, tryRefresh, shrinkImageToFit,
  };
}

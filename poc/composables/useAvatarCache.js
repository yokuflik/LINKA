// Device-side cache for full-resolution avatar images (ADR 0016 follow-up).
//
// The lightbox is the only place an avatar's real bytes are fetched. Without a
// cache, every open re-downloads the full-res JPEG from S3. Here we keep the
// downloaded bytes in the browser's Cache Storage, keyed by the avatar object
// key (the URL path, minus the presigned query string - which rotates on every
// page load but always points at the same stored object). A brand-new avatar
// produces a brand-new object key, so a fresh URL naturally misses the cache
// and re-downloads; a cache hit is decoded before the lightbox opens so it
// shows with no preview and no fade. On top of that, `evictAvatar(url)` is called from the
// `profile_updated` / `chat_updated` handlers to actively drop the *previous*
// full-res entry the moment we learn a new avatar exists.
//
// Global `useAvatarCache(ctx)` factory (no build step, <script src>).
function useAvatarCache(ctx) {
  const CACHE_NAME = 'linka-avatar-fullres-v1';
  const supported = typeof caches !== 'undefined' && typeof URL !== 'undefined';

  // Last full-res URL we resolved per owner (userId / chatId), so a later
  // profile/chat update can evict the object that is now stale.
  const lastUrlByOwner = new Map();

  function objectKey(url) {
    if (!url || typeof url !== 'string') return null;
    try {
      const u = new URL(url, window.location.href);
      return u.origin + u.pathname; // drop the presigned query
    } catch (_) {
      const q = url.indexOf('?');
      return q === -1 ? url : url.slice(0, q);
    }
  }

  // Return { objectUrl, fromCache } for the avatar bytes: an already-decoded
  // blob URL and whether it came straight from Cache Storage (a hit -> the
  // lightbox can show it with no preview and no fade). Fetches + caches on a
  // miss. On any failure returns { objectUrl: url, fromCache: false } so the
  // caller falls back to the plain network URL + the usual preview/fade.
  async function getAvatarImage(url) {
    if (!url) return { objectUrl: null, fromCache: false };
    if (!supported) return { objectUrl: url, fromCache: false };
    const key = objectKey(url);
    try {
      const cache = await caches.open(CACHE_NAME);
      let res = await cache.match(key);
      const hit = !!res;
      if (!res) {
        const net = await fetch(url, { mode: 'cors' });
        if (!net.ok) return { objectUrl: url, fromCache: false };
        await cache.put(key, net.clone());
        res = net;
      }
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      // Pre-decode so the first paint is instant (no visible pop-in).
      try {
        const img = new Image();
        img.src = objectUrl;
        if (img.decode) await img.decode();
      } catch (_) { /* decode is best-effort */ }
      return { objectUrl, fromCache: hit };
    } catch (_) {
      return { objectUrl: url, fromCache: false };
    }
  }

  // Note the current URL for an owner; if it changed, evict the old object.
  function noteAvatarUrl(ownerId, url) {
    if (!ownerId) return;
    const prev = lastUrlByOwner.get(ownerId);
    if (prev && prev !== url) evictAvatar(prev);
    if (url) lastUrlByOwner.set(ownerId, url);
    else lastUrlByOwner.delete(ownerId);
  }

  async function evictAvatar(url) {
    if (!supported || !url) return;
    const key = objectKey(url);
    try {
      const cache = await caches.open(CACHE_NAME);
      await cache.delete(key);
    } catch (_) { /* ignore */ }
  }

  async function clearAvatarCache() {
    lastUrlByOwner.clear();
    if (!supported) return;
    try { await caches.delete(CACHE_NAME); } catch (_) { /* ignore */ }
  }

  return { getAvatarImage, noteAvatarUrl, evictAvatar, clearAvatarCache };
}

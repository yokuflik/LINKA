// Linka PoC - intentionally NON-caching service worker.
//
// It exists only to guarantee fast updates on mobile:
//   1. takes control of open pages immediately (skipWaiting + clients.claim),
//   2. deletes any Cache Storage entries a previous SW version created.
// There is deliberately no `fetch` handler, so every request hits the network
// and the HTTP cache headers from deploy/Caddyfile are the single source of
// truth. If real offline caching is added later, keep the update semantics
// below and only add a fetch handler + a versioned cache.
const CACHE_VERSION = 'linka-poc-v1';
// Caches this SW must NOT delete (owned by app code, not by any SW):
//   linka-avatar-fullres-v1 - ADR 0016 avatar device cache (useAvatarCache.js)
const KEEP_CACHES = ['linka-avatar-fullres-v1'];

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter((k) => k !== CACHE_VERSION && KEEP_CACHES.indexOf(k) === -1)
        .map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

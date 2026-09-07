// Full-screen avatar viewer (ADR 0016). Every <Avatar enlargeable> funnels its
// { url, preview, name } here on tap.
//
// Cache hit (bytes already on the device, useAvatarCache): we decode the image
// first, then open the lightbox with the full-res blob already showing - no
// preview, no fade, no delay.
// Cache miss (first ever open, or no Cache Storage): open immediately with the
// tiny inline preview as a backdrop and fade the real image in once it loads;
// the fetched bytes are stored for next time.
// Global `useAvatarLightbox(ctx)` factory (no build step, <script src>).
function useAvatarLightbox(ctx) {
  const { ref } = Vue;

  // { url, preview, name, cachedUrl, instant } or null.
  //   cachedUrl - decoded blob URL to show instead of `url` (may be null).
  //   instant   - true => skip the preview backdrop and the opacity fade.
  const avatarLightbox = ref(null);
  let openToken = 0;

  function revoke(entry) {
    if (entry && entry.cachedUrl && entry.cachedUrl.startsWith('blob:')) {
      try { URL.revokeObjectURL(entry.cachedUrl); } catch (_) { /* ignore */ }
    }
  }

  async function openAvatarLightbox(payload) {
    if (!payload || (!payload.url && !payload.preview)) return;
    const token = ++openToken;

    // Try the device cache first. A hit is decoded here so the very first
    // paint of the lightbox is the sharp image.
    let cachedUrl = null;
    let instant = false;
    if (payload.url && ctx.getAvatarImage) {
      try {
        const { objectUrl, fromCache } = await ctx.getAvatarImage(payload.url);
        if (token !== openToken) { // superseded while we awaited
          if (objectUrl && objectUrl.startsWith('blob:')) {
            try { URL.revokeObjectURL(objectUrl); } catch (_) { /* ignore */ }
          }
          return;
        }
        if (objectUrl && objectUrl !== payload.url) {
          cachedUrl = objectUrl;
          instant = fromCache; // decoded + already on device -> no fade
        }
      } catch (_) { /* fall through to the preview + fade path */ }
    }

    revoke(avatarLightbox.value);
    avatarLightbox.value = {
      url: payload.url || null,
      preview: payload.preview || null,
      name: payload.name || '',
      cachedUrl,
      instant,
    };
  }

  function closeAvatarLightbox() {
    revoke(avatarLightbox.value);
    openToken++;
    avatarLightbox.value = null;
  }

  return { avatarLightbox, openAvatarLightbox, closeAvatarLightbox };
}

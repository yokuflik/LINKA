// Media blur placeholders (ADR 0014). Decodes a message's `media_blur_hash`
// (ThumbHash base64, computed by the sender - see useMediaUpload.js) into a
// tiny blurred PNG data: URL and an aspect ratio, so an image/video bubble can
// reserve its box and show an instant preview without fetching any bytes.
//
// Both results are memoized by hash string - the same forwarded image reuses
// one decode across every bubble.
// Global `useMediaPlaceholder(ctx)` factory (no build step, <script src>).
function useMediaPlaceholder(ctx) {
  const dataUrlCache = new Map(); // hash -> data: URL (or null on failure)
  const aspectCache = new Map();  // hash -> width/height number

  function bytesFor(hash) {
    // Untrusted-ish: the backend already validates charset/length, but a
    // malformed value here must not throw into a template render.
    return window.ThumbHash.base64ToThumbHash(hash);
  }

  // Blurred PNG data: URL for a bubble background. Returns null if we can't
  // decode - the caller then falls back to eager-load behavior.
  function thumbHashToDataUrl(hash) {
    if (!hash || !window.ThumbHash) return null;
    if (dataUrlCache.has(hash)) return dataUrlCache.get(hash);
    let url = null;
    try {
      url = window.ThumbHash.thumbHashToDataURL(bytesFor(hash));
    } catch (err) {
      ctx.logError('thumbhash decode failed', err);
    }
    dataUrlCache.set(hash, url);
    return url;
  }

  // Approximate width/height ratio encoded in the hash. Returns null when
  // unavailable so the caller keeps its legacy portrait/landscape guess.
  function thumbHashToAspect(hash) {
    if (!hash || !window.ThumbHash) return null;
    if (aspectCache.has(hash)) return aspectCache.get(hash);
    let ratio = null;
    try {
      ratio = window.ThumbHash.thumbHashToApproximateAspectRatio(bytesFor(hash));
      if (!isFinite(ratio) || ratio <= 0) ratio = null;
    } catch (err) {
      ctx.logError('thumbhash aspect failed', err);
    }
    aspectCache.set(hash, ratio);
    return ratio;
  }

  // Compute a tiny inline avatar thumbnail (ADR 0016) from an image File the
  // user just picked. Decodes it, draws into a <=64px canvas, re-encodes as a
  // JPEG data: URI (~1-3 KB) that gets stored and rendered directly as the
  // avatar - a real, if small, picture (no blur). Any failure returns null,
  // and the avatar then loads eagerly like a legacy row.
  async function encodeAvatarPreview(file) {
    if (!file) return null;
    const url = URL.createObjectURL(file);
    try {
      const img = new Image();
      img.decoding = 'async';
      await new Promise((resolve, reject) => {
        img.onload = resolve;
        img.onerror = () => reject(new Error('image decode failed'));
        img.src = url;
      });
      const srcW = img.naturalWidth, srcH = img.naturalHeight;
      if (!srcW || !srcH) return null;
      const scale = Math.min(1, 64 / Math.max(srcW, srcH));
      const w = Math.max(1, Math.round(srcW * scale));
      const h = Math.max(1, Math.round(srcH * scale));
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
      // Guard against a too-big result (backend cap is 8192) or a failed encode.
      if (!dataUrl.startsWith('data:image/') || dataUrl.length > 8000) return null;
      return dataUrl;
    } catch (err) {
      ctx.logError('avatar preview encode failed', err);
      return null;
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  return { thumbHashToDataUrl, thumbHashToAspect, encodeAvatarPreview };
}

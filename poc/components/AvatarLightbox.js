// Full-screen avatar viewer (ADR 0016). This is the only component that puts an
// avatar's real full-res image in an <img src>.
//   data.instant  -> bytes were already on the device and decoded before open:
//                    show the sharp image at once, no preview, no fade.
//   otherwise     -> show the tiny inline preview (stretched) as a backdrop and
//                    fade the real image in once it loads.
// Close on backdrop click or Esc.
const AvatarLightbox = {
  props: {
    // { url, preview, name, cachedUrl, instant } or null when closed.
    data: { default: null },
  },
  emits: ['close'],
  data() {
    return { loaded: false };
  },
  watch: {
    fullUrl() { this.loaded = false; },
  },
  computed: {
    instant() {
      return !!(this.data && this.data.instant && this.data.cachedUrl);
    },
    previewUrl() {
      if (this.instant) return null;
      const p = this.data && this.data.preview;
      return (typeof p === 'string' && p.startsWith('data:image/')) ? p : null;
    },
    // Prefer the device-cached blob URL; fall back to the presigned network URL.
    fullUrl() {
      if (!this.data) return null;
      return this.data.cachedUrl || this.data.url || null;
    },
  },
  mounted() {
    this._onKey = (e) => { if (e.key === 'Escape') this.$emit('close'); };
    window.addEventListener('keydown', this._onKey);
  },
  beforeUnmount() {
    window.removeEventListener('keydown', this._onKey);
  },
  template: `
    <div v-if="data" class="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
         @click.self="$emit('close')">
      <div class="relative w-[80vw] max-w-sm aspect-square rounded-lg overflow-hidden bg-slate-800 shadow-2xl">
        <img v-if="previewUrl" :src="previewUrl" alt=""
             class="absolute inset-0 w-full h-full object-cover" />
        <img v-if="fullUrl" :src="fullUrl" :alt="data.name || ''" crossorigin="anonymous"
             class="absolute inset-0 w-full h-full object-cover"
             :class="instant ? 'opacity-100' : (loaded ? 'opacity-100 transition-opacity duration-300' : 'opacity-0 transition-opacity duration-300')"
             @load="loaded = true" />
        <div v-if="fullUrl && !loaded && !instant"
             class="absolute inset-0 flex items-center justify-center">
          <span class="w-6 h-6 border-2 border-white/60 border-t-transparent rounded-full animate-spin"></span>
        </div>
      </div>
      <button type="button" @click="$emit('close')"
              class="absolute top-4 right-4 text-white/80 hover:text-white text-2xl leading-none">&times;</button>
    </div>
  `,
};

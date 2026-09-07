// WhatsApp-style circular avatar. Rendering priority (ADR 0016 - inline avatar
// thumbnail):
//   1. `preview` present -> a tiny real JPEG data: URI, shown DIRECTLY as the
//      avatar (small but recognisable, no blur). The full-res image is not
//      loaded here; tapping the circle opens the shared lightbox (injected
//      `openAvatarLightbox`) which downloads it once.
//   2. no preview but `url` present -> legacy behaviour: eager <img> with native
//      lazy-loading (pre-feature rows / seed data).
//   3. neither -> a colored circle with the name's first initial.
// The fallback color is a deterministic hash of `colorKey`.
const Avatar = {
  props: {
    url: { default: null },
    preview: { default: null },
    name: { default: '' },
    colorKey: { default: '' },
    sizeClass: { default: 'w-9 h-9 text-sm' },
    // When false, the circle is not clickable (e.g. inside a picker preview).
    enlargeable: { type: Boolean, default: true },
  },
  inject: {
    openAvatarLightbox: { default: () => () => {} },
  },
  data() {
    return { failed: false, previewFailed: false };
  },
  watch: {
    url() { this.failed = false; },
    preview() { this.previewFailed = false; },
  },
  computed: {
    previewUrl() {
      const p = this.preview;
      return (typeof p === 'string' && p.startsWith('data:image/')) ? p : null;
    },
    initial() {
      const s = String(this.name || '').trim();
      return s ? s[0].toUpperCase() : '';
    },
    colorClass() {
      if (!this.initial) return 'bg-slate-300';
      const colors = [
        'bg-teal-600', 'bg-rose-500', 'bg-amber-500', 'bg-indigo-500',
        'bg-emerald-600', 'bg-fuchsia-600', 'bg-sky-600', 'bg-orange-500',
      ];
      const str = String(this.colorKey || this.name || '');
      let h = 0;
      for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
      return colors[h % colors.length];
    },
    canEnlarge() {
      // Something to actually show enlarged: a real URL or at least the preview.
      return this.enlargeable && (this.url || this.previewUrl);
    },
  },
  methods: {
    onClick(e) {
      if (!this.canEnlarge) return;
      // Don't let a row/button behind the avatar also react (chat-list select,
      // header edit-profile, ...).
      e.stopPropagation();
      this.openAvatarLightbox({ url: this.url || null, preview: this.preview || null, name: this.name });
    },
  },
  template: `
    <span class="shrink-0 rounded-full overflow-hidden bg-slate-200 flex items-center justify-center"
          :class="[sizeClass, canEnlarge ? 'cursor-pointer' : '']"
          @click="onClick">
      <img v-if="previewUrl && !previewFailed" :src="previewUrl" alt="" class="w-full h-full object-cover" @error="previewFailed = true" />
      <img v-else-if="url && !failed" :src="url" alt="" loading="lazy" decoding="async" fetchpriority="low"
           class="w-full h-full object-cover" @error="failed = true" />
      <span v-else class="w-full h-full flex items-center justify-center font-semibold text-white leading-none" :class="colorClass">{{ initial }}</span>
    </span>
  `,
};

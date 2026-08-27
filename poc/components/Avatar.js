// WhatsApp-style circular avatar. Renders the profile image when a URL is
// given (native lazy-loading + low fetch priority so a long chat list never
// blocks on images); on load failure, or when there's no URL, it falls back
// to a colored circle with the first initial of the name. A name-less avatar
// (e.g. a group, for now) just shows a plain neutral circle. The fallback
// color is a deterministic hash of `colorKey`, so a person keeps one color.
const Avatar = {
  props: {
    url: { default: null },
    name: { default: '' },
    colorKey: { default: '' },
    sizeClass: { default: 'w-9 h-9 text-sm' },
  },
  data() {
    return { failed: false };
  },
  watch: {
    url() { this.failed = false; },
  },
  computed: {
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
  },
  template: `
    <span class="shrink-0 rounded-full overflow-hidden bg-slate-200 flex items-center justify-center" :class="sizeClass">
      <img v-if="url && !failed" :src="url" alt="" loading="lazy" decoding="async" fetchpriority="low"
           class="w-full h-full object-cover" @error="failed = true" />
      <span v-else class="w-full h-full flex items-center justify-center font-semibold text-white leading-none" :class="colorClass">{{ initial }}</span>
    </span>
  `,
};

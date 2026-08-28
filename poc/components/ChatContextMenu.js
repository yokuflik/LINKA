// Right-click menu on a chat row in the sidebar.
// "Pin to top" / "Unpin" and mute. When not muted, "Mute" expands to quick
// presets PLUS a "Custom..." row with a datetime-local picker for any expiry
// (the server accepts any absolute muted_until - ADR 0004). When muted, a
// single "Unmute" entry with the current expiry as a hint.
const ChatContextMenu = {
  props: {
    position: { type: Object, required: true }, // { x, y } - viewport-clamped by caller
    pinned: { type: Boolean, default: false },
    muted: { type: Boolean, default: false },
    mutePresets: { type: Array, required: true }, // [{ key, label }]
    mutedUntilLabel: { type: String, default: '' }, // human hint, '' when muted "always"
  },
  emits: ['close', 'toggle-pin', 'mute-preset', 'mute-until', 'unmute'],
  data() {
    return { showMuteOptions: false, showCustom: false, customValue: '' };
  },
  methods: {
    submitCustom() {
      if (!this.customValue) return;
      // datetime-local gives a local wall-clock string; Date() reads it as
      // local time, toISOString() normalizes to UTC for the API.
      const iso = new Date(this.customValue).toISOString();
      this.$emit('mute-until', iso);
    },
  },
  template: `
    <div class="fixed inset-0 z-50" @click="$emit('close')" @contextmenu.prevent="$emit('close')">
      <div class="absolute w-52 bg-white rounded-lg shadow-lg border border-slate-200 py-1 text-sm"
           :style="{ top: position.y + 'px', left: position.x + 'px' }" @click.stop>
        <button @click="$emit('toggle-pin')" class="w-full text-left px-3 py-2 hover:bg-slate-50">
          {{ pinned ? 'Unpin' : 'Pin to top' }}
        </button>

        <button v-if="muted" @click="$emit('unmute')" class="w-full text-left px-3 py-2 hover:bg-slate-50">
          Unmute<span v-if="mutedUntilLabel" class="text-slate-400"> · until {{ mutedUntilLabel }}</span>
        </button>

        <template v-else>
          <button @click="showMuteOptions = !showMuteOptions"
                  class="w-full text-left px-3 py-2 hover:bg-slate-50 flex items-center justify-between">
            <span>Mute</span><span class="text-slate-400">{{ showMuteOptions ? '▾' : '▸' }}</span>
          </button>

          <template v-if="showMuteOptions">
            <button v-for="opt in mutePresets" :key="opt.key"
                    @click="$emit('mute-preset', opt.key)"
                    class="w-full text-left pl-6 pr-3 py-2 hover:bg-slate-50 text-slate-600">
              {{ opt.label }}
            </button>

            <button @click="showCustom = !showCustom"
                    class="w-full text-left pl-6 pr-3 py-2 hover:bg-slate-50 text-slate-600">
              Custom…
            </button>
            <div v-if="showCustom" class="px-3 py-2 flex flex-col gap-1.5">
              <input type="datetime-local" v-model="customValue"
                     class="w-full px-2 py-1 text-xs border border-slate-300 rounded" />
              <button @click="submitCustom" :disabled="!customValue"
                      class="w-full py-1 text-xs bg-teal-700 text-white rounded disabled:opacity-40">
                Mute until this time
              </button>
            </div>
          </template>
        </template>
      </div>
    </div>
  `,
};

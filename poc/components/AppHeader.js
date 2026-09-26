// Top bar: app name, subtle WS connection status, search/settings, and a
// compact avatar-only profile menu (dropdown holds user info + logout).
// "Linka Agent" access moved into the sidebar as a pinned-style chat entry.
const AppHeader = {
  props: {
    wsStatus: { type: String, required: true },
    currentUser: { type: Object, required: true },
    avatarUrl: { default: null },
    avatarPreview: { default: null },
  },
  emits: ['logout', 'edit-profile', 'open-settings', 'open-search'],
  data() {
    return { showProfileMenu: false };
  },
  computed: {
    // User-friendly label for the connection dot - never the raw
    // 'disconnected' / 'error' state names.
    wsStatusLabel() {
      return {
        connected: 'Connected',
        connecting: 'Connecting…',
      }[this.wsStatus] || 'Reconnecting…';
    },
    displayName() {
      return this.currentUser.display_name || this.currentUser.username || this.currentUser.phone_number;
    },
  },
  mounted() {
    this._onDocClick = (e) => {
      if (this.showProfileMenu && !this.$refs.profileMenuRoot?.contains(e.target)) {
        this.showProfileMenu = false;
      }
    };
    document.addEventListener('click', this._onDocClick);
  },
  beforeUnmount() {
    document.removeEventListener('click', this._onDocClick);
  },
  methods: {
    editProfile() {
      this.showProfileMenu = false;
      this.$emit('edit-profile');
    },
    logout() {
      this.showProfileMenu = false;
      this.$emit('logout');
    },
  },
  template: `
    <header class="h-14 shrink-0 flex items-center justify-between px-4 bg-white border-b border-slate-200">
      <div class="flex items-center gap-3 min-w-0">
        <span class="flex items-center gap-2 font-semibold shrink-0">
          <img src="assets/maskable_icon_x192.png" alt="Linka"
               class="w-7 h-7 shrink-0 rounded-lg object-cover select-none" draggable="false" />
          <span class="hidden sm:inline">Linka</span>
        </span>
        <!-- Reconnecting is a normal, frequent occurrence (not an error) - a
             quiet dot next to the logo instead of bold red text. Connected
             gets its own small solid green dot; only the label text (shown on
             wider screens) actually changes between states. -->
        <span class="flex items-center gap-1.5 text-[11px] text-slate-400" :title="wsStatusLabel">
          <span class="w-1.5 h-1.5 rounded-full shrink-0"
                :class="wsStatus === 'connected' ? 'bg-emerald-500' : (wsStatus === 'connecting' ? 'bg-amber-300' : 'bg-slate-300 animate-pulse')"></span>
          <span v-if="wsStatus !== 'connected'" class="hidden sm:inline">{{ wsStatusLabel }}</span>
        </span>
      </div>
      <div class="flex items-center gap-1 text-sm">
        <button type="button" @click="$emit('open-search')"
                class="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700"
                title="Search messages">
          <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none"
               stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.2-3.2" />
          </svg>
        </button>
        <button type="button" @click="$emit('open-settings')"
                class="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700"
                title="Settings">
          <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none"
               stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
        <div class="relative ms-1" ref="profileMenuRoot">
          <button type="button" @click.stop="showProfileMenu = !showProfileMenu"
                  class="block rounded-full hover:ring-2 hover:ring-slate-200"
                  title="Account">
            <Avatar :url="avatarUrl" :preview="avatarPreview" :enlargeable="false"
                    :name="displayName" :colorKey="currentUser.id" sizeClass="w-8 h-8 text-xs" />
          </button>
          <div v-if="showProfileMenu"
               class="absolute end-0 mt-2 w-56 bg-white rounded-xl shadow-lg border border-slate-200 py-1.5 z-30 text-start">
            <div class="px-3 py-2 border-b border-slate-100">
              <p class="text-sm font-medium text-slate-800 truncate">{{ displayName }}</p>
              <p v-if="currentUser.username" class="text-xs text-slate-400 truncate">@{{ currentUser.username }}</p>
            </div>
            <button type="button" @click="editProfile"
                    class="w-full text-start px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">Edit profile</button>
            <button type="button" @click="logout"
                    class="w-full text-start px-3 py-2 text-sm text-red-600 hover:bg-slate-50">Log out</button>
          </div>
        </div>
      </div>
    </header>
  `,
};

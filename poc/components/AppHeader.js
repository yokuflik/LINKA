// Top bar: app name, WS connection status dot, current user, logout button.
const AppHeader = {
  props: {
    wsStatus: { type: String, required: true },
    currentUser: { type: Object, required: true },
    avatarUrl: { default: null },
  },
  emits: ['logout', 'edit-profile', 'open-settings'],
  template: `
    <header class="flex items-center justify-between px-4 py-2 bg-white border-b border-slate-200">
      <div class="flex items-center gap-3">
        <span class="font-semibold">Linka</span>
        <span class="flex items-center gap-1.5 text-xs text-slate-500">
          <span class="w-2 h-2 rounded-full"
                :class="{ 'bg-emerald-500': wsStatus === 'connected', 'bg-amber-400': wsStatus === 'connecting', 'bg-red-500': wsStatus === 'error' || wsStatus === 'disconnected' }"></span>
          {{ wsStatus }}
        </span>
      </div>
      <div class="flex items-center gap-3 text-sm">
        <button type="button" @click="$emit('edit-profile')"
                class="flex items-center gap-3 hover:bg-slate-50 rounded-lg px-2 py-1 -mx-1"
                title="Edit your profile">
          <Avatar :url="avatarUrl" :name="currentUser.display_name || currentUser.phone_number" :colorKey="currentUser.id" sizeClass="w-7 h-7 text-xs" />
          <span class="text-slate-500">You are <span class="font-medium text-slate-800">{{ currentUser.display_name || currentUser.phone_number }}</span></span>
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
        <button @click="$emit('logout')" class="px-3 py-1 border border-slate-300 rounded-lg text-sm">Log out</button>
      </div>
    </header>
  `,
};

// Top bar: app name, WS connection status dot, current user, logout button.
const AppHeader = {
  props: {
    wsStatus: { type: String, required: true },
    currentUser: { type: Object, required: true },
    avatarUrl: { default: null },
  },
  emits: ['logout', 'edit-profile'],
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
        <button @click="$emit('logout')" class="px-3 py-1 border border-slate-300 rounded-lg text-sm">Log out</button>
      </div>
    </header>
  `,
};

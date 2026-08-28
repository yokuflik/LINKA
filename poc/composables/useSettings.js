// Per-user settings (privacy, ...). Backed by GET/PATCH /users/me/settings,
// which returns a fully-resolved settings object. The PoC only surfaces the
// privacy section for now; the server keeps the shape open so new groups can
// be added without touching this file's transport.
// Global `useSettings(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, logError.
function useSettings(ctx) {
  const { ref } = Vue;

  // Last resolved settings from the server (null until first load).
  const userSettings = ref(null);

  // Visibility choices for the privacy controls - kept in sync with
  // services/settings/schema.py::PRIVACY_VISIBILITY.
  const ONLINE_VISIBILITY_OPTIONS = [
    { value: 'everyone', label: 'Everyone' },
    { value: 'contacts', label: 'Only people I have a chat with' },
    { value: 'nobody', label: 'Nobody' },
  ];

  async function loadSettings() {
    try {
      const res = await ctx.apiFetch('/users/me/settings');
      userSettings.value = res.settings;
    } catch (err) {
      ctx.logError('failed to load settings', err);
    }
  }

  // `patch` is a partial nested object, e.g. { privacy: { online: 'nobody' } }.
  async function saveSettings(patch) {
    const res = await ctx.apiFetch('/users/me/settings', {
      method: 'PATCH',
      body: JSON.stringify({ settings: patch }),
    });
    userSettings.value = res.settings;
    return res.settings;
  }

  function resetSettings() {
    userSettings.value = null;
  }

  return {
    userSettings, ONLINE_VISIBILITY_OPTIONS,
    loadSettings, saveSettings, resetSettings,
  };
}

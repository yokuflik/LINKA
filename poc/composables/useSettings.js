// Per-user settings (privacy, ...). Backed by GET/PATCH /users/me/settings,
// which returns a fully-resolved settings object. The PoC only surfaces the
// privacy section for now; the server keeps the shape open so new groups can
// be added without touching this file's transport.
// Global `useSettings(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, logError, showToast.
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
    showSettingsModal.value = false;
  }

  // ---------------------------------------------------------------
  // The ⚙ Settings modal (privacy only): online visibility + read receipts.
  // ---------------------------------------------------------------
  const showSettingsModal = ref(false);
  const settingsForm = ref({ privacy_online: 'everyone', privacy_read_receipts: true });
  const settingsBusy = ref(false);
  const settingsError = ref('');

  function privacyOf(s) {
    const p = (s && s.privacy) || {};
    return {
      privacy_online: p.online || 'everyone',
      privacy_read_receipts: p.read_receipts !== false,
    };
  }

  async function openSettingsModal() {
    if (!userSettings.value) await loadSettings();
    settingsForm.value = privacyOf(userSettings.value);
    settingsError.value = '';
    showSettingsModal.value = true;
  }

  async function submitSettings() {
    settingsError.value = '';
    settingsBusy.value = true;
    try {
      const current = privacyOf(userSettings.value);
      const patch = {};
      if (settingsForm.value.privacy_online !== current.privacy_online) patch.online = settingsForm.value.privacy_online;
      if (settingsForm.value.privacy_read_receipts !== current.privacy_read_receipts) patch.read_receipts = settingsForm.value.privacy_read_receipts;
      if (Object.keys(patch).length) await saveSettings({ privacy: patch });
      showSettingsModal.value = false;
      ctx.showToast('Settings saved');
    } catch (err) {
      settingsError.value = err.message || String(err);
    } finally {
      settingsBusy.value = false;
    }
  }

  return {
    userSettings, ONLINE_VISIBILITY_OPTIONS,
    loadSettings, saveSettings, resetSettings,
    showSettingsModal, settingsForm, settingsBusy, settingsError,
    openSettingsModal, submitSettings,
  };
}

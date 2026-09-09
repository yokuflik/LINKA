// Scheduled messages (ADR 0026). A message the user composes now but that the
// server sends automatically at a chosen future time. Management is REST (not
// WS) - like chat pin/mute: POST /chats/{id}/scheduled-messages, then
// GET/PATCH/DELETE /scheduled-messages[/{id}].
//
// Global `useScheduleMessage(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx (call-time): apiFetch, friendlyError, showToast, showErrorToast,
// log, logError, activeChatId, messageInput, replyingToMessage, currentUser,
// prepareMediaBlock, mediaKindForMime, MEDIA_MESSAGE_TYPE-less (uses local map).
function useScheduleMessage(ctx) {
  const { ref, computed, watch } = Vue;

  const MESSAGE_TYPE = { image: 2, video: 3, audio: 4, file: 5 };

  const showScheduleModal = ref(false);
  // { content, scheduled_for (local datetime-local string), media: {file, kind} | null }
  const scheduleForm = ref({ content: '', scheduled_for: '', media: null });
  const scheduleBusy = ref(false);
  const scheduleError = ref('');

  // Pending scheduled messages for the *active* chat only (list view + chip).
  const scheduledMessages = ref([]);
  const showScheduleList = ref(false);

  const scheduledCount = computed(() => scheduledMessages.value.length);

  // Local datetime-local value (no seconds, no tz) -> absolute UTC ISO.
  function localInputToUtcIso(v) {
    if (!v) return null;
    const d = new Date(v); // parsed as local time
    if (isNaN(d.getTime())) return null;
    return d.toISOString();
  }

  // ISO -> value a <input type="datetime-local"> accepts (local, minute precision).
  function utcIsoToLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // Friendly "in 2 hours" / "tomorrow at 9:00 AM" style label for a fire time.
  function scheduleRelativeLabel(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const diffMs = d.getTime() - Date.now();
    const mins = Math.round(diffMs / 60000);
    if (mins < 1) return 'in a moment';
    if (mins < 60) return `in ${mins} minute${mins === 1 ? '' : 's'}`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `in ${hrs} hour${hrs === 1 ? '' : 's'}`;
    const days = Math.round(hrs / 24);
    if (days < 7) return `in ${days} day${days === 1 ? '' : 's'}`;
    return `on ${d.toLocaleString()}`;
  }

  // --- Presets for the picker --------------------------------------------------
  function presetIn1Hour() {
    const d = new Date(Date.now() + 60 * 60 * 1000);
    scheduleForm.value.scheduled_for = utcIsoToLocalInput(d.toISOString());
  }
  function presetTonight8pm() {
    const d = new Date();
    d.setHours(20, 0, 0, 0);
    if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
    scheduleForm.value.scheduled_for = utcIsoToLocalInput(d.toISOString());
  }
  function presetTomorrow9am() {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    scheduleForm.value.scheduled_for = utcIsoToLocalInput(d.toISOString());
  }

  // --- Open / edit ------------------------------------------------------------
  function openScheduleModal(mediaFile, forceKind) {
    if (!ctx.activeChatId.value) {
      ctx.showToast('Open a chat first to schedule a message.');
      return;
    }
    scheduleError.value = '';
    let media = null;
    if (mediaFile) {
      const kind = ctx.mediaKindForMime(mediaFile.type, forceKind);
      if (!kind) {
        ctx.showErrorToast("That file type isn't supported.");
        return;
      }
      media = { file: mediaFile, kind };
    }
    scheduleForm.value = {
      content: (ctx.messageInput.value || '').trim(),
      scheduled_for: '',
      media,
      editingId: null,
    };
    showScheduleModal.value = true;
  }

  function editScheduled(row) {
    scheduleError.value = '';
    scheduleForm.value = {
      content: row.content || '',
      scheduled_for: utcIsoToLocalInput(row.scheduled_for),
      media: null, // caption-only edit; changing media = cancel + reschedule
      editingId: row.id,
      hasMedia: row.message_type !== 1,
    };
    showScheduleList.value = false;
    showScheduleModal.value = true;
  }

  // --- Submit ----------------------------------------------------------------
  async function submitSchedule() {
    scheduleError.value = '';
    const chatId = ctx.activeChatId.value;
    if (!chatId) { scheduleError.value = 'Open a chat first.'; return; }
    const form = scheduleForm.value;
    const iso = localInputToUtcIso(form.scheduled_for);
    if (!iso) { scheduleError.value = 'Please pick a valid date and time.'; return; }
    if (new Date(iso).getTime() <= Date.now()) {
      scheduleError.value = 'Please pick a time in the future.';
      return;
    }
    const content = (form.content || '').trim();

    scheduleBusy.value = true;
    try {
      // --- Edit: PATCH caption / time only ---
      if (form.editingId) {
        const body = { scheduled_for: iso, content: content || null };
        const updated = await ctx.apiFetch(`/scheduled-messages/${form.editingId}`, {
          method: 'PATCH',
          body: JSON.stringify(body),
        });
        _upsertScheduled(updated);
        showScheduleModal.value = false;
        ctx.showToast('Scheduled message updated.', 'success');
        return;
      }

      // --- Create ---
      if (!content && !form.media) {
        scheduleError.value = 'Type a message or attach a file.';
        return;
      }
      const payload = {
        client_message_id: crypto.randomUUID(),
        scheduled_for: iso,
        message_type: 1,
      };
      if (content) payload.content = content;
      const replyToId = ctx.replyingToMessage.value ? ctx.replyingToMessage.value.id : null;
      if (replyToId) payload.reply_to_message_id = replyToId;

      if (form.media) {
        // Reuse the shared prepare+upload pipeline (step 7).
        const block = await ctx.prepareMediaBlock(form.media.file, form.media.kind, { chatId });
        payload.message_type = MESSAGE_TYPE[form.media.kind];
        payload.media = { key: block.key, name: block.name };
        if (block.blur_hash) payload.media.blur_hash = block.blur_hash;
      }

      const created = await ctx.apiFetch(`/chats/${chatId}/scheduled-messages`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      _upsertScheduled(created);
      showScheduleModal.value = false;
      ctx.messageInput.value = '';
      ctx.replyingToMessage.value = null;
      ctx.showToast('Message scheduled ' + scheduleRelativeLabel(created.scheduled_for) + '.', 'success');
    } catch (err) {
      ctx.logError('schedule failed', err);
      // {reason}-coded 409 (limit / storage_quota_exceeded) keeps the user on the form.
      const reason = err && err.body && err.body.reason;
      if (reason === 'storage_quota_exceeded') {
        scheduleError.value = "You've run out of storage space. Delete some media and try again.";
      } else if (reason === 'limit_exceeded' || (err && err.status === 409)) {
        scheduleError.value = 'You have too many scheduled messages. Cancel one and try again.';
      } else {
        scheduleError.value = ctx.friendlyError(err, "We couldn't schedule that message. Please try again.");
      }
    } finally {
      scheduleBusy.value = false;
    }
  }

  // --- List / cancel -------------------------------------------------------
  async function loadScheduledMessages(chatId) {
    const target = chatId || ctx.activeChatId.value;
    if (!target) { scheduledMessages.value = []; return; }
    try {
      const rows = await ctx.apiFetch(`/scheduled-messages?chat_id=${encodeURIComponent(target)}`);
      // Only render this chat's pending rows, soonest first.
      if (ctx.activeChatId.value !== target) return;
      scheduledMessages.value = (rows || [])
        .filter((r) => r.status === 0)
        .sort((a, b) => new Date(a.scheduled_for) - new Date(b.scheduled_for));
    } catch (err) {
      ctx.logError('load scheduled messages failed', err);
      // Non-fatal: a background list, no toast needed.
    }
  }

  async function cancelScheduled(row) {
    if (!window.confirm('Cancel this scheduled message?')) return;
    try {
      await ctx.apiFetch(`/scheduled-messages/${row.id}`, { method: 'DELETE' });
      scheduledMessages.value = scheduledMessages.value.filter((r) => r.id !== row.id);
      ctx.showToast('Scheduled message cancelled.');
      if (!scheduledMessages.value.length) showScheduleList.value = false;
    } catch (err) {
      ctx.logError('cancel scheduled failed', err);
      ctx.showErrorToast(ctx.friendlyError(err, "We couldn't cancel that scheduled message."));
    }
  }

  function _upsertScheduled(row) {
    if (!row || row.status !== 0) return;
    if (String(row.chat_id) !== String(ctx.activeChatId.value)) return;
    const idx = scheduledMessages.value.findIndex((r) => r.id === row.id);
    if (idx >= 0) scheduledMessages.value[idx] = row;
    else scheduledMessages.value.push(row);
    scheduledMessages.value.sort((a, b) => new Date(a.scheduled_for) - new Date(b.scheduled_for));
  }

  // --- Live events (from useWsRouter) --------------------------------------
  function onScheduledMessageSent(msg) {
    // The real message arrives via the normal new_message echo; just drop the row.
    scheduledMessages.value = scheduledMessages.value.filter((r) => String(r.id) !== String(msg.id));
    if (!scheduledMessages.value.length) showScheduleList.value = false;
  }
  function onScheduledMessageFailed(msg) {
    scheduledMessages.value = scheduledMessages.value.filter((r) => String(r.id) !== String(msg.id));
    const reason = (msg.reason || '').trim();
    ctx.showErrorToast("A scheduled message couldn't be sent"
      + (reason ? ': ' + reason + '.' : '.'));
  }

  // Refresh the list whenever the active chat changes (chat open).
  watch(() => ctx.activeChatId.value, (id) => {
    showScheduleList.value = false;
    scheduledMessages.value = [];
    if (id) loadScheduledMessages(id);
  });

  return {
    showScheduleModal, scheduleForm, scheduleBusy, scheduleError,
    scheduledMessages, scheduledCount, showScheduleList,
    openScheduleModal, editScheduled, submitSchedule,
    loadScheduledMessages, cancelScheduled,
    onScheduledMessageSent, onScheduledMessageFailed,
    scheduleRelativeLabel,
    presetIn1Hour, presetTonight8pm, presetTomorrow9am,
  };
}

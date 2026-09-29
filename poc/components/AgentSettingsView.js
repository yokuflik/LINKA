// Settings body of the AI agent drawer (AGENT_DRAWER_UI_PLAN.md Step 8).
// Frontend-only trim (2026-09-28, no ADR): shows ONLY the hard,
// server-enforced restrictions. Soft guidance, triggers, knowledge base and
// BYOK sections removed from this view per explicit user request - backend
// fields/endpoints are untouched, this is display-only.
const AgentSettingsView = {
  props: {
    form: { type: Object, required: true }, // agentForm - never null while this view is shown
    busy: { type: Boolean, required: true },
    error: { type: String, default: '' },
  },
  emits: [
    'set-restriction',
  ],
  template: `
    <div class="h-full overflow-y-auto p-3 space-y-4">
      <!-- HARD section: server-enforced -->
      <div class="rounded-lg border-2 border-solid border-rose-300 bg-rose-50/40 p-3">
        <div class="flex items-center gap-2 mb-1">
          <span class="text-xs font-semibold uppercase tracking-wide text-rose-700">Restrictions</span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-rose-100 text-rose-700 border border-rose-300">enforced by the server</span>
        </div>
        <p class="text-xs text-slate-500 mb-2">
          Hard limits checked before every action the agent takes, saved instantly.
          Cannot be bypassed by the agent itself.
        </p>

        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can send messages</span>
          <input type="checkbox" :checked="form.restrictions.can_send_messages"
                 @change="$emit('set-restriction', 'can_send_messages', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can message groups</span>
          <input type="checkbox" :checked="form.restrictions.can_message_groups"
                 @change="$emit('set-restriction', 'can_message_groups', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can message private chats</span>
          <input type="checkbox" :checked="form.restrictions.can_message_private"
                 @change="$emit('set-restriction', 'can_message_private', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can start new private chats</span>
          <input type="checkbox" :checked="form.restrictions.can_message_new_private_contacts"
                 @change="$emit('set-restriction', 'can_message_new_private_contacts', $event.target.checked)" />
        </label>
        <label class="flex items-center justify-between gap-2 py-1 text-sm">
          <span>Can leave groups</span>
          <input type="checkbox" :checked="form.restrictions.can_leave_groups"
                 @change="$emit('set-restriction', 'can_leave_groups', $event.target.checked)" />
        </label>
      </div>

      <InlineAlert :message="error" />
    </div>
  `,
};

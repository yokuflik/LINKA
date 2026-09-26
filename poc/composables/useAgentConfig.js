// AI agent config + chat drawer (ADR 0045, frontend Wave 1 - see
// AGENT_DRAWER_UI_PLAN.md). GET/POST/PATCH /agents/me for config; the agent's
// own chat (owner_agent_chat_id) is a real chat_id and reuses the normal
// send_message WS action (ctx.sendRaw) - no new backend endpoint for sending.
// Global `useAgentConfig(ctx)` factory (no build step, loaded via <script src>).
//
// Needs from ctx: apiFetch, friendlyError, showToast, sendRaw, currentUser,
// clearUnreadCount (merged onto ctx from LinkaChatStore via useChats - ADR
// 0035), log, logError.
//
// The agent's own chat is deliberately kept OUT of LinkaChatStore.messages -
// that ref holds only the currently-active chat's history (selectChat's
// contract), and opening the agent drawer must never act like navigating
// away from whatever chat the user has open behind it. So this composable
// keeps its own small `agentMessages` list, fetched via the same
// `GET /chats/{id}/messages` endpoint the main chat pane uses, and appended
// to live by useWsRouter's new_message branch matching on chat_id (see
// AGENT_DRAWER_UI_PLAN.md Step 3/4).
//
// Live "thinking" status (agent_thinking WS event, modules/agents/
// invoke_worker.py) and cross-tab config sync (agent_config_changed WS
// event, modules/agents/router.py's PATCH /agents/me) are both live -
// agentThinkingStatus / applyAgentConfigChanged below are driven by
// useWsRouter.js's matching branches. Sending into the agent's own chat
// wakes it directly (modules/agents/trigger_engine.py's owner-chat special
// case), bypassing the normal on_specific_chats/on_time_window gating.
function useAgentConfig(ctx) {
  const { ref } = Vue;

  // The agent's own chat history - separate from LinkaChatStore.messages
  // (see file header). Newest-last, same row shape as the main chat's
  // messages ref (id, client_message_id, sender_id, type, content,
  // created_at, status, pending, send_failed).
  const agentMessages = ref([]);
  const agentMessagesLoading = ref(false);
  const agentMessagesLoaded = ref(false); // true once the first fetch completes (even if empty)
  const AGENT_MESSAGE_PAGE_SIZE = 50;
  // Keyset pagination for older history - same before_id convention as the
  // main chat pane's loadOlderMessages (useChatOpen.js), scoped locally
  // since the agent chat never touches LinkaChatStore.
  const agentHasMoreMessages = ref(false);
  const agentLoadingOlderMessages = ref(false);

  async function loadAgentMessages(chatId) {
    agentMessagesLoading.value = true;
    try {
      const history = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${AGENT_MESSAGE_PAGE_SIZE}`);
      // Server returns newest-first (see useChatOpen.js's history-merge
      // convention) - reverse to oldest-first for top-to-bottom rendering.
      agentMessages.value = Array.isArray(history) ? history.slice().reverse() : [];
      agentHasMoreMessages.value = Array.isArray(history) && history.length === AGENT_MESSAGE_PAGE_SIZE;
    } catch (err) {
      ctx.logError && ctx.logError('failed to load agent chat history', err);
      agentMessages.value = [];
      agentHasMoreMessages.value = false;
    } finally {
      agentMessagesLoading.value = false;
      agentMessagesLoaded.value = true;
    }
  }

  // Fetches the next older page (before_id = oldest loaded id) and prepends
  // it. Caller (AgentChatView) preserves scroll position around this call.
  async function loadOlderAgentMessages(chatId) {
    if (agentLoadingOlderMessages.value || !agentHasMoreMessages.value) return;
    if (!agentMessages.value.length) return;
    const oldestId = agentMessages.value[0].id;
    if (oldestId == null) return;
    agentLoadingOlderMessages.value = true;
    try {
      const page = await ctx.apiFetch(`/chats/${chatId}/messages?limit=${AGENT_MESSAGE_PAGE_SIZE}&before_id=${oldestId}`);
      agentHasMoreMessages.value = Array.isArray(page) && page.length === AGENT_MESSAGE_PAGE_SIZE;
      if (Array.isArray(page) && page.length) {
        agentMessages.value = page.slice().reverse().concat(agentMessages.value);
      }
    } catch (err) {
      ctx.logError && ctx.logError('failed to load older agent chat history', err);
    } finally {
      agentLoadingOlderMessages.value = false;
    }
  }

  // Called by useWsRouter's new_message branch when msg.chat_id matches the
  // agent's own chat - reconciles an optimistic bubble (our own send) or
  // appends the agent's reply.
  function onAgentChatMessage(msg) {
    const optimistic = msg.client_message_id
      ? agentMessages.value.find((x) => x.client_message_id === msg.client_message_id)
      : null;
    if (optimistic) {
      optimistic.id = msg.message_id;
      optimistic.created_at = msg.created_at;
      optimistic.status = msg.status;
      optimistic.content = msg.content;
      optimistic.pending = false;
      optimistic.send_failed = false;
      return;
    }
    agentMessages.value.push({
      id: msg.message_id, client_message_id: msg.client_message_id || null,
      sender_id: msg.sender_id, type: msg.type, content: msg.content,
      created_at: msg.created_at, status: msg.status,
    });
    // The agent's own reply arriving is the real "turn is over" signal - more
    // reliable than waiting on the separate agent_thinking {status: "done"}
    // event, which travels over fire-and-forget Redis pub/sub and is lost if
    // this tab's WS connection isn't subscribed at the exact moment it's
    // published (found via a real case: the reply landed but the thinking
    // indicator never cleared). AGENT_REPLY_MESSAGE_TYPE = 7
    // (modules/messaging/common.py) marks an agent-authored message.
    if (msg.type === 7) applyAgentThinking(null);
  }

  function onAgentChatMessageFailed(clientMessageId) {
    const m = agentMessages.value.find((x) => x.client_message_id === clientMessageId);
    if (m) { m.pending = false; m.send_failed = true; }
  }

  // Jump to a search hit inside the agent's own chat (see useSearch.js /
  // AgentDrawer's search button). Mirrors useChatOpen.js's jumpToMessage, but
  // operates on the local agentMessages list instead of LinkaChatStore, since
  // the agent chat is deliberately kept out of the store (see file header).
  const agentHighlightedId = ref(null);
  let agentHighlightTimer = null;

  async function jumpToAgentMessage(chatId, messageId) {
    if (!myAgent.value || chatId !== myAgent.value.owner_agent_chat_id) return;
    const alreadyLoaded = agentMessages.value.some((m) => m.id === messageId);
    if (!alreadyLoaded) {
      try {
        const window_ = await ctx.apiFetch(`/chats/${chatId}/messages/around/${messageId}`);
        if (Array.isArray(window_)) {
          agentMessages.value = window_.slice().reverse();
          agentHasMoreMessages.value = true;
        }
      } catch (err) {
        ctx.logError && ctx.logError('agent chat jump to message failed for', messageId, err);
        ctx.showToast(ctx.friendlyError(err, "Couldn't open that message."));
        return;
      }
    }
    if (agentHighlightTimer) clearTimeout(agentHighlightTimer);
    agentHighlightedId.value = messageId;
    agentHighlightTimer = setTimeout(() => { agentHighlightedId.value = null; }, 1600);
  }

  // null = not loaded yet / caller has no agent. Shape mirrors AgentOut.
  const myAgent = ref(null);
  const agentLoading = ref(false);

  // --- Drawer (replaces the old centered AgentConfigModal) ---
  const showAgentDrawer = ref(false);
  // 'chat' | 'settings'
  const agentDrawerView = ref('chat');
  const agentForm = ref(null); // editable clone of myAgent while the drawer is open
  const agentBusy = ref(false);
  const agentError = ref('');

  // Ephemeral "thinking" state - {status, detail} | null. Populated by
  // useWsRouter's agent_thinking branch (invoke_worker.py pushes it at turn
  // start/each tool call/turn end); never persisted or replayed on reconnect.
  const agentThinkingStatus = ref(null);
  let thinkingClearTimer = null;

  // --- Token usage windows (ADR 0059) ---
  // Polled via GET /agents/me/usage while the drawer is open - unlike
  // agent_thinking/agent_config_changed (pushed over WS from the worker),
  // usage isn't event-driven from any single call site the frontend could
  // subscribe to, so this establishes a small REST-poll loop instead (no
  // existing "poll a REST endpoint on an interval" convention in poc/ prior
  // to this - the closest analogs, useWebsocket.js's heartbeatTimer and
  // useMediaUpload.js's recordingTimer, both store the timer id in a closure
  // variable and clear it explicitly, which this follows).
  const agentUsage = ref(null); // null = not loaded yet. Shape: {window_5h, window_7d}
  let usagePollTimer = null;
  const AGENT_USAGE_POLL_INTERVAL_MS = 10000;

  async function loadAgentUsage() {
    try {
      const data = await ctx.apiFetch('/agents/me/usage');
      // _fetchedAtMs anchors UsageProgressBar.js's client-side countdown
      // ticker between polls - not part of the server response shape.
      data._fetchedAtMs = Date.now();
      agentUsage.value = data;
    } catch (err) {
      ctx.logError && ctx.logError('failed to load agent usage', err);
    }
  }

  function startAgentUsagePolling() {
    stopAgentUsagePolling();
    loadAgentUsage();
    usagePollTimer = setInterval(loadAgentUsage, AGENT_USAGE_POLL_INTERVAL_MS);
  }

  function stopAgentUsagePolling() {
    if (usagePollTimer) { clearInterval(usagePollTimer); usagePollTimer = null; }
  }

  // Any window at/over its cap disables the composer (chat input, +, send) -
  // the real enforcement is server-side (modules/agents/invoke_worker.py's
  // pre-flight gate); this is a UX convenience so the owner isn't left
  // typing into a box that will just silently fail to get a reply.
  const agentUsageBlocked = Vue.computed(() => {
    const u = agentUsage.value;
    if (!u) return false;
    return !!(u.window_5h && u.window_5h.is_blocked) || !!(u.window_7d && u.window_7d.is_blocked);
  });

  function applyAgentThinking(payload) {
    if (thinkingClearTimer) { clearTimeout(thinkingClearTimer); thinkingClearTimer = null; }
    if (!payload || payload.status === 'done' || payload.status === 'error') {
      agentThinkingStatus.value = payload || null;
      thinkingClearTimer = setTimeout(() => { agentThinkingStatus.value = null; }, 2000);
      return;
    }
    agentThinkingStatus.value = payload;
    // Safety net: the reply message itself (onAgentChatMessage, type 7)
    // normally clears this first, well within the turn's own
    // AGENT_TURN_TIMEOUT_SECONDS (20s) + a margin for round-trip/render time.
    // If the reply's new_message event AND every agent_thinking update after
    // this one are all lost (fire-and-forget WS/pub-sub, no replay), the
    // indicator would otherwise stick forever with no way to tell it's stale
    // - clear it unconditionally so a lost signal never looks like a live one.
    thinkingClearTimer = setTimeout(() => { agentThinkingStatus.value = null; }, 30000);
  }

  async function loadMyAgent() {
    agentLoading.value = true;
    try {
      myAgent.value = await ctx.apiFetch('/agents/me');
    } catch (err) {
      if (err && err.status === 404) {
        myAgent.value = null;
      } else {
        ctx.logError && ctx.logError('failed to load agent', err);
      }
    } finally {
      agentLoading.value = false;
    }
  }

  async function activateMyAgent() {
    agentBusy.value = true;
    agentError.value = '';
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', { method: 'POST' });
      agentForm.value = cloneForm(myAgent.value);
      ctx.showToast('Agent activated');
    } catch (err) {
      agentError.value = ctx.friendlyError(err, "We couldn't activate your agent. Please try again.");
    } finally {
      agentBusy.value = false;
    }
  }

  function cloneForm(agent) {
    return JSON.parse(JSON.stringify(agent));
  }

  // --- Drawer open/close (replaces openAgentModal/closeAgentModal) ---

  async function openAgentDrawer() {
    agentError.value = '';
    agentDrawerView.value = 'chat';
    if (!myAgent.value) await loadMyAgent();
    agentForm.value = myAgent.value ? cloneForm(myAgent.value) : null;
    promptDirty.value = false;
    hardTextDirty.value = false;
    byokKeyInput.value = '';
    byokDirty.value = false;
    showAgentDrawer.value = true;
    if (myAgent.value) {
      ctx.clearUnreadCount(myAgent.value.owner_agent_chat_id);
      if (!agentMessagesLoaded.value) await loadAgentMessages(myAgent.value.owner_agent_chat_id);
      startAgentUsagePolling();
    }
  }

  function closeAgentDrawer() {
    showAgentDrawer.value = false;
    agentDrawerView.value = 'chat';
    stopAgentUsagePolling();
  }

  function openAgentSettings() {
    agentDrawerView.value = 'settings';
    // Knowledge base (ADR 0046 decision 4) - lazy-loaded on first entry into
    // the settings view, not on every drawer open (chat view doesn't need it).
    if (ctx.loadKnowledgeDocuments && !ctx.knowledgeLoaded.value) {
      ctx.loadKnowledgeDocuments();
    }
  }

  function backToAgentChat() {
    agentDrawerView.value = 'chat';
  }

  // --- Kill switch (master toggle) - commits immediately, like a checkbox ---

  async function toggleAgentEnabled(enabled) {
    if (!myAgent.value) return;
    agentForm.value = { ...agentForm.value, is_enabled: enabled };
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({ is_enabled: enabled }),
      });
      agentForm.value = cloneForm(myAgent.value);
    } catch (err) {
      agentForm.value = { ...agentForm.value, is_enabled: !enabled }; // revert on failure
      ctx.showToast(ctx.friendlyError(err, "We couldn't update your agent. Please try again."));
    }
  }

  // --- Soft section: system_prompt, Save/Cancel, dirty-guarded ---

  const promptDirty = ref(false);

  function onPromptInput(value) {
    agentForm.value = { ...agentForm.value, system_prompt: value };
    promptDirty.value = true;
  }

  async function saveSoftPrompt() {
    if (!agentForm.value) return;
    agentBusy.value = true;
    agentError.value = '';
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({ system_prompt: agentForm.value.system_prompt }),
      });
      agentForm.value = cloneForm(myAgent.value);
      promptDirty.value = false;
      ctx.showToast('Instructions saved');
    } catch (err) {
      agentError.value = ctx.friendlyError(err, "We couldn't save your instructions. Please try again.");
    } finally {
      agentBusy.value = false;
    }
  }

  function cancelSoftPromptEdit() {
    if (!myAgent.value) return;
    agentForm.value = { ...agentForm.value, system_prompt: myAgent.value.system_prompt };
    promptDirty.value = false;
  }

  // --- Hard section: checkboxes commit immediately; text fields (max/day,
  // chat-trigger keywords) are Save/Cancel, dirty-guarded like the prompt ---

  const hardTextDirty = ref(false);

  async function setRestrictionCheckbox(key, value) {
    if (!myAgent.value) return;
    agentForm.value = {
      ...agentForm.value,
      restrictions: { ...agentForm.value.restrictions, [key]: value },
    };
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({ restrictions: { [key]: value } }),
      });
      agentForm.value = cloneForm(myAgent.value);
    } catch (err) {
      agentForm.value = {
        ...agentForm.value,
        restrictions: { ...agentForm.value.restrictions, [key]: !value },
      };
      ctx.showToast(ctx.friendlyError(err, "We couldn't update that restriction. Please try again."));
    }
  }

  function onMaxMessagesInput(value) {
    agentForm.value = {
      ...agentForm.value,
      restrictions: { ...agentForm.value.restrictions, max_messages_per_day: value },
    };
    hardTextDirty.value = true;
  }

  async function saveHardTextFields() {
    if (!agentForm.value) return;
    agentBusy.value = true;
    agentError.value = '';
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({
          restrictions: { max_messages_per_day: agentForm.value.restrictions.max_messages_per_day },
        }),
      });
      agentForm.value = cloneForm(myAgent.value);
      hardTextDirty.value = false;
      ctx.showToast('Restrictions saved');
    } catch (err) {
      agentError.value = ctx.friendlyError(err, "We couldn't save your restrictions. Please try again.");
    } finally {
      agentBusy.value = false;
    }
  }

  function cancelHardTextEdit() {
    if (!myAgent.value) return;
    agentForm.value = {
      ...agentForm.value,
      restrictions: { ...agentForm.value.restrictions, max_messages_per_day: myAgent.value.restrictions.max_messages_per_day },
    };
    hardTextDirty.value = false;
  }

  // --- BYOK: write-only Gemini key field (ADR 0046 decision 6). The server
  // never echoes the key back (AgentOut only exposes has_custom_key), so the
  // input always starts empty; typing something and saving replaces the
  // stored key, clearing and saving with an empty value falls back to the
  // shared key. Save/Cancel like the other text fields; Cancel just clears
  // the local draft (there is nothing server-side to revert to).

  const byokDirty = ref(false);
  const byokKeyInput = ref('');

  function onByokKeyInput(value) {
    byokKeyInput.value = value;
    byokDirty.value = true;
  }

  async function saveByokKey() {
    if (!myAgent.value) return;
    agentBusy.value = true;
    agentError.value = '';
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({ gemini_api_key: byokKeyInput.value.trim() || null }),
      });
      agentForm.value = cloneForm(myAgent.value);
      byokKeyInput.value = '';
      byokDirty.value = false;
      ctx.showToast(myAgent.value.has_custom_key ? 'Custom Gemini key saved' : 'Custom Gemini key cleared');
    } catch (err) {
      agentError.value = ctx.friendlyError(err, "We couldn't save your Gemini key. Please try again.");
    } finally {
      agentBusy.value = false;
    }
  }

  function cancelByokKeyEdit() {
    byokKeyInput.value = '';
    byokDirty.value = false;
  }

  async function clearByokKey() {
    if (!myAgent.value || !myAgent.value.has_custom_key) return;
    agentBusy.value = true;
    agentError.value = '';
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({ gemini_api_key: null }),
      });
      agentForm.value = cloneForm(myAgent.value);
      byokKeyInput.value = '';
      byokDirty.value = false;
      ctx.showToast('Custom Gemini key cleared');
    } catch (err) {
      agentError.value = ctx.friendlyError(err, "We couldn't clear your Gemini key. Please try again.");
    } finally {
      agentBusy.value = false;
    }
  }

  // --- Trigger-list editing (agentForm.triggers.on_specific_chats) ---
  // Add/remove-chat buttons commit immediately (checkbox-like); the keyword
  // text per chat is Save/Cancel like any other text field.

  async function addAgentChatTrigger(chatId) {
    if (!agentForm.value || !chatId || agentForm.value.triggers.on_specific_chats[chatId]) return;
    const nextTriggers = {
      ...agentForm.value.triggers,
      on_specific_chats: { ...agentForm.value.triggers.on_specific_chats, [chatId]: { keywords: [] } },
    };
    agentForm.value = { ...agentForm.value, triggers: nextTriggers };
    await patchTriggersImmediate(nextTriggers);
  }

  async function removeAgentChatTrigger(chatId) {
    if (!agentForm.value) return;
    const rest = { ...agentForm.value.triggers.on_specific_chats };
    delete rest[chatId];
    const nextTriggers = { ...agentForm.value.triggers, on_specific_chats: rest };
    agentForm.value = { ...agentForm.value, triggers: nextTriggers };
    await patchTriggersImmediate(nextTriggers);
  }

  async function patchTriggersImmediate(triggers) {
    try {
      myAgent.value = await ctx.apiFetch('/agents/me', {
        method: 'PATCH',
        body: JSON.stringify({ triggers }),
      });
      agentForm.value = cloneForm(myAgent.value);
    } catch (err) {
      ctx.showToast(ctx.friendlyError(err, "We couldn't update the agent's triggers. Please try again."));
    }
  }

  const chatKeywordsDirty = ref({}); // chat_id -> bool

  function onChatTriggerKeywordsInput(chatId, keywordsText) {
    if (!agentForm.value || !agentForm.value.triggers.on_specific_chats[chatId]) return;
    const keywords = keywordsText.split(',').map((k) => k.trim()).filter(Boolean);
    agentForm.value = {
      ...agentForm.value,
      triggers: {
        ...agentForm.value.triggers,
        on_specific_chats: {
          ...agentForm.value.triggers.on_specific_chats,
          [chatId]: { keywords },
        },
      },
    };
    chatKeywordsDirty.value = { ...chatKeywordsDirty.value, [chatId]: true };
  }

  async function saveChatTriggerKeywords(chatId) {
    if (!agentForm.value) return;
    await patchTriggersImmediate(agentForm.value.triggers);
    const next = { ...chatKeywordsDirty.value };
    delete next[chatId];
    chatKeywordsDirty.value = next;
  }

  function cancelChatTriggerKeywordsEdit(chatId) {
    if (!myAgent.value || !agentForm.value) return;
    agentForm.value = {
      ...agentForm.value,
      triggers: {
        ...agentForm.value.triggers,
        on_specific_chats: {
          ...agentForm.value.triggers.on_specific_chats,
          [chatId]: myAgent.value.triggers.on_specific_chats[chatId] || { keywords: [] },
        },
      },
    };
    const next = { ...chatKeywordsDirty.value };
    delete next[chatId];
    chatKeywordsDirty.value = next;
  }

  async function setTimeWindowField(key, value) {
    if (!agentForm.value) return;
    const nextTriggers = {
      ...agentForm.value.triggers,
      on_time_window: { ...agentForm.value.triggers.on_time_window, [key]: value },
    };
    agentForm.value = { ...agentForm.value, triggers: nextTriggers };
    await patchTriggersImmediate(nextTriggers);
  }

  async function setAnyMessageEnabled(enabled) {
    if (!agentForm.value) return;
    const nextTriggers = {
      ...agentForm.value.triggers,
      on_any_message: { ...agentForm.value.triggers.on_any_message, enabled },
    };
    agentForm.value = { ...agentForm.value, triggers: nextTriggers };
    await patchTriggersImmediate(nextTriggers);
  }

  // --- Sending a message into the agent's own chat (wakes it directly via
  // trigger_engine.py's owner-chat special case) ---

  function sendAgentChatMessage(text) {
    const content = (text || '').trim();
    if (!content || !myAgent.value) return;
    const chatId = myAgent.value.owner_agent_chat_id;
    const clientMessageId = crypto.randomUUID();
    ctx.sendRaw({
      type: 'send_message',
      chat_id: chatId,
      client_message_id: clientMessageId,
      content,
      message_type: 1,
    });
    agentMessages.value.push({
      id: null, client_message_id: clientMessageId,
      sender_id: ctx.currentUser.value.id, type: 1, content,
      created_at: new Date().toISOString(),
      status: 'SENT', pending: true, send_failed: false,
    });
  }

  // Placeholder only - the PDF picker in the agent chat's [+] button is
  // wired up (file selection + type restriction) but nothing is done with
  // the picked file yet (no upload/parsing), per explicit instruction.
  function onAgentPdfPicked(file) {
    ctx.showToast('PDF picked: ' + file.name + ' (not sent yet)');
  }

  // --- Reset to default (ADR 0050): irreversibly wipes the owner-agent
  // chat's message history + knowledge base, and restores every soft/hard
  // setting to default. Confirmation happens here (window.confirm), matching
  // the existing delete-message-forever convention (useMessageEdit.js) -
  // there's no ConfirmModal component in this codebase.

  const agentResetBusy = ref(false);

  async function resetAgentToDefault() {
    if (!myAgent.value || agentResetBusy.value) return;
    const confirmed = window.confirm(
      "Reset this agent to default? This deletes all of its chat history and knowledge base forever, and restores its settings and restrictions to default. This cannot be undone."
    );
    if (!confirmed) return;

    agentResetBusy.value = true;
    agentError.value = '';
    try {
      myAgent.value = await ctx.apiFetch('/agents/me/reset', { method: 'POST' });
      agentForm.value = cloneForm(myAgent.value);
      promptDirty.value = false;
      hardTextDirty.value = false;
      byokDirty.value = false;
      byokKeyInput.value = '';
      chatKeywordsDirty.value = {};
      // Every prior message in the chat was just hard-purged server-side
      // (content/media wiped, rows kept for the partition key - ADR 0021/
      // 0050) - a reload here would re-fetch them as empty "message deleted"
      // tombstones (GET /chats/{id}/messages always includes soft-deleted/
      // purged rows so a live delete can render in place, see read_api.py).
      // Just clear the list instead; the fresh greeting the backend just
      // sent arrives on its own via the normal new_message WS event, same as
      // right after agent creation (activateMyAgent never reloads either).
      agentMessages.value = [];
      agentMessagesLoaded.value = true;
      agentHasMoreMessages.value = false;
      if (ctx.knowledgeLoaded) ctx.knowledgeLoaded.value = false;
      if (ctx.knowledgeDocuments) ctx.knowledgeDocuments.value = [];
      ctx.showToast('Agent reset to default');
    } catch (err) {
      agentError.value = ctx.friendlyError(err, "We couldn't reset your agent. Please try again.");
    } finally {
      agentResetBusy.value = false;
    }
  }

  // Applied by useWsRouter's agent_config_changed branch (fired by
  // router.py's PATCH /agents/me for cross-tab/cross-device sync). Guards
  // against clobbering an in-progress text edit (promptDirty / hardTextDirty
  // / chatKeywordsDirty).
  function applyAgentConfigChanged(agent) {
    myAgent.value = agent;
    if (!agentForm.value) return;
    const merged = cloneForm(agent);
    if (promptDirty.value) merged.system_prompt = agentForm.value.system_prompt;
    if (hardTextDirty.value) merged.restrictions.max_messages_per_day = agentForm.value.restrictions.max_messages_per_day;
    for (const chatId of Object.keys(chatKeywordsDirty.value)) {
      if (agentForm.value.triggers.on_specific_chats[chatId]) {
        merged.triggers.on_specific_chats[chatId] = agentForm.value.triggers.on_specific_chats[chatId];
      }
    }
    agentForm.value = merged;
  }

  // Called from useAuth.logout() - without this, switching users in the same
  // tab leaves the previous owner's agent/messages/drawer state in these refs
  // until loadMyAgent() happens to overwrite myAgent, and agentMessagesLoaded
  // staying true means the next owner's openAgentDrawer() never re-fetches,
  // showing the previous user's agent chat.
  function resetAgentConfig() {
    myAgent.value = null;
    agentLoading.value = false;
    agentMessages.value = [];
    agentMessagesLoading.value = false;
    agentMessagesLoaded.value = false;
    agentHasMoreMessages.value = false;
    agentLoadingOlderMessages.value = false;
    showAgentDrawer.value = false;
    agentDrawerView.value = 'chat';
    agentForm.value = null;
    agentBusy.value = false;
    agentError.value = '';
    agentThinkingStatus.value = null;
    promptDirty.value = false;
    hardTextDirty.value = false;
    byokDirty.value = false;
    byokKeyInput.value = '';
    chatKeywordsDirty.value = {};
    stopAgentUsagePolling();
    agentUsage.value = null;
  }

  return {
    myAgent, agentLoading,
    agentMessages, agentMessagesLoading, agentMessagesLoaded,
    agentHasMoreMessages, agentLoadingOlderMessages,
    loadAgentMessages, loadOlderAgentMessages, onAgentChatMessage, onAgentChatMessageFailed,
    agentHighlightedId, jumpToAgentMessage,
    showAgentDrawer, agentDrawerView, agentForm, agentBusy, agentError,
    agentThinkingStatus, applyAgentThinking, applyAgentConfigChanged,
    agentUsage, agentUsageBlocked, loadAgentUsage,
    loadMyAgent, activateMyAgent,
    openAgentDrawer, closeAgentDrawer, openAgentSettings, backToAgentChat,
    toggleAgentEnabled,
    promptDirty, onPromptInput, saveSoftPrompt, cancelSoftPromptEdit,
    hardTextDirty, setRestrictionCheckbox, onMaxMessagesInput, saveHardTextFields, cancelHardTextEdit,
    byokDirty, byokKeyInput, onByokKeyInput, saveByokKey, cancelByokKeyEdit, clearByokKey,
    addAgentChatTrigger, removeAgentChatTrigger, setTimeWindowField, setAnyMessageEnabled,
    chatKeywordsDirty, onChatTriggerKeywordsInput, saveChatTriggerKeywords, cancelChatTriggerKeywordsEdit,
    sendAgentChatMessage,
    onAgentPdfPicked,
    resetAgentConfig,
    agentResetBusy, resetAgentToDefault,
  };
}

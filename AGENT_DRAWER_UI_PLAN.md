# AI Agent Drawer UI — Implementation Plan (planning only, no code yet)

Extends ADR 0045/0046/0047. Replaces the current centered `AgentConfigModal`
with a slide-in drawer that is both a live chat with the agent AND its
settings screen. Touches backend (new WS events, one new endpoint) and
frontend (new components/composables). No code is written in this pass.

## ⚠ Execution scope: FRONTEND ONLY for now (2026-09-23 decision)

ADR 0046 and ADR 0047 are being implemented concurrently (other sessions),
and they touch the **same three backend files** this plan's backend steps
need: `modules/agents/invoke_worker.py`, `modules/agents/router.py`, and
`modules/agents/trigger_engine.py`. To avoid merge collisions / racing
logic, this plan is executed in two waves:

- **Wave 1 (now):** Steps 3, 4, 6, 7, 8, 10 — frontend only (new Vue
  components/composables, WS-router branches for events that don't exist
  yet, the drawer UI, badge). These don't touch any backend file.
- **Wave 2 (after 0046/0047 land):** Steps 1, 2, 5a, 9 — the backend
  additions (`agent_thinking` push, `agent_config_changed` push, owner-chat
  wake-up fix, default-restrictions change). Revisit `invoke_worker.py`/
  `router.py`/`trigger_engine.py` against their post-0046/0047 shape before
  writing these, since the exact call sites (`_run_turn`'s loop structure,
  the trigger-matching function names) may have shifted.

Frontend work in Wave 1 can proceed against today's `GET/POST/PATCH
/agents/me` and today's plain `new_message`/`typing`/`chat_pin_changed`
events — the drawer UI, chat view, and settings view all render correctly
without `agent_thinking`/`agent_config_changed` existing yet; those two
events are additive enhancements the frontend can wire in later without
reshaping the components.

## Current state (baseline)

- `poc/components/AgentConfigModal.js` + `poc/composables/useAgentConfig.js`:
  centered modal, config-only (no chat), `@click.self` backdrop close.
- Entry point: circular avatar button in `AppHeader.js`, left of the user's
  own avatar (`@open-agent`).
- Backend: `GET/POST/PATCH /agents/me` only. The agent's actual messages land
  as ordinary `new_message` WS events in `Agent.owner_agent_chat_id` (a
  1:1-shaped chat with only the owner as participant) — same path any human
  message takes. **There is no "thinking" status push and no dedicated
  cross-tab "agent config/state changed" event today** — this is the main
  backend gap this feature needs to close.
- Cross-tab sync precedent already exists for other personal settings
  (`chat_pin_changed`, `chat_mute_changed`, `profile_updated`) — fanned out
  over the user's **personal channel** (`realtime_service.subscribe_to_chat`
  equivalent for a user, see `realtime/realtime_service.py`), applied in
  `useWsRouter.js` by matching `msg.event` and patching `LinkaChatStore` in
  place. The agent-config sync should follow this exact pattern.
- Badges: `LinkaChatStore.unreadCountByChatId` (`useChatStore.js`), bumped in
  `useWsRouter.js` on `new_message` when the message's chat isn't the active
  one, cleared via `store.clearUnreadCount`. Since the agent's messages are
  just `new_message` events in `owner_agent_chat_id`, **this already works
  today as long as the drawer counts as "closed = chat not active"** — needs
  a small adaptation since the drawer isn't a chat window governed by
  `store.activeChatId`.
- Typing precedent: `useTyping.js` (`typingUsersByChatId`, WS `typing`
  event) — same shape reused conceptually for "agent is thinking", but the
  agent's thinking state needs richer intermediate content (tool-call
  progress), not just a boolean, so it's a new mechanism, not a reuse of
  `useTyping.js` itself.
- No drawer/slide-in pattern exists yet in the PoC — every existing overlay
  (`AgentConfigModal`, `SettingsModal`, `SearchModal`, etc.) is a centered
  `fixed inset-0` backdrop + boxed panel. The drawer is new UI grammar for
  this codebase.

---

## Step 0 — ADR

Per CLAUDE.md Rule 7: this is a UI-architecture + new-WS-event decision, not
just a component rewrite (introduces a "live agent status" channel + a new
drawer interaction pattern). Write `docs/adr/0048-agent-chat-drawer-ui.md`
before touching code. Content to lock in:
- Drawer supersedes the modal; single entry point unchanged (`AppHeader`
  button), but click target now toggles a drawer, not a modal.
- New WS event family: `agent_thinking` (ephemeral, not persisted) and
  `agent_config_changed` (persisted-state echo, cross-tab sync), fanned out
  over the owner's **personal channel** — never broadcast beyond the owner,
  since `Agent` is single-owner by construction (ADR 0045).
- Badge counting reuses `unreadCountByChatId[owner_agent_chat_id]` rather
  than inventing a parallel counter — the agent's chat already is a real
  `chat_id`.
- Settings changes commit **immediately per-field** for checkboxes/toggles
  (PATCH on change), but batch with explicit Save/Cancel for free-text
  (`system_prompt`, keyword lists) — mirrors the split already implied by
  the request ("כל מה שהוא טקסט יהיה כפתור של שמירה וביטול וכל השאר... צק
  בוקס בכל מקרה").

---

## Step 1 — Backend: live "thinking" status push [WAVE 2 — deferred]

**Why needed:** `invoke_worker.py::_run_turn` currently only writes to DB /
calls tools; it never tells the owner's live connections "I'm working on
this." Without it there is nothing for the drawer to render as "thinking…".

- New tiny fire-and-forget publish helper, e.g.
  `modules/agents/status_events.py::publish_agent_status(owner_user_id,
  status, detail=None)` → calls the existing personal-channel publish used
  by `chat_pin_changed`/`profile_updated` (reuse `realtime_service`'s
  publish-to-user primitive, do not invent a second transport).
- Emits `{event: "agent_thinking", agent_id, status: "started"|"tool_call"|
  "done"|"error", detail}` where `detail` is a short human string ("Reading
  chat history…", "Sending a message…", tied to which tool is about to run).
- Call sites inside `invoke_worker.py::_run_turn`:
  - turn start → `status="started"`.
  - right before `tools.execute_tool_call` → `status="tool_call"`, detail
    from a small `tool_name -> label` map.
  - turn end (success, error, or timeout — the existing `finally`) →
    `status="done"` or `"error"`.
- Ephemeral only — never persisted, never replayed on reconnect (matches how
  `typing` already works). A drawer opened mid-turn simply shows nothing
  until the next status event; acceptable, matches existing typing-indicator
  semantics in this codebase.
- Rate/volume: bounded by the existing turn-level limits (max 4 tool
  round-trips, 20s timeout), so at most ~6 events per turn — no new limiter
  needed.

## Step 2 — Backend: config cross-tab sync event [WAVE 2 — deferred]

**Why needed:** today `PATCH /agents/me` only returns the new state to the
caller's own tab; a second open tab/device never learns about it (unlike
`chat_pin_changed` etc., which already fan out).

- Extend `modules/agents/router.py`'s `PATCH /agents/me` handler: after a
  successful `update_agent_config` + `cache.sync_agent_cache`, publish
  `{event: "agent_config_changed", agent: <AgentOut serialized>}` to the
  owner's personal channel (same helper as Step 1).
- Frontend applies it by replacing `myAgent.value` wholesale (simplest
  correct merge — the payload is the full authoritative row, same shape
  `GET /agents/me` already returns).
- Self-echo is fine and desired: the tab that made the change also gets this
  event, matching the existing `chat_pin_changed` idiom (setting the same
  value is a no-op if `agentForm` was already saved).

## Step 3 — Frontend: `useWsRouter.js` wiring

Add two `if (msg.event === ...)` branches (same flat-dispatch style as the
rest of the file, no refactor of the dispatcher). **Wave 1 note:** since
Steps 1/2 (the events themselves) are deferred to Wave 2, these branches
are added now but are effectively dead code until the backend emits the
events — safe to land early since an unmatched `msg.event` is already a
silent no-op in this dispatcher (falls through to the `log('unhandled...')`
line at the bottom today; adding the branch just pre-wires the handler).
Alternatively, skip adding these two branches in Wave 1 and add them
together with Steps 1/2 in Wave 2 — either ordering is safe; recommend
pre-wiring now since it's a small, isolated addition and lets Wave 2 be
backend-only.
- `agent_thinking` → write into a new small piece of state (see Step 4),
  keyed by `agent_id` (single agent per user, so effectively a singleton,
  but keep the key for shape-consistency / future multi-agent-proofing is
  explicitly NOT wanted per YAGNI — just use a flat ref, no map).
- `agent_config_changed` → `useAgentConfig`'s state update (see Step 5);
  requires `useAgentConfig`'s refs to be reachable from the router the same
  way `store.*` is — either promote agent state into `LinkaChatStore` (see
  decision in Step 4) or pass the setter in via `ctx` like `onSendError` etc.
  are already done for cross-composable calls.

## Step 4 — Decide: does agent chat/live-state join `LinkaChatStore`?

Recommendation: **yes, partially.** `LinkaChatStore` (ADR 0035) is explicitly
"single source of truth for chats/messages/unread/buffered-messages." The
agent's messages are already ordinary rows in a real `chat_id` — treating
`owner_agent_chat_id` as just another chat in `store.chats`/`store.messages`
means:
- The existing `new_message`, unread-badge, and message-list machinery works
  for the agent chat for free, with zero special-casing in `useWsRouter.js`'s
  `new_message` branch.
- Only the *new* pieces are genuinely new: `agentThinkingStatus` (ephemeral,
  small, lives in `useAgentConfig.js` — no reason to bloat the store with a
  transient UI-only ref) and the settings (`myAgent`/`agentForm`, stays in
  `useAgentConfig.js` — config is not chat data).
- So: **messages/unread reuse the store as-is; only `agentThinkingStatus` and
  the settings form stay in `useAgentConfig.js`.** No wholesale merge needed.

## Step 5 — Frontend: `useAgentConfig.js` changes

- Add `agentThinkingStatus = ref(null)` (`{status, detail}` or `null`), set
  by the new `agent_thinking` WS branch, cleared to `null` on `"done"`/
  `"error"` after a short delay (so "Done" doesn't just vanish instantly —
  reuse the same fade-timeout idiom `useTyping.js` uses for expiry).
- Add `applyAgentConfigChanged(agent)` — replaces `myAgent.value = agent`;
  if the drawer's settings screen is open and the user has **no unsaved
  edit** for a given field, refresh `agentForm` for that field live (see
  Step 7's per-section dirty-tracking — needed so an incoming sync doesn't
  clobber something the user is mid-typing).
- Split `saveAgentConfig` into two: `savePendingHardChanges()` (checkboxes —
  fires immediately per Step 8, not batched) and `saveSoftPrompt()` /
  `cancelSoftPromptEdit()` (the free-text section, explicit Save/Cancel).
- Add `sendAgentChatMessage(text)` — this is new: today nothing lets the
  owner *talk to* the agent from the UI. To be precise about what already
  works vs. what's missing: the agent can already message **anyone**
  (other users, groups) via the existing `send_message`/`reply_message`
  tools in `modules/agents/tools.py` — that path is not the gap. The gap is
  narrower: the dedicated 1:1 **owner↔agent** chat (`owner_agent_chat_id`,
  only the owner as participant) has no wake mechanism today. The Trigger
  Rule Engine only evaluates *other* participants' agents against an
  incoming message (`trigger_engine.py`'s candidate lookup is "the chat's
  other participants' enabled agents") — since the owner-agent chat's only
  member is the owner, a message the owner sends into it today is never
  looked at as a trigger for their own agent at all.
  - **Recommended transport:** reuse the existing `send_message` WS action
    targeting `owner_agent_chat_id` exactly like a normal chat message (no
    new backend endpoint for sending) — the message persists and echoes
    normally; only the *wake* path needs a new special case (Step 5a).
  - **Step 5a (backend) [WAVE 2 — deferred]:** special-case the owner-agent
    chat in the send path (`modules/messaging/send.py::process_outgoing` or
    the trigger engine): if `message.chat_id == agent.owner_agent_chat_id`
    and `sender_id == agent.owner_user_id`, enqueue an invocation directly
    (bypassing the normal "other participants' agents" trigger match,
    bypassing time-window/keyword gating — talking to your own agent
    directly is always an explicit wake, not a passive trigger) but still
    subject to `is_enabled` and the existing hourly/daily quotas.
  - **Wave 1 consequence:** until Step 5a lands, `sendAgentChatMessage` can
    still be built and wired end-to-end (the message sends, persists, and
    appears in the drawer via the normal `new_message` path) but the agent
    will not actually respond to it yet — this is expected and fine for
    Wave 1; call it out to the user/tester so "the agent didn't reply" isn't
    read as a frontend bug during this wave.
- Add `openingGreeting` logic: when the drawer is opened and
  `store.messages` for `owner_agent_chat_id` is empty (first-ever open),
  locally inject one client-side-only greeting bubble ("שלום, אני סוכן
  ה-AI שלך...") — **not persisted, not sent through the WS**, purely a
  local UI nicety (decide in Step 0's ADR whether this should instead be a
  real system message written once at agent-creation time in
  `POST /agents/me` — simpler, persists correctly across devices/reloads,
  recommended over a client-side-only fake).

## Step 6 — New component: `AgentDrawer.js` (replaces `AgentConfigModal.js`)

Structural shell only in this step (chat body + settings body are Steps 7-8):
- Slide-in panel from the right (or left, RTL — confirm with user during
  implementation given Hebrew UI), `fixed inset-y-0 ... transform
  transition-transform`, backdrop `fixed inset-0 bg-black/30` behind it with
  `@click` (not `@click.self`, since the backdrop is a separate full-screen
  sibling element here, unlike the modal's self-click trick) closing the
  drawer — first actual drawer pattern in this codebase, so this is new CSS,
  not a copy-paste.
- Header row: master `is_enabled` toggle switch (real sliding toggle, not a
  checkbox — new small presentational piece, e.g. inline SVG/CSS toggle;
  check if Tailwind-only is enough or a tiny reusable `ToggleSwitch.js`
  component is worth extracting — recommend extracting it since a "real"
  sliding toggle is visually distinct from every other `<input type=
  checkbox>` in this codebase and worth one shared component), settings
  icon button (sliders icon, 3 horizontal lines with circles — new inline
  SVG, same style as the existing header icon buttons), close (×).
- Body: `v-if="!agentEnabled"` → whole body gets a `pointer-events-none
  opacity-50` wrapper (the "grayed out" requirement) but is still mounted
  (not `v-if` on the whole content) so state isn't lost by toggling.
  `v-else` → `v-if="settingsView"` picks chat view vs settings view.
- Not-yet-activated state (no `myAgent` at all) keeps today's single
  "Activate agent" CTA, shown instead of the toggle/body.

## Step 7 — New component: `AgentChatView.js` (chat body of the drawer)

- Reuses `MessageList.js` if its props allow pointing at an arbitrary
  `chat_id`/message subset (check current prop contract — it currently
  reads from `store.messages` filtered by `store.activeChatId`; the agent
  drawer is deliberately **not** changing `store.activeChatId`, since
  opening the agent drawer must not act like navigating away from whatever
  chat the user is in behind it). Likely needs `MessageList` to accept an
  explicit `chatId` prop instead of always trusting the store's
  `activeChatId` — check this during implementation; if too invasive, a
  small dedicated read-only render loop inside `AgentChatView.js` is an
  acceptable fallback (still binding to the same `store.messages` array,
  just filtering by the fixed `owner_agent_chat_id` instead of the active
  one).
- Reuses `MessageInput.js` for the compose box, wired to
  `sendAgentChatMessage` from Step 5 instead of the normal chat send path.
- New: `agentThinkingStatus` renders as a "typing"-indicator-styled row (
  visually consistent with `activeChatTypingLabel` in `useTyping.js`, but
  fed by `agentThinkingStatus.detail` instead) — shows the live intermediate
  step text ("Reading chat history…", etc.), replaced/cleared per Step 5.

## Step 8 — New component: `AgentSettingsView.js` (settings body of the drawer)

Two scrollable sections, per the request:
- **Top: soft section.** `system_prompt` textarea, reactive to external
  changes (`agent_config_changed` events, Step 5) *only when not actively
  being edited* — track a local `promptDirty` bool, set true on first
  `input`, false after Save/Cancel; while dirty, incoming sync updates
  `myAgent` but does NOT overwrite the textarea (avoid clobbering an
  in-progress edit — matches how `agentForm` already diffs against
  `myAgent` in the current code, just needs the dirty-guard added since sync
  is now live instead of load-once). Save/Cancel buttons appear only while
  `promptDirty` — `Save` → `PATCH {system_prompt}`; `Cancel` → revert
  textarea to `myAgent.system_prompt`, `promptDirty=false`.
- **Bottom: hard section.** Every checkbox (`restrictions.can_*`, and the
  new per-request default change — see Step 9) commits on-change
  immediately via its own small PATCH (or a debounced micro-batch if rapid
  double-toggles are a concern — recommend immediate, un-batched, since
  these are infrequent user actions and simplicity wins per CLAUDE.md
  YAGNI). Any **text** field in this section (`max_messages_per_day` number
  input counts as "not a checkbox" per the request's wording — clarify
  with user whether a number input should behave like text [Save/Cancel] or
  like a toggle [immediate]; recommend treating it like text since it's
  typed, not clicked) gets the same dirty/Save/Cancel treatment as the soft
  section. Chat-trigger keyword rows (comma-separated text) also fall under
  "text → Save/Cancel"; the add/remove-chat-trigger buttons themselves
  behave like checkboxes (immediate).
- Both sections read from `myAgent`/`agentForm` reactively, so an
  `agent_config_changed` echo from another tab updates checkboxes live with
  no user action needed (checkboxes have no local "dirty" concept — they
  always reflect server state, consistent with "immediate commit").
- **Wave 1 note:** everything above works today against the existing
  `PATCH /agents/me` (immediate per-checkbox PATCH, Save/Cancel for text).
  The only piece that needs Wave 2's `agent_config_changed` event is true
  **live** cross-tab refresh while a second tab is open at the same time —
  without it, a second open tab simply won't see the change until it
  reloads/reopens the drawer (same as today's behavior). Not a regression,
  just not yet the fully live experience described in the request; call
  this out once Wave 1 ships.

## Step 9 — Default restrictions change [WAVE 2 — deferred] (explicit product decision, needs confirmation)

The request states a new default: "מותר לשלוח הודעות רק לאנשים פרטיים
ולקרוא מהכל" (allowed to send only to private chats, read everything). This
differs from the current `DEFAULT_AGENT_RESTRICTIONS` in
`modules/agents/models.py` (today: `can_message_groups: true`). Options:
- **(a)** Change the default for *newly created* agents only (no
  migration/backfill of existing rows — matches this repo's no-migration
  convention, ADR pattern "existing rows just don't have the new default").
- **(b)** Also backfill existing agents (needs the user's explicit go-ahead
  per CLAUDE.md Rule 10 — this is a backend behavior/security-relevant
  change to existing users' live agents).
Recommend (a) alone unless the user asks for (b). This edit lands in
`modules/agents/models.py::DEFAULT_AGENT_RESTRICTIONS` — flag it as a
backend change requiring go-ahead per Rule 10 before implementing.

## Step 10 — Badge on the `AppHeader` agent button

- `AppHeader.js` gains a small badge span, same visual treatment as
  wherever the chat-list unread badge is rendered — reuse that markup.
- Count source: `store.unreadCountByChatId[myAgent.owner_agent_chat_id]`.
  Needs `owner_agent_chat_id` threaded into `AppHeader`'s props (currently
  it only receives `wsStatus`/`currentUser`/avatar props) — pass `agentChat
  Id` down, or compute the badge count in the parent and pass a plain
  number prop (simpler, keeps `AppHeader` presentational — recommended).
- Unread-clearing: opening the drawer's chat view should call
  `store.clearUnreadCount(owner_agent_chat_id)` (mirrors what
  `selectChat`/`markActiveChatReadIfVisible` already do for normal chats) —
  but since the drawer does NOT set `store.activeChatId`, this needs an
  explicit call in `AgentDrawer.js`'s open handler rather than falling out
  of the existing "active chat" machinery for free.

## Step 11 — `.claude_docs/ai_agent.md` update

Per the AUTO-MAINTENANCE RULE: once implemented, document the new WS events
(`agent_thinking`, `agent_config_changed`), the owner-chat-direct-invoke
special case in the send path, the drawer UI structure, and the changed
default restrictions, in `.claude_docs/ai_agent.md`'s Status section.

---

## Open questions to resolve before/during implementation

1. Drawer slides from which side (RTL considerations)?
2. `max_messages_per_day` — text-style Save/Cancel, or checkbox-style
   immediate (Step 8)?
3. Opening greeting: real persisted system message at agent-creation
   (recommended) vs. local-only fake bubble (Step 5)?
4. Backfill existing agents' restrictions to the new default, or new agents
   only (Step 9)?
5. Does `MessageList.js` accept a `chatId` prop today, or does it hard-need
   `store.activeChatId` — determines whether Step 7 reuses it directly or
   needs a lightweight fallback renderer (needs a quick read of
   `MessageList.js` at implementation time, not answerable from this
   planning pass alone).

## Suggested implementation order

1. ADR 0048 (Step 0).
2. Backend: Steps 1, 2, 5a (thinking status push, config-changed event,
   owner-chat direct invoke) — batched as one "ask before backend changes"
   confirmation per CLAUDE.md Rule 10.
3. Frontend: Steps 3, 4 (wiring + store decision), then 6 (drawer shell), 7
   (chat view), 8 (settings view), 10 (badge).
4. Step 9 default-restrictions change — separate, explicit confirmation.
5. Step 11 docs update.

# ADR 0053: LLM Judge / Semantic Router gate before the main agent turn

Status: Accepted (implemented 2026-09-25)

## Context

Extends ADR 0045/0046/0047/0049/0051/0052. Today, once the Trigger Rule
Engine (`trigger_engine.py`) matches and enqueues onto `agent_invoke_stream`,
`invoke_worker.py::_run_turn` goes straight into building the full
conversation (`_build_initial_contents` — up to 20 messages of chat history)
and calling the main Gemini model (`gemini-flash-latest`, up to
`AGENT_TURN_MAX_TOOL_ROUNDTRIPS` round-trips) with the full tool registry
attached. For execution-mode chats (any chat except the owner's own
`owner_agent_chat_id`), the "caller" is an external chat participant, not the
agent's owner — anything they type reaches the main model with real tools
attached (`send_message`, `create_chat`, etc.).

Three real risks fall out of that:
1. **Prompt injection** — an external sender's message becomes untrusted
   input sitting right next to real tool-calling capability.
2. **Out-of-domain drift** — a `sales_agent` persona can be talked into
   discussing unrelated topics, diluting the deployment's purpose.
3. **Denial-of-wallet** — every matched trigger, however off-topic or hostile,
   currently costs at least one full Gemini call (often several, across
   round-trips) against the paid tier (ADR 0047 raised the budget to 30
   calls/min specifically because volume was expected to grow).

## Decision

Insert a lightweight **LLM Judge** gate between the Trigger Rule Engine and
the main agent turn, for execution-mode message-fired turns only. The judge
evaluates the single latest inbound message in total isolation — no chat
history, no tool schemas — and returns a strict boolean verdict. Only an
approved message reaches `_build_initial_contents` / the main model.

### 1. Scope — when the judge runs

Runs on **every trigger fired by an external chat participant** — i.e.
whenever `chat_id is not None and not is_config_mode(agent, chat_id)`. This
covers `on_specific_chats`, `on_unknown_sender`, and `on_any_message`
(ADR 0052) alike — these are exactly the cases with an untrusted external
sender, so all three go through the gate identically; there is no
trigger-type carve-out.

Does **not** run for:
- Config-mode turns (`chat_id == agent.owner_agent_chat_id`) — the owner
  talking to their own agent in the drawer is not an untrusted party; running
  a domain-enforcement judge on Supervisor/Builder/Help would actively break
  the builder interview flow.
- Schedule-fired turns (`chat_id is None`) — the "message" is the agent's own
  `on_schedule` instruction, not external input.

Insertion point: `invoke_worker.py::_run_turn`, immediately after the
existing `config_mode_turn` computation (`invoke_worker.py:248`) and before
`_build_initial_contents`/`_build_schedule_contents` is called — gated on
`message_id is not None and not config_mode_turn` (schedule-fired turns pass
`schedule_instruction`, not `message_id`, so this condition also naturally
excludes them without a second flag).

### 2. Total context isolation

The judge call is a **separate Gemini request** with its own `contents` —
never appended to or built from the main turn's `contents`. It receives:
- The latest message's `content` only (fetched via `message_id`, already
  available in `_run_turn`'s signature — no new DB read beyond the existing
  single-row fetch).
- A system prompt built fresh each call from: (a) fixed, hardcoded security
  rules (see §4), (b) a domain description derived from
  `agent.active_skill` + a length-capped prefix of `agent.system_prompt`
  (capped, e.g. 500 chars, so an oversized owner-authored prompt can't turn
  the judge itself into an injection surface), (c) the follow-up metadata
  flag (see §3).

It never receives: prior chat history, tool schemas, function-calling
capability, or the main turn's `contents`. This is a `generate_turn`-shaped
call but through a **new**, separate function (not reusing
`gemini_client.generate_turn`'s tool-calling path) — structured-output only
(`responseMimeType: application/json` + `responseSchema`), no
`tool_schemas` argument at all.

### 3. The pronoun problem — minimal metadata only

Short, contextless follow-ups ("how much?", "yes", "why?") must be approved
when they're plausibly part of an active conversation, without ever handing
the judge actual conversation content. Solution: a single boolean computed
in Python *before* the judge call —

```
is_follow_up_in_active_conversation: bool
```

True when the triggering chat has an agent reply
(`AGENT_REPLY_MESSAGE_TYPE`) either (a) within the last 3–5 minutes, or (b)
within the last 2 agent messages — whichever check is cheaper to run first
short-circuits (a cheap recent-timestamp check before falling back to a
2-row query if needed). This is metadata, not content — no message text
from that prior exchange is included. The judge's system prompt instructs
it: when this flag is true, default to approving short/generic/ambiguous
messages that would otherwise look out-of-domain in isolation.

### 4. Structured output — Pydantic schema

```python
class JudgeVerdict(BaseModel):
    is_approved: bool
    reason: str
```

Enforced via Gemini structured output (`responseSchema`), not function
calling — this is a single classification response, not a tool call.

### 5. Judge model & budget

Separate, smaller/cheaper model than the main turn's `gemini-flash-latest` —
`gemini-flash-lite-latest` (or current equivalent at implementation time,
verified against the live API the same way ADR 0045's model bump was).
Configured as its own constant (`AGENT_JUDGE_MODEL`) in
`gemini_client.py`/`config/agent_settings.py`, independent of
`GEMINI_CHAT_MODEL`.

Own rate-limit bucket, **not** shared with the main turn's
`agent_gemini_calls:{agent_id}` (30/min, ADR 0047 decision 1) — a judge call
that consumed from the same bucket would let hostile/off-topic traffic starve
the main model's budget, defeating the DoW-protection purpose. New bucket:
`agent_judge_calls:{agent_id}`, its own per-minute limit
(`AGENT_JUDGE_CALLS_PER_MINUTE`, config value TBD at implementation, sized
generously since each call is cheap/fast — this bucket exists for cost
ceiling, not scarcity).

### 6. Failure mode — fail-open, log loudly

If the judge call fails for any technical reason (timeout, network error,
Gemini API error, quota exceeded on the judge's own bucket, malformed
response): **do not block the turn** — proceed to the main agent turn as if
approved, and log an `ERROR`-level log line identifying the agent/chat/
message and the failure. Rationale (explicit user decision): a judge outage
must never make the whole agent silently stop responding to real customers;
the judge is a cost/safety optimization layer, not a hard security boundary
(the hard security boundary is still `is_config_mode` + `Agent.restrictions`
+ `execute_tool_call`'s server-side enforcement, all untouched by this ADR).
This is the opposite failure posture from those — deliberately, per explicit
user instruction.

### 7. Rejection UX — short, on-brand redirect, not silence, in the customer's language

When `is_approved=False`, the main Gemini turn is skipped entirely (the
entire point — no tool-calling call is made), but the agent still replies
into the chat with a short, persona-appropriate redirect — not silence and
not a raw "rejected" message. Content: acknowledge briefly that the topic is
outside what this agent handles, then steer back to the agent's actual
purpose (e.g. "That's a bit outside what I help with here — happy to help
with [domain] though, what can I do for you?"). **Updated 2026-09-25:** the
judge's own structured-output schema now includes a `redirect_message`
field, generated by the *same* judge call — the judge is instructed to write
this reply in the same language as the customer's message. This is still
**not** a second Gemini call — the redirect text piggybacks on the one
judge call that already ran, so a rejected turn still costs exactly one
cheap judge call, zero main-model calls. The old locally-templated,
English-only `local_redirect_text(agent)` is kept as a fallback only, used
when the judge call itself failed/rate-limited (fail-open — no
`redirect_message` was ever generated) or returned an empty string. Sent via
the same `process_outgoing`/`AGENT_REPLY_MESSAGE_TYPE` path every other
agent reply uses (`_tool_send_message`'s call shape, reused directly — not
through the tool dispatcher, since there's no Gemini-issued tool call here).

### 8. Audit logging

Every judge verdict (approved or rejected) is logged to a new table,
`AgentJudgeLog` (parallel to the existing `AgentToolCallLog` pattern):
`id, agent_id, chat_id, message_id, is_approved, reason, is_follow_up_flag,
created_at`. Unpartitioned to start (same posture as `AgentToolCallLog` and
`AgentKnowledgeDocument` — ADR 0005's partitioning is reserved for tables
that actually grow to that scale). This is what makes false-positive/
false-negative tuning possible after launch.

## Consequences

- Every external-sender-triggered turn now costs one extra (cheap, small-
  model) Gemini call before the real turn — a small latency/cost add on the
  approved path, in exchange for skipping the much more expensive main-model
  call entirely on the rejected path.
- New Redis bucket, new DB table, new config constants — no schema change to
  `Agent` itself.
- No change to the hard tool-mode gate (`is_config_mode`,
  `execute_tool_call`'s allowlist) — the judge is a pre-filter in front of
  the existing security boundary, not a replacement for it.
- Known gap deferred to a future ADR if needed: no owner-facing UI to view
  `AgentJudgeLog` or tune the domain description independently of
  `active_skill`/`system_prompt` — v1 reuses what's already stored.

## Implementation log

Built 2026-09-25, all 8 decisions as designed above:

- New `modules/agents/judge.py`: `evaluate_message(session, agent, chat_id,
  message_id, message_content)` returns a `JudgeVerdict(is_approved, reason,
  is_follow_up)`. `_is_follow_up_in_active_conversation` runs the cheap
  2-row query first (`Message.type == AGENT_REPLY_MESSAGE_TYPE`, `chat_id`-
  scoped, `ORDER BY id DESC LIMIT AGENT_JUDGE_FOLLOW_UP_RECENT_MESSAGES`) and
  only falls back to comparing the newest row's `created_at` against
  `AGENT_JUDGE_FOLLOW_UP_WINDOW_SECONDS` when that doesn't already prove
  "active" by row count alone. `_domain_description` builds the domain text
  from `personas.get_persona_system_prompt(agent.active_skill)` +
  `agent.system_prompt[:AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS]`.
  `local_redirect_text(agent)` is a small dict keyed on `active_skill` (no
  second Gemini call). A message with no text content (e.g. media-only) is
  approved without ever calling the judge - nothing to evaluate.
- New `gemini_client.py::generate_structured(model, system_prompt, user_text,
  response_schema)` - a separate function from `generate_turn`, not a mode
  flag on it: single-message `contents`, no `tools` key at all,
  `generationConfig.responseMimeType=application/json` +
  `responseSchema`. Always uses the shared `settings.GEMINI_API_KEY` (the
  judge never runs under BYOK). Raises the same `GeminiChatError` as
  `generate_turn` on any HTTP/shape failure.
- `config/agent_settings.py`: `AGENT_JUDGE_MODEL` (`gemini-flash-lite-
  latest`), `AGENT_JUDGE_CALLS_PER_MINUTE`/`_WINDOW_SECONDS` (60/min, own
  `agent_judge_calls:{agent_id}` bucket via the existing
  `infra.ratelimit.service.check_and_increment` - never
  `agent_gemini_calls`), `AGENT_JUDGE_SYSTEM_PROMPT_PREVIEW_CHARS` (500),
  `AGENT_JUDGE_FOLLOW_UP_WINDOW_SECONDS` (300) and
  `_RECENT_MESSAGES` (2). All exported through `config.settings` (ADR 0029)
  automatically via `__all__`.
- New `AgentJudgeLog` model (`modules/agents/models.py`): `id, agent_id,
  chat_id, message_id, is_approved, reason, is_follow_up_flag, created_at`.
  Unpartitioned, same posture as `AgentToolCallLog`. Brand-new table, so
  `Base.metadata.create_all` in both `scripts/init_db.py` and
  `tests/conftest.py`'s ephemeral-DB setup picks it up automatically - no
  `ALTER TABLE` safety-net line needed (that pattern is only for existing
  tables gaining a column).
- Insertion point landed exactly as planned:
  `invoke_worker.py::_run_turn`, immediately after `config_mode_turn` is
  computed and before `_build_initial_contents`/`_build_schedule_contents`,
  gated on `message_id is not None and not config_mode_turn`. Fetches the
  triggering message via the existing `modules.messaging.crud.
  get_message_by_id(session, chat_id, message_id)` (already the standard
  single-message lookup, no new query helper needed) and passes
  `message.content` to the judge. On `is_approved=False`, posts
  `local_redirect_text(agent)` via `message_service.process_outgoing`
  directly (same call shape `_tool_send_message`/`_post_config_reply` use,
  not through the tool dispatcher - there's no Gemini-issued tool call here)
  and returns before `_build_initial_contents` ever runs, so the rejected
  path makes zero main-model calls.
- Fail-open implemented at two points inside `evaluate_message`: a
  `GeminiChatError`/`KeyError`/`TypeError` from `generate_structured` logs
  `ERROR` and returns an approved verdict; separately, the judge's own rate
  limit being exceeded also fails open (logged `WARNING`, not `ERROR` - a
  self-inflicted cap, not a Gemini-side outage, but the same "never block
  the turn" posture either way) rather than rejecting or blocking. Every
  path (real approve, real reject, no-content approve, rate-limit fail-open,
  Gemini-error fail-open) writes exactly one `AgentJudgeLog` row.
- No schema change to `Agent` itself, confirmed per the ADR's own
  Consequences section. No new tests (same gap as every prior agents-module
  step, per `.claude_docs/ai_agent.md`'s standing note) - import-smoke-
  tested + full suite run (430/430 passed) against the ephemeral test DB,
  confirming the new table's DDL and every existing agents-module code path
  still work end to end.
- Known gap carried forward unchanged: no owner-facing UI for
  `AgentJudgeLog` or independent domain-description tuning - v1 reuses
  `active_skill`/`system_prompt` as designed.
- **2026-09-25 follow-up:** `redirect_message` (see §7) added to
  `_JUDGE_RESPONSE_SCHEMA` (now `required: [is_approved, reason,
  redirect_message]`) and to `_JUDGE_SECURITY_RULES`, instructing the judge
  to write a customer-facing, same-language redirect whenever
  `is_approved=false` (empty string when approved). `JudgeVerdict` gained a
  fourth field `redirect_message: str = ""`. `invoke_worker.py`'s rejection
  branch now sends `verdict.redirect_message or local_redirect_text(agent)`
  - the local template is now a fail-open-only fallback, not the normal
    path. No new Gemini call, no new rate-limit bucket, no schema/DB change.

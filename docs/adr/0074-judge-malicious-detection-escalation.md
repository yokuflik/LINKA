# 0074 - LLM Judge malicious-intent detection and auto-escalation

Status: Accepted

## Context

ADR 0053 added an LLM Judge pre-filter gate (`modules/agents/judge.py::
evaluate_message`) in front of every execution-mode, message-fired agent
turn. Today it returns a single boolean, `is_approved`: off-topic drift,
mild abuse, and real prompt-injection/security-probing attempts are all
folded into the same `is_approved=False` outcome, which just gets a polite
redirect reply and nothing else - the owner never finds out.

The owner asked for a stronger response specifically to attempts to extract
their business's trade secrets/internal information, or to get the agent to
execute arbitrary code/commands, or otherwise mount a clear, targeted attack
on the deployment (as opposed to a customer just asking an off-topic
question, which should keep behaving exactly as it does today). For that
narrow, more serious class, the owner wants the same outcome
`pause_and_escalate` already produces on its own initiative: the chat frozen
and a notification sent to them, so a human looks at it - not just a redirect
that lets the conversation quietly continue.

This must stay cheap: the judge already runs on every single inbound
message in scope, so the new classification has to ride the exact same
Gemini call - no second request, no new rate-limit bucket.

## Decision

Extend the existing judge call's structured-output schema with one more
boolean, `is_malicious`, and use it to auto-trigger the same
freeze-and-notify behavior `pause_and_escalate` already implements, without
adding a second Gemini call or changing the customer-facing rejection UX.

### 1. Schema change - `is_malicious`, not a new severity scale

`_JUDGE_RESPONSE_SCHEMA` (`modules/agents/judge.py`) gains a fourth boolean
field, `is_malicious`, alongside the existing `is_approved` / `reason` /
`redirect_message`. Not a free-text category or a numeric severity score -
a flat boolean is enough to drive one binary behavior switch (escalate or
don't), and keeps the judge's output cheap and easy to reason about.

`is_malicious=true` implies `is_approved=false` (a malicious message is by
definition not approved), but the reverse doesn't hold - most rejections
stay ordinary off-topic drift with `is_malicious=false`, exactly like today.

`_JUDGE_SECURITY_RULES` gains one explicit third rejection category (on top
of the existing "prompt injection" and "off-domain" ones), instructing the
judge to set `is_malicious=true` when the message:
- Asks the agent to reveal confidential/internal information about the
  owner's business - pricing internals, supplier/vendor details, private
  configuration, credentials, or anything explicitly framed as secret/
  internal rather than public product information.
- Asks the agent to execute code, shell commands, or system-level
  instructions of any kind.
- Makes a clear, deliberate attempt to override/extract the agent's own
  system prompt or instructions (sharpens, doesn't replace, the existing
  injection wording in `_JUDGE_SECURITY_RULES` - now that a targeted attempt
  in this category has a stronger consequence than a plain redirect, it
  needs to be judged with the same "when genuinely unsure, don't over-flag"
  permissiveness as the rest of the gate, so an ordinary customer typing
  something clumsy that only superficially resembles an attack isn't
  escalated).

Ordinary off-topic questions, small talk, or a customer just being rude/
impatient stay `is_malicious=false` even though `is_approved=false` -
nothing changes for that existing path.

`JudgeVerdict` (`modules/agents/judge.py`) gains a matching `is_malicious:
bool = False` field. Every fail-open path (judge call error, judge's own
rate limit exceeded, no text content to evaluate) forces `is_malicious=
False` unconditionally - a technical failure must never itself trigger an
escalation; ADR 0053 section 6's fail-open posture is unchanged.

### 2. Customer-facing behavior - unchanged

The customer gets exactly the same reply as any other rejected message
today: the judge's own `redirect_message` (or `local_redirect_text(agent)`
on the fail-open path), via the same `process_outgoing` call
`invoke_worker.py` already uses. Nothing in the customer-visible text or
delivery path reveals that the message was flagged as malicious rather than
merely off-topic - the owner explicitly asked for silent redirect-only
behavior on that side, not a different-looking bounce message that would tip
off an attacker that they'd been detected.

### 3. Owner-facing behavior - reuse `pause_and_escalate`'s mechanics

When `verdict.is_malicious` is true, `invoke_worker.py`'s existing rejection
branch (right after sending the customer's redirect) additionally runs the
same freeze-and-notify sequence `_tool_pause_and_escalate`
(`modules/agents/tools/execution.py`) already performs for a model-initiated
escalation:
- `pause_agent_chat(session, agent, chat_id)` + `sync_agent_cache(updated)` -
  same chat-scoped freeze as ADR 0047/0054 (lazy-expiry `paused_chat_ids`
  entry, auto-expires after `AGENT_ESCALATION_PAUSE_HOURS` exactly like a
  normal escalation).
- `send_push` to `agent.owner_user_id` - same shape, `data={"chat_id":
  str(chat_id)}`.
- A `send_system_message` into `agent.owner_agent_chat_id`, formatted the
  same visual way (`*bold*` counterpart line via the existing
  `_describe_escalation_counterpart` helper) - but with **fixed, translated-
  by-the-judge wording**, not a model-authored free-text `reason` the way a
  real `pause_and_escalate` tool call gets one. There is no conversational
  turn here to ask the main model to phrase a sentence - the judge already
  returned a short `reason` string (existing field, already populated on
  every verdict) describing why it rejected the message; that `reason` is
  used verbatim as the notice body, prefixed with a fixed, language-neutral
  warning glyph/marker (parallel to the existing 🤝 prefix
  `_tool_pause_and_escalate` uses) so the owner can visually tell an
  auto-detected security escalation apart from a normal handoff at a glance.

To avoid duplicating this freeze-and-notify sequence in two places, the
common body of `_tool_pause_and_escalate` (pause + push + system message,
everything after resolving `reason`/`counterpart`) is extracted into a
shared helper in `modules/agents/tools/execution.py` (or `common.py`,
decided at implementation time) that both the real tool handler and the new
judge-triggered path call with their own `reason` string. No new pause/
notify code path is invented - this ADR is strictly "call the existing
mechanism from a second place," not a new escalation primitive.

### 4. Audit logging - extend `AgentJudgeLog`, no new table

`AgentJudgeLog` (`modules/agents/models.py`, ADR 0053) gains one new
non-nullable boolean column, `is_malicious` (default `False`) - not a
separate table. This is a genuinely new column on an existing table, so
(unlike ADR 0053's brand-new-table case) it needs the usual `ALTER TABLE`
safety-net line in `scripts/init_db.py` for any already-provisioned dev/prod
database, alongside `Base.metadata.create_all` already covering a fresh
database. `judge.py::_log_verdict` is updated to pass `verdict.is_malicious`
through on every call.

### 5. Cost

Zero additional Gemini calls and no new Redis rate-limit bucket - this rides
the exact same `agent_judge_calls:{agent_id}` call and
`gemini-flash-lite-latest` model ADR 0053 already pays for per message; only
the JSON response schema and the security-rules prompt text grow by a
handful of lines.

## Consequences

- A message flagged malicious now produces three side effects instead of
  one: the same customer redirect as before, plus a chat freeze plus an
  owner notification - functionally identical to the agent calling
  `pause_and_escalate` on itself, just triggered by the judge instead of the
  main model, and firing *before* the main model ever runs (zero main-model
  calls on this path, same as any other judge rejection).
- `AgentJudgeLog` becomes the audit trail for both plain rejections and
  malicious-flagged ones (`is_malicious` column distinguishes them) - no
  separate table or owner-facing UI for security events specifically, same
  "reuse what's already stored" posture ADR 0053 took for its own audit log.
- False positives are possible (an oddly-phrased but innocent message
  mistaken for an attack) - since the customer-facing behavior is
  unchanged (a plain redirect, not a visibly different bounce), the only
  cost of a false positive is an unnecessary chat freeze + owner
  notification, recoverable the same way any other escalation is (owner
  resumes via the existing resume flow, ADR 0054/0055) - not a customer-
  visible incident.
- No schema change to `Agent` itself; the only schema change is the one new
  `AgentJudgeLog` column.
- Deferred: no separate severity scale beyond the single `is_malicious`
  boolean, no distinct sub-categories (e.g. "trade-secret request" vs.
  "code execution attempt" vs. "injection attempt") logged separately - all
  three collapse into one flag and one `reason` string for now, matching the
  "don't build more than asked" scope the owner confirmed when this was
  planned.

## Implementation log

Built 2026-09-27, exactly as designed in §1-§4:

- `judge.py`: `_JUDGE_RESPONSE_SCHEMA` gained `is_malicious` (required).
  `_JUDGE_SECURITY_RULES` gained the third rejection category (trade-secret/
  internal-info requests, code-execution asks, targeted prompt-injection),
  explicitly instructing the judge to stay conservative and to never set
  `is_malicious=true` when `is_approved=true`. `JudgeVerdict` gained
  `is_malicious: bool = False`, defaulted false at every constructor call
  site except the real successful-response branch - the no-content path,
  the judge-rate-limit fail-open path, and the `GeminiChatError`/`KeyError`/
  `TypeError` fail-open path all leave it at the default, so a technical
  failure can never itself trigger an escalation. `_log_verdict` now passes
  `verdict.is_malicious` through.
- `modules/agents/models.py`: `AgentJudgeLog.is_malicious` (`Boolean,
  nullable=False, server_default="false"`). `scripts/init_db.py` got the
  matching `ALTER TABLE agent_judge_log ADD COLUMN IF NOT EXISTS
  is_malicious BOOLEAN NOT NULL DEFAULT false` safety-net line (existing
  table, unlike ADR 0053's brand-new one).
- Shared helper landed as `modules/agents/tools/common.py::escalate_chat`
  (not `execution.py` - `common.py` is already the shared-helper module both
  `execution.py` and the judge need to import from, and keeps
  `_describe_escalation_counterpart` and its caller together): takes
  `session, agent, chat_id, reason, notice_prefix="🤝"` and runs the full
  pause + push + system-message sequence, independently try/excepted per
  side effect exactly as `_tool_pause_and_escalate` did inline before.
  `_tool_pause_and_escalate` (`tools/execution.py`) now just resolves
  `reason` from `arguments` and calls `escalate_chat(session, agent,
  chat_id, reason)` - default `notice_prefix` keeps its notice
  byte-for-byte identical to before. `execution.py`'s now-unused
  `pause_agent_chat`/`send_push`/`_describe_escalation_counterpart` imports
  were removed (all three fully absorbed into `escalate_chat`); `sync_agent_
  cache` stays imported (still used by `_tool_update_own_triggers`).
- `invoke_worker.py`'s existing judge-rejection branch, right after posting
  the customer's redirect and committing: `if verdict.is_malicious:` logs a
  `WARNING` (not `INFO`, unlike the plain-rejection log line above it) and
  calls `escalate_chat(session, agent, chat_id, verdict.reason,
  notice_prefix="⚠️")` - the distinct prefix is the only visible difference
  from a model-initiated `pause_and_escalate` notice, so the owner can tell
  the two apart at a glance as designed. Followed by its own `session.
  commit()`.
- No import cycle introduced - verified by importing `judge.py`,
  `tools/common.py`, `tools/execution.py`, and `invoke_worker.py` directly
  (`common.py` importing `modules.agents.cache`/`crud` was already safe,
  neither of those imports `tools/`).
- No new tests (same gap as every prior agents-module step, per
  `.claude_docs/ai_agent.md`'s standing note) - existing judge/escalation
  tests (36 cases) re-run unchanged and green; full suite run pending at
  time of writing.

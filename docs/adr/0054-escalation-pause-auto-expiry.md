# 0054. Auto-expiry for `pause_and_escalate` chat pauses

Status: Accepted

## Context

`pause_and_escalate` (ADR 0047 decision 5) freezes the agent on one chat
indefinitely - the only way back was the human-only
`POST /agents/me/resume-chat/{chat_id}` endpoint. In practice an owner may
miss the push notification / system message entirely, leaving a customer
permanently unanswered with no automatic recovery.

## Decision

**Pauses now auto-expire.** `pause_agent_chat` stamps each pause with
`paused_at` and `expires_at` (`paused_at + AGENT_ESCALATION_PAUSE_HOURS`,
default 24, env-overridable like every other agent limit). The trigger
engine treats an expired entry as not-paused (lazy expiry - no cron/sweep;
the entry is physically dropped from `paused_chat_ids` the next time that
chat's pause state is read in `_evaluate_triggers`, same "check and clean on
read" style already used nowhere else in this module but consistent with
the fail-open philosophy of `_within_time_window`).

**`paused_chat_ids` changes shape**: a flat list of chat_id strings becomes
a list of `{"chat_id": "<id>", "paused_at": "<iso>", "expires_at": "<iso>"}`
objects - same precedent as ADR 0051's `_auto_added_at` tagging of
`on_specific_chats` entries (timestamp stored explicitly rather than relying
on ordering). `resume_agent_chat` (the human endpoint) is unaffected in
signature, just filters on the new shape.

**Early lift from the owner-agent chat**: a message arriving in
`owner_agent_chat_id` - the exact predicate already used by
`is_config_mode`/the trigger engine's owner-direct-wake branch (ADR 0048) -
resumes the **most recently escalated** paused chat (max `paused_at`) for
that agent, not all of them. Rationale: with multiple chats paused
concurrently, a reply from the owner is presumed to be about whichever
escalation is freshest in the conversation; resuming every paused chat on
any incidental owner message would be too broad and could un-pause a chat
the owner hasn't actually looked at yet. This is a plain heuristic (no NLU,
no reference resolution) - it does not try to parse *which* chat the owner
means.

## Consequences

- New setting `AGENT_ESCALATION_PAUSE_HOURS` (default `24`) in
  `config/agent_settings.py`, added to the settings snapshot list like every
  other tunable.
- `Agent.paused_chat_ids` JSONB shape change (additive/no migration, same as
  ADR 0051 - existing rows with the old flat-string shape are treated as
  already-expired on first read and dropped, so no backfill needed).
- `pause_agent_chat(session, agent, chat_id)` now also sets `paused_at`/
  `expires_at`; idempotent re-escalation of an already-paused, non-expired
  chat refreshes nothing (matches the existing idempotent-no-op behavior).
- New helper `resume_agent_chat_by_owner_reply(session, agent)` (or
  equivalent) picks the most-recent-`paused_at` entry and clears just that
  one; wired into `_evaluate_triggers`'s existing owner-chat branch
  (`trigger_engine.py:199-214`), after the existing `is_enabled` check,
  before the normal enqueue - fire-and-forget like the rest of that
  function, never blocking the owner's own turn from being enqueued.
- `sync_agent_cache` still the only cache-invalidation path; no new Redis
  keys.
- No frontend change required - the mechanism is invisible except that a
  chat starts responding again either after 24h or after the owner replies
  in their agent chat.

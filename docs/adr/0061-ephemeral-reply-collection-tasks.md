# 0061 - Ephemeral reply-collection tasks (`spawn_ephemeral_task`)

Status: Accepted

## Context

`schedule_one_off_task` (ADR 0046 decision 3) fires a single scoped turn at a
given time - good for "say X to person Y at time T," but with no way to wait
for a reply and report it back. The owner's actual need (per
`EPHEMERAL_TASK_PLAN.md` item 5) is multi-turn coordination: "ask X and Y if
they're coming Saturday, then tell me what they said" - one call, an
arbitrary number of expected repliers, and a single summary back to the
owner once everyone's answered or a timeout passes.

This needed a genuinely new lifecycle pattern - state accumulated across
multiple, independently-timed inbound messages, with an active-sweep
fallback for the one case no inbound message can ever trigger (nobody
replies at all) - hence its own ADR per CLAUDE.md rule 7, rather than folding
it into ADR 0046/0047 as a footnote.

## Decision

New config-mode tool `spawn_ephemeral_task(instruction, chat_ids,
timeout_minutes?)`. The owner must have already resolved each named person to
a `chat_id` via `resolve_user` - never guessed.

**Data shape** - new top-level `Agent.triggers["on_ephemeral_task"]` map,
additive to the existing JSONB column (no migration, same pattern as every
prior trigger addition, e.g. ADR 0052's `on_any_message`), kept separate from
`on_specific_chats` so that key's existing manual/ADR-0051-auto-registered
semantics stay untouched:

```python
{
    "<task_id>": {
        "instruction": "...",
        "owner_chat_id": "<agent.owner_agent_chat_id>",
        "expected_chat_ids": ["111", "222"],
        "collected": {"111": "yes I'll be there"},
        "created_at": "...", "expires_at": "...",
    }
}
```

**Spawning** (`ephemeral_tasks.py::spawn_ephemeral_task`): writes the
`on_ephemeral_task` entry, then schedules one immediate (`execute_at="now"`)
`on_schedule` entry per expected `chat_id`, each carrying
`scoped_system_prompt = build_relay_prompt(instruction)` - a fixed template
narrowly instructing that turn to send the message and do nothing else. The
actual `send_message` call therefore happens inside a normal
execution-mode-scoped turn against that chat, not inside the spawning call
itself (which runs in config mode against the owner's own chat and has no
`send_message` tool of its own before ADR 0062).

**Matching an incoming reply** (`trigger_engine.py`, modeled on
`_active_paused_chat_ids`'s lazy-expiry style, ADR 0054): a message whose
`chat_id` is in some non-expired `on_ephemeral_task[task_id].expected_chat_ids`
wakes the agent with that same relay-scoped prompt; the turn appends the
incoming text to `collected[chat_id]` (`ephemeral_tasks.py::record_reply`).

**Completion**: after any reply lands, if
`set(expected_chat_ids) <= set(collected.keys())`, or independently on
timeout, `fire_summary_and_complete` schedules one more immediate scoped turn
- `build_summary_prompt(instruction, collected)`, targeting
`owner_chat_id`, tools = the owner's normal execution set - then deletes the
task entry (`complete_task`). Both completion paths (a reply that finishes
the set, and the timeout sweep below) share this one function.

**Timeout sweep for tasks nobody ever replies to**: the existing
`agent_worker` schedule-poll loop (already ticking every
`AGENT_SCHEDULE_POLL_INTERVAL_SECONDS`) gets one extra cheap step
(`invoke_worker.py::_sweep_expired_ephemeral_tasks`, using
`ephemeral_tasks.py::sweep_expired_task_ids`): for each agent with a
non-empty `on_ephemeral_task`, lazily check `expires_at` and fire the
summarize-with-whatever-we-have turn for any lapsed entry that never got a
completing reply. No new worker/cron process.

## Guardrails

- `AGENT_MAX_EPHEMERAL_TASKS` (default 5) - concurrent `on_ephemeral_task`
  entries per agent (`EphemeralTaskQuotaExceededError`).
- `AGENT_EPHEMERAL_TASK_MAX_MINUTES` (default 4320 = 3 days) - hard ceiling on
  `timeout_minutes`, regardless of what's requested.
- `AGENT_EPHEMERAL_TASK_DEFAULT_MINUTES` (default 1440 = 24h) - used when
  `timeout_minutes` is omitted.
- Each scheduled relay/summary turn rides the *existing* `on_schedule` entry
  mechanism and its `AGENT_MAX_SCHEDULE_ENTRIES` (10) cap - spawning enough
  ephemeral tasks to blow that shared cap raises
  `EphemeralTaskQuotaExceededError`, not a silent partial spawn.
- Every relay send still goes through the unmodified `_tool_send_message`
  checks (`can_message_private`/`blocked_read_chat_ids`/daily
  quota/owner send-rate budget) - no new permission surface; if the owner
  couldn't message someone directly, `spawn_ephemeral_task` can't either.
- `get_capacity_status` gains an `ephemeral_tasks: {used, max}` row, same
  pattern as its existing `schedule_entries`/`auto_registered_chats` rows.

## What this deliberately does not change

`on_specific_chats`, `on_schedule`'s existing shape, `on_time_window`,
`on_unknown_sender`, `on_any_message` - untouched. The Judge gate (ADR 0053)
only gates inbound execution-mode turns from external senders; ephemeral-task
relay/summary turns are config-mode-spawned and outbound, so unaffected.
`pause_and_escalate`/`paused_chat_ids` - untouched; if a relay counterpart
escalates, that's the existing mechanism firing normally.

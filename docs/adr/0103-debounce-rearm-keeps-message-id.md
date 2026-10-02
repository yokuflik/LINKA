# ADR 0103 — Debounce re-arm must carry the popped message_id

Status: Accepted
Date: 2026-10-02

## Context
`_debounce_poll_loop` pops the stashed `message_id` (`pop_latest_message_id`)
to enqueue a fired turn. If `process_entry` then finds the turn mutex held, it
re-armed the debounce *without* a message_id, assuming the stash still held
one. It didn't, so the next fire found no id and silently skipped — the owner's
message was never answered (observed: "turn already running … superseded
mid-call" followed by silence).

## Decision
`process_entry` re-arms with the entry's own `message_id` and
`arm_debounce(..., keep_newer=True)` (Redis `SET NX`) so a newer message
stashed in the meantime is never overwritten. No schema change.

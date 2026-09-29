# 0080. Agent marks the triggering message read at turn start

Status: Accepted

## Context

The agent already publishes a peer-visible `typing` indicator while it composes
a reply (`invoke_worker.py`'s `_publish_peer_typing_loop`, ADR 0075). It never
sent a read receipt for the customer's message that woke it up, so a customer
would see "typing…" appear without ever seeing the blue tick that a human
reading the chat would produce first. This looked inconsistent with how a real
person uses the app (read, then type).

## Decision

At the start of every execution-mode, message-fired agent turn (the same gate
already used for the LLM Judge call: `message_id is not None and not
config_mode_turn`), the agent enqueues a `read` receipt for the triggering
message via the existing `modules.messaging.receipts.mark_as_read` path
(`XADD receipt_log_stream`, ADR 0037) — the same mechanism a real WS
`mark_read` frame uses.

No new privacy logic is introduced. `mark_as_read` is called unconditionally;
whether the resulting blue tick is actually shown to the customer is already
decided downstream, per-reader, by the existing ADR 0003 machinery:

- `modules/receipts/apply.py` checks `reader_hides_read_receipts(chat_id,
  reader_id=agent.owner_user_id)` before publishing the live `read_receipt`
  event — so if the agent's owner has `privacy.read_receipts=false`, the
  customer never sees a read tick from the agent either, exactly as if the
  owner had read it themselves with receipts off.
- `reader_hides_read_receipts` (and the whole read-receipt visibility model)
  only ever applies to 1:1 chats — group chats already always show read
  receipts to everyone (ADR 0003), so no group carve-out is needed in the
  agent code; the trigger scopes that matter here (`on_specific_chats` /
  `on_unknown_sender` / `on_any_message`) are 1:1-only by construction anyway
  (`on_any_message`/`on_unknown_sender` explicitly exclude groups per ADR
  0051/0052).
- Config-mode turns (the owner's own agent-drawer chat) and schedule-fired
  turns (`chat_id is None`) are excluded — same condition as the Judge gate —
  since there is no external customer to signal to.

The read watermark (`Participant.last_read_message_id`) and detailed receipt
log advance exactly as they would for a normal `mark_read` frame; this also
means the agent's own unread badge for that chat clears immediately, matching
what a human would experience after opening the chat to read and reply.

## Consequences

- One extra `XADD` per execution-mode turn (negligible cost, same stream
  already used for every WS `mark_read`).
- Customers see a read tick appear right when the agent starts working on
  their message, before the `typing` indicator — consistent with human
  behavior, and silently suppressed end-to-end if the owner has read
  receipts turned off.
- No schema change, no new config, no new tool.

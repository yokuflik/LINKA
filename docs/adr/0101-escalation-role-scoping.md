# ADR 0101 - Scope `pause_and_escalate` to the agent being approached

Status: Accepted

## Context
The shared `CHAT_STYLE_RULES` escalation clause ("customer ready to buy -> MUST call
`pause_and_escalate`") applies to every execution persona. Observed bug: owner 3's
`one_off_executor` agent was *the buyer* in a task ("buy a Mazda from Hila"); when Hila's
sales agent said "connecting you with a rep", the buyer agent escalated its own chat to
its own owner. Meanwhile the seller (`sales_agent`) told the customer it was handing off
but never called the tool, so its owner was never notified.

## Decision
Prompt-only fix, no schema/code-path change:
1. Shared escalation clause gains a role guard: it applies only when the agent is the one
   being approached on the owner's behalf; an agent that itself initiated contact (task
   from the owner) must never escalate because the other side offers a human.
2. `sales_agent` base prompt: saying you are connecting someone with a human requires
   calling `pause_and_escalate` in that same turn.

## Consequences
Routing is unchanged (`chat_id` still comes from turn context). Relies on model
compliance; a code-level guard can follow if it recurs.

# 0071 - History Transcript: Mark Already-Handled Owner/Customer Lines

Status: Accepted
Date: 2026-09-26

## Context

Every agent turn (execution-mode and config-mode alike) is seeded by
`_build_initial_contents` → `_format_history_transcript`
(`modules/agents/invoke_worker.py`), which flattens the last 20 messages of a
chat into a single plain-text block ("Agent: ..." / "Customer: ..." lines,
oldest first) and hands it to Gemini as one `user` turn. The structured
`functionCall`/`functionResponse` exchange for a given turn is entirely
in-memory and discarded once the turn ends - only the agent's resulting text
reply, if any, survives into the next turn's transcript, as an ordinary
"Agent: ..." line.

Bug report: in the config-mode (Builder/Supervisor) chat, an owner instruction
like "summarize the chat with Yossi" gets acted on once via a tool call. But
that owner line stays in the flattened transcript on every subsequent turn
(until it scrolls past the `AGENT_HISTORY_TRANSCRIPT_MAX_CHARS` window), with
nothing distinguishing "already actioned" from "still pending." A later,
unrelated turn can see that old instruction sitting unmarked in history and
re-invoke the same tool. Neither the ADR 0063 debounce/turn-mutex nor the ADR
0065 `no_reply_needed` tool address this - both operate within a single
coalesced turn, not across turns separated by unrelated activity.

## Decision

`_format_history_transcript` gains a structural, non-LLM-dependent signal:
any Customer/Owner line that is followed *later in the same transcript* by an
Agent line is provably already-handled (the agent would not have replied
without processing it), and gets suffixed with a fixed marker,
`" [already handled]"`. Only a trailing run of Customer/Owner lines with no
Agent line after them - i.e. the truly unanswered tail - stays unmarked.

This is computed from message order alone (a single reverse pass comparing
each line's position to the last-seen Agent line), not from tool-call
metadata, so it needs no new schema, no persisted state, and works
identically for both the generic "Agent"/"Customer" execution-mode label pair
and the config-mode chat (same function, same message stream).

A one-line prompt rule is added next to the existing `STYLE_RULES ` in
`modules/agents/prompts.py` / the Builder prompt: lines marked
`[already handled]` describe past turns and must never be re-executed or
re-answered - only the unmarked tail represents new, unaddressed input.

## Consequences

- No schema/DB change; purely a transcript-rendering + prompt change.
- The marker is a heuristic, not a guarantee: if the agent's reply for a given
  instruction was itself suppressed (e.g. via `no_reply_needed`, or the turn
  hit the round-trip cap before replying), the instruction has no following
  Agent line and is *not* marked - which is the safe failure mode (it reads as
  still-pending, matching reality) rather than the unsafe one.
- Does not address a genuinely repeated owner instruction ("summarize Yossi
  again") - that produces a *new* Customer/Owner line with no Agent line after
  it yet, which is correctly left unmarked and actionable.
- `AGENT_HISTORY_TRANSCRIPT_MAX_CHARS` truncation is unaffected; marking
  happens before truncation, on the full line set.

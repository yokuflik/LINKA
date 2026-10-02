# ADR 0102 — `continue_message` tool: long answers split across up to 3 messages

Status: Accepted
Date: 2026-10-02

## Context

A single Gemini response is capped at `maxOutputTokens` (8192, thinking tokens
included). A very large request can hit `MAX_TOKENS`; in execution mode
`finish_max_tokens` then discards the partial text, so the counterpart gets
nothing. This is independent of the token *usage* budget (ADR 0059).

## Decision

New execution-mode tool `continue_message(chat_id, content)`:

- Handler delegates to `_tool_send_message` — identical restrictions, quota and
  owner-send-budget checks. Registered in `TOOL_SCHEMAS` /
  `EXECUTION_TOOL_HANDLERS`, hence also in `one_off_action` (ADR 0093 union).
  Not added to the zero-action Help/clarify states or the Builder.
- Semantics: "send this part, I will write the next one" — used for every part
  but the last (which is a normal `send_message`). The loop already continues
  after a tool call, so each part gets a fresh output budget.
- Hard cap `AGENT_MAX_CONTINUATION_MESSAGES` (default 3) per turn, enforced in
  `invoke_turn_loop.dispatch_tool_call` via `TurnCtx.continuations_used`
  (handlers get no turn state). Over the cap the call is refused unexecuted
  with an error telling the model to finish with `send_message`. Failed calls
  don't consume the budget; successes return `continuations_remaining`.
- Added to `_MESSAGE_SENDING_TOOL_NAMES`, so the supersede check (ADR 00732)
  and typing-indicator cancel apply to it like `send_message`.

### Config-mode addendum (same day)

Config-mode replies are plain text (no send tool), so a `MAX_TOKENS` cut there
posted the partial and ended the turn (found via a 92-message summary request).
`finish_max_tokens` now returns "continue": after posting the partial it appends
it + a `CONTINUATION_PROMPT` ("continue exactly where it stopped") to the turn
contents and the loop runs another Gemini call, sharing the same
`continuations_used` counter/cap (3). Execution mode is unchanged (partial
still discarded; the model must use `continue_message` proactively).

## Consequences

No schema change. In execution mode a response that exceeds the output cap
before the tool is called is still truncated. Config-mode continuation costs
one extra Gemini call per part and can duplicate a little text at the seam.

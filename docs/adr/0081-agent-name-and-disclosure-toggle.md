# 0081 - Agent display name + owner-controlled AI-disclosure toggle

Status: Accepted

## Context

Today the agent has no configured identity of its own: it sends as the
owner's own `user_id` (recipients see the owner's identity, never the
agent's) and, if asked "what are you," personas mostly fall back to the
hardcoded generic label "Linka Agent" (`ONE_OFF_EXECUTOR`'s existing
self-description clause, `modules/agents/personas.py`). There is no way for
an owner to give the agent its own name, and no way to control whether it's
allowed to admit being an AI if a chat counterpart asks directly - it just
implicitly impersonates the owner.

The owner should be asked, during Builder setup, whether they want full
impersonation (today's default, unchanged) or a disclosing agent that can
give itself a name and admit to being AI when asked.

## Decision

**1. Two new `Agent` columns**, new-rows-only per the repo's no-migration
convention (`init_db.py` only):
- `agent_name` (`String(64)`, nullable, default `NULL`) - optional, owner-set
  display name. **Prompt-only scope**: it is never surfaced anywhere in the
  UI (no message-bubble/typing-indicator change) - it only lets the agent
  refer to itself by that name in conversation text if asked who it is.
- `disclose_as_agent` (`Boolean`, not null, default `False`) - default
  matches today's implicit full-impersonation behavior, so every existing
  agent is unaffected until the owner opts in.

**2. New config-mode tool `set_agent_identity`**, following the exact
`set_agent_persona`/`update_agent_rules` pattern (`modules/agents/tools/config_mode.py`,
`schemas.py`, `dispatch.py`). Both arguments (`agent_name`,
`disclose_as_agent`) are independently optional - omitting one leaves that
field unchanged, so the owner can set either without touching the other.
Writes go through the existing `update_agent_config` allowlist
(`modules/agents/crud.py`), extended with two more `if "key" in patch:`
blocks alongside `active_skill`/`system_prompt`.

**3. Builder interview gets a 5th checklist item**, added to
`BUILDER_PROMPT` (`modules/agents/builder_flow.py`) next to the existing
tone/boundaries item. Unlike the other checklist items, **the name half is
explicitly optional and skippable** - the owner is never forced to pick one,
and `agent_name` simply stays unset if they skip it. The disclosure choice
itself is still asked explicitly (mandatory to ask, not to a particular
answer), defaulting to `disclose_as_agent = False` if unanswered.

**4. `CHAT_STYLE_RULES` (`modules/agents/personas.py`) becomes agent-aware.**
Since it's shared across every execution persona, both new behaviors are
injected there once instead of duplicated per-persona:
- If `agent_name` is set, the agent may use it when asked who it is;
  otherwise it stays generic ("Linka Agent"), unchanged from today.
- If `disclose_as_agent` is `True`, the agent must truthfully confirm being
  an AI/bot when directly asked. If `False` (default), behavior is
  unchanged from today - no instruction to lie, simply no permission to
  volunteer AI status unprompted (same pattern `ONE_OFF_EXECUTOR` already
  uses).

This requires `CHAT_STYLE_RULES` to become a small per-turn templating call
(e.g. `get_chat_style_rules(agent)`) instead of a bare module constant,
since it now needs `agent.agent_name`/`agent.disclose_as_agent`
interpolated. Both assembly sites already have `agent` in scope
(`modules/agents/invoke_worker.py:690` and `:694`), so no new plumbing is
needed to reach it.

## What this deliberately does not do

- **No change to any peer-visible identity surface.** The real typing
  indicator (`_publish_peer_typing_loop`, `invoke_worker.py`) keeps its
  existing payload - it already fires as the owner's `user_id`, matching
  every other part of the send path, and is out of scope here per the
  owner's explicit choice. The drawer-only `agent_thinking` event is
  unrelated and untouched.
- **No forced name selection.** `agent_name` staying `NULL` is a fully
  supported, expected steady state, not a validation error.
- **No retroactive backfill or migration** for existing agents - both
  columns default to today's implicit behavior (no name, no disclosure),
  so nothing changes for an agent whose owner never visits this Builder
  step again.

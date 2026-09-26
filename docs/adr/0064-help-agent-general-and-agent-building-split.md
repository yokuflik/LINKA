# 0064 - Help Agent splits into General Help and Agent-Building Help

Status: Accepted

## Context

`BuilderState.HELP` (ADR 0049) has always been one undifferentiated
persona: `HELP_PROMPT` explains "how an agent is, what its configuration
options mean, and how the building process works" - i.e. only
agent-building/config questions. It has no material for general Linka
platform questions (chat history, search, groups, media, receipts,
presence, forwarding, scheduling messages, profile/username, storage) -
`SUPERVISOR_PROMPT` only ever routes "technical or conceptual question about
how the system works" into this one state, with no distinction between "how
do triggers work" and "how do I search old messages."

The user asked for this to be split into two dedicated help personas - one
platform-navigation guide, one agent-building specialist - so each prompt
stays focused and the routing decision itself carries information (which
kind of question this is), rather than one prompt trying to cover both
domains adequately.

Both help personas remain subject to the existing zero-backend-leakage
discipline every customer/owner-facing persona already follows
(`CHAT_STYLE_RULES`/`STYLE_RULES`'s tone rules, the identity-masking rule in
`ai_agent_judge_and_escalation.md`): user-facing language only, never
code/model/infra terms, never internal ids.

## Decision

`BuilderState` (`modules/agents/builder_flow.py`) gains a fourth value,
replacing the single `HELP`:

```python
class BuilderState(str, Enum):
    SUPERVISOR = "supervisor"
    BUILDER = "builder_agent"
    HELP_GENERAL = "help_general"
    HELP_BUILDING = "help_agent_building"
```

No migration: `Agent.builder_state` is a plain string column (ADR 0049),
never DB-enum-constrained. Existing agents currently parked in
`"help_agent"` are “stuck” for a message or two but self-heal - the
Supervisor/Builder's `transfer_to_help_general`/`transfer_to_help_building`
tools (below) always overwrite `builder_state` outright, and lazy-expiry
style state doesn't warrant a backfill script for an ephemeral routing
value nobody stays parked in for long (`ai_agent_judge_and_escalation.md`'s
`paused_chat_ids` precedent is the one case here that *does* get backfilled
treatment, because pauses are long-lived; this isn't).

Two new prompts replace `HELP_PROMPT`:

- **`HELP_GENERAL_PROMPT`** ("Platform Navigator") - explains any
  non-agent Linka feature: chat history/sync, search, groups, media,
  receipts, presence, edit/delete, forward, scheduled messages, mute,
  profile/username, storage quota. Interface-first framing only - what the
  user sees/taps, never the mechanism behind it.
- **`HELP_BUILDING_PROMPT`** ("Agent-Building Mentor") - the prior
  `HELP_PROMPT` content essentially unchanged (persona/triggers/knowledge
  base/restrictions/escalation/usage/reset/BYOK), scoped explicitly to
  building and configuring an agent.

Both keep the existing Help contract: they only explain, never
gather/save configuration (that stays Builder-only), and hand back to
`transfer_to_builder` once the user is ready to resume configuring (only
`HELP_BUILDING_PROMPT` offers that tool - `HELP_GENERAL_PROMPT` has no
reason to jump into the builder interview, so it offers
`transfer_to_supervisor` instead, for "ok, back to what I was doing").

`SUPERVISOR_PROMPT` and `BUILDER_PROMPT`'s existing single hand-off
instruction ("if a technical/conceptual question about how the system
works... call `transfer_to_help`") is replaced with a two-way instruction:
route to `transfer_to_help_building` when the question is about the
agent/its configuration/triggers/skills, `transfer_to_help_general` when
it's about anything else in the app. Ambiguous or "how does Linka/this
agent thing work" style openers default to `transfer_to_help_general` (the
broader net) - the Platform Navigator can itself redirect into
`transfer_to_help_building` if the conversation turns out to be
agent-specific, via the same handoff-tool pattern.

New/renamed handoff tools (`modules/agents/tools/builder_handoff.py`,
schemas in `builder_flow.py`):

| Tool | From | To |
|---|---|---|
| `transfer_to_help_general` | Supervisor, Builder, Help-Building | `help_general` |
| `transfer_to_help_building` | Supervisor, Builder, Help-General | `help_agent_building` |
| `transfer_to_builder` | Help-Building (only - General has no reason to) | `builder_agent` |
| `transfer_to_supervisor` | Help-General, Help-Building, Builder | `supervisor` |

(`transfer_to_help` is removed outright, not kept as an alias - both
Supervisor's and Builder's prompts are rewritten in the same change, so
nothing keeps calling the old name.)

`BUILDER_STATE_TOOL_SCHEMAS`/`BUILDER_STATE_HANDLERS`
(`schemas.py`/`builder_handoff.py`) gain two dict entries
(`HELP_GENERAL`, `HELP_BUILDING`) in place of the one `HELP` entry, each
with its own minimal tool set (a transfer tool or two, no execution/config
tools - unchanged from the original Help's zero-action posture). Every
other `BuilderState`-keyed tool-schema/handler table
(`SUPERVISOR`/`BUILDER`) gets the `transfer_to_help` reference swapped for
the two new tool names.

Two docstring/comment references to the literal `"help_agent"` value
(`modules/agents/models.py`, `modules/agents/schemas.py`, both describing
`Agent.builder_state`'s possible values in a comment, not enforced) are
updated to the new two-value list.

## Consequences

- No schema/migration, no new rate-limit bucket - same class of
  Python-dict-composition-only change as ADR 0062/0063's non-schema
  entries.
- The standing obligation in `.claude_docs/ai_agent.md` ("whenever the
  agent system's user-facing behavior changes, check whether `HELP_PROMPT`
  needs a matching update") now applies to both new prompts individually -
  a future change to, say, `on_schedule` triggers only needs
  `HELP_BUILDING_PROMPT` touched, not `HELP_GENERAL_PROMPT`, and a new
  platform feature (e.g. a future call feature) only needs
  `HELP_GENERAL_PROMPT` touched. This ADR renames that obligation
  accordingly in `.claude_docs/ai_agent.md`.
- `HELP_GENERAL_PROMPT` is the first persona in this codebase whose
  knowledge is entirely about the *platform* rather than the agent
  system itself - it needs accurate, current descriptions of chat/search/
  media/group features maintained by hand in the prompt text (no RAG/
  knowledge-base tool wired to it), so it will drift if a platform feature
  changes shape and this prompt isn't updated alongside it. Accepted as a
  known maintenance cost, consistent with how every other static prompt in
  this module already works.
- Both new prompts must never leak backend/implementation detail
  (server, database, WebSocket, model name, rate limits, internal ids) -
  same zero-leakage discipline as every other customer/owner-facing
  prompt in this module, made explicit in both prompts' own text rather
  than only relying on the shared style blocks.

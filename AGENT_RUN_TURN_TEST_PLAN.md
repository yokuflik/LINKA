# `_run_turn` Real-Execution Test Plan

## Goal

Today every test that touches `_run_turn` (`test_invoke_worker_schedule.py`,
`test_trigger_engine.py`, `test_invoke_debounce.py`, `test_judge.py`) mocks
`_run_turn` itself with `AsyncMock()`. That proves the *caller* wires it up
correctly, but the actual body of `_run_turn` — the tool-calling loop, token
budget accounting, MAX_TOKENS handling, round-trip cap, supersede checks,
judge integration, config-mode vs execution-mode branching — has **zero**
test coverage. It only ever runs live, against the real Gemini API, in
production or manual dev testing.

## The seam (no Gemini credits spent)

`_run_turn` never calls the Gemini HTTP API directly. It always goes through:

```
_run_turn → _generate_turn_or_supersede (invoke_turn_helpers.py)
          → generate_turn (gemini_client.py)   <-- the only httpx.AsyncClient call
```

`generate_turn` is a standalone async function imported into
`invoke_turn_helpers.py` as `from modules.agents.gemini_client import ...,
generate_turn`. Patching `modules.agents.invoke_turn_helpers.generate_turn`
(not `modules.agents.gemini_client.generate_turn` — it's imported by name,
so the reference inside `invoke_turn_helpers` module needs patching) with an
`AsyncMock` that returns a scripted `TurnResult` makes the **entire rest of
`_run_turn` run for real**: real DB session, real tool dispatch
(`execute_tool_call`), real token/time budget accounting, real judge call
(that one also needs mocking separately — see Step 3), real supersede
checks, real message persistence. No network call ever happens, no API key
needed, `settings.GEMINI_API_KEY` can stay unset in tests.

This is exactly the "mock only the Gemini HTTP layer" approach already
validated in conversation — the same shape as how `test_judge.py` mocks
`generate_structured` for the judge's separate model call.

## Test helper (build once, reuse everywhere)

A small helper module (new file:
`tests/modules/agents/_gemini_stub.py`) that builds `TurnResult` objects and
a context manager to patch `generate_turn` with a scripted sequence (one
`TurnResult` per round-trip, or a `side_effect` list for multi-call turns):

```python
from unittest.mock import AsyncMock, patch
from modules.agents.gemini_client import TurnResult, TurnUsage

def text_result(text, *, finish_reason="STOP", usage=(10, 5)) -> TurnResult:
    ...

def function_call_result(name, args, *, finish_reason="STOP", usage=(10, 5)) -> TurnResult:
    ...

def mock_gemini_turn(*results_or_exceptions):
    """Patches invoke_turn_helpers.generate_turn with a side_effect list -
    each call to _run_turn's loop pops the next scripted result/exception."""
    return patch(
        "modules.agents.invoke_turn_helpers.generate_turn",
        new=AsyncMock(side_effect=list(results_or_exceptions)),
    )
```

This keeps every real `_run_turn` test terse: `with mock_gemini_turn(text_result("hi")):`.

---

## Step 1 — Add the stub helper + one smoke test

**File:** `tests/modules/agents/_gemini_stub.py` (new)
**File:** `tests/modules/agents/test_run_turn_execution_mode.py` (new)

Write the helper module above, then a single smoke test that proves the
seam works end to end:

- Config-mode turn (owner's own agent chat, Supervisor state), agent replies
  with plain text (no tool call) → `generate_turn` mocked to return
  `text_result("Hello owner")` → assert a real message with that content
  lands in the DB via `_post_config_reply`'s path (query
  `modules.messaging.crud` or the chat's messages directly), sender_agent_id
  set, `AGENT_REPLY_MESSAGE_TYPE`.

This step's only purpose is confirming the patch target/mechanics are
correct before building out the full suite (existing `_run_turn` callers use
real Postgres/Redis fixtures — `db_session`, `redis_db` — same as
`test_invoke_worker_schedule.py`; reuse its `_make_user`/`_make_chat`/
`_make_agent` factories, or lift them into a shared conftest/helper since at
least 3 files will now want them).

**Refactor call:** `_make_user`/`_make_chat`/`_make_agent`/`_next_id` in
`test_invoke_worker_schedule.py` are currently private to that file. Move
them to a shared `tests/modules/agents/_factories.py` and import from both
files rather than duplicating — do this in Step 1 since every subsequent
step needs them.

---

## Step 2 — Execution-mode text-reply and tool-call turns

**File:** `tests/modules/agents/test_run_turn_execution_mode.py` (extends Step 1)

Covers a real 1:1 chat where the agent is triggered by an incoming message
(`message_id` set, `config_mode_turn=False`). The judge must be mocked here
too (it's a real, separate Gemini-shaped call via `evaluate_message`) —
mock `modules.agents.invoke_worker.evaluate_message` to return an
approved verdict, since judge behavior itself is already covered by
`test_judge.py` and is out of scope for this file.

Tests:

1. **Plain text reply, no tool call is not expected** — agent has
   `send_message`-shaped tools available (execution mode) and Gemini
   returns a text-only response. Assert the turn ends (`ended_status` path
   not directly observable, but no message is force-posted — execution mode
   doesn't call `_post_config_reply` on plain text, per the code at
   invoke_worker.py:510).
2. **Single tool-call round-trip: `send_message`** — `generate_turn` scripted
   with `[function_call_result("send_message", {...}), text_result("done")]`
   (Gemini calls the tool, gets a functionResponse, then replies with text).
   Assert: `execute_tool_call` really ran (a real message was sent to the
   real target chat via the real send path), `contents` grew with the
   functionResponse part, the turn ended after the second (text) result.
3. **Multi-round-trip tool chain** (e.g. `read_history` then `send_message`)
   — 3 scripted results — assert both tools executed in order and the final
   text ended the turn without a 3rd call.
4. **Round-trip cap hit** — monkeypatch
   `settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS` down to e.g. `1`, script a
   `generate_turn` side_effect that always returns a function-call result
   (never text) — assert the turn ends after the cap, posts
   `_ROUND_TRIP_CAP_NOTICE` into the *owner's own agent chat*
   (`agent.owner_agent_chat_id`), not the execution chat.
5. **MAX_TOKENS mid-turn (execution mode)** — script a
   `text_result(..., finish_reason="MAX_TOKENS")` — assert the turn ends
   immediately, **no partial text is posted to the execution chat** (only
   config-mode turns show partial text, per invoke_worker.py:486), and
   `record_tokens` was still called with the real usage numbers (assert via
   `modules.agents.token_budget.peek_usage`).
6. **`GeminiChatError` from the transport** — script `generate_turn` to
   `raise GeminiChatError("boom")` — assert a real
   "This took a bit too long..." notice lands in the owner's agent chat, and
   the turn returns cleanly (no exception escapes `_run_turn`).

---

## Step 3 — Config-mode turns (Supervisor / Builder / Help)

**File:** `tests/modules/agents/test_run_turn_config_mode.py` (new)

Config-mode turns run in the owner's own agent chat
(`chat_id == agent.owner_agent_chat_id`), skip the judge entirely (already
true — code path only runs the judge when `not config_mode_turn`), and post
plain-text replies directly via `_post_config_reply` instead of a
`send_message` tool.

Tests:

1. **Supervisor plain-text reply** — `generate_turn` → `text_result("hi
   there")` — assert the reply is posted into the owner-agent chat with
   `sender_agent_id` set.
2. **Empty-text fallback (ADR 0089)** — script a `TurnResult` whose
   `content` has no text parts (e.g. only a thought-signature-shaped dict
   with empty `parts`) — assert the fixed fallback notice
   ("Sorry, something went wrong on my end...") is posted instead of nothing.
3. **Config-mode tool call, e.g. `set_agent_persona`** — script
   `[function_call_result("set_agent_persona", {...}), text_result("done")]`
   — assert the agent's real DB row was updated (persona/system_prompt
   field) via the real tool dispatch, and the final text reply landed.
4. **`no_reply_needed`** — script a `function_call_result("no_reply_needed",
   {})` — assert the turn ends with **no message posted at all** (distinct
   from the empty-text case in #2).
5. **Builder → Supervisor handoff mid-turn (`finish_building_agent`)** —
   script `[function_call_result("finish_building_agent", {...}),
   text_result("all set")]` starting from `builder_state="builder_agent"` —
   assert `agent.builder_state` flips and `is_enabled` becomes `True` (per
   ADR 0049) by the time the second round-trip's reply lands, proving the
   mid-turn re-derivation of `tool_schemas`/`system_prompt` (invoke_worker.py
   ~360-376) actually works against a real DB-backed tool handler, not a
   mock.

---

## Step 4 — Token/time budget interplay (real accounting, mocked transport)

**File:** `tests/modules/agents/test_run_turn_budgets.py` (new)

These are the ones most likely to have silently-broken logic since nothing
ever exercised them end-to-end.

1. **Token window exhaustion notice** — pre-seed the token budget Redis
   counters (via `modules.agents.token_budget`) right up to the limit, then
   run a turn with a scripted `TurnUsage` that tips it over. Assert
   `_notify_token_budget_exhausted` really fires (owner gets a real notice
   message) and it only fires once (cooldown) if you run a second turn right
   after.
2. **Per-minute Gemini call budget retry path** — monkeypatch
   `settings.AGENT_GEMINI_BUDGET_RETRY_SECONDS` to something tiny (e.g.
   `0.01`) and `AGENT_GEMINI_BUDGET_MAX_RETRIES` to `2` so the test doesn't
   actually sleep for real minutes; pre-exhaust
   `modules.agents.invoke_turn_helpers._check_gemini_call_budget`'s
   underlying Redis counter so the first check fails, then let it recover
   before retries run out — assert the turn proceeds normally once budget
   frees up. Also test the exhausted-after-all-retries path → assert the
   "handling a lot of requests" notice.
3. **Daily active-time budget accounted in `finally`** — assert
   `record_active_seconds` is called with a real elapsed duration after a
   real (mocked-Gemini) turn completes — this already has partial coverage
   in `test_invoke_worker_schedule.py` but only with `_run_turn` itself
   mocked, so the seconds recorded there are meaningless (the mock returns
   instantly). Redo it here with `_run_turn` genuinely running so the
   duration reflects real turn work.

---

## Step 5 — Supersede / mid-turn interruption (ADR 0063 / 00732 / 0075)

**File:** `tests/modules/agents/test_run_turn_supersede.py` (new)

This is the highest-value gap: the supersede logic has three separate check
points inside `_run_turn` (top-of-loop pre-call, mid-call via
`_generate_turn_or_supersede`'s poll loop, and pre-send just before a
message-sending tool runs) and none of them currently run against a real
loop — only unit tests of `invoke_debounce.py`'s primitives exist.

1. **Pre-call supersede, no Gemini call yet (case A, merge)** — call
   `mark_superseded(agent_id, chat_id)` before invoking `_run_turn`, script
   `generate_turn` to return a normal text result — assert the turn does
   **not** abort; it rebuilds contents and continues (per the case-A
   "merging is free" branch), and a real reply still gets sent.
2. **Mid-call supersede (case B)** — this one genuinely needs to interrupt
   an in-flight (mocked) call. Make the `generate_turn` mock's
   `side_effect` an async function that awaits `asyncio.sleep(...)` long
   enough for the test to call `mark_superseded` while it's "in flight",
   relying on `_generate_turn_or_supersede`'s real 0.5s poll
   (`_SUPERSEDE_POLL_SECONDS`) to detect it and cancel — assert the turn
   ends via `_TurnSuperseded` with **no message sent**, `arm_debounce_now`
   really re-armed the ZSET (check Redis), and
   `record_tokens` was charged the flat `AGENT_SUPERSEDED_CALL_TOKEN_PENALTY`.
   (This test is inherently the slowest in the suite — budget ~1s real wall
   time. Consider monkeypatching `_SUPERSEDE_POLL_SECONDS` down via `patch`
   if it's not already read from `settings` — check the constant's
   definition; if it's a module-level literal, `monkeypatch.setattr` the
   `invoke_turn_helpers` module attribute instead of `settings`.)
3. **Pre-send supersede (right before `send_message`)** — script
   `generate_turn` → `function_call_result("send_message", {...})`, but call
   `mark_superseded` between the mock resolving and the dispatch check
   (achievable by making the tool-schemas lookup or `execute_tool_call`
   patched with a side effect that marks superseded first — or more
   simply: mark superseded right after starting the coroutine and before it
   yields to the point after the Gemini call but before dispatch, using a
   real `asyncio.sleep(0)` checkpoint). Assert `send_message`'s tool handler
   never actually runs (no message reaches the chat) and the turn re-arms
   the debounce instead.

If contriving precise timing for #2/#3 proves too flaky, an acceptable
fallback (note this as a decision point when working the step, not a
silent scope cut) is testing `_generate_turn_or_supersede` and the pre-send
gate as slightly more isolated unit tests that still call the real function
with a real asyncio task, just not always through the full `_run_turn`
entry point — still real code, still zero Gemini calls, just narrower
blast radius per test.

---

## Step 6 — Knowledge-fired and schedule-fired turns through the real body

**File:** extend `test_invoke_worker_schedule.py` (schedule-fired) and add
`test_run_turn_knowledge.py` (new, knowledge-fired)

`test_invoke_worker_schedule.py`'s existing tests only prove `process_entry`
calls `_run_turn` with the right arguments — they never let `_run_turn`
itself run. Add a parallel set of tests (same file or a new
`test_run_turn_schedule_fired.py`) that keep `process_entry`'s own
`_run_turn` mock removed and instead mock only `generate_turn`, to check
`_build_schedule_contents`/`_build_knowledge_contents` produce a working
turn end to end — a schedule-fired turn with `scoped_system_prompt` unset
that calls `send_message`, a knowledge-notice turn that ends in a
`_post_config_reply` describing ingestion success.

---

## Ordering & dependencies

Steps 1 → 2 → 3 → 4 must be done in order (each reuses fixtures/helpers the
previous step builds). Step 5 depends only on Step 1's helper module, not on
2-4, and Step 6 depends only on Step 1. So after Step 1 is done, 2/3/4 can
proceed in order while 5 and 6 could be picked up independently if you want
to parallelize across sessions.

## What NOT to change

- `test_invoke_worker_schedule.py`'s existing tests stay as-is (they test
  `process_entry`'s dispatch logic, which is a legitimately different unit
  from `_run_turn`'s body) — Step 6 *adds* new tests alongside them, it does
  not replace the existing mocked-`_run_turn` ones.
- `test_judge.py` stays as-is — the judge's `generate_structured` call is
  already correctly tested at the transport-mock level; Steps 2/3 only need
  to mock `evaluate_message` at a higher level to keep those tests focused
  on `_run_turn`'s own logic, not re-prove judge behavior.
- No real `GEMINI_API_KEY` or network access ever gets added to any test —
  every step here mocks `generate_turn`/`generate_structured`, never
  removes the mock.

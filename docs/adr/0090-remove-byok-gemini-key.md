# 0090 - Remove BYOK (bring-your-own Gemini key) entirely

Status: Accepted

## Context

ADR 0046 decision 5 introduced BYOK: an owner could store their own Gemini
API key (Fernet-encrypted `Agent.encrypted_gemini_api_key`) so their agent's
calls would draw on their own quota instead of the shared project key. ADR
0046 decision 6 added the frontend for it (masked key input in the drawer).

`invoke_worker.py` has hardcoded BYOK off since 2026-09-24 ("not available
yet in the frontend") - `api_key` is always `None`, every turn always uses
the shared `settings.GEMINI_API_KEY`, and the comment there explicitly notes
the decrypt path is "left in place so re-enabling later is just removing
this early return." `AgentSettingsView.js` already had its BYOK section
removed "per explicit user request."

The user has now confirmed BYOK will not return: the personal-API-key option
is being dropped as a product decision, not deferred. `decrypt_api_key`
(`modules/agents/crypto.py`) has zero call sites anywhere in the codebase -
confirmed dead - and is the first thing flagged, but the feature's full
footprint is much larger: a DB column, two Pydantic schema fields, dedicated
PATCH-endpoint branching in `router.py`, a reset-path field, a status note
in a tool response, a dedicated settings key, an `init_db.py` migration
step, and three frontend files/composables still wired to the (already
partially removed) UI.

## Decision

Remove BYOK completely, across every layer, reversing ADR 0046 decisions 5
and 6:

- **DB**: drop `agents.encrypted_gemini_api_key` (`init_db.py`'s `ALTER
  TABLE ... ADD COLUMN IF NOT EXISTS` step removed; per this project's
  no-migrations convention, ADR 0032/dev-DB re-init handles the actual drop
  - no Alembic migration).
- **Model**: remove the column from `modules/agents/models.py`.
- **Schemas**: remove `has_custom_key` (output) and `gemini_api_key` (input)
  from `modules/agents/schemas.py`.
- **Router**: remove the `gemini_api_key` patch-field branch and
  `has_custom_key` computed field from `modules/agents/router.py`; drop the
  now-unused `encrypt_api_key`/`ByokKeyError` import.
- **Reset**: remove the `encrypted_gemini_api_key = None` line from
  `modules/agents/reset.py` (nothing left to reset).
- **Tools**: remove the "unlimited when BYOK" status note from
  `modules/agents/tools/config_mode.py`'s `get_capacity_status` (ADR 0057) -
  the capacity note was always conditional on a field that no longer exists;
  the underlying quota is unconditionally enforced now that there is no
  alternate key path.
- **crypto.py**: delete `decrypt_api_key` (dead) and `encrypt_api_key` (only
  caller was the router branch just removed) - the whole module becomes
  dead once both functions are gone, so delete `modules/agents/crypto.py`
  entirely.
- **gemini_client.py**: `generate_turn`'s `api_key` parameter and
  `_require_api_key`'s override branch are removed - every call always uses
  `settings.GEMINI_API_KEY`. `generate_structured` already never accepted an
  override (judge only ever uses the shared key) - unchanged.
- **invoke_worker.py**: remove the `api_key: str | None = None` local, the
  BYOK comment block, and every `api_key is None` / `api_key is not None`
  branch condition (the Gemini-call-budget check and `_check_gemini_call_budget`
  gate become unconditional - there is no other path anymore).
- **Settings**: remove `AGENT_BYOK_ENCRYPTION_KEY` from
  `config/agent_settings.py` (both the definition and the `__all__` export).
- **Frontend**: remove all BYOK state/handlers from
  `poc/composables/useAgentConfig.js` (`byokDirty`, `byokKeyInput`,
  `onByokKeyInput`, `saveByokKey`, `cancelByokKeyEdit`, `clearByokKey`, and
  their resets), the corresponding props/emits from
  `poc/components/AgentDrawer.js`, and the wiring in `poc/index.html`.
  `AgentSettingsView.js` needs no further change (BYOK UI already absent
  there).
- **Tests**: `tests/modules/agents/test_reset.py` drops its
  `encrypted_gemini_api_key=b"fake-encrypted-key"` seed and the assertion
  that reset clears it (nothing left to seed or assert).

## Consequences

- No owner can ever supply their own Gemini key again; every agent turn
  always draws on the shared project quota/budget (already true in practice
  since the 2026-09-24 hardcoded disable - this just removes the dead
  machinery instead of leaving it dormant).
- Smaller attack surface: no Fernet key management, no ciphertext-at-rest
  concern for a third-party API key.
- Any future "let owners bring their own model key" request would be a new
  feature built from scratch, not a re-enable - this ADR is a deliberate,
  final reversal, not a pause.
- No new settings, no new schema, no new rate-limit bucket - this ADR only
  removes.

# ADR 0104: Eager agent provisioning on signup

Status: Accepted (reverses ADR 0047 decision 2, "provisioning stays lazy")

## Context
ADR 0047 kept `Agent` + owner-agent chat creation lazy (`POST /agents/me`, triggered by the PoC's
"Activate" button). A brand-new user therefore opens the app with no agent chat/greeting until they
find and click Activate.

## Decision
- New `modules/agents/provisioning.py::provision_agent(session, user_id)`: the idempotent
  create-if-missing body of `POST /agents/me` (owner-agent chat + `Agent` row + cache sync + greeting),
  extracted unchanged from `router.py`. `POST /agents/me` now delegates to it.
- `modules/auth/service.py::_find_or_create_and_issue` calls it, best-effort (exceptions logged, never
  fail login), only when this call actually created the user. `GET /agents/me` stays 404-free for new
  users; `POST /agents/me` stays as the self-heal path for failed or pre-existing accounts.
- `Agent.is_enabled` still defaults to `False` (ADR 0047 decision 2, first half) - only the "no
  creation in signup" half is reversed.

## Consequences
One extra chat + agent row + greeting message per signup. Existing users without an agent are
unaffected until they hit `POST /agents/me`.

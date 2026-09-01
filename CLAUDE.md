# Linka — Root Router & Global Rulebook

Real-time messaging platform (WhatsApp/Telegram-style), designed for tens-of-billions-of-messages scale. Backend-first; **no real client app**, only a single-file HTML/Vue PoC (`poc/`) for manual testing.

**Stack (summary):** FastAPI (REST + WebSocket) · PostgreSQL 15 / SQLAlchemy 2.0 async / asyncpg · Redis 7 · PyJWT · Pydantic v2 · pytest/pytest-asyncio/httpx · MinIO (S3-compatible storage).

---

# CORE BEHAVIOR RULES (always in force)

1. **No Yapping:** Be extremely concise. Output only the necessary code or commands, with a 1-sentence explanation maximum. Skip pleasantries, apologies, and verbose explanations.
2. **The 3-Strike Rule (Anti-Loop):** If you hit an error on the exact same issue 3 times in a row, DO NOT attempt another fix. Stop, state that you are stuck in a loop, and ask the user for guidance.
3. **Code Standards:** Write ALL source code comments exclusively in English.
4. **Targeted Edits:** For small tasks, minor features, or bug fixes, NEVER rewrite or output the entire file. Use targeted diffs or provide ONLY the specific code blocks/functions that change.
5. **Frontend Display Rule:** NEVER display the raw `user_id` in the UI (chat lists, message bubbles, headers). ALWAYS use `phone_number` or, preferably, the resolved human-readable name from the local contacts dictionary. `user_id` is strictly for backend logic / API calls.
6. **No Autonomous Visual Testing:** NEVER use browser tools, Puppeteer, or screenshots to test frontend changes. Do NOT spin up local servers to visually verify the UI. The user tests manually and reports back.
7. **Architecture Decision Records (ADR):** Whenever we make a significant architectural, database schema, or infrastructure decision, you MUST proactively generate a new ADR file in `docs/adr/` BEFORE writing the code. Always review existing ADRs before proposing systemic changes.
9. **Token & Bottleneck Alerts:** If asked to read/analyze/edit a file over ~300 lines, or you spot a token-wasting workflow bottleneck, STOP. Alert the user about the specific file/bottleneck, explain the cost, suggest a split strategy, and wait for a decision.
10. **Ask before backend changes** (user's standing request). Flag security-relevant or destructive changes clearly instead of silently reverting them.

---

# CONTEXT ROUTING RULE (mandatory)

This file is a **router only**. It does NOT contain domain detail.

**Before working on ANY specific task, you MUST read the relevant sub-file(s) in `.claude_docs/` first.** Do not write or edit code in a domain until you have read that domain's file in the current session. If a task spans multiple domains, read all the relevant files.

---

# AUTO-MAINTENANCE RULE (mandatory)

`.claude_docs/` is the persistent external brain. You are **required** to keep it current:

- **Every time a major architectural decision, schema change, new service/module, new WS action, or new API endpoint is made or discovered**, proactively update the specific sub-file(s) in `.claude_docs/` in the same task — before concluding it.
- If new information does not fit any existing sub-file, **create a new sub-file** in `.claude_docs/` and add it to the Index below.
- Keep each sub-file focused and under ~300 lines; split further if it grows past that.
- For a significant architectural / schema / infrastructure decision, first add a numbered ADR in `docs/adr/` (Rule 7), then update the affected `.claude_docs/` sub-file(s), then write the code.
- Update this root file ONLY for: core behavior rules, the routing/maintenance rules, or the two Indexes.

---

# INDEX — `.claude_docs/`

| File | Contents |
|---|---|
| `.claude_docs/backend_services_and_api.md` | `services/` + `services/messaging/` layout & facade contract, `services/settings/` (per-user settings), `routers/`, auth / OTP caveat, group membership & roles, system messages (incl. `role_changed` JSON pattern), leaving/removing members, profile-edit propagation, backend known-gaps, working conventions. |
| `.claude_docs/realtime_and_redis.md` | All Redis usage. Async send path (`services/fanout/`, streams, workers, sharding), routing layer (`chat_instances`, `instance_inbox`, heartbeats), `connection_manager` inbox task, presence (subscribe-on-demand, 1:1 only), typing/recording indicator, receipt-log async writes. |
| `.claude_docs/database_schema.md` | Models & CRUD, no-migrations rule, partitioning, Snowflake-id-as-string rule, watermark receipt model, detailed `message_receipt_log`, chat-list denormalization, unread count, reply-to, edit/delete/restore, scripts, DB-test-wipes-dev-DB warning. |
| `.claude_docs/storage_and_media.md` | Object storage design principle, MinIO/config, `services/storage/` (`client.py`, `media_service.py`, `errors.py`), media messages (kinds/caps/MIME whitelist incl. the 3-place `file=set()` sentinel), `Message` media columns, presigned-URL flow, user & group avatars. |
| `.claude_docs/deployment.md` | Single-host demo deploy: `Dockerfile`, `docker-compose.prod.yml`, `deploy/` (Caddyfile, postgres.prod.conf, env example, prod cron), first-boot/update steps, memory budget, demo compromises (open OTP stub, in-box MinIO). Runbook: `deploy/README.md`. |
| `.claude_docs/frontend.md` | PoC structure (`poc/index.html` + `components/*.js` + `composables/*.js`), running it, syntax-check-after-edit rule, `$emit` chaining rule, `useWsRouter.js` live-event handling, optimistic send flow. |

# INDEX — `docs/adr/` (Architecture Decision Records)

Per Core Behavior Rule 7, review these before proposing any systemic change, and add a new numbered ADR before writing code for a significant architectural / schema / infrastructure decision.

| ADR | Title | Status |
|---|---|---|
| `docs/adr/0001-redis-pubsub-fanout-routing.md` | Redis pub/sub fan-out routing layer and queue workers | Accepted |
| `docs/adr/0002-user-settings-jsonb.md` | Per-user settings as a single extensible JSONB blob | Accepted |
| `docs/adr/0003-read-receipts-privacy.md` | Read-receipts (blue-tick) privacy: asymmetric per-reader 1:1 mask, groups exempt | Accepted |
| `docs/adr/0004-chat-mute.md` | Per-user chat mute as `Participant.muted_until`; client owns durations, server only suppresses offline push | Accepted |
| `docs/adr/0005-time-partition-management.md` | Weekly `messages` / daily `message_receipt_log` partitions, managed by a standalone Python script (not `pg_partman`); DEFAULT kept as safety net; online DEFAULT migration | Accepted |
| `docs/adr/0006-partition-maintenance-cron.md` | Partition-maintenance scheduling: committed `deploy/partition-maintenance.crontab` + `scripts/partition_maintenance.sh` wrapper, run on exactly one host (no in-app scheduler) | Accepted |
| `docs/adr/0007-single-host-docker-compose-deploy.md` | Demo deploy: one 1 GB host, `Dockerfile` (multi-stage) + `docker-compose.prod.yml` (app + Caddy + db + redis + minio), single Uvicorn process, git-ignored `.env` | Accepted |

---

# Running locally

```bash
docker compose up -d                                    # test_db (5433), test_redis (6380), test_minio (9100 API / 9101 console)
DATABASE_URL="postgresql+asyncpg://test_user:test_password@localhost:5433/test_db" python3 -m scripts.init_db
python3 -m scripts.init_storage
DATABASE_URL="..." REDIS_URL="redis://localhost:6380/0" uvicorn main:app --reload
```

Open `poc/index.html` directly. OTP codes print to the server console — no real SMS/FCM.

**Testing caveat:** any DB-backed test wipes the dev DB (`drop_all` on teardown). Dump first or re-run `init_db` + `seed_mock_data`. MinIO is unaffected. When manually verifying against dev Postgres, leave the schema created (not dropped).

# Reference plan files (root)

`FANOUT_REWRITE_PLAN.md` (steps 1–4 all landed), `poc/composables/REFACTOR_PLAN.md` (complete).

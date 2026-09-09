# 1. Redis pub/sub fan-out routing layer and queue workers

## Status

Accepted (landed — FANOUT_REWRITE_PLAN.md steps 1–4).

## Context

The original design used one Redis pub/sub channel per chat. Every app process subscribed to a channel for each chat any of its connected users belonged to. At target scale (tens of billions of messages, large groups spread across many processes) this caused:

- Subscription churn: a process subscribing/unsubscribing per chat as connections come and go.
- Publish amplification: a message to a group with members on N processes still relied on per-process channel subscriptions, and every process holding any subscriber woke up.
- Sending was synchronous inside the WebSocket handler: persistence, media HEAD validation, and fan-out all blocked the `send_message` ACK, coupling client latency to DB/storage health.

We needed group-size-independent routing cost, resilience to process crashes, and a send path that ACKs fast and retries transient failures.

## Decision

1. **Async send path.** WS `send_message` does only a rate-limit + participant check, then `XADD message_send_stream` and ACKs `{"type":"ack","for":"send_message","status":"queued"}`. `services/fanout/worker.py` (`run_forever`, one task/process) consumes the stream and runs `message_service.process_outgoing` (idempotency, `_validate_media` HEAD, `create_message`).

2. **Fan-out as a second hop.** After `create_message` commits, `process_outgoing` calls `send_queue.enqueue_fanout` (`XADD message_fanout_stream`). `services/fanout/fanout_worker.py` drains it and runs `message_service.fan_out_message` (build `new_message`, `publish_event`, push to offline).

3. **Routing layer replaces per-chat channels.** Each process registers the chats it serves in Redis (`chat_instances:{chat_id}` SET of `server_id`, TTL 90s, refreshed by `_routing_heartbeat` every 30s; reverse map `instance_chats:{server_id}`). `realtime_service.publish_event(chat_id, event)` injects `chat_id`, looks up `routing.instances_for_chat`, and `PUBLISH`es once to each `instance_inbox:{server_id}`. Each process runs exactly **one** `_instance_inbox_task` consuming `instance_inbox:{SERVER_ID}` and dispatches events by `event["chat_id"]` to local connections.

4. **Stream sharding.** Both streams are sharded by `chat_id` (`SEND_STREAM_SHARDS` / `FANOUT_STREAM_SHARDS`, default 4; shard 0 = bare key for upgrade safety). Each worker runs one consumer task per shard, one consumer group per shard.

All Redis routing operations are best-effort; a dropped registration self-heals on the next heartbeat.

## Consequences

**Positive**

- Fan-out publish cost is O(processes serving the chat), not O(subscribed processes) or O(group size).
- `send_message` ACK latency is decoupled from DB/storage; transient failures are retried via `XAUTOCLAIM` without client involvement.
- One inbox subscription per process instead of one per chat — no per-chat subscription churn.
- Crash resilience: consumer groups + `XAUTOCLAIM` reclaim work from dead workers; routing registrations expire and re-register.

**Negative / trade-offs**

- Read-after-write lag: `GET /chats` and other readers do not see a message until the send worker commits (sub-second in practice). The PoC is optimistic so this is invisible there.
- The `send_message` ACK no longer returns `message_id` / `created_at`; clients reconcile optimistic bubbles via the `client_message_id` echoed on `new_message`.
- More moving parts: two Redis Streams, two worker types, a heartbeat loop, and routing state to operate and monitor.
- At-least-once delivery: redelivered stream entries re-publish `new_message`; clients must dedupe by `message_id`.
- Recovery edge case: if the send worker hits `MessageAlreadySentError` (row persisted, fan-out possibly not enqueued) it must re-enqueue fan-out.

## Related

- `.claude_docs/realtime_and_redis.md` — operational detail.
- `FANOUT_REWRITE_PLAN.md` — the 4-step implementation plan.

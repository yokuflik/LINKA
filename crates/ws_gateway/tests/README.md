# Integration tests

Each file here is a separate test binary compiled against the `ws_gateway`
crate as an external crate — used for anything that needs a real `AppState`,
a bound axum router, a live WS client (`tokio-tungstenite`), or a mocked
`/internal/*` HTTP server (`wiremock`). Per-file test strategy and Redis
setup follow `RUST_GATEWAY_TEST_PLAN.md` Phase 0.

Redis: point tests at `redis://127.0.0.1:6380/1` (test_redis, DB index 1 —
NOT `/0`, which the Python suite's `flushdb()` collides with; see
`project_linka_redis_test_collision` in project memory). Each test must
`FLUSHDB`-equivalent only its own keys via a unique random prefix in
setup/teardown, never a global flush, so tests can run concurrently.

Do not run this suite at the same time as `run_dev.sh` or the Python test
suite against the same Redis container.

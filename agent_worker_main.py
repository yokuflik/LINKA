"""Standalone entrypoint for the `agent_worker` Docker Compose service
(ADR 0045, step 3).

Deliberately its own process/container, not a task inside main.py's FastAPI
lifespan like the other stream consumers (receipt/send/fan-out/scheduled):
a stuck or crashing Gemini turn must not affect the WS gateway hand-off,
message delivery, or any of the other workers. Horizontal scaling is "run
more agent_worker containers" - the shared consumer group on
agent_invoke_stream splits the load automatically.

Run directly (`python agent_worker_main.py`), not through uvicorn.
"""
import asyncio
import logging
import signal

from dotenv import load_dotenv

load_dotenv()

from infra.db.connection import dispose_engine
from infra.redis.client import close_redis
from modules.agents import invoke_worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("agent_worker: shutdown signal received, draining in-flight entries")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_stop)

    try:
        await invoke_worker.run_forever(stop_event)
    finally:
        await dispose_engine()
        await close_redis()
        from infra.ids import client as id_client

        await id_client.close()  # no-op unless ID_SERVICE_ADDR is set (ADR 0011)


if __name__ == "__main__":
    asyncio.run(main())

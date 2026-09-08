import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from infra.db.connection import check_database_connection
from infra.db.connection import dispose_engine
from modules.auth.router import router as auth_router
from modules.chats.router import router as chats_router
from modules.messaging.router import router as messages_router
from modules.users.router import router as users_router
from realtime.ws_router import router as websocket_router
from config import (
    ALLOWED_HOSTS,
    API_IP_BACKSTOP_MAX,
    API_IP_BACKSTOP_WINDOW_SECONDS,
    CORS_ALLOW_ORIGINS,
    ROUTING_HEARTBEAT_INTERVAL_SECONDS,
    SERVER_ID,
)
from modules.auth import service as auth_service
from modules.chats import service as chat_service
from modules.messaging import service as message_service
from infra.ratelimit import service as rate_limit_service
from modules.users import service as user_service
from infra.ratelimit.service import RateLimited
from realtime.fanout import fanout_worker
from realtime.fanout import routing
from realtime.fanout import worker as send_worker
from modules.receipts import worker as receipt_worker
from infra.redis.client import close_redis
from modules.settings.errors import SettingsValidationError
from modules.media import media_service
from modules.media.errors import MediaNotFoundError
from modules.media.errors import MediaValidationError
from modules.media.errors import StorageUnavailableError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the media / avatars buckets if they don't exist yet (dev
    # convenience - a no-op against IaC-provisioned production buckets).
    try:
        await media_service.ensure_buckets()
    except Exception as exc:  # storage being down must not stop the app booting
        import logging

        logging.getLogger(__name__).warning("ensure_buckets failed at startup: %s", exc)

    # Background consumer draining the receipt Redis Stream into
    # message_receipt_log (see services/receipts). One task per process;
    # the shared consumer group spreads the load across replicas.
    receipt_task = asyncio.create_task(receipt_worker.run_forever())

    # Background consumer draining message_send_stream: persists each queued
    # outgoing message and fans it out (see services/fanout). Same one-task-
    # per-process / shared-consumer-group model as the receipt worker.
    send_task = asyncio.create_task(send_worker.run_forever())

    # Background consumer draining message_fanout_stream: builds and publishes
    # the new_message event and pushes to offline members. Second hop after
    # the send worker (see services/fanout/fanout_worker).
    fanout_task = asyncio.create_task(fanout_worker.run_forever())

    # Routing heartbeat (FANOUT_REWRITE_PLAN.md step 3): re-asserts this
    # process's chat_instances registrations and refreshes their TTL, so a
    # crashed process's entries expire instead of lingering.
    async def _routing_heartbeat() -> None:
        while True:
            try:
                await routing.heartbeat(SERVER_ID)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("routing heartbeat failed")
            await asyncio.sleep(ROUTING_HEARTBEAT_INTERVAL_SECONDS)

    heartbeat_task = asyncio.create_task(_routing_heartbeat())

    yield

    for task in (receipt_task, send_task, fanout_task, heartbeat_task):
        task.cancel()
    for task in (receipt_task, send_task, fanout_task, heartbeat_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    try:
        await routing.unregister_instance(SERVER_ID)
    except Exception:
        logging.getLogger(__name__).warning("routing unregister failed at shutdown")
    await dispose_engine()
    await close_redis()
    from infra.ids import client as id_client

    await id_client.close()  # no-op unless ID_SERVICE_ADDR is set (ADR 0011)


app = FastAPI(lifespan=lifespan)

# Reject requests whose Host header isn't in ALLOWED_HOSTS (DNS-rebinding /
# Host-header injection). "*" disables the check for local dev.
if ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# CORS. Prod is same-origin (Caddy serves the PoC + API together) so this is
# normally the single site origin. "*" is dev-only: the "*" + credentials
# combination is invalid per the CORS spec and silently unsafe, so whenever
# origins is "*" we force credentials off (a file:// PoC doesn't send cookies
# anyway - it holds the JWT in JS).
_cors_wildcard = CORS_ALLOW_ORIGINS == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Coarse per-IP REST backstop (ADR 0012). Not the primary control - the
# per-user / per-identity limits in the routers are. This just stops one IP
# from hammering the box. Skips /healthz (infra probes) and /ws (WebSocket
# upgrade has its own handshake-churn limit in step 5).
@app.middleware("http")
async def _per_ip_backstop(request: Request, call_next):
    path = request.url.path
    if path != "/healthz" and not path.startswith("/ws"):
        ip = rate_limit_service.client_ip(request)
        allowed = await rate_limit_service.check_and_increment(
            ip, "api_ip_backstop", API_IP_BACKSTOP_MAX, API_IP_BACKSTOP_WINDOW_SECONDS
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limited", "action": "api_ip_backstop"},
                headers={"Retry-After": str(API_IP_BACKSTOP_WINDOW_SECONDS)},
            )
    return await call_next(request)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(chats_router)
app.include_router(messages_router)
app.include_router(websocket_router)


@app.get("/healthz")
async def healthz():
    db_ok = await check_database_connection()
    return JSONResponse(status_code=200 if db_ok else 503, content={"database": db_ok})


# Centralized error mapping: every router above just calls a service and
# returns its result, instead of repeating try/except HTTPException
# boilerplate in each route - these are the only place service-layer
# exceptions turn into HTTP status codes.
@app.exception_handler(auth_service.OTPRequestRateLimitedError)
async def _handle_otp_rate_limited(request: Request, exc: Exception):
    return JSONResponse(status_code=429, content={"detail": str(exc)})


@app.exception_handler(RateLimited)
async def _handle_rate_limited(request: Request, exc: RateLimited):
    return JSONResponse(
        status_code=429,
        content={"detail": "rate limited", "action": exc.action},
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(auth_service.InvalidOTPError)
async def _handle_invalid_otp(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(auth_service.PhoneAlreadyRegisteredError)
async def _handle_phone_already_registered(request: Request, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(auth_service.PhoneNotRegisteredError)
async def _handle_phone_not_registered(request: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(auth_service.AccountCreationRateLimitedError)
async def _handle_account_creation_rate_limited(request: Request, exc: Exception):
    return JSONResponse(status_code=429, content={"detail": str(exc)})


@app.exception_handler(auth_service.InvalidRefreshTokenError)
async def _handle_invalid_refresh_token(request: Request, exc: Exception):
    return JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(chat_service.PermissionDeniedError)
async def _handle_permission_denied(request: Request, exc: Exception):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(chat_service.TooManyMembersError)
async def _handle_too_many_members(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(chat_service.UserNotFoundError)
async def _handle_user_not_found(request: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(chat_service.OwnershipTransferRequiredError)
async def _handle_ownership_transfer_required(request: Request, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(message_service.NotAParticipantError)
async def _handle_not_a_participant(request: Request, exc: Exception):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(message_service.MessageTooLongError)
async def _handle_message_too_long(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(user_service.UsernameError)
async def _username_error_handler(_, exc: user_service.UsernameError):
    return JSONResponse(status_code=exc.http_status, content={"detail": str(exc), "reason": exc.reason})


@app.exception_handler(SettingsValidationError)
async def _handle_settings_validation(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# --- Object storage (services.storage) ---
@app.exception_handler(MediaValidationError)
async def _handle_media_validation(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(MediaNotFoundError)
async def _handle_media_not_found(request: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(StorageUnavailableError)
async def _handle_storage_unavailable(request: Request, exc: Exception):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

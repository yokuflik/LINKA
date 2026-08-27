import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database.connection import check_database_connection, dispose_engine
from routers.auth import router as auth_router
from routers.chats import router as chats_router
from routers.messages import router as messages_router
from routers.users import router as users_router
from routers.websocket import router as websocket_router
from services import auth_service, chat_service, message_service
from services.redis_client import close_redis
from services.storage import media_service
from services.storage.errors import (
    MediaNotFoundError,
    MediaValidationError,
    StorageUnavailableError,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the media / avatars buckets if they don't exist yet (dev
    # convenience - a no-op against IaC-provisioned production buckets).
    try:
        await media_service.ensure_buckets()
    except Exception as exc:  # storage being down must not stop the app booting
        import logging

        logging.getLogger(__name__).warning("ensure_buckets failed at startup: %s", exc)
    yield
    await dispose_engine()
    await close_redis()


app = FastAPI(lifespan=lifespan)

# Wide open by default (dev/PoC only): a browser blocks every cross-origin
# fetch()/WebSocket without this, and a locally-opened HTML file (file://)
# or anything not served from this exact host:port counts as cross-origin.
# Lock this down to real origins before this is ever public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.exception_handler(auth_service.InvalidOTPError)
async def _handle_invalid_otp(request: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(auth_service.PhoneAlreadyRegisteredError)
async def _handle_phone_already_registered(request: Request, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(auth_service.PhoneNotRegisteredError)
async def _handle_phone_not_registered(request: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


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

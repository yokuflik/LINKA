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


@asynccontextmanager
async def lifespan(app: FastAPI):
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

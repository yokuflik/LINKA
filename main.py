from contextlib import asynccontextmanager

from fastapi import FastAPI

from database.connection import dispose_engine
from routers.websocket import router as websocket_router
from services.redis_client import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await dispose_engine()
    await close_redis()


app = FastAPI(lifespan=lifespan)
app.include_router(websocket_router)

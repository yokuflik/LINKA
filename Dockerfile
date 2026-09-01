# syntax=docker/dockerfile:1

# ---- Stage 1: build an isolated venv with all deps -------------------------
# Wheels + build tools live only here; the final image never sees them.
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# asyncpg / boto3 ship wheels for slim, so no compiler is normally needed.
# build-essential is kept only as a fallback for a source-only transitive
# dep and is dropped with the whole stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- Stage 2: runtime -----------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root runtime user.
RUN useradd --create-home --uid 10001 linka

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=linka:linka . .

USER linka
EXPOSE 8000

# Liveness/readiness: /healthz returns 503 until Postgres answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

# Single process on purpose: the send / fan-out / receipt workers and the
# routing heartbeat run in the app's FastAPI lifespan, so one Uvicorn
# process is the whole system at demo scale (see ADR 0007). No --reload.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]

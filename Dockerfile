# Multi-stage Dockerfile for Tether Agent Runtime & API (Cloud Run ready)
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --upgrade pip && \
    pip install build && \
    pip install ".[dev]"

COPY . .
RUN pip install .

# Production runner image
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN addgroup --system --gid 1001 tether && \
    adduser --system --uid 1001 --gid 1001 tether

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

RUN chown -R tether:tether /app
USER tether

EXPOSE 8000

# Default entrypoint runs API server; can be overridden to run worker CLI: ["python", "-m", "tether.worker"]
CMD ["uvicorn", "tether.api:app", "--host", "0.0.0.0", "--port", "8000"]

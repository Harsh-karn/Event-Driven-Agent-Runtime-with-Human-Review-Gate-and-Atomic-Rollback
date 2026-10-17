"""FastAPI REST Service for Tether runtime: Enqueueing, Webhooks, Health, and Run Status."""

import hmac
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from tether import __version__
from tether.db import (
    close_db_pool,
    enqueue_run,
    get_approval,
    get_completed_steps,
    get_connection,
    get_run,
    get_staged_effects,
    init_db_pool,
)
from tether.models import (
    Approval,
    EnqueueRequest,
    EnqueueResponse,
    Run,
    RunState,
    StagedEffect,
    Step,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle."""
    logger.info("Initializing Tether API service...")
    try:
        await init_db_pool()
    except Exception as e:
        logger.warning(f"Could not connect to database on startup: {e}. Will reconnect on demand.")
    yield
    logger.info("Shutting down Tether API service...")
    await close_db_pool()


app = FastAPI(
    title="Tether Agent Runtime API",
    description="Durable, event-triggered agent execution runtime on Supabase and GCP.",
    version=__version__,
    lifespan=lifespan,
)

# CORS configuration for Review UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    environment: str


class RunDetailResponse(BaseModel):
    run: Run
    steps: list[Step]
    staged_effects: list[StagedEffect]
    approval: Optional[Approval] = None


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Liveness and readiness probe for Cloud Run and load balancers."""
    db_status = "connected"
    try:
        async with get_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1;")
                await cur.fetchone()
    except Exception as e:
        logger.error(f"Health check database failure: {e}")
        db_status = f"degraded ({str(e)})"

    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        version=__version__,
        database=db_status,
        environment=os.getenv("ENVIRONMENT", "development"),
    )


@app.post(
    "/runs",
    response_model=EnqueueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enqueue a new agent execution run",
)
async def create_run(request: EnqueueRequest):
    """Atomically insert a run record and dispatch to the pgmq queue."""
    try:
        response = await enqueue_run(
            agent_id=request.agent_id,
            trigger=request.trigger,
            payload=request.payload,
            dedup_key=request.dedup_key,
        )
        return response
    except Exception as e:
        logger.error(f"Failed to enqueue run: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enqueue agent run: {str(e)}",
        )


@app.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
    summary="Fetch comprehensive run state, checkpointed steps, and staged effects",
)
async def get_run_details(run_id: UUID):
    """Retrieve full audit and execution details for an individual run."""
    run = await get_run(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    steps = await get_completed_steps(run_id)
    staged_effects = await get_staged_effects(run_id)
    approval = await get_approval(run_id)

    return RunDetailResponse(
        run=run,
        steps=steps,
        staged_effects=staged_effects,
        approval=approval,
    )


@app.post(
    "/webhook/{agent_id}",
    response_model=EnqueueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Receive an inbound webhook and trigger an agent run with deduplication",
)
async def receive_webhook(
    agent_id: str,
    payload: dict[str, Any],
    x_event_id: Optional[str] = Header(None, alias="X-Event-ID"),
    x_webhook_signature: Optional[str] = Header(None, alias="X-Webhook-Signature"),
):
    """Validate webhook payload and enqueue with deduplication key."""
    # Optional HMAC SHA256 signature verification if WEBHOOK_SECRET is configured
    webhook_secret = os.getenv("WEBHOOK_SECRET")
    if webhook_secret and x_webhook_signature:
        body_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        computed_signature = hmac.new(
            webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed_signature, x_webhook_signature):
            logger.warning(f"Invalid webhook signature for agent {agent_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook HMAC signature",
            )

    # Use x_event_id for deduplication if provided, or derive from payload
    dedup_key = x_event_id or payload.get("id") or payload.get("event_id")
    if dedup_key:
        dedup_key = f"{agent_id}:{dedup_key}"

    response = await enqueue_run(
        agent_id=agent_id,
        trigger="webhook",
        payload=payload,
        dedup_key=dedup_key,
    )
    return response

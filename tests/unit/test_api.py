"""Unit tests for the Tether FastAPI service."""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from tether.api import app
from tether.models import EnqueueResponse, Run, RunState, StagedEffect, Step, StepStatus, utc_now

client = TestClient(app)


def test_health_check_healthy():
    """Verify /health returns 200 and healthy status."""
    from contextlib import asynccontextmanager

    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = {"?column?": 1}

    mock_conn = AsyncMock()

    @asynccontextmanager
    async def fake_cursor():
        yield mock_cursor

    mock_conn.cursor = fake_cursor

    @asynccontextmanager
    async def fake_get_connection():
        yield mock_conn

    with patch("tether.api.get_connection", side_effect=fake_get_connection):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"


def test_enqueue_run_endpoint():
    """Verify POST /runs successfully enqueues an agent run."""
    run_id = uuid4()
    mock_response = EnqueueResponse(run_id=run_id, is_duplicate=False, state=RunState.QUEUED)

    with patch("tether.api.enqueue_run", new=AsyncMock(return_value=mock_response)):
        payload = {
            "agent_id": "digest_agent",
            "trigger": "api",
            "payload": {"schedule": "hourly"},
        }
        response = client.post("/runs", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["run_id"] == str(run_id)
        assert data["is_duplicate"] is False
        assert data["state"] == "queued"


def test_get_run_details_found():
    """Verify GET /runs/{id} returns run state, steps, and staged effects."""
    run_id = uuid4()
    run = Run(id=run_id, agent_id="triage", state=RunState.AWAITING_APPROVAL)
    step = Step(
        run_id=run_id,
        idx=1,
        kind="model_call",
        idempotency_key="step-key-1",
        status=StepStatus.COMPLETED,
        started_at=utc_now(),
    )
    effect = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="notify",
        params={"channel": "#ops"},
        compensation={"action": "delete"},
        idempotency_key="eff-key-1",
    )

    with (
        patch("tether.api.get_run", new=AsyncMock(return_value=run)),
        patch("tether.api.get_completed_steps", new=AsyncMock(return_value=[step])),
        patch("tether.api.get_staged_effects", new=AsyncMock(return_value=[effect])),
        patch("tether.api.get_approval", new=AsyncMock(return_value=None)),
    ):
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["run"]["id"] == str(run_id)
        assert data["run"]["state"] == "awaiting_approval"
        assert len(data["steps"]) == 1
        assert len(data["staged_effects"]) == 1


def test_get_run_details_not_found():
    """Verify GET /runs/{id} returns 404 for non-existent runs."""
    run_id = uuid4()
    with patch("tether.api.get_run", new=AsyncMock(return_value=None)):
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


def test_webhook_ingest_endpoint():
    """Verify POST /webhook/{agent_id} receives events and sets deduplication key."""
    run_id = uuid4()
    mock_response = EnqueueResponse(run_id=run_id, is_duplicate=False, state=RunState.QUEUED)

    with patch("tether.api.enqueue_run", new=AsyncMock(return_value=mock_response)) as mock_enqueue:
        payload = {"event_id": "evt_12345", "type": "customer.signup", "user_id": 999}
        response = client.post("/webhook/triage_agent", json=payload, headers={"X-Event-ID": "evt_12345"})

        assert response.status_code == 201
        data = response.json()
        assert data["run_id"] == str(run_id)
        mock_enqueue.assert_awaited_once_with(
            agent_id="triage_agent",
            trigger="webhook",
            payload=payload,
            dedup_key="triage_agent:evt_12345",
        )

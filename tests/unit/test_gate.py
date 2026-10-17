"""Unit tests for the Human Review Gate and cryptographic hash verification."""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from tether.api import app
from tether.effects import compute_effects_hash
from tether.gate import EffectsHashMismatchError, ReviewGate
from tether.models import (
    ApprovalDecision,
    Run,
    RunState,
    StagedEffect,
)

client = TestClient(app)


@pytest.mark.asyncio
async def test_approve_run_valid_hash_succeeds():
    """Verify run is approved when provided effects_hash matches exactly."""
    run_id = uuid4()
    run = Run(id=run_id, agent_id="ops_agent", state=RunState.AWAITING_APPROVAL)
    effect = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="post_announcement",
        params={"msg": "Approved launch"},
        compensation={"action": "delete"},
        idempotency_key="key-1",
    )
    valid_hash = compute_effects_hash([effect])

    gate = ReviewGate()

    with (
        patch("tether.gate.get_run", new=AsyncMock(return_value=run)),
        patch("tether.gate.get_staged_effects", new=AsyncMock(return_value=[effect])),
        patch("tether.gate.record_approval", new=AsyncMock(side_effect=lambda a: a)),
        patch("tether.gate.update_run_state", new=AsyncMock()) as mock_update_state,
        patch("tether.gate.emit_event", new=AsyncMock()),
    ):
        approval = await gate.approve_run(
            run_id=run_id,
            decided_by="admin@tether.dev",
            effects_hash=valid_hash,
            reason="Verified and approved",
        )

        assert approval.decision == ApprovalDecision.APPROVED
        assert approval.effects_hash == valid_hash
        assert approval.decided_by == "admin@tether.dev"
        mock_update_state.assert_awaited_once_with(run_id, RunState.APPROVED)


@pytest.mark.asyncio
async def test_approve_run_tampered_hash_raises_mismatch():
    """Verify tampering with effects_hash aborts commit immediately with EffectsHashMismatchError."""
    run_id = uuid4()
    run = Run(id=run_id, agent_id="ops_agent", state=RunState.AWAITING_APPROVAL)
    effect = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="post_announcement",
        params={"msg": "Real message"},
        compensation={"action": "delete"},
        idempotency_key="key-1",
    )

    tampered_hash = "0000000000000000000000000000000000000000000000000000000000000000"

    gate = ReviewGate()

    with (
        patch("tether.gate.get_run", new=AsyncMock(return_value=run)),
        patch("tether.gate.get_staged_effects", new=AsyncMock(return_value=[effect])),
        patch("tether.gate.update_run_state", new=AsyncMock()) as mock_update_state,
    ):
        with pytest.raises(EffectsHashMismatchError) as exc_info:
            await gate.approve_run(
                run_id=run_id,
                decided_by="hacker@tether.dev",
                effects_hash=tampered_hash,
            )

        assert "hash mismatch" in str(exc_info.value).lower()
        mock_update_state.assert_not_called()


@pytest.mark.asyncio
async def test_reject_run_terminates_cleanly():
    """Verify rejection moves run to REJECTED with zero effects committed."""
    run_id = uuid4()
    run = Run(id=run_id, agent_id="ops_agent", state=RunState.AWAITING_APPROVAL)
    effect = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="post",
        params={},
        compensation={},
        idempotency_key="k",
    )

    gate = ReviewGate()

    with (
        patch("tether.gate.get_run", new=AsyncMock(return_value=run)),
        patch("tether.gate.get_staged_effects", new=AsyncMock(return_value=[effect])),
        patch("tether.gate.record_approval", new=AsyncMock(side_effect=lambda a: a)),
        patch("tether.gate.update_run_state", new=AsyncMock()) as mock_update_state,
        patch("tether.gate.emit_event", new=AsyncMock()),
    ):
        approval = await gate.reject_run(
            run_id=run_id,
            decided_by="reviewer@tether.dev",
            reason="Budget exceeded",
        )

        assert approval.decision == ApprovalDecision.REJECTED
        assert approval.reason == "Budget exceeded"
        mock_update_state.assert_awaited_once_with(run_id, RunState.REJECTED, error="Budget exceeded")


def test_api_approve_hash_mismatch_returns_409():
    """Verify HTTP POST /runs/{id}/approve returns 409 Conflict when hash fails."""
    run_id = uuid4()
    with patch(
        "tether.gate.ReviewGate.approve_run",
        side_effect=EffectsHashMismatchError(run_id, "expected", "provided"),
    ):
        response = client.post(
            f"/runs/{run_id}/approve",
            json={"decided_by": "user@tether.dev", "effects_hash": "bad-hash"},
        )
        assert response.status_code == 409
        assert "mismatch" in response.json()["detail"].lower()

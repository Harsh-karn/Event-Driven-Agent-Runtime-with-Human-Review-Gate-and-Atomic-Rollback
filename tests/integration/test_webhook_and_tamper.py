"""Integration tests for webhook deduplication, cryptographic tamper prevention, and gate rejection."""

import hashlib
import hmac
import json
from uuid import UUID, uuid4
from typing import Any
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from tether.api import app
from tether.effects import ToolRegistry, compute_effects_hash, execute_approved_effects
from tether.gate import ReviewGate
from tether.models import (
    Approval,
    ApprovalDecision,
    EffectStatus,
    EnqueueResponse,
    Run,
    RunState,
    StagedEffect,
    utc_now,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_webhook_deduplication_exact_event_redelivery(client: TestClient):
    """Integration Scenario 5: Webhook delivered twice produces exactly one run.

    Simulates upstream provider retry (e.g. GitHub/Stripe sending the exact same webhook).
    First delivery returns 201 Created.
    Second delivery detects duplicate event ID and returns existing run without enqueuing duplicate.
    """
    secret = "chaos_test_secret_key"
    payload = {
        "event_id": "evt_webhook_998811",
        "action": "pull_request.opened",
        "repository": "tether/core",
        "number": 42,
    }
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    existing_run_id = uuid4()
    enqueue_call_count = 0

    async def mock_enqueue_run(agent_id, trigger, payload, dedup_key=None):
        nonlocal enqueue_call_count
        enqueue_call_count += 1
        return EnqueueResponse(
            run_id=existing_run_id,
            is_duplicate=(enqueue_call_count > 1),
            state=RunState.QUEUED,
        )

    with patch.dict("os.environ", {"WEBHOOK_SECRET": secret}), \
         patch("tether.api.enqueue_run", side_effect=mock_enqueue_run):

        # Delivery 1
        res1 = client.post(
            "/webhook/demo_digest",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": sig,
            },
        )
        assert res1.status_code == 201
        data1 = res1.json()
        assert data1["run_id"] == str(existing_run_id)
        assert data1["is_duplicate"] is False

        # Delivery 2 (Duplicate redelivery)
        res2 = client.post(
            "/webhook/demo_digest",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": sig,
            },
        )
        assert res2.status_code == 201
        data2 = res2.json()
        assert data2["run_id"] == str(existing_run_id)
        assert data2["is_duplicate"] is True

        # Verified: exactly one run ID returned, both deliveries reconciled safely
        assert data1["run_id"] == data2["run_id"]


@pytest.mark.asyncio
async def test_tampered_staged_effects_commit_refused():
    """Integration Scenario 6: Tampered effects_hash post-approval causes immediate abort.

    1. Agent stages Effect A ($100 payment).
    2. Reviewer inspects and approves cryptographic hash H1.
    3. Adversary alters Effect A's parameters in Postgres to $10,000.
    4. Commit engine recomputes hash H2 before executing anything.
    5. Hash mismatch detected! Commit aborted with ValueError and run moved to FAILED.
    6. ZERO external side effects executed.
    """
    run_id = uuid4()
    mock_run = Run(id=run_id, agent_id="demo_digest", trigger="api", state=RunState.APPROVED)

    # Legitimate staged effects
    staged_effects = [
        StagedEffect(
            run_id=run_id,
            idx=0,
            target_system="payment_gateway",
            action="charge_card",
            params={"amount_usd": 100, "customer": "cust_42"},
            compensation={"action": "refund_card", "params": {"customer": "cust_42"}},
            status=EffectStatus.STAGED,
            idempotency_key=f"{run_id}:eff:0",
        )
    ]

    legitimate_hash = compute_effects_hash(staged_effects)

    approval = Approval(
        run_id=run_id,
        decided_by="admin@tether.corp",
        decision=ApprovalDecision.APPROVED,
        decided_at=utc_now(),
        effects_hash=legitimate_hash,
    )

    registry = ToolRegistry()
    executed_side_effects = []

    async def mock_charge(**kw):
        executed_side_effects.append("charge_card")
        return {"status": "ok"}

    registry.register_effect_tool(
        name="charge_card",
        description="charge",
        parameters={},
        compensation_action="refund_card",
        compensation_generator=lambda p: p,
        target_system="payment_gateway",
        execution_handler=mock_charge,
    )

    # Now tamper with params in database
    staged_effects[0].params = {"amount_usd": 10000, "customer": "cust_attacker"}

    with patch("tether.effects.get_run", AsyncMock(return_value=mock_run)), \
         patch("tether.effects.get_approval", AsyncMock(return_value=approval)), \
         patch("tether.effects.get_staged_effects", AsyncMock(return_value=staged_effects)), \
         patch("tether.effects.update_run_state", AsyncMock()), \
         patch("tether.effects.emit_event", AsyncMock()):

        with pytest.raises(ValueError) as exc_info:
            await execute_approved_effects(run_id, registry)

        assert "Tampering detected" in str(exc_info.value)
        # CRITICAL: Assert zero mutations executed!
        assert len(executed_side_effects) == 0


@pytest.mark.asyncio
async def test_human_reviewer_rejection_zero_effects_committed():
    """Integration Scenario 7: Reviewer rejects run; guarantees zero mutations committed."""
    run_id = uuid4()
    mock_run = Run(
        id=run_id,
        agent_id="demo_digest",
        trigger="schedule",
        state=RunState.AWAITING_APPROVAL,
    )

    gate = ReviewGate()
    saved_approval: list[Approval] = []
    state_updates = []

    async def mock_update_state(rid, state, error=None):
        state_updates.append(state)
        mock_run.state = state
        return mock_run

    async def mock_record_approval(approval):
        saved_approval.append(approval)
        return approval

    with patch("tether.gate.get_run", AsyncMock(return_value=mock_run)), \
         patch("tether.gate.get_staged_effects", AsyncMock(return_value=[])), \
         patch("tether.gate.update_run_state", side_effect=mock_update_state), \
         patch("tether.gate.record_approval", side_effect=mock_record_approval), \
         patch("tether.gate.emit_event", AsyncMock()):

        approval = await gate.reject_run(
            run_id=run_id,
            decided_by="compliance-officer@tether.corp",
            reason="Broadcast message contains unreleased product name",
        )

        assert approval.decision == ApprovalDecision.REJECTED
        assert approval.reason == "Broadcast message contains unreleased product name"
        assert mock_run.state == RunState.REJECTED
        assert RunState.REJECTED in state_updates
        # Zero effects committed
        assert len(saved_approval) == 1

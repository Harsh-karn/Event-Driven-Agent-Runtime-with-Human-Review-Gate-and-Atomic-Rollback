"""Human Review Gate engine with cryptographic hash validation, Slack alerts, and expiry."""

import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from tether.db import (
    emit_event,
    get_connection,
    get_run,
    get_staged_effects,
    record_approval,
    send_queue_message,
    update_run_state,
)
from tether.effects import compute_effects_hash
from tether.models import (
    Approval,
    ApprovalDecision,
    Event,
    EventType,
    Run,
    RunState,
    StagedEffect,
    utc_now,
)

logger = logging.getLogger(__name__)


class EffectsHashMismatchError(ValueError):
    """Raised when the hash of staged effects presented by the reviewer

    does not match the current cryptographic hash in the database.
    """

    def __init__(self, run_id: UUID, expected_hash: str, provided_hash: str):
        super().__init__(
            f"Run {run_id}: Effects hash mismatch! Provided '{provided_hash}' does not "
            f"match expected '{expected_hash}'. Effects were altered or corrupted."
        )
        self.run_id = run_id
        self.expected_hash = expected_hash
        self.provided_hash = provided_hash


class ApproveRequest(BaseModel):
    """Payload to approve an agent run."""

    decided_by: str
    effects_hash: str
    reason: Optional[str] = None


class RejectRequest(BaseModel):
    """Payload to reject an agent run."""

    decided_by: str
    reason: Optional[str] = None


class ReviewGate:
    """Manages the human review boundary, cryptographic verification, and Slack alerts."""

    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        review_portal_url: Optional[str] = None,
        approval_expiry_seconds: int = 86400,
    ):
        self.slack_webhook_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
        self.review_portal_url = review_portal_url or os.getenv(
            "REVIEW_PORTAL_URL", "http://localhost:3000/review"
        )
        self.approval_expiry_seconds = approval_expiry_seconds

    async def notify_slack(self, run: Run, staged_effects: list[StagedEffect]) -> bool:
        """Send an interactive Slack notification requesting human approval."""
        if not self.slack_webhook_url:
            logger.debug("No SLACK_WEBHOOK_URL configured; skipping Slack notification.")
            return False

        review_link = f"{self.review_portal_url}/{run.id}"
        effects_summary = "\n".join(
            [
                f"• *[{e.target_system.upper()}]* `{e.action}`: {e.params}"
                for e in staged_effects
            ]
        ) or "_No side effects staged._"

        payload: dict[str, Any] = {
            "text": f"🚨 Human Approval Required for Agent Run: `{run.agent_id}`",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🚨 Human Review Gate: Agent Run Approval Required",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Agent:*\n`{run.agent_id}`"},
                        {"type": "mrkdwn", "text": f"*Run ID:*\n`{run.id}`"},
                        {"type": "mrkdwn", "text": f"*Trigger:*\n`{run.trigger}`"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Staged Effects:*\n{len(staged_effects)} mutations",
                        },
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Proposed Side Effects:*\n{effects_summary}",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Review & Decide ↗"},
                            "style": "primary",
                            "url": review_link,
                        }
                    ],
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(self.slack_webhook_url, json=payload)
                return res.status_code == 200
        except Exception as e:
            logger.warning(f"Failed to post Slack notification for run {run.id}: {e}")
            return False

    async def approve_run(
        self,
        run_id: UUID,
        decided_by: str,
        effects_hash: str,
        reason: Optional[str] = None,
    ) -> Approval:
        """Approve an agent run after verifying cryptographic effects_hash."""
        run = await get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} does not exist")

        if run.state != RunState.AWAITING_APPROVAL:
            raise ValueError(
                f"Cannot approve run {run_id} in state '{run.state.value}'. "
                f"Run must be in 'awaiting_approval'."
            )

        # Retrieve current staged effects from database
        staged_effects = await get_staged_effects(run_id)
        expected_hash = compute_effects_hash(staged_effects)

        # Cryptographically verify the hash matches
        if not hmac.compare_digest(expected_hash, effects_hash):
            logger.error(
                f"Run {run_id} APPROVAL REJECTED: hash mismatch. "
                f"Expected '{expected_hash}', provided '{effects_hash}'"
            )
            raise EffectsHashMismatchError(run_id, expected_hash, effects_hash)

        # Record approval
        approval = Approval(
            run_id=run_id,
            decided_by=decided_by,
            decision=ApprovalDecision.APPROVED,
            reason=reason,
            decided_at=utc_now(),
            effects_hash=effects_hash,
        )
        recorded = await record_approval(approval)

        # Advance state to APPROVED
        await update_run_state(run_id, RunState.APPROVED)

        # Telemetry outbox event
        await emit_event(
            Event(
                run_id=run_id,
                type=EventType.APPROVAL_DECISION,
                payload={
                    "decided_by": decided_by,
                    "decision": "approved",
                    "reason": reason,
                    "effects_hash": effects_hash,
                    "effects_count": len(staged_effects),
                },
            )
        )

        logger.info(f"Run {run_id} APPROVED by {decided_by} with verified effects_hash {effects_hash}")

        # Enqueue commit execution into the queue for worker to execute
        try:
            await send_queue_message("agent_runs", {"run_id": str(run_id), "action": "commit"})
            logger.info(f"Dispatched commit job to queue for run {run_id}")
        except Exception as q_err:
            logger.debug(f"Queue dispatch notice for run {run_id}: {q_err}")

        return recorded

    async def reject_run(
        self,
        run_id: UUID,
        decided_by: str,
        reason: Optional[str] = None,
    ) -> Approval:
        """Reject an agent run, terminating it immediately with zero side effects committed."""
        run = await get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} does not exist")

        if run.state != RunState.AWAITING_APPROVAL:
            raise ValueError(
                f"Cannot reject run {run_id} in state '{run.state.value}'. "
                f"Run must be in 'awaiting_approval'."
            )

        staged_effects = await get_staged_effects(run_id)
        current_hash = compute_effects_hash(staged_effects)

        approval = Approval(
            run_id=run_id,
            decided_by=decided_by,
            decision=ApprovalDecision.REJECTED,
            reason=reason or "Rejected by reviewer",
            decided_at=utc_now(),
            effects_hash=current_hash,
        )
        recorded = await record_approval(approval)

        # Transition run directly to REJECTED (terminal state)
        await update_run_state(run_id, RunState.REJECTED, error=reason)

        # Telemetry outbox event
        await emit_event(
            Event(
                run_id=run_id,
                type=EventType.APPROVAL_DECISION,
                payload={
                    "decided_by": decided_by,
                    "decision": "rejected",
                    "reason": reason,
                    "effects_hash": current_hash,
                    "effects_count": len(staged_effects),
                },
            )
        )
        await emit_event(
            Event(
                run_id=run_id,
                type=EventType.RUN_FINISHED,
                payload={"final_state": "rejected", "committed_effects": 0},
            )
        )

        logger.info(f"Run {run_id} REJECTED by {decided_by}. Reason: {reason}. Zero effects committed.")
        return recorded

    async def expire_stale_runs(self, max_age_seconds: Optional[int] = None) -> list[UUID]:
        """Move runs awaiting approval beyond the timeout threshold to FAILED."""
        expiry_seconds = max_age_seconds or self.approval_expiry_seconds
        expired_ids: list[UUID] = []

        async with get_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id FROM runs
                    WHERE state = 'awaiting_approval'
                      AND updated_at < NOW() - (%s * INTERVAL '1 second')
                    FOR UPDATE SKIP LOCKED;
                    """,
                    (expiry_seconds,),
                )
                rows = await cur.fetchall()
                expired_ids = [r["id"] for r in rows]

        for rid in expired_ids:
            logger.warning(f"Run {rid} expired while awaiting approval (> {expiry_seconds}s)")
            await update_run_state(
                rid,
                RunState.FAILED,
                error=f"Approval expired after {expiry_seconds} seconds",
            )
            await emit_event(
                Event(
                    run_id=rid,
                    type=EventType.RUN_FAILED,
                    payload={"reason": "approval_expired", "timeout_seconds": expiry_seconds},
                )
            )

        return expired_ids

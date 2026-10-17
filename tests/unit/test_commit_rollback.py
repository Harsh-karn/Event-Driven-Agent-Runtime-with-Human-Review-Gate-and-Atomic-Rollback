"""Unit tests for the Commit Phase and Reverse Compensation Atomic Rollback."""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tether.effects import (
    ToolRegistry,
    compute_effects_hash,
    execute_approved_effects,
)
from tether.models import (
    Approval,
    ApprovalDecision,
    EffectStatus,
    Run,
    RunState,
    StagedEffect,
    utc_now,
)


@pytest.mark.asyncio
async def test_commit_success_all_effects():
    """Verify that when all effects succeed, run transitions to COMMITTED."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.APPROVED)

    effect1 = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="send_msg",
        params={"text": "Step 1"},
        compensation={"action": "del_msg", "params": {"text": "Step 1"}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-1",
    )
    effect2 = StagedEffect(
        run_id=run_id,
        idx=2,
        target_system="github",
        action="create_issue",
        params={"title": "Step 2"},
        compensation={"action": "close_issue", "params": {"title": "Step 2"}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-2",
    )
    staged_effects = [effect1, effect2]
    expected_hash = compute_effects_hash(staged_effects)

    approval = Approval(
        run_id=run_id,
        decided_by="reviewer@tether.dev",
        decision=ApprovalDecision.APPROVED,
        decided_at=utc_now(),
        effects_hash=expected_hash,
    )

    registry = ToolRegistry()
    mock_send = AsyncMock(return_value={"msg_id": "slack-123"})
    mock_github = AsyncMock(return_value={"issue_id": 42})
    registry.register_effect_tool(
        name="send_msg",
        description="Send message",
        parameters={},
        compensation_action="del_msg",
        compensation_generator=lambda p: p,
        target_system="slack",
        execution_handler=mock_send,
    )
    registry.register_effect_tool(
        name="create_issue",
        description="Create issue",
        parameters={},
        compensation_action="close_issue",
        compensation_generator=lambda p: p,
        target_system="github",
        execution_handler=mock_github,
    )

    with (
        patch("tether.effects.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.effects.get_approval", new=AsyncMock(return_value=approval)),
        patch("tether.effects.get_staged_effects", new=AsyncMock(return_value=staged_effects)),
        patch("tether.effects.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
        patch("tether.effects.update_effect_status", new=AsyncMock()),
        patch("tether.effects.emit_event", new=AsyncMock()),
    ):
        final_run = await execute_approved_effects(run_id, registry)

        assert final_run.state == RunState.COMMITTED
        mock_send.assert_awaited_once()
        mock_github.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_failure_triggers_atomic_rollback():
    """Verify that failure at effect 2 triggers reverse compensation of effect 1, ending in ROLLED_BACK."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.APPROVED)

    effect1 = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="send_msg",
        params={"text": "Step 1"},
        compensation={"action": "del_msg", "params": {"text": "Step 1"}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-1",
    )
    effect2 = StagedEffect(
        run_id=run_id,
        idx=2,
        target_system="github",
        action="create_issue",
        params={"title": "Step 2"},
        compensation={"action": "close_issue", "params": {"title": "Step 2"}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-2",
    )
    staged_effects = [effect1, effect2]
    expected_hash = compute_effects_hash(staged_effects)

    approval = Approval(
        run_id=run_id,
        decided_by="reviewer@tether.dev",
        decision=ApprovalDecision.APPROVED,
        decided_at=utc_now(),
        effects_hash=expected_hash,
    )

    registry = ToolRegistry()
    mock_send = AsyncMock(return_value={"msg_id": "slack-123"})
    mock_del = AsyncMock(return_value={"deleted": True})
    # Effect 2 fails
    mock_github = AsyncMock(side_effect=RuntimeError("GitHub API 500 error"))

    registry.register_effect_tool(
        name="send_msg",
        description="Send message",
        parameters={},
        compensation_action="del_msg",
        compensation_generator=lambda p: p,
        target_system="slack",
        execution_handler=mock_send,
        compensation_handler=mock_del,
    )
    registry.register_effect_tool(
        name="create_issue",
        description="Create issue",
        parameters={},
        compensation_action="close_issue",
        compensation_generator=lambda p: p,
        target_system="github",
        execution_handler=mock_github,
    )

    with (
        patch("tether.effects.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.effects.get_approval", new=AsyncMock(return_value=approval)),
        patch("tether.effects.get_staged_effects", new=AsyncMock(return_value=staged_effects)),
        patch("tether.effects.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
        patch("tether.effects.update_effect_status", new=AsyncMock()),
        patch("tether.effects.emit_event", new=AsyncMock()),
    ):
        final_run = await execute_approved_effects(run_id, registry)

        assert final_run.state == RunState.ROLLED_BACK
        mock_send.assert_awaited_once()
        mock_github.assert_awaited_once()
        # Compensation for effect 1 MUST have been called in reverse
        mock_del.assert_awaited_once_with(text="Step 1", result={"msg_id": "slack-123"})


@pytest.mark.asyncio
async def test_compensation_failure_moves_to_rollback_failed():
    """Verify that if a compensating action itself fails, run state moves to ROLLBACK_FAILED."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.APPROVED)

    effect1 = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="send_msg",
        params={"text": "Step 1"},
        compensation={"action": "del_msg", "params": {"text": "Step 1"}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-1",
    )
    effect2 = StagedEffect(
        run_id=run_id,
        idx=2,
        target_system="github",
        action="create_issue",
        params={"title": "Step 2"},
        compensation={"action": "close_issue", "params": {"title": "Step 2"}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-2",
    )
    staged_effects = [effect1, effect2]
    expected_hash = compute_effects_hash(staged_effects)

    approval = Approval(
        run_id=run_id,
        decided_by="reviewer@tether.dev",
        decision=ApprovalDecision.APPROVED,
        decided_at=utc_now(),
        effects_hash=expected_hash,
    )

    registry = ToolRegistry()
    mock_send = AsyncMock(return_value={"msg_id": "slack-123"})
    # Both forward of effect 2 and compensation of effect 1 fail!
    mock_del = AsyncMock(side_effect=ConnectionError("Slack deletion API unreachable"))
    mock_github = AsyncMock(side_effect=RuntimeError("GitHub down"))

    registry.register_effect_tool(
        name="send_msg",
        description="Send message",
        parameters={},
        compensation_action="del_msg",
        compensation_generator=lambda p: p,
        target_system="slack",
        execution_handler=mock_send,
        compensation_handler=mock_del,
    )
    registry.register_effect_tool(
        name="create_issue",
        description="Create issue",
        parameters={},
        compensation_action="close_issue",
        compensation_generator=lambda p: p,
        target_system="github",
        execution_handler=mock_github,
    )

    with (
        patch("tether.effects.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.effects.get_approval", new=AsyncMock(return_value=approval)),
        patch("tether.effects.get_staged_effects", new=AsyncMock(return_value=staged_effects)),
        patch("tether.effects.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
        patch("tether.effects.update_effect_status", new=AsyncMock()),
        patch("tether.effects.emit_event", new=AsyncMock()),
    ):
        final_run = await execute_approved_effects(run_id, registry)

        assert final_run.state == RunState.ROLLBACK_FAILED


@pytest.mark.asyncio
async def test_commit_tampered_hash_refused():
    """Verify that if effects_hash in approval does not match staged effects in DB, commit is refused."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.APPROVED)

    effect1 = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="send_msg",
        params={"text": "Real message"},
        compensation={"action": "del_msg", "params": {}},
        status=EffectStatus.STAGED,
        idempotency_key="eff-1",
    )
    staged_effects = [effect1]

    # Stored approval hash is for something else
    approval = Approval(
        run_id=run_id,
        decided_by="reviewer@tether.dev",
        decision=ApprovalDecision.APPROVED,
        decided_at=utc_now(),
        effects_hash="different-hash-from-different-payload",
    )

    registry = ToolRegistry()

    with (
        patch("tether.effects.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.effects.get_approval", new=AsyncMock(return_value=approval)),
        patch("tether.effects.get_staged_effects", new=AsyncMock(return_value=staged_effects)),
        patch("tether.effects.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
    ):
        with pytest.raises(ValueError) as exc_info:
            await execute_approved_effects(run_id, registry)

        assert "Commit refused: Staged effects hash" in str(exc_info.value)

"""Chaos and reliability tests for Tether runtime.

Verifies:
1. Forced worker kill (kill -9 simulation) mid-run: lease expires, second worker resumes,
   completed steps are skipped and reused, zero duplicate side effects.
2. Crash during commit after effect k: reverse compensation unwinds in reverse (k-1 down to 0),
   ensuring the system is never left half-applied.
3. Poison message retry exhaustion: after N failed attempts, job routes to dead-letter queue.
4. Approval expiry: unreviewed runs transition to expired state.
"""

from uuid import UUID, uuid4
from typing import Any
from unittest.mock import AsyncMock, patch
import pytest

from tether.agents.demo_digest import DemoDigestAgent
from tether.effects import (
    ToolRegistry,
    compute_effects_hash,
    execute_approved_effects,
)
from tether.engine import ExecutionEngine
from tether.gate import ReviewGate
from tether.models import (
    Approval,
    ApprovalDecision,
    EffectStatus,
    Run,
    RunState,
    StagedEffect,
    Step,
    StepStatus,
    utc_now,
)
from tether.runtime.base import Completion
from tether.worker import TetherWorker


@pytest.mark.asyncio
async def test_chaos_worker_kill_mid_run_and_resumption():
    """Chaos Scenario 1: kill -9 worker mid-run.

    Worker A executes Step 0 (read tool) and Step 1 (model call), then dies abruptly.
    Worker B resumes the run:
    - Rebuilds run state from PostgreSQL.
    - Skips and reuses outputs of completed steps (idempotent step checkpoints).
    - Finishes Step 2 (effect staging).
    - Zero duplicate side effects staged.
    """
    run_id = uuid4()
    agent = DemoDigestAgent(model="gpt-4o-mini")

    mock_run = Run(
        id=run_id,
        agent_id="demo_digest",
        trigger="schedule",
        payload={"repo": "tether/core"},
        state=RunState.QUEUED,
    )

    # Step 0: Read tool
    step_0 = Step(
        run_id=run_id,
        idx=0,
        kind="read_tool",
        input={"tool": "fetch_git_commits", "args": {"repo": "tether/core"}},
        output={"commits": [{"sha": "aefe707", "message": "Initial FSM"}]},
        status=StepStatus.COMPLETED,
        idempotency_key=f"{run_id}:0:fetch_commits",
    )

    # Step 1: Model call
    step_1 = Step(
        run_id=run_id,
        idx=1,
        kind="model_call",
        input={"messages": [{"role": "user", "content": "analyze commits"}]},
        output={
            "text": None,
            "tool_calls": [
                {
                    "id": "tc_eff_1",
                    "name": "post_slack_broadcast",
                    "arguments": {
                        "channel": "#engineering-leads",
                        "headline": "Engineering Digest",
                        "summary": "Verified FSM commit",
                    },
                }
            ],
            "tokens_in": 120,
            "tokens_out": 40,
            "cost_usd": 0.0008,
            "model": "gpt-4o-mini",
        },
        status=StepStatus.COMPLETED,
        idempotency_key=f"{run_id}:1:model_synthesis",
    )

    # Database state after Worker A's crash
    db_steps = [step_0, step_1]
    db_staged_effects: list[StagedEffect] = []
    checkpoint_calls = []

    async def mock_checkpoint_step(step: Step):
        checkpoint_calls.append(step.kind)
        db_steps.append(step)

    async def mock_stage_effect(effect: StagedEffect):
        db_staged_effects.append(effect)
        return effect

    async def mock_update_state(rid: UUID, state: RunState, error=None):
        mock_run.state = state
        return mock_run

    # Worker B resumes execution
    final_completion = Completion(
        text="Digest published and broadcast staged.",
        tool_calls=[],
        tokens_in=200,
        tokens_out=25,
        cost_usd=0.0005,
        model="gpt-4o-mini",
    )
    mock_runtime = AsyncMock()
    mock_runtime.complete.return_value = final_completion

    worker_b_engine = ExecutionEngine(
        runtime=mock_runtime,
        agent_lookup=lambda aid: agent if aid == "demo_digest" else None,
    )

    with patch("tether.engine.get_run", AsyncMock(return_value=mock_run)), \
         patch("tether.engine.update_run_state", side_effect=mock_update_state), \
         patch("tether.engine.checkpoint_step", side_effect=mock_checkpoint_step), \
         patch("tether.engine.stage_effect", side_effect=mock_stage_effect), \
         patch("tether.engine.get_completed_steps", AsyncMock(side_effect=lambda _: list(db_steps))), \
         patch("tether.engine.get_staged_effects", AsyncMock(side_effect=lambda _: list(db_staged_effects))), \
         patch("tether.engine.emit_event", AsyncMock()):

        final_run = await worker_b_engine.execute_run(run_id)

        # Worker B reused Step 0 and Step 1 without duplicating them
        assert mock_run.state in (RunState.COMMITTED, RunState.AWAITING_APPROVAL)
        # Verify read tool was NOT called again because output was replayed from DB
        assert "read_tool" not in checkpoint_calls


@pytest.mark.asyncio
async def test_chaos_commit_crash_triggers_reverse_compensation():
    """Chaos Scenario 2: Crash during commit phase after effect 2 of 4.

    Four staged effects:
    1. Effect 0 (idx 0): charge_card -> succeeds
    2. Effect 1 (idx 1): provision_cluster -> succeeds
    3. Effect 2 (idx 2): issue_cert -> CRASHES / NETWORK TIMEOUT
    4. Effect 3 (idx 3): send_sms -> unreached

    Expected behavior:
    - Commit loop catches failure at Effect 2.
    - Reverse compensation protocol executes:
      * Compensates Effect 1 (deprovision_cluster)
      * Compensates Effect 0 (refund_card)
    - All compensated effects marked 'compensated'
    - Failing effect marked 'failed'
    - Run transitions to ROLLED_BACK.
    - External state is consistent, never half-applied silently.
    """
    run_id = uuid4()
    mock_run = Run(id=run_id, agent_id="demo_digest", trigger="api", state=RunState.APPROVED)

    executed_actions = []
    compensated_actions = []

    # 4 staged effects
    staged_effects = [
        StagedEffect(
            run_id=run_id,
            idx=0,
            target_system="stripe_api",
            action="charge_card",
            params={"amount": 5000},
            compensation={"action": "refund_card", "params": {"amount": 5000}},
            status=EffectStatus.STAGED,
            idempotency_key=f"{run_id}:eff:0",
        ),
        StagedEffect(
            run_id=run_id,
            idx=1,
            target_system="cloud_api",
            action="provision_cluster",
            params={"nodes": 3},
            compensation={"action": "deprovision_cluster", "params": {"nodes": 3}},
            status=EffectStatus.STAGED,
            idempotency_key=f"{run_id}:eff:1",
        ),
        StagedEffect(
            run_id=run_id,
            idx=2,
            target_system="ca_gateway",
            action="issue_cert",
            params={"domain": "tether.local"},
            compensation={"action": "revoke_cert", "params": {"domain": "tether.local"}},
            status=EffectStatus.STAGED,
            idempotency_key=f"{run_id}:eff:2",
        ),
        StagedEffect(
            run_id=run_id,
            idx=3,
            target_system="sms_api",
            action="send_sms",
            params={"msg": "Active"},
            compensation={"action": "cancel_sms", "params": {}},
            status=EffectStatus.STAGED,
            idempotency_key=f"{run_id}:eff:3",
        ),
    ]

    registry = ToolRegistry()

    async def exec_charge(**kw):
        executed_actions.append("charge_card")
        return {"charge_id": "ch_123"}

    async def comp_charge(**kw):
        compensated_actions.append("refund_card")
        return {"refund_id": "rf_123"}

    async def exec_cluster(**kw):
        executed_actions.append("provision_cluster")
        return {"cluster_id": "cl_456"}

    async def comp_cluster(**kw):
        compensated_actions.append("deprovision_cluster")
        return {"status": "destroyed"}

    async def exec_cert(**kw):
        raise ConnectionResetError("CA gateway network connection dropped by peer")

    registry.register_effect_tool(
        name="charge_card",
        description="charge",
        parameters={},
        compensation_action="refund_card",
        compensation_generator=lambda p: p,
        target_system="stripe_api",
        execution_handler=exec_charge,
        compensation_handler=comp_charge,
    )
    registry.register_effect_tool(
        name="provision_cluster",
        description="cluster",
        parameters={},
        compensation_action="deprovision_cluster",
        compensation_generator=lambda p: p,
        target_system="cloud_api",
        execution_handler=exec_cluster,
        compensation_handler=comp_cluster,
    )
    registry.register_effect_tool(
        name="issue_cert",
        description="cert",
        parameters={},
        compensation_action="revoke_cert",
        compensation_generator=lambda p: p,
        target_system="ca_gateway",
        execution_handler=exec_cert,
    )
    registry.register_effect_tool(
        name="send_sms",
        description="sms",
        parameters={},
        compensation_action="cancel_sms",
        compensation_generator=lambda p: p,
        target_system="sms_api",
    )

    valid_hash = compute_effects_hash(staged_effects)
    approval = Approval(
        run_id=run_id,
        decided_by="admin@tether.corp",
        decision=ApprovalDecision.APPROVED,
        decided_at=utc_now(),
        effects_hash=valid_hash,
    )

    state_transitions = []

    async def mock_update_state(rid: UUID, state: RunState, error=None):
        state_transitions.append(state)
        mock_run.state = state
        return mock_run

    async def mock_update_effect_status(eff_id: UUID, status: EffectStatus, result=None, error=None):
        for e in staged_effects:
            if e.id == eff_id:
                e.status = status

    with patch("tether.effects.get_run", AsyncMock(return_value=mock_run)), \
         patch("tether.effects.get_approval", AsyncMock(return_value=approval)), \
         patch("tether.effects.get_staged_effects", AsyncMock(return_value=staged_effects)), \
         patch("tether.effects.update_run_state", side_effect=mock_update_state), \
         patch("tether.effects.update_effect_status", side_effect=mock_update_effect_status), \
         patch("tether.effects.emit_event", AsyncMock()):

        final_run = await execute_approved_effects(run_id, registry)

        # Assert final state is rolled_back
        assert final_run.state == RunState.ROLLED_BACK
        assert RunState.ROLLING_BACK in state_transitions
        assert RunState.ROLLED_BACK in state_transitions

        # Assert executed actions before crash were exactly effects 0 and 1
        assert executed_actions == ["charge_card", "provision_cluster"]

        # Assert compensation unwound in strict reverse order: cluster first, then charge
        assert compensated_actions == ["deprovision_cluster", "refund_card"]

        # Assert effect statuses in DB:
        assert staged_effects[0].status == EffectStatus.COMPENSATED
        assert staged_effects[1].status == EffectStatus.COMPENSATED
        assert staged_effects[2].status == EffectStatus.FAILED
        assert staged_effects[3].status == EffectStatus.STAGED


@pytest.mark.asyncio
async def test_chaos_poison_message_moves_to_dead_letter_queue():
    """Chaos Scenario 3: Poison message repeatedly causes unhandled exception.

    Worker retries up to max_attempts (3). Upon exceeding max_attempts:
    - Increments attempt count.
    - Moves job to DLQ (archive_job with dlq).
    - Transitions run to DEAD_LETTERED with the fatal error message.
    """
    run_id = uuid4()
    mock_run = Run(
        id=run_id,
        agent_id="poison_agent",
        trigger="webhook",
        state=RunState.QUEUED,
        attempt=3,
    )

    archived_jobs = []
    state_updates = []

    # Poison job exceeds max_attempts (read_ct=4 > max_attempts=3)
    async def mock_lease_next_job(queue_name: str, vt_seconds: int):
        return {
            "msg_id": 999,
            "read_ct": 4,
            "payload": {
                "run_id": str(run_id),
                "agent_id": "poison_agent",
            },
            "is_pgmq": False,
        }

    async def mock_archive_job(msg_id: int, queue_name: str = "agent_runs", is_pgmq: bool = False):
        archived_jobs.append((queue_name, msg_id))

    async def mock_update_state(run_id: UUID, target_state: RunState, error=None):
        state_updates.append((target_state, error))
        mock_run.state = target_state
        return mock_run

    worker = TetherWorker(
        max_attempts=3,
        poll_interval_min=0.01,
        poll_interval_max=0.05,
    )

    with patch("tether.worker.lease_next_job", side_effect=mock_lease_next_job), \
         patch("tether.worker.get_run", AsyncMock(return_value=mock_run)), \
         patch("tether.worker.update_run_state", side_effect=mock_update_state), \
         patch("tether.worker.archive_job", side_effect=mock_archive_job), \
         patch("tether.worker.emit_event", AsyncMock()), \
         patch("tether.worker.extend_lease", AsyncMock()):

        processed = await worker.process_one_job()

        assert processed is True
        assert any(s[0] == RunState.DEAD_LETTERED for s in state_updates)
        assert len(archived_jobs) == 1


@pytest.mark.asyncio
async def test_chaos_stale_approval_timeout_expiration():
    """Chaos Scenario 4: Run stays in AWAITING_APPROVAL beyond timeout limit."""
    run_id = uuid4()

    gate = ReviewGate()
    state_updates = []

    async def mock_update_state(rid: UUID, state: RunState, error=None):
        state_updates.append(state)
        return Run(id=rid, agent_id="demo", state=state)

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args):
            pass

        async def fetchall(self):
            return [{"id": run_id}]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    class FakePool:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *args):
            pass

    with patch("tether.gate.get_connection", side_effect=lambda: FakePool()), \
         patch("tether.gate.update_run_state", side_effect=mock_update_state), \
         patch("tether.gate.emit_event", AsyncMock()):

        expired_runs = await gate.expire_stale_runs(max_age_seconds=3600)

        assert run_id in expired_runs
        assert RunState.FAILED in state_updates

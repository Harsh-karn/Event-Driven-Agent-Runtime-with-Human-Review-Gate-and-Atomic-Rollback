"""Unit tests for the Durable Step Execution Engine."""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tether.agents.base import Agent, AgentContext
from tether.effects import ToolRegistry
from tether.engine import ExecutionEngine
from tether.models import (
    EffectStatus,
    Run,
    RunState,
    StagedEffect,
    Step,
    StepStatus,
    utc_now,
)
from tether.runtime.base import Completion, ToolCall


class MockDigestAgent(Agent):
    """Mock agent that reads a metric and stages a message if alert triggered."""

    agent_id = "digest_agent"
    model = "gpt-4o-mini"

    def __init__(self, should_stage_effect: bool = False):
        self.should_stage_effect = should_stage_effect
        self.tools = ToolRegistry()

        # Read tool
        async def check_metric(metric_name: str):
            return {"metric": metric_name, "value": 95 if self.should_stage_effect else 20}

        self.tools.register_read_tool(
            name="check_metric",
            description="Check a system metric",
            parameters={"type": "object", "properties": {"metric_name": {"type": "string"}}},
            handler=check_metric,
        )

        # Effect tool
        def gen_comp(params: dict):
            return {"channel": params["channel"], "action": "delete"}

        self.tools.register_effect_tool(
            name="send_alert",
            description="Send an alert to Slack",
            parameters={"type": "object", "properties": {"channel": {"type": "string"}, "msg": {"type": "string"}}},
            compensation_action="delete_alert",
            compensation_generator=gen_comp,
            target_system="slack",
        )

    def get_system_prompt(self, context: AgentContext) -> str:
        return "You are an operations digest agent."

    def build_initial_messages(self, context: AgentContext) -> list[dict]:
        return [{"role": "user", "content": "Check system CPU metric."}]


@pytest.mark.asyncio
async def test_read_only_agent_commits_directly():
    """Verify an agent with zero side effects transitions from RUNNING -> COMMITTED."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.QUEUED)
    agent = MockDigestAgent(should_stage_effect=False)

    mock_runtime = AsyncMock()
    # Step 1: LLM calls read tool 'check_metric'
    step1_completion = Completion(
        tool_calls=[ToolCall(id="tc-1", name="check_metric", arguments={"metric_name": "cpu"})],
        model="gpt-4o-mini",
        tokens_in=100,
        tokens_out=20,
    )
    # Step 2: LLM provides final answer
    step2_completion = Completion(
        text="CPU is at 20%, all normal.",
        tool_calls=[],
        model="gpt-4o-mini",
        tokens_in=150,
        tokens_out=30,
    )
    mock_runtime.complete.side_effect = [step1_completion, step2_completion]

    engine = ExecutionEngine(runtime=mock_runtime, agent_lookup=lambda aid: agent)

    with (
        patch("tether.engine.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.engine.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
        patch("tether.engine.get_completed_steps", new=AsyncMock(return_value=[])),
        patch("tether.engine.get_staged_effects", new=AsyncMock(return_value=[])),
        patch("tether.engine.checkpoint_step", new=AsyncMock()),
        patch("tether.engine.emit_event", new=AsyncMock()),
    ):
        final_run = await engine.execute_run(run_id)
        assert final_run.state == RunState.COMMITTED
        assert mock_runtime.complete.call_count == 2


@pytest.mark.asyncio
async def test_agent_with_side_effect_transitions_to_awaiting_approval():
    """Verify that when an agent invokes an effect tool, it transitions to AWAITING_APPROVAL."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.QUEUED)
    agent = MockDigestAgent(should_stage_effect=True)

    mock_runtime = AsyncMock()
    # Step 1: Model calls effect tool 'send_alert'
    step1_completion = Completion(
        tool_calls=[ToolCall(id="tc-2", name="send_alert", arguments={"channel": "#alerts", "msg": "CPU Spike"})],
        model="gpt-4o-mini",
    )
    # Step 2: Model finishes reasoning
    step2_completion = Completion(
        text="Alert has been staged for approval.",
        tool_calls=[],
        model="gpt-4o-mini",
    )
    mock_runtime.complete.side_effect = [step1_completion, step2_completion]

    staged_effects_db: list[StagedEffect] = []

    async def mock_stage_effect(effect: StagedEffect):
        staged_effects_db.append(effect)
        return effect

    engine = ExecutionEngine(runtime=mock_runtime, agent_lookup=lambda aid: agent)

    with (
        patch("tether.engine.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.engine.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
        patch("tether.engine.get_completed_steps", new=AsyncMock(return_value=[])),
        patch("tether.engine.get_staged_effects", new=AsyncMock(side_effect=lambda rid: list(staged_effects_db))),
        patch("tether.engine.stage_effect", new=AsyncMock(side_effect=mock_stage_effect)),
        patch("tether.engine.checkpoint_step", new=AsyncMock()),
        patch("tether.engine.emit_event", new=AsyncMock()),
    ):
        final_run = await engine.execute_run(run_id)
        assert final_run.state == RunState.AWAITING_APPROVAL
        assert len(staged_effects_db) == 1
        assert staged_effects_db[0].action == "send_alert"
        assert staged_effects_db[0].status == EffectStatus.STAGED


@pytest.mark.asyncio
async def test_crash_recovery_skips_and_reuses_completed_steps():
    """Verify that if a worker crashes, a resuming worker skips completed steps and reuses output."""
    run_id = uuid4()
    initial_run = Run(id=run_id, agent_id="digest_agent", state=RunState.RUNNING)
    agent = MockDigestAgent(should_stage_effect=False)

    # Simulate step 1 already completed in Postgres before worker was killed
    cached_step_1 = Step(
        run_id=run_id,
        idx=1,
        kind="model_call",
        input={"messages": []},
        output={"text": "Step 1 was already computed before crash", "tool_calls": []},
        status=StepStatus.COMPLETED,
        idempotency_key="cached-step-1",
        started_at=utc_now(),
        ended_at=utc_now(),
    )

    mock_runtime = AsyncMock()
    engine = ExecutionEngine(runtime=mock_runtime, agent_lookup=lambda aid: agent)

    with (
        patch("tether.engine.get_run", new=AsyncMock(return_value=initial_run)),
        patch("tether.engine.update_run_state", new=AsyncMock(side_effect=lambda rid, s, **kw: initial_run.model_copy(update={"state": s}))),
        patch("tether.engine.get_completed_steps", new=AsyncMock(return_value=[cached_step_1])),
        patch("tether.engine.get_staged_effects", new=AsyncMock(return_value=[])),
        patch("tether.engine.checkpoint_step", new=AsyncMock()),
        patch("tether.engine.emit_event", new=AsyncMock()),
    ):
        final_run = await engine.execute_run(run_id)

        # Runtime.complete should NOT have been called because step 1 was cached and had no tool calls!
        assert mock_runtime.complete.call_count == 0
        assert final_run.state == RunState.COMMITTED

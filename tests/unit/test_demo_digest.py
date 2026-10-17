"""Unit tests for the concrete DemoDigestAgent."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch
import pytest

from tether.agents.base import AgentContext
from tether.agents.demo_digest import DemoDigestAgent
from tether.effects import ToolCategory
from tether.engine import ExecutionEngine
from tether.models import Run, RunState
from tether.runtime.base import Completion, ToolCall


@pytest.fixture
def digest_agent() -> DemoDigestAgent:
    return DemoDigestAgent(model="gpt-4o-mini")


def test_demo_digest_tools_registration(digest_agent: DemoDigestAgent):
    """Verify tool categories, compensation bindings, and schemas."""
    # Check Read Tools
    commits_tool = digest_agent.tools.get_tool("fetch_git_commits")
    assert commits_tool is not None
    assert commits_tool.category == ToolCategory.READ
    assert commits_tool.target_system == "git_provider"

    metrics_tool = digest_agent.tools.get_tool("fetch_system_metrics")
    assert metrics_tool is not None
    assert metrics_tool.category == ToolCategory.READ

    # Check Effect Tools
    slack_tool = digest_agent.tools.get_tool("post_slack_broadcast")
    assert slack_tool is not None
    assert slack_tool.category == ToolCategory.EFFECT
    assert slack_tool.compensation_action == "delete_slack_broadcast"
    assert slack_tool.target_system == "slack_webhook"

    tag_tool = digest_agent.tools.get_tool("tag_git_release")
    assert tag_tool is not None
    assert tag_tool.category == ToolCategory.EFFECT
    assert tag_tool.compensation_action == "delete_git_release_tag"

    # Verify tool schemas generated for model completion
    schemas = digest_agent.tools.get_all_tool_schemas()
    assert len(schemas) == 4
    tool_names = {s["function"]["name"] for s in schemas}
    assert "fetch_git_commits" in tool_names
    assert "post_slack_broadcast" in tool_names


@pytest.mark.asyncio
async def test_demo_digest_read_tools_execution(digest_agent: DemoDigestAgent):
    """Verify read tools execute immediately and return structured data."""
    read_handler = digest_agent.tools._read_handlers["fetch_git_commits"]
    result = await read_handler(repo="tether/core", since_hours=12)
    assert result["repo"] == "tether/core"
    assert len(result["commits"]) == 3
    assert result["commits"][0]["sha"] == "aefe707"

    metrics_handler = digest_agent.tools._read_handlers["fetch_system_metrics"]
    metrics = await metrics_handler(environment="production")
    assert metrics["environment"] == "production"
    assert metrics["uptime_percent"] == 99.98


def test_demo_digest_compensation_generators(digest_agent: DemoDigestAgent):
    """Verify reverse compensation generators generate valid rollback payloads."""
    slack_comp_gen = digest_agent.tools._compensation_generators["post_slack_broadcast"]
    slack_params = {"channel": "#engineering-announcements", "headline": "Daily", "summary": "Text"}
    slack_comp = slack_comp_gen(slack_params)
    assert slack_comp["channel"] == "#engineering-announcements"
    assert "lookup_key" in slack_comp

    git_comp_gen = digest_agent.tools._compensation_generators["tag_git_release"]
    git_params = {"repo": "tether/core", "tag": "v1.0.0", "commit_sha": "aefe707"}
    git_comp = git_comp_gen(git_params)
    assert git_comp["repo"] == "tether/core"
    assert git_comp["tag"] == "v1.0.0"


@pytest.mark.asyncio
async def test_demo_digest_end_to_end_engine_run(digest_agent: DemoDigestAgent):
    """Test full execution of DemoDigestAgent through ExecutionEngine.

    Verifies:
    1. Read tool is invoked and output checkpointed.
    2. Effect tool is staged into staged_effects and NOT executed.
    3. Run transitions to AWAITING_APPROVAL.
    """
    run_id = uuid.uuid4()
    mock_run = Run(
        id=run_id,
        agent_id="demo_digest",
        trigger="schedule",
        payload={"repo": "tether/core", "period": "daily"},
        state=RunState.QUEUED,
    )

    # Step 1: Model requests read tool fetch_git_commits
    completion_1 = Completion(
        text=None,
        tool_calls=[
            ToolCall(
                id="call_read_1",
                name="fetch_git_commits",
                arguments={"repo": "tether/core"},
            )
        ],
        tokens_in=100,
        tokens_out=25,
        cost_usd=0.0005,
        model="gpt-4o-mini",
    )

    # Step 2: Model requests effect tool post_slack_broadcast
    completion_2 = Completion(
        text=None,
        tool_calls=[
            ToolCall(
                id="call_eff_1",
                name="post_slack_broadcast",
                arguments={
                    "channel": "#engineering-leads",
                    "headline": "Engineering Digest",
                    "summary": "3 critical features committed.",
                },
            )
        ],
        tokens_in=250,
        tokens_out=60,
        cost_usd=0.0015,
        model="gpt-4o-mini",
    )

    # Step 3: Model finishes reasoning
    completion_3 = Completion(
        text="Digest compiled and broadcast staged for human approval.",
        tool_calls=[],
        tokens_in=350,
        tokens_out=20,
        cost_usd=0.001,
        model="gpt-4o-mini",
    )

    mock_runtime = AsyncMock()
    mock_runtime.complete.side_effect = [completion_1, completion_2, completion_3]

    engine = ExecutionEngine(
        runtime=mock_runtime,
        agent_lookup=lambda aid: digest_agent if aid == "demo_digest" else None,
    )

    checkpointed_steps = []
    staged_effects_db = []

    async def mock_checkpoint(step):
        checkpointed_steps.append(step)

    async def mock_stage_effect(effect):
        staged_effects_db.append(effect)
        return effect

    async def mock_update_state(rid, new_state, error=None):
        mock_run.state = new_state
        return mock_run

    with patch("tether.engine.get_run", AsyncMock(return_value=mock_run)), \
         patch("tether.engine.update_run_state", side_effect=mock_update_state), \
         patch("tether.engine.checkpoint_step", side_effect=mock_checkpoint), \
         patch("tether.engine.stage_effect", side_effect=mock_stage_effect), \
         patch("tether.engine.get_completed_steps", AsyncMock(return_value=[])), \
         patch("tether.engine.get_staged_effects", AsyncMock(side_effect=lambda _: list(staged_effects_db))), \
         patch("tether.engine.emit_event", AsyncMock()):

        result_run = await engine.execute_run(run_id)

        # Verify final state is awaiting approval because side effects were staged
        assert result_run.state == RunState.AWAITING_APPROVAL

        # Verify effect tool was quarantined in staged_effects
        assert len(staged_effects_db) == 1
        staged = staged_effects_db[0]
        assert staged.action == "post_slack_broadcast"
        assert staged.target_system == "slack_webhook"
        assert staged.compensation["action"] == "delete_slack_broadcast"
        assert staged.status == "staged"

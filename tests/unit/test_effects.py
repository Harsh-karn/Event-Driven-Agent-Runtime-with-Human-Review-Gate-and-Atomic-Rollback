"""Unit tests for tool partitioning, effect staging, and effects_hash verification."""

import pytest
from uuid import uuid4

from tether.effects import (
    ToolCategory,
    ToolRegistry,
    compute_effect_idempotency_key,
    compute_effects_hash,
    compute_step_idempotency_key,
)
from tether.models import StagedEffect


@pytest.mark.asyncio
async def test_read_tool_immediate_execution():
    """Verify read tools execute immediately without creating staged effects."""
    registry = ToolRegistry()

    async def fetch_summary(topic: str):
        return f"Summary of {topic}: all systems operational."

    registry.register_read_tool(
        name="get_summary",
        description="Fetch operational summary",
        parameters={"type": "object", "properties": {"topic": {"type": "string"}}},
        handler=fetch_summary,
    )

    run_id = uuid4()
    output, staged_effect = await registry.execute_or_stage(
        name="get_summary",
        arguments={"topic": "database"},
        run_id=run_id,
        next_effect_idx=0,
    )

    assert staged_effect is None
    assert output["status"] == "success"
    assert output["result"] == "Summary of database: all systems operational."


@pytest.mark.asyncio
async def test_effect_tool_staging_without_execution():
    """Verify effect tools are intercepted, staged with compensation, and never executed."""
    registry = ToolRegistry()

    def generate_delete_compensation(params: dict):
        return {"channel": params["channel"], "delete_reason": "Rollback"}

    registry.register_effect_tool(
        name="send_slack_message",
        description="Send an announcement to a Slack channel",
        parameters={
            "type": "object",
            "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
        },
        compensation_action="delete_slack_message",
        compensation_generator=generate_delete_compensation,
        target_system="slack",
    )

    run_id = uuid4()
    output, staged_effect = await registry.execute_or_stage(
        name="send_slack_message",
        arguments={"channel": "#alerts", "text": "Deploying release v2.0"},
        run_id=run_id,
        next_effect_idx=1,
    )

    # Must produce a StagedEffect
    assert staged_effect is not None
    assert staged_effect.idx == 1
    assert staged_effect.target_system == "slack"
    assert staged_effect.action == "send_slack_message"
    assert staged_effect.params == {"channel": "#alerts", "text": "Deploying release v2.0"}
    assert staged_effect.compensation == {
        "action": "delete_slack_message",
        "target_system": "slack",
        "params": {"channel": "#alerts", "delete_reason": "Rollback"},
    }
    assert staged_effect.idempotency_key is not None

    # LLM receives a synthetic staging notification
    assert output["status"] == "staged"
    assert output["staged_idx"] == 1
    assert "will not take effect until human review" in output["message"]


def test_effects_hash_determinism_and_tampering():
    """Verify effects_hash is deterministic and detects any payload tampering."""
    run_id = uuid4()

    effect1 = StagedEffect(
        run_id=run_id,
        idx=1,
        target_system="slack",
        action="send_slack_message",
        params={"channel": "#prod", "text": "Deploying"},
        compensation={"action": "delete_slack_message", "params": {}},
        idempotency_key="key1",
    )

    effect2 = StagedEffect(
        run_id=run_id,
        idx=2,
        target_system="github",
        action="merge_pull_request",
        params={"pr_id": 42},
        compensation={"action": "revert_pull_request", "params": {}},
        idempotency_key="key2",
    )

    hash_original = compute_effects_hash([effect1, effect2])
    # Hash must be reproducible regardless of input order
    hash_reversed = compute_effects_hash([effect2, effect1])
    assert hash_original == hash_reversed

    # Tampering: attacker changes PR ID from 42 to 43
    tampered_effect2 = effect2.model_copy(update={"params": {"pr_id": 43}})
    hash_tampered = compute_effects_hash([effect1, tampered_effect2])

    assert hash_tampered != hash_original


def test_idempotency_keys():
    """Verify reproducible yet distinct idempotency keys."""
    run_id = uuid4()
    step_key1 = compute_step_idempotency_key(run_id, idx=1, input_payload={"query": "test"})
    step_key2 = compute_step_idempotency_key(run_id, idx=1, input_payload={"query": "test"})
    assert step_key1 == step_key2

    step_key3 = compute_step_idempotency_key(run_id, idx=2, input_payload={"query": "test"})
    assert step_key1 != step_key3

    eff_key = compute_effect_idempotency_key(run_id, idx=1, action="send", params={"msg": "hi"})
    assert len(eff_key) == 64

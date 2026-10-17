"""Unit tests for LiteLLMRuntime and Cost Engine."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tether.cost import calculate_cost
from tether.runtime.litellm_runtime import LiteLLMRuntime


def test_cost_calculation():
    """Verify dollar cost computation across various model pricing rates."""
    # gpt-4o-mini: prompt $0.15/1M, completion $0.60/1M
    cost = calculate_cost("gpt-4o-mini", tokens_in=1000, tokens_out=500)
    # (1000/1e6)*0.15 + (500/1e6)*0.60 = 0.00015 + 0.00030 = 0.00045
    assert cost == 0.00045

    # claude-3-5-sonnet: prompt $3.00/1M, completion $15.00/1M
    cost_sonnet = calculate_cost("claude-3-5-sonnet-20241022", tokens_in=2000, tokens_out=1000)
    # (2000/1e6)*3.00 + (1000/1e6)*15.00 = 0.006 + 0.015 = 0.021
    assert cost_sonnet == 0.021

    # Unlisted fallback model
    fallback_cost = calculate_cost("unknown-custom-model", tokens_in=1000, tokens_out=1000)
    assert fallback_cost > 0.0


@pytest.mark.asyncio
async def test_litellm_runtime_success():
    """Verify completion parsing, tool call extraction, and metrics."""
    mock_choice = MagicMock()
    mock_choice.finish_reason = "tool_calls"
    mock_choice.message.content = "I will check the database."

    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_abc123"
    mock_tool_call.function.name = "query_database"
    mock_tool_call.function.arguments = '{"table": "users", "filter": "active"}'
    mock_choice.message.tool_calls = [mock_tool_call]

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 500
    mock_response.usage.completion_tokens = 200
    mock_response.id = "chatcmpl-test"

    runtime = LiteLLMRuntime(default_model="gpt-4o-mini", fallback_model="claude-3-5-haiku-20241022")

    with patch("tether.runtime.litellm_runtime.acompletion", new=AsyncMock(return_value=mock_response)):
        completion = await runtime.complete(
            messages=[{"role": "user", "content": "Fetch active users"}],
            run_id="run-1",
            step_id="step-1",
        )

        assert completion.text == "I will check the database."
        assert len(completion.tool_calls) == 1
        assert completion.tool_calls[0].name == "query_database"
        assert completion.tool_calls[0].arguments == {"table": "users", "filter": "active"}
        assert completion.tokens_in == 500
        assert completion.tokens_out == 200
        assert completion.cost_usd > 0.0
        assert completion.latency_ms >= 0.0
        assert completion.model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_litellm_runtime_fallback_on_primary_error():
    """Verify fallback model failover when primary model errors."""
    mock_fallback_choice = MagicMock()
    mock_fallback_choice.message.content = "Fallback response succeeded"
    mock_fallback_choice.message.tool_calls = []
    mock_fallback_response = MagicMock()
    mock_fallback_response.choices = [mock_fallback_choice]
    mock_fallback_response.usage.prompt_tokens = 300
    mock_fallback_response.usage.completion_tokens = 100
    mock_fallback_response.id = "fallback-test"

    runtime = LiteLLMRuntime(default_model="gpt-4o-mini", fallback_model="claude-3-5-haiku-20241022")

    # Primary fails with ConnectionError, fallback succeeds
    side_effects = [ConnectionError("OpenAI API unreachable"), mock_fallback_response]

    with patch("tether.runtime.litellm_runtime.acompletion", new=AsyncMock(side_effect=side_effects)):
        completion = await runtime.complete(
            messages=[{"role": "user", "content": "Hello"}],
            run_id="run-2",
            step_id="step-2",
        )

        assert completion.text == "Fallback response succeeded"
        assert completion.model == "claude-3-5-haiku-20241022"


@pytest.mark.asyncio
async def test_litellm_runtime_all_candidates_fail():
    """Verify RuntimeError is raised when all model candidates fail."""
    runtime = LiteLLMRuntime(default_model="gpt-4o-mini", fallback_model="claude-3-5-haiku-20241022")

    with patch(
        "tether.runtime.litellm_runtime.acompletion",
        new=AsyncMock(side_effect=TimeoutError("Model timed out")),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await runtime.complete(
                messages=[{"role": "user", "content": "Hello"}],
                run_id="run-3",
                step_id="step-3",
            )
        assert "All model candidates failed" in str(exc_info.value)

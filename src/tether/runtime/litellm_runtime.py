"""LiteLLM-backed concrete implementation of the Runtime protocol."""

import json
import logging
import os
import time
from typing import Any, Optional

import litellm
from litellm import acompletion

from tether.cost import calculate_cost
from tether.runtime.base import Completion, Runtime, ToolCall

logger = logging.getLogger(__name__)

# Suppress noisy LiteLLM telemetry and banner logs
litellm.telemetry = False
litellm.suppress_debug_info = True


class LiteLLMRuntime:
    """Headless, model-agnostic runtime powered by LiteLLM.

    Features:
    - Provider-agnostic tool calling and completions
    - Configurable automatic fallback model failover
    - Strict per-step timeout protection
    - Sub-millisecond latency tracking
    - Precise financial token cost attribution
    """

    def __init__(
        self,
        default_model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        timeout_seconds: float = 45.0,
    ):
        self.default_model = default_model or os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
        self.fallback_model = fallback_model or os.getenv(
            "FALLBACK_MODEL", "claude-3-5-haiku-20241022"
        )
        self.timeout_seconds = timeout_seconds

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        model: Optional[str] = None,
        run_id: str,
        step_id: str,
    ) -> Completion:
        """Execute a completion with automatic provider fallback on transient failure."""
        target_model = model or self.default_model
        models_to_try = [target_model]
        if self.fallback_model and self.fallback_model != target_model:
            models_to_try.append(self.fallback_model)

        last_exception: Optional[Exception] = None

        for attempt_idx, candidate_model in enumerate(models_to_try):
            start_time = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {
                    "model": candidate_model,
                    "messages": messages,
                    "timeout": self.timeout_seconds,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                response = await acompletion(**kwargs)
                latency_ms = (time.perf_counter() - start_time) * 1000.0

                return self._parse_response(
                    response=response,
                    model=candidate_model,
                    latency_ms=latency_ms,
                )

            except Exception as e:
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                last_exception = e
                logger.warning(
                    f"Run {run_id} Step {step_id}: Model '{candidate_model}' failed "
                    f"after {latency_ms:.1f}ms: {e}. "
                    f"{'Retrying with fallback...' if attempt_idx < len(models_to_try) - 1 else 'No more fallbacks.'}"
                )

        raise RuntimeError(
            f"All model candidates failed for run {run_id} step {step_id}. Last error: {last_exception}"
        ) from last_exception

    def _parse_response(
        self,
        response: Any,
        model: str,
        latency_ms: float,
    ) -> Completion:
        """Sanitize LiteLLM response into a pure, framework-agnostic Completion object."""
        choice = response.choices[0]
        message = choice.message

        # Extract text content
        text = message.content

        # Extract normalized tool calls
        tool_calls: list[ToolCall] = []
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        for tc in raw_tool_calls:
            tc_id = getattr(tc, "id", None) or str(time.time_ns())
            tc_func = getattr(tc, "function", None)
            if tc_func:
                tc_name = getattr(tc_func, "name", "")
                raw_args = getattr(tc_func, "arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                    except Exception:
                        parsed_args = {"raw": raw_args}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc_id,
                        name=tc_name,
                        arguments=parsed_args,
                    )
                )

        # Extract token usage
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0

        # Calculate financial cost
        cost_usd = calculate_cost(
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        return Completion(
            text=text,
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            latency_ms=round(latency_ms, 2),
            cost_usd=cost_usd,
            raw={
                "id": getattr(response, "id", None),
                "finish_reason": getattr(choice, "finish_reason", None),
            },
        )

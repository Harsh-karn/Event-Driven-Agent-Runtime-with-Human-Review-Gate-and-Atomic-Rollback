"""Base abstractions and protocols for the model-agnostic runtime."""

from typing import Any, Optional, Protocol
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """Standardized representation of an LLM tool call."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Completion(BaseModel):
    """Normalized completion payload returned by any LLM provider.

    Provider-specific SDK objects are strictly converted into this model,
    preventing vendor lock-in and dependency leakage.
    """

    text: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


class Runtime(Protocol):
    """Model-agnostic protocol for completing prompts and invoking tools."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        *,
        model: str,
        run_id: str,
        step_id: str,
    ) -> Completion:
        """Execute a completion with provider fallback and token instrumentation."""
        ...

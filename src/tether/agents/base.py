"""Base agent protocol and context definitions."""

from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from tether.effects import ToolRegistry
from tether.models import StagedEffect, Step


class AgentContext(BaseModel):
    """Execution context provided to an agent during its run lifecycle."""

    run_id: UUID
    agent_id: str
    trigger: str
    payload: dict[str, Any] = Field(default_factory=dict)
    completed_steps: list[Step] = Field(default_factory=list)
    staged_effects: list[StagedEffect] = Field(default_factory=list)


class Agent(Protocol):
    """Protocol that all domain-specific Tether agents implement."""

    agent_id: str
    model: str
    tools: ToolRegistry

    def get_system_prompt(self, context: AgentContext) -> str:
        """Generate the system prompt for the agent."""
        ...

    def build_initial_messages(self, context: AgentContext) -> list[dict[str, Any]]:
        """Construct the initial conversation messages from trigger and payload."""
        ...

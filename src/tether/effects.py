"""Tool partitioning, side-effect staging, and cryptographic hash verification."""

import hashlib
import json
from enum import Enum
from typing import Any, Callable, Coroutine, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from tether.models import EffectStatus, StagedEffect


class ToolCategory(str, Enum):
    """Classification of tool execution side effects."""

    READ = "read"      # Safe, read-only queries executed immediately
    EFFECT = "effect"  # Mutations/side effects staged for human gate; never executed in loop


def canonical_json(data: Any) -> str:
    """Produce deterministic, sorted JSON representation for cryptographic hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_step_idempotency_key(run_id: UUID | str, idx: int, input_payload: dict[str, Any]) -> str:
    """Compute cryptographic idempotency key for a step."""
    seed = f"{run_id}:{idx}:{canonical_json(input_payload)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def compute_effect_idempotency_key(
    run_id: UUID | str, idx: int, action: str, params: dict[str, Any]
) -> str:
    """Compute cryptographic idempotency key for a staged effect."""
    seed = f"{run_id}:{idx}:{action}:{canonical_json(params)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def compute_effects_hash(effects: list[StagedEffect]) -> str:
    """Compute cryptographic SHA256 digest of all staged effects in a run.

    This hash represents the exact contract presented to the human reviewer.
    The commit engine enforces that the database state at commit time matches
    this hash, preventing unauthorized post-approval parameter alterations.
    """
    if not effects:
        return hashlib.sha256(b"").hexdigest()

    sorted_effects = sorted(effects, key=lambda e: e.idx)
    serialized_items = [
        {
            "idx": e.idx,
            "target_system": e.target_system,
            "action": e.action,
            "params": e.params,
            "compensation": e.compensation,
        }
        for e in sorted_effects
    ]

    payload_str = canonical_json(serialized_items)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


class ToolDefinition(BaseModel):
    """Specification of an agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    category: ToolCategory = ToolCategory.READ
    target_system: str = "internal"
    compensation_action: Optional[str] = None

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert tool specification to LiteLLM / OpenAI tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.category.value.upper()}] {self.description}",
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registry managing read tools and side-effect tools."""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._read_handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        self._compensation_generators: dict[
            str, Callable[[dict[str, Any]], dict[str, Any]]
        ] = {}

    def register_read_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Coroutine[Any, Any, Any]],
        target_system: str = "internal",
    ) -> None:
        """Register a read-only tool that executes immediately."""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            category=ToolCategory.READ,
            target_system=target_system,
        )
        self._read_handlers[name] = handler

    def register_effect_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        compensation_action: str,
        compensation_generator: Callable[[dict[str, Any]], dict[str, Any]],
        target_system: str,
    ) -> None:
        """Register an effect tool that is staged for human gate approval."""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            category=ToolCategory.EFFECT,
            target_system=target_system,
            compensation_action=compensation_action,
        )
        self._compensation_generators[name] = compensation_generator

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Look up tool definition by name."""
        return self._tools.get(name)

    def get_all_tool_schemas(self) -> list[dict[str, Any]]:
        """Return all tools formatted for LLM invocation."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    async def execute_or_stage(
        self,
        name: str,
        arguments: dict[str, Any],
        run_id: UUID,
        next_effect_idx: int,
    ) -> tuple[dict[str, Any], Optional[StagedEffect]]:
        """Process tool invocation: execute if READ, or stage if EFFECT.

        Returns:
            A tuple of (tool_output_for_llm, optional_staged_effect).
        """
        tool = self._tools.get(name)
        if not tool:
            return (
                {"error": f"Tool '{name}' not found in registry"},
                None,
            )

        if tool.category == ToolCategory.READ:
            handler = self._read_handlers.get(name)
            if not handler:
                return ({"error": f"No handler registered for read tool '{name}'"}, None)
            try:
                result = await handler(**arguments)
                return ({"status": "success", "result": result}, None)
            except Exception as e:
                return ({"status": "error", "message": str(e)}, None)

        elif tool.category == ToolCategory.EFFECT:
            # Generate compensating action definition
            comp_gen = self._compensation_generators.get(name)
            compensation_params = comp_gen(arguments) if comp_gen else {}
            compensation = {
                "action": tool.compensation_action,
                "target_system": tool.target_system,
                "params": compensation_params,
            }

            idempotency_key = compute_effect_idempotency_key(
                run_id=run_id,
                idx=next_effect_idx,
                action=name,
                params=arguments,
            )

            staged = StagedEffect(
                run_id=run_id,
                idx=next_effect_idx,
                target_system=tool.target_system,
                action=name,
                params=arguments,
                compensation=compensation,
                status=EffectStatus.STAGED,
                idempotency_key=idempotency_key,
            )

            synthetic_response = {
                "status": "staged",
                "staged_idx": next_effect_idx,
                "action": name,
                "target_system": tool.target_system,
                "message": (
                    f"Mutation '{name}' has been staged with compensation "
                    f"'{tool.compensation_action}'. It will not take effect until "
                    f"human review approval."
                ),
            }
            return (synthetic_response, staged)

        return ({"error": f"Unknown category for tool '{name}'"}, None)

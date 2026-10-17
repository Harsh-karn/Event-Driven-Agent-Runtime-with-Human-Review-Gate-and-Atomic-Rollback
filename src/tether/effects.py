"""Tool partitioning, side-effect staging, and cryptographic hash verification."""

import hashlib
import json
import logging
from enum import Enum
from typing import Any, Callable, Coroutine, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from tether.db import (
    emit_event,
    get_approval,
    get_run,
    get_staged_effects,
    update_effect_status,
    update_run_state,
)
from tether.models import (
    ApprovalDecision,
    EffectStatus,
    Event,
    EventType,
    Run,
    RunState,
    StagedEffect,
)

logger = logging.getLogger(__name__)


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
        self._execution_handlers: dict[
            str, Callable[..., Coroutine[Any, Any, Any]]
        ] = {}
        self._compensation_handlers: dict[
            str, Callable[..., Coroutine[Any, Any, Any]]
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
        execution_handler: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
        compensation_handler: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
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
        if execution_handler:
            self._execution_handlers[name] = execution_handler
        if compensation_handler:
            self._compensation_handlers[name] = compensation_handler

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Look up tool definition by name."""
        return self._tools.get(name)

    def get_execution_handler(
        self, name: str
    ) -> Optional[Callable[..., Coroutine[Any, Any, Any]]]:
        """Retrieve execution handler for an effect tool."""
        return self._execution_handlers.get(name)

    def get_compensation_handler(
        self, name: str
    ) -> Optional[Callable[..., Coroutine[Any, Any, Any]]]:
        """Retrieve compensation handler for an effect tool."""
        return self._compensation_handlers.get(name)

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


async def execute_approved_effects(run_id: UUID, registry: ToolRegistry) -> Run:
    """Commit phase: Executes approved staged effects in order.

    If effect k fails:
      1. Forward execution halts immediately.
      2. Run switches state from COMMITTING to ROLLING_BACK.
      3. Compensations for completed effects k-1...1 are invoked in strict reverse order.
      4. If all compensations succeed -> Run state moves to ROLLED_BACK.
      5. If any compensation fails -> Run state moves to ROLLBACK_FAILED with alert.
    """
    run = await get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")

    approval = await get_approval(run_id)
    if not approval or approval.decision != ApprovalDecision.APPROVED:
        raise ValueError(f"Run {run_id} has not been approved for execution")

    staged_effects = list(await get_staged_effects(run_id))
    current_hash = compute_effects_hash(staged_effects)

    if approval.effects_hash != current_hash:
        error_msg = (
            f"Commit refused: Staged effects hash '{current_hash}' does not match "
            f"approved hash '{approval.effects_hash}'. Tampering detected."
        )
        await update_run_state(run_id, RunState.FAILED, error=error_msg)
        raise ValueError(error_msg)

    # Transition run to COMMITTING
    run = await update_run_state(run_id, RunState.COMMITTING)
    await emit_event(
        Event(
            run_id=run_id,
            type=EventType.COMMIT_STARTED,
            payload={"effects_count": len(staged_effects)},
        )
    )

    completed_effects: list[StagedEffect] = []

    for effect in staged_effects:
        # If already committed in a prior run attempt, reuse
        if effect.status == EffectStatus.COMMITTED:
            completed_effects.append(effect)
            continue

        await update_effect_status(effect.id, EffectStatus.COMMITTING)
        handler = registry.get_execution_handler(effect.action)

        try:
            result = None
            if handler:
                result = await handler(**effect.params, idempotency_key=effect.idempotency_key)
            else:
                # Default mock/stub execution
                result = {"status": "executed", "action": effect.action, "id": str(effect.id)}

            await update_effect_status(effect.id, EffectStatus.COMMITTED, result=result)
            committed_effect = effect.model_copy(update={"status": EffectStatus.COMMITTED, "result": result})
            completed_effects.append(committed_effect)

            await emit_event(
                Event(
                    run_id=run_id,
                    type=EventType.EFFECT_COMMITTED,
                    payload={"effect_id": str(effect.id), "idx": effect.idx, "action": effect.action, "result": result},
                )
            )

        except Exception as forward_err:
            logger.error(
                f"Run {run_id} FAILED during commit of effect {effect.idx} ('{effect.action}'): {forward_err}"
            )
            await update_effect_status(effect.id, EffectStatus.FAILED, error=str(forward_err))

            # Atomic Rollback Phase: unwind completed effects in reverse
            run = await update_run_state(run_id, RunState.ROLLING_BACK, error=str(forward_err))
            await emit_event(
                Event(
                    run_id=run_id,
                    type=EventType.ROLLBACK_STARTED,
                    payload={
                        "failed_effect_idx": effect.idx,
                        "failed_action": effect.action,
                        "compensations_to_run": len(completed_effects),
                        "error": str(forward_err),
                    },
                )
            )

            rollback_failed = False
            for comp_target in reversed(completed_effects):
                await update_effect_status(comp_target.id, EffectStatus.COMPENSATING)
                comp_action = comp_target.compensation.get("action")
                comp_params = comp_target.compensation.get("params", {})
                comp_handler = registry.get_compensation_handler(comp_target.action)

                try:
                    comp_result = None
                    if comp_handler:
                        comp_result = await comp_handler(**comp_params, result=comp_target.result)
                    else:
                        comp_result = {"status": "compensated", "action": comp_action}

                    await update_effect_status(comp_target.id, EffectStatus.COMPENSATED, result=comp_result)
                    await emit_event(
                        Event(
                            run_id=run_id,
                            type=EventType.EFFECT_COMPENSATED,
                            payload={
                                "effect_id": str(comp_target.id),
                                "idx": comp_target.idx,
                                "action": comp_target.action,
                                "compensation_action": comp_action,
                            },
                        )
                    )
                except Exception as comp_err:
                    logger.critical(
                        f"Run {run_id} CRITICAL: Compensation failed for effect {comp_target.idx} ('{comp_target.action}'): {comp_err}"
                    )
                    await update_effect_status(comp_target.id, EffectStatus.FAILED, error=str(comp_err))
                    rollback_failed = True
                    break

            if rollback_failed:
                run = await update_run_state(
                    run_id,
                    RunState.ROLLBACK_FAILED,
                    error=f"Atomic rollback failed during reverse compensation of effect {comp_target.idx}",
                )
                await emit_event(
                    Event(
                        run_id=run_id,
                        type=EventType.RUN_FAILED,
                        payload={"status": "rollback_failed", "unwound_count": len(completed_effects)},
                    )
                )
                return run

            run = await update_run_state(
                run_id,
                RunState.ROLLED_BACK,
                error=f"Rolled back cleanly after failure at effect {effect.idx}: {forward_err}",
            )
            await emit_event(
                Event(
                    run_id=run_id,
                    type=EventType.RUN_FINISHED,
                    payload={"status": "rolled_back", "unwound_count": len(completed_effects)},
                )
            )
            return run

    # All effects executed cleanly!
    run = await update_run_state(run_id, RunState.COMMITTED)
    await emit_event(
        Event(
            run_id=run_id,
            type=EventType.RUN_FINISHED,
            payload={"status": "committed", "committed_count": len(completed_effects)},
        )
    )
    return run

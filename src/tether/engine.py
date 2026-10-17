"""Durable step execution engine for Tether Agent Runs.

Guarantees:
- Runs are rebuilt completely from PostgreSQL; never in-memory only.
- Steps are checkpointed with cryptographic idempotency keys.
- On worker crash resume, completed steps are skipped and their outputs reused.
- Side effects are staged, never executed during reasoning.
- Strict finite state machine transitions are validated.
"""

import json
import logging
from typing import Any, Callable, Optional
from uuid import UUID

from tether.agents.base import Agent, AgentContext
from tether.db import (
    checkpoint_step,
    emit_event,
    get_completed_steps,
    get_run,
    get_staged_effects,
    stage_effect,
    update_run_state,
)
from tether.effects import (
    compute_step_idempotency_key,
)
from tether.models import (
    Event,
    EventType,
    Run,
    RunState,
    Step,
    StepStatus,
    utc_now,
)
from tether.runtime.base import Runtime

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Stateful step engine managing durable agent execution and recovery."""

    def __init__(
        self,
        runtime: Runtime,
        agent_lookup: Callable[[str], Optional[Agent]],
        max_steps: int = 25,
    ):
        self.runtime = runtime
        self.agent_lookup = agent_lookup
        self.max_steps = max_steps

    async def execute_run(self, run_id: UUID) -> Run:
        """Execute or resume an agent run from PostgreSQL state."""
        # Step 1: Rebuild run from database
        run = await get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} does not exist in database")

        # Step 2: Validate and transition to RUNNING
        if run.state == RunState.QUEUED:
            run = await update_run_state(run_id, RunState.RUNNING)
            await emit_event(
                Event(
                    run_id=run_id,
                    type=EventType.RUN_STARTED,
                    payload={"agent_id": run.agent_id, "trigger": run.trigger},
                )
            )
        elif run.state != RunState.RUNNING:
            logger.info(f"Run {run_id} is in non-executable state '{run.state.value}'. Skipping execution.")
            return run

        # Step 3: Resolve agent
        agent = self.agent_lookup(run.agent_id)
        if not agent:
            error_msg = f"Agent '{run.agent_id}' is not registered in runtime"
            await update_run_state(run_id, RunState.FAILED, error=error_msg)
            raise ValueError(error_msg)

        # Step 4: Rebuild completed steps & staged effects from PostgreSQL
        completed_steps = list(await get_completed_steps(run_id))
        staged_effects = list(await get_staged_effects(run_id))

        context = AgentContext(
            run_id=run_id,
            agent_id=run.agent_id,
            trigger=run.trigger,
            payload=run.payload,
            completed_steps=completed_steps,
            staged_effects=staged_effects,
        )

        # Step 5: Initialize message history
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.get_system_prompt(context)},
            *agent.build_initial_messages(context),
        ]

        # Replay conversation context from previously completed steps
        for step in completed_steps:
            if step.kind == "model_call" and step.output:
                msg_content = step.output.get("text")
                raw_tool_calls = step.output.get("tool_calls", [])
                msg: dict[str, Any] = {"role": "assistant"}
                if msg_content:
                    msg["content"] = msg_content
                if raw_tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"])
                                if isinstance(tc["arguments"], dict)
                                else str(tc["arguments"]),
                            },
                        }
                        for tc in raw_tool_calls
                    ]
                messages.append(msg)
            elif step.kind in ("read_tool", "effect_staging") and step.output:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": step.input.get("tool_call_id", "cached"),
                        "content": json.dumps(step.output)
                        if isinstance(step.output, dict)
                        else str(step.output),
                    }
                )

        logger.info(
            f"Run {run_id}: Rebuilt state with {len(completed_steps)} completed steps "
            f"and {len(staged_effects)} staged effects."
        )

        try:
            # Check if execution was already completed before crash
            reasoning_complete = False
            if completed_steps:
                last_step = completed_steps[-1]
                if last_step.kind == "model_call" and last_step.output:
                    tool_calls = last_step.output.get("tool_calls", [])
                    if not tool_calls:
                        reasoning_complete = True

            # Step 6: Thought & Action Loop
            while not reasoning_complete and len(completed_steps) < self.max_steps:
                next_step_idx = len(completed_steps) + 1

                # Model Step Idempotency
                step_input = {"messages": messages}
                idempotency_key = compute_step_idempotency_key(run_id, next_step_idx, step_input)

                # Pre-step checkpoint
                pending_step = Step(
                    run_id=run_id,
                    idx=next_step_idx,
                    kind="model_call",
                    input=step_input,
                    status=StepStatus.RUNNING,
                    idempotency_key=idempotency_key,
                    started_at=utc_now(),
                )
                await checkpoint_step(pending_step)

                # Model invocation via Runtime
                completion = await self.runtime.complete(
                    messages=messages,
                    tools=agent.tools.get_all_tool_schemas(),
                    model=agent.model,
                    run_id=str(run_id),
                    step_id=str(next_step_idx),
                )

                # Post-step checkpoint
                completion_dict = completion.model_dump()
                completed_step = pending_step.model_copy(
                    update={
                        "output": completion_dict,
                        "status": StepStatus.COMPLETED,
                        "ended_at": utc_now(),
                    }
                )
                await checkpoint_step(completed_step)
                completed_steps.append(completed_step)

                # Outbox telemetry event
                model_name = str(completion.model) if completion.model else agent.model
                await emit_event(
                    Event(
                        run_id=run_id,
                        step_id=completed_step.id,
                        type=EventType.MODEL_CALL,
                        payload={
                            "model": model_name,
                            "latency_ms": completion.latency_ms,
                            "tool_call_count": len(completion.tool_calls),
                            "text": completion.text,
                        },
                        tokens_in=completion.tokens_in,
                        tokens_out=completion.tokens_out,
                        cost_usd=completion.cost_usd,
                        model=model_name,
                    )
                )

                text = completion.text
                raw_tool_calls = [tc.model_dump() for tc in completion.tool_calls]

                # Append assistant message to context
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if text:
                    assistant_msg["content"] = text
                if raw_tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"])
                                if isinstance(tc["arguments"], dict)
                                else str(tc["arguments"]),
                            },
                        }
                        for tc in raw_tool_calls
                    ]
                messages.append(assistant_msg)

                # If no tool calls, reasoning is done!
                if not raw_tool_calls:
                    logger.info(f"Run {run_id}: Agent concluded reasoning.")
                    reasoning_complete = True
                    break

                # Process tool calls
                for tc in raw_tool_calls:
                    tool_step_idx = len(completed_steps) + 1
                    tc_id = tc["id"]
                    tc_name = tc["name"]
                    tc_args = tc.get("arguments", {})
                    if isinstance(tc_args, str):
                        try:
                            tc_args = json.loads(tc_args)
                        except Exception:
                            tc_args = {"raw": tc_args}

                    tool_step_input = {"tool_call_id": tc_id, "name": tc_name, "arguments": tc_args}
                    tool_idempotency_key = compute_step_idempotency_key(run_id, tool_step_idx, tool_step_input)

                    next_effect_idx = len(staged_effects) + 1
                    tool_output, potential_effect = await agent.tools.execute_or_stage(
                        name=tc_name,
                        arguments=tc_args,
                        run_id=run_id,
                        next_effect_idx=next_effect_idx,
                    )

                    step_kind = "effect_staging" if potential_effect else "read_tool"

                    # If side effect staged, persist to staged_effects
                    if potential_effect:
                        staged_effect = await stage_effect(potential_effect)
                        staged_effects.append(staged_effect)

                    # Checkpoint tool step
                    tool_step = Step(
                        run_id=run_id,
                        idx=tool_step_idx,
                        kind=step_kind,
                        input=tool_step_input,
                        output=tool_output,
                        status=StepStatus.COMPLETED,
                        idempotency_key=tool_idempotency_key,
                        started_at=utc_now(),
                        ended_at=utc_now(),
                    )
                    await checkpoint_step(tool_step)
                    completed_steps.append(tool_step)

                    # Outbox telemetry event
                    await emit_event(
                        Event(
                            run_id=run_id,
                            step_id=tool_step.id,
                            type=EventType.EFFECT_STAGED if potential_effect else EventType.TOOL_CALL,
                            payload={
                                "tool_name": tc_name,
                                "arguments": tc_args,
                                "is_effect": potential_effect is not None,
                                "output": tool_output,
                            },
                        )
                    )

                    # Append tool result to context
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(tool_output)
                            if isinstance(tool_output, dict)
                            else str(tool_output),
                        }
                    )

            # Step 7: Reasoning complete. Evaluate gate transition.
            latest_staged_effects = await get_staged_effects(run_id)
            if latest_staged_effects:
                logger.info(f"Run {run_id}: Has {len(latest_staged_effects)} staged effects. Moving to AWAITING_APPROVAL.")
                run = await update_run_state(run_id, RunState.AWAITING_APPROVAL)
                await emit_event(
                    Event(
                        run_id=run_id,
                        type=EventType.AWAITING_APPROVAL,
                        payload={"staged_effect_count": len(latest_staged_effects)},
                    )
                )
            else:
                logger.info(f"Run {run_id}: Pure read run with zero side effects. Moving to COMMITTED.")
                run = await update_run_state(run_id, RunState.COMMITTING)
                run = await update_run_state(run_id, RunState.COMMITTED)
                await emit_event(
                    Event(
                        run_id=run_id,
                        type=EventType.RUN_FINISHED,
                        payload={"final_state": "committed", "effects_count": 0},
                    )
                )

            return run

        except Exception as e:
            logger.error(f"Run {run_id} failed during execution: {e}", exc_info=True)
            run = await update_run_state(run_id, RunState.FAILED, error=str(e))
            await emit_event(
                Event(
                    run_id=run_id,
                    type=EventType.RUN_FAILED,
                    payload={"error": str(e)},
                )
            )
            raise

"""Worker process polling pgmq / PostgreSQL queue with heartbeat leases and DLQ routing."""

import asyncio
import logging
import os
import signal
from typing import Any, Callable, Optional
from uuid import UUID

from tether.agents.base import Agent
from tether.db import (
    archive_job,
    emit_event,
    extend_lease,
    get_run,
    lease_next_job,
    update_run_state,
)
from tether.engine import ExecutionEngine
from tether.models import Event, EventType, RunState
from tether.runtime.litellm_runtime import LiteLLMRuntime

logger = logging.getLogger(__name__)


class TetherWorker:
    """Background worker that continuously leases jobs, maintains heartbeats,

    and delegates execution to the ExecutionEngine.
    """

    def __init__(
        self,
        engine: Optional[ExecutionEngine] = None,
        agent_lookup: Optional[Callable[[str], Optional[Agent]]] = None,
        queue_name: str = "agent_runs",
        dlq_name: str = "agent_runs_dlq",
        visibility_timeout: int = 60,
        heartbeat_interval: int = 20,
        max_attempts: int = 5,
        poll_interval_min: float = 1.0,
        poll_interval_max: float = 10.0,
    ):
        self.queue_name = queue_name
        self.dlq_name = dlq_name
        self.visibility_timeout = visibility_timeout
        self.heartbeat_interval = heartbeat_interval
        self.max_attempts = max_attempts
        self.poll_interval_min = poll_interval_min
        self.poll_interval_max = poll_interval_max

        if engine:
            self.engine = engine
        else:
            runtime = LiteLLMRuntime()
            self.engine = ExecutionEngine(
                runtime=runtime,
                agent_lookup=agent_lookup or (lambda _: None),
            )

        self._shutdown_event = asyncio.Event()

    def stop(self) -> None:
        """Signal the worker to halt gracefully."""
        logger.info("Worker stop requested. Initiating graceful shutdown...")
        self._shutdown_event.set()

    async def _heartbeat_loop(
        self,
        msg_id: int,
        is_pgmq: bool,
        stop_event: asyncio.Event,
    ) -> None:
        """Background task renewing the visibility lease until job completes."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.heartbeat_interval)
                break
            except asyncio.TimeoutError:
                # Heartbeat timer expired: extend the lease
                try:
                    await extend_lease(
                        msg_id=msg_id,
                        queue_name=self.queue_name,
                        vt_seconds=self.visibility_timeout,
                        is_pgmq=is_pgmq,
                    )
                    logger.debug(f"Extended lease for message {msg_id} by {self.visibility_timeout}s")
                except Exception as e:
                    logger.warning(f"Failed to extend heartbeat for message {msg_id}: {e}")

    async def process_one_job(self) -> bool:
        """Poll and execute a single job from the queue.

        Returns:
            True if a job was picked up and processed; False if the queue was empty.
        """
        job = await lease_next_job(
            queue_name=self.queue_name,
            vt_seconds=self.visibility_timeout,
        )

        if not job:
            return False

        msg_id: int = job["msg_id"]
        read_ct: int = job["read_ct"]
        payload: dict[str, Any] = job["payload"]
        is_pgmq: bool = job.get("is_pgmq", False)

        run_id_str = payload.get("run_id")
        if not run_id_str:
            logger.error(f"Malformed queue message {msg_id}: missing 'run_id'")
            await archive_job(msg_id, queue_name=self.queue_name, is_pgmq=is_pgmq)
            return True

        run_id = UUID(str(run_id_str))
        logger.info(f"Picked up job for run {run_id} (msg_id={msg_id}, attempt={read_ct})")

        # Check for Dead-Letter exhaustion
        if read_ct > self.max_attempts:
            logger.error(
                f"Run {run_id} has exceeded max attempts ({read_ct} > {self.max_attempts}). "
                f"Moving to dead-letter queue."
            )
            run = await get_run(run_id)
            if run and run.state != RunState.DEAD_LETTERED:
                await update_run_state(
                    run_id=run_id,
                    target_state=RunState.DEAD_LETTERED,
                    error=f"Exhausted max attempts ({read_ct}/{self.max_attempts})",
                )
                await emit_event(
                    Event(
                        run_id=run_id,
                        type=EventType.RUN_DEAD_LETTERED,
                        payload={"attempt": read_ct, "max_attempts": self.max_attempts},
                    )
                )

            await archive_job(msg_id, queue_name=self.queue_name, is_pgmq=is_pgmq)
            return True

        # Start concurrent heartbeat lease extension
        heartbeat_stop_event = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(msg_id, is_pgmq, heartbeat_stop_event)
        )

        try:
            # Delegate to Execution Engine
            await self.engine.execute_run(run_id)

            # Execution succeeded: archive the queue message
            await archive_job(msg_id, queue_name=self.queue_name, is_pgmq=is_pgmq)
            logger.info(f"Cleanly archived message {msg_id} for run {run_id}")

        except Exception as e:
            logger.error(f"Error processing run {run_id}: {e}", exc_info=True)
            # The visibility lease will expire and allow another worker to retry,
            # or the next attempt counter will trigger DLQ.
        finally:
            # Cancel heartbeat task
            heartbeat_stop_event.set()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        return True

    async def start(self) -> None:
        """Run the main polling loop with adaptive backoff."""
        logger.info(
            f"Tether Worker started on queue '{self.queue_name}' "
            f"(visibility={self.visibility_timeout}s, max_attempts={self.max_attempts})"
        )

        backoff = self.poll_interval_min

        while not self._shutdown_event.is_set():
            try:
                found_job = await self.process_one_job()
                if found_job:
                    # Reset backoff when work is actively found
                    backoff = self.poll_interval_min
                else:
                    # Adaptive exponential backoff when idle
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, self.poll_interval_max)

            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}", exc_info=True)
                await asyncio.sleep(self.poll_interval_min)

        logger.info("Tether Worker polling loop terminated.")


def run_worker() -> None:
    """Synchronous entrypoint for CLI invocation with signal handling."""
    from tether.agents.demo_digest import DemoDigestAgent

    agent_registry = {"demo_digest": DemoDigestAgent()}

    worker = TetherWorker(
        agent_lookup=lambda aid: agent_registry.get(aid),
        queue_name=os.getenv("QUEUE_NAME", "agent_runs"),
        dlq_name=os.getenv("DLQ_NAME", "agent_runs_dlq"),
        visibility_timeout=int(os.getenv("VISIBILITY_TIMEOUT_SECONDS", "60")),
        heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "20")),
        max_attempts=int(os.getenv("MAX_ATTEMPTS", "5")),
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Register OS signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            # Windows does not support add_signal_handler in certain environments
            pass

    try:
        loop.run_until_complete(worker.start())
    finally:
        loop.close()

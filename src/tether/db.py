"""Database connection pool and repository operations using psycopg 3."""

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tether.models import (
    Approval,
    ApprovalDecision,
    EffectStatus,
    EnqueueResponse,
    Event,
    EventType,
    Run,
    RunState,
    StagedEffect,
    Step,
    StepStatus,
)
from tether.state_machine import validate_transition

logger = logging.getLogger(__name__)

# Global connection pool instance
_pool: Optional[AsyncConnectionPool] = None


def get_database_url() -> str:
    """Retrieve database connection string from environment."""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/postgres",
    )


async def init_db_pool(min_size: int = 2, max_size: int = 10) -> AsyncConnectionPool:
    """Initialize the asynchronous PostgreSQL connection pool."""
    global _pool
    if _pool is None:
        conninfo = get_database_url()
        _pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await _pool.open()
        logger.info("Initialized Tether database connection pool")
    return _pool


async def close_db_pool() -> None:
    """Close the global database connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Closed Tether database connection pool")


@asynccontextmanager
async def get_connection() -> AsyncGenerator[AsyncConnection, None]:
    """Context manager to acquire a connection from the pool."""
    global _pool
    if _pool is None:
        await init_db_pool()
    assert _pool is not None
    async with _pool.connection() as conn:
        yield conn


# ------------------------------------------------------------------------------
# Repository Functions
# ------------------------------------------------------------------------------


async def enqueue_run(
    agent_id: str,
    trigger: str = "api",
    payload: Optional[dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
) -> EnqueueResponse:
    """Atomically enqueue an agent run using stored procedure or transaction."""
    payload = payload or {}
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            # Use the atomic stored function tether_enqueue
            await cur.execute(
                """
                SELECT run_id, is_duplicate, state
                FROM tether_enqueue(%s, %s, %s, %s);
                """,
                (agent_id, trigger, json.dumps(payload), dedup_key),
            )
            row = await cur.fetchone()
            assert row is not None
            await conn.commit()

            return EnqueueResponse(
                run_id=row["run_id"],
                is_duplicate=row["is_duplicate"],
                state=RunState(row["state"]),
            )


async def get_run(run_id: UUID) -> Optional[Run]:
    """Fetch a run by its UUID."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM runs WHERE id = %s;", (run_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return Run(
                id=row["id"],
                agent_id=row["agent_id"],
                trigger=row["trigger"],
                payload=row["payload"],
                dedup_key=row["dedup_key"],
                state=RunState(row["state"]),
                attempt=row["attempt"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                error=row["error"],
            )


async def update_run_state(
    run_id: UUID,
    target_state: RunState,
    error: Optional[str] = None,
    increment_attempt: bool = False,
) -> Run:
    """Update a run state, enforcing finite state machine validity."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            # Lock the run row for update
            await cur.execute("SELECT * FROM runs WHERE id = %s FOR UPDATE;", (run_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError(f"Run {run_id} not found")

            current_state = RunState(row["state"])
            validate_transition(current_state, target_state, str(run_id))

            attempt_clause = "attempt = attempt + 1," if increment_attempt else ""
            await cur.execute(
                f"""
                UPDATE runs
                SET state = %s,
                    {attempt_clause}
                    error = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *;
                """,
                (target_state.value, error, run_id),
            )
            updated_row = await cur.fetchone()
            assert updated_row is not None

            # Emit a state transition event to the outbox
            await cur.execute(
                """
                INSERT INTO events (run_id, type, payload)
                VALUES (%s, %s, %s);
                """,
                (
                    run_id,
                    EventType.APPROVAL_DECISION.value
                    if target_state in (RunState.APPROVED, RunState.REJECTED)
                    else "state_transition",
                    json.dumps(
                        {
                            "from_state": current_state.value,
                            "to_state": target_state.value,
                            "error": error,
                        }
                    ),
                ),
            )
            await conn.commit()

            return Run(
                id=updated_row["id"],
                agent_id=updated_row["agent_id"],
                trigger=updated_row["trigger"],
                payload=updated_row["payload"],
                dedup_key=updated_row["dedup_key"],
                state=RunState(updated_row["state"]),
                attempt=updated_row["attempt"],
                created_at=updated_row["created_at"],
                updated_at=updated_row["updated_at"],
                error=updated_row["error"],
            )


async def checkpoint_step(step: Step) -> Step:
    """Save or update a checkpointed step. Idempotent by idempotency_key."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO steps (id, run_id, idx, kind, input, output, status, idempotency_key, started_at, ended_at, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET output = EXCLUDED.output,
                    status = EXCLUDED.status,
                    ended_at = EXCLUDED.ended_at,
                    error = EXCLUDED.error
                RETURNING *;
                """,
                (
                    step.id,
                    step.run_id,
                    step.idx,
                    step.kind,
                    json.dumps(step.input),
                    json.dumps(step.output) if step.output is not None else None,
                    step.status.value,
                    step.idempotency_key,
                    step.started_at,
                    step.ended_at,
                    step.error,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            await conn.commit()

            return Step(
                id=row["id"],
                run_id=row["run_id"],
                idx=row["idx"],
                kind=row["kind"],
                input=row["input"],
                output=row["output"],
                status=StepStatus(row["status"]),
                idempotency_key=row["idempotency_key"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                error=row["error"],
            )


async def get_completed_steps(run_id: UUID) -> list[Step]:
    """Retrieve all completed steps for a run ordered by index."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT * FROM steps
                WHERE run_id = %s AND status = 'completed'
                ORDER BY idx ASC;
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
            return [
                Step(
                    id=r["id"],
                    run_id=r["run_id"],
                    idx=r["idx"],
                    kind=r["kind"],
                    input=r["input"],
                    output=r["output"],
                    status=StepStatus(r["status"]),
                    idempotency_key=r["idempotency_key"],
                    started_at=r["started_at"],
                    ended_at=r["ended_at"],
                    error=r["error"],
                )
                for r in rows
            ]


async def stage_effect(effect: StagedEffect) -> StagedEffect:
    """Stage a side effect without executing it. Idempotent by idempotency_key."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO staged_effects (
                    id, run_id, idx, target_system, action, params, compensation, status, idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET params = EXCLUDED.params,
                    compensation = EXCLUDED.compensation
                RETURNING *;
                """,
                (
                    effect.id,
                    effect.run_id,
                    effect.idx,
                    effect.target_system,
                    effect.action,
                    json.dumps(effect.params),
                    json.dumps(effect.compensation),
                    effect.status.value,
                    effect.idempotency_key,
                ),
            )
            row = await cur.fetchone()
            assert row is not None

            # Outbox event
            await cur.execute(
                """
                INSERT INTO events (run_id, type, payload)
                VALUES (%s, %s, %s);
                """,
                (
                    effect.run_id,
                    EventType.EFFECT_STAGED.value,
                    json.dumps(
                        {
                            "effect_id": str(effect.id),
                            "target_system": effect.target_system,
                            "action": effect.action,
                            "params": effect.params,
                        }
                    ),
                ),
            )
            await conn.commit()

            return StagedEffect(
                id=row["id"],
                run_id=row["run_id"],
                idx=row["idx"],
                target_system=row["target_system"],
                action=row["action"],
                params=row["params"],
                compensation=row["compensation"],
                status=EffectStatus(row["status"]),
                idempotency_key=row["idempotency_key"],
                result=row["result"],
                error=row["error"],
                staged_at=row["staged_at"],
                committed_at=row["committed_at"],
                compensated_at=row["compensated_at"],
            )


async def get_staged_effects(run_id: UUID) -> list[StagedEffect]:
    """Retrieve all staged effects for a run ordered by idx."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM staged_effects WHERE run_id = %s ORDER BY idx ASC;",
                (run_id,),
            )
            rows = await cur.fetchall()
            return [
                StagedEffect(
                    id=r["id"],
                    run_id=r["run_id"],
                    idx=r["idx"],
                    target_system=r["target_system"],
                    action=r["action"],
                    params=r["params"],
                    compensation=r["compensation"],
                    status=EffectStatus(r["status"]),
                    idempotency_key=r["idempotency_key"],
                    result=r["result"],
                    error=r["error"],
                    staged_at=r["staged_at"],
                    committed_at=r["committed_at"],
                    compensated_at=r["compensated_at"],
                )
                for r in rows
            ]


async def update_effect_status(
    effect_id: UUID,
    status: EffectStatus,
    result: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Update effect execution or compensation status."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            timestamp_col = (
                "committed_at = NOW(),"
                if status == EffectStatus.COMMITTED
                else ("compensated_at = NOW()," if status == EffectStatus.COMPENSATED else "")
            )
            await cur.execute(
                f"""
                UPDATE staged_effects
                SET status = %s,
                    result = COALESCE(%s, result),
                    error = %s,
                    {timestamp_col}
                    id = id
                WHERE id = %s;
                """,
                (
                    status.value,
                    json.dumps(result) if result else None,
                    error,
                    effect_id,
                ),
            )
            await conn.commit()


async def record_approval(approval: Approval) -> Approval:
    """Record a human review decision (approved or rejected)."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO approvals (id, run_id, decided_by, decision, reason, decided_at, effects_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET decided_by = EXCLUDED.decided_by,
                    decision = EXCLUDED.decision,
                    reason = EXCLUDED.reason,
                    decided_at = EXCLUDED.decided_at,
                    effects_hash = EXCLUDED.effects_hash
                RETURNING *;
                """,
                (
                    approval.id,
                    approval.run_id,
                    approval.decided_by,
                    approval.decision.value,
                    approval.reason,
                    approval.decided_at,
                    approval.effects_hash,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            await conn.commit()

            return Approval(
                id=row["id"],
                run_id=row["run_id"],
                decided_by=row["decided_by"],
                decision=ApprovalDecision(row["decision"]),
                reason=row["reason"],
                decided_at=row["decided_at"],
                effects_hash=row["effects_hash"],
            )


async def get_approval(run_id: UUID) -> Optional[Approval]:
    """Retrieve approval record for a run."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM approvals WHERE run_id = %s;", (run_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return Approval(
                id=row["id"],
                run_id=row["run_id"],
                decided_by=row["decided_by"],
                decision=ApprovalDecision(row["decision"]),
                reason=row["reason"],
                decided_at=row["decided_at"],
                effects_hash=row["effects_hash"],
            )


async def emit_event(event: Event) -> None:
    """Emit a structured event to the PostgreSQL outbox."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO events (id, run_id, step_id, ts, type, payload, tokens_in, tokens_out, cost_usd, model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    event.id,
                    event.run_id,
                    event.step_id,
                    event.ts,
                    event.type.value,
                    json.dumps(event.payload),
                    event.tokens_in,
                    event.tokens_out,
                    event.cost_usd,
                    event.model,
                ),
            )
            await conn.commit()


async def record_approval_and_transition(
    approval: Approval,
    target_state: RunState = RunState.APPROVED,
) -> tuple[Approval, Run]:
    """Atomically record approval and update run state in a single transaction."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            # 1. Lock the run row for update
            await cur.execute("SELECT * FROM runs WHERE id = %s FOR UPDATE;", (approval.run_id,))
            run_row = await cur.fetchone()
            if not run_row:
                raise ValueError(f"Run {approval.run_id} not found")

            current_state = RunState(run_row["state"])
            validate_transition(current_state, target_state, str(approval.run_id))

            # 2. Insert approval
            await cur.execute(
                """
                INSERT INTO approvals (id, run_id, decided_by, decision, reason, decided_at, effects_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET decided_by = EXCLUDED.decided_by,
                    decision = EXCLUDED.decision,
                    reason = EXCLUDED.reason,
                    decided_at = EXCLUDED.decided_at,
                    effects_hash = EXCLUDED.effects_hash
                RETURNING *;
                """,
                (
                    approval.id,
                    approval.run_id,
                    approval.decided_by,
                    approval.decision.value,
                    approval.reason,
                    approval.decided_at,
                    approval.effects_hash,
                ),
            )
            app_row = await cur.fetchone()
            assert app_row is not None

            # 3. Update run state
            await cur.execute(
                """
                UPDATE runs
                SET state = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *;
                """,
                (target_state.value, approval.run_id),
            )
            updated_run_row = await cur.fetchone()
            assert updated_run_row is not None

            # 4. Outbox state transition event
            await cur.execute(
                """
                INSERT INTO events (run_id, type, payload)
                VALUES (%s, %s, %s);
                """,
                (
                    approval.run_id,
                    EventType.APPROVAL_DECISION.value,
                    json.dumps(
                        {
                            "decided_by": approval.decided_by,
                            "decision": approval.decision.value,
                            "reason": approval.reason,
                            "effects_hash": approval.effects_hash,
                            "from_state": current_state.value,
                            "to_state": target_state.value,
                        }
                    ),
                ),
            )
            await conn.commit()

            recorded_approval = Approval(
                id=app_row["id"],
                run_id=app_row["run_id"],
                decided_by=app_row["decided_by"],
                decision=ApprovalDecision(app_row["decision"]),
                reason=app_row["reason"],
                decided_at=app_row["decided_at"],
                effects_hash=app_row["effects_hash"],
            )

            updated_run = Run(
                id=updated_run_row["id"],
                agent_id=updated_run_row["agent_id"],
                trigger=updated_run_row["trigger"],
                payload=updated_run_row["payload"],
                dedup_key=updated_run_row["dedup_key"],
                state=RunState(updated_run_row["state"]),
                attempt=updated_run_row["attempt"],
                created_at=updated_run_row["created_at"],
                updated_at=updated_run_row["updated_at"],
                error=updated_run_row["error"],
            )

            return recorded_approval, updated_run


async def send_queue_message(
    queue_name: str,
    message: dict[str, Any],
) -> None:
    """Send an arbitrary payload to pgmq or fallback queue."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgmq');")
            row = await cur.fetchone()
            has_pgmq = row["exists"] if row else False

            if has_pgmq:
                try:
                    await cur.execute(
                        "SELECT pgmq.send(%s, %s);",
                        (queue_name, json.dumps(message)),
                    )
                    await conn.commit()
                    return
                except Exception as e:
                    logger.warning(f"pgmq.send failed ({e}); falling back to table queue")

            await cur.execute(
                """
                INSERT INTO tether_queue_messages (queue_name, message, vt)
                VALUES (%s, %s, NOW());
                """,
                (queue_name, json.dumps(message)),
            )
            await conn.commit()


async def get_stale_in_flight_runs(max_stale_seconds: int = 120) -> list[Run]:
    """Retrieve runs stuck in committing or rolling_back states beyond threshold."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT * FROM runs
                WHERE state IN ('committing', 'rolling_back')
                  AND updated_at < NOW() - (%s * INTERVAL '1 second')
                FOR UPDATE SKIP LOCKED;
                """,
                (max_stale_seconds,),
            )
            rows = await cur.fetchall()
            await conn.commit()
            return [
                Run(
                    id=r["id"],
                    agent_id=r["agent_id"],
                    trigger=r["trigger"],
                    payload=r["payload"],
                    dedup_key=r["dedup_key"],
                    state=RunState(r["state"]),
                    attempt=r["attempt"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    error=r["error"],
                )
                for r in rows
            ]


# ------------------------------------------------------------------------------
# Queue Leasing Operations (pgmq + Fallback)
# ------------------------------------------------------------------------------


async def lease_next_job(
    queue_name: str = "agent_runs",
    vt_seconds: int = 60,
) -> Optional[dict[str, Any]]:
    """Lease next message from pgmq or table fallback using SELECT FOR UPDATE SKIP LOCKED."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            # Check if pgmq extension is present
            await cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgmq');")
            row = await cur.fetchone()
            has_pgmq = row["exists"] if row else False

            if has_pgmq:
                try:
                    await cur.execute(
                        "SELECT * FROM pgmq.read(%s, %s, 1);",
                        (queue_name, vt_seconds),
                    )
                    msg_row = await cur.fetchone()
                    if msg_row:
                        await conn.commit()
                        return {
                            "msg_id": msg_row["msg_id"],
                            "read_ct": msg_row["read_ct"],
                            "payload": msg_row["message"],
                            "is_pgmq": True,
                        }
                except Exception as e:
                    logger.warning(f"pgmq.read failed ({e}); falling back to table queue")

            # Fallback queue using SKIP LOCKED
            await cur.execute(
                """
                SELECT msg_id, read_ct, message
                FROM tether_queue_messages
                WHERE queue_name = %s
                  AND vt <= NOW()
                  AND archived_at IS NULL
                ORDER BY msg_id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1;
                """,
                (queue_name,),
            )
            fb_row = await cur.fetchone()
            if not fb_row:
                await conn.commit()
                return None

            msg_id = fb_row["msg_id"]
            await cur.execute(
                """
                UPDATE tether_queue_messages
                SET vt = NOW() + (%s * INTERVAL '1 second'),
                    read_ct = read_ct + 1
                WHERE msg_id = %s;
                """,
                (vt_seconds, msg_id),
            )
            await conn.commit()

            return {
                "msg_id": msg_id,
                "read_ct": fb_row["read_ct"] + 1,
                "payload": fb_row["message"],
                "is_pgmq": False,
            }


async def extend_lease(
    msg_id: int,
    queue_name: str = "agent_runs",
    vt_seconds: int = 60,
    is_pgmq: bool = False,
) -> None:
    """Heartbeat lease extension."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            if is_pgmq:
                try:
                    await cur.execute(
                        "SELECT pgmq.set_vt(%s, %s, %s);",
                        (queue_name, msg_id, vt_seconds),
                    )
                    await conn.commit()
                    return
                except Exception as e:
                    logger.warning(f"pgmq.set_vt failed ({e}); checking fallback")

            await cur.execute(
                """
                UPDATE tether_queue_messages
                SET vt = NOW() + (%s * INTERVAL '1 second')
                WHERE msg_id = %s;
                """,
                (vt_seconds, msg_id),
            )
            await conn.commit()


async def archive_job(
    msg_id: int,
    queue_name: str = "agent_runs",
    is_pgmq: bool = False,
) -> None:
    """Archive a cleanly finished message from the queue."""
    async with get_connection() as conn:
        async with conn.cursor() as cur:
            if is_pgmq:
                try:
                    await cur.execute("SELECT pgmq.archive(%s, %s);", (queue_name, msg_id))
                    await conn.commit()
                    return
                except Exception as e:
                    logger.warning(f"pgmq.archive failed: {e}")

            await cur.execute(
                """
                UPDATE tether_queue_messages
                SET archived_at = NOW()
                WHERE msg_id = %s;
                """,
                (msg_id,),
            )
            await conn.commit()

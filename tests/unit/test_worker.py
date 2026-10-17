"""Unit tests for the TetherWorker background lease and DLQ loop."""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tether.models import Run, RunState
from tether.worker import TetherWorker


@pytest.mark.asyncio
async def test_worker_empty_queue():
    """Verify worker returns False when queue is empty, enabling backoff."""
    worker = TetherWorker()

    with patch("tether.worker.lease_next_job", new=AsyncMock(return_value=None)):
        processed = await worker.process_one_job()
        assert processed is False


@pytest.mark.asyncio
async def test_worker_successful_job_execution():
    """Verify worker leases message, calls engine, and cleanly archives."""
    run_id = uuid4()
    msg_id = 42

    job = {
        "msg_id": msg_id,
        "read_ct": 1,
        "payload": {"run_id": str(run_id)},
        "is_pgmq": True,
    }

    mock_engine = AsyncMock()
    worker = TetherWorker(engine=mock_engine, max_attempts=5)

    with (
        patch("tether.worker.lease_next_job", new=AsyncMock(return_value=job)),
        patch("tether.worker.archive_job", new=AsyncMock()) as mock_archive,
        patch("tether.worker.extend_lease", new=AsyncMock()) as mock_extend,
    ):
        processed = await worker.process_one_job()

        assert processed is True
        mock_engine.execute_run.assert_awaited_once_with(run_id)
        mock_archive.assert_awaited_once_with(msg_id, queue_name="agent_runs", is_pgmq=True)


@pytest.mark.asyncio
async def test_worker_max_attempts_exceeded_moves_to_dlq():
    """Verify repeated worker failures move run to DEAD_LETTERED and archive from queue."""
    run_id = uuid4()
    msg_id = 99
    existing_run = Run(id=run_id, agent_id="triage", state=RunState.QUEUED)

    # read_ct is 6, exceeding max_attempts=5
    job = {
        "msg_id": msg_id,
        "read_ct": 6,
        "payload": {"run_id": str(run_id)},
        "is_pgmq": False,
    }

    mock_engine = AsyncMock()
    worker = TetherWorker(engine=mock_engine, max_attempts=5)

    with (
        patch("tether.worker.lease_next_job", new=AsyncMock(return_value=job)),
        patch("tether.worker.get_run", new=AsyncMock(return_value=existing_run)),
        patch("tether.worker.update_run_state", new=AsyncMock()) as mock_update_state,
        patch("tether.worker.emit_event", new=AsyncMock()) as mock_emit_event,
        patch("tether.worker.archive_job", new=AsyncMock()) as mock_archive,
    ):
        processed = await worker.process_one_job()

        assert processed is True
        # Engine should NOT be called for dead lettered runs
        mock_engine.execute_run.assert_not_called()

        # Run state updated to DEAD_LETTERED
        mock_update_state.assert_awaited_once_with(
            run_id=run_id,
            target_state=RunState.DEAD_LETTERED,
            error="Exhausted max attempts (6/5)",
        )
        mock_emit_event.assert_awaited_once()
        mock_archive.assert_awaited_once_with(msg_id, queue_name="agent_runs", is_pgmq=False)

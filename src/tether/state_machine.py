"""Finite State Machine for Tether Agent Runs.

Enforces valid lifecycle transitions and rejects illegal mutations.
"""

from typing import Set

from tether.models import RunState


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal run state transition is attempted."""

    def __init__(self, current_state: RunState, target_state: RunState, run_id: str | None = None):
        msg = f"Invalid state transition: Cannot transition run from '{current_state.value}' to '{target_state.value}'"
        if run_id:
            msg = f"Run {run_id}: {msg}"
        super().__init__(msg)
        self.current_state = current_state
        self.target_state = target_state
        self.run_id = run_id


# Explicit State Transition Table
LEGAL_TRANSITIONS: dict[RunState, Set[RunState]] = {
    # Newly enqueued run picked up by worker
    RunState.QUEUED: {
        RunState.RUNNING,
        RunState.DEAD_LETTERED,
        RunState.FAILED,
    },
    # Active reasoning and step execution
    RunState.RUNNING: {
        RunState.AWAITING_APPROVAL,  # Has staged effects requiring human review
        RunState.COMMITTING,         # Pre-approved or internal-only commit
        RunState.COMMITTED,          # Pure read agent with zero side effects
        RunState.FAILED,             # Unhandled execution exception
        RunState.DEAD_LETTERED,      # Max retry exhaustion
    },
    # Paused at the human review gate
    RunState.AWAITING_APPROVAL: {
        RunState.APPROVED,           # Human reviewer accepted
        RunState.REJECTED,           # Human reviewer rejected
        RunState.FAILED,             # Lease expiry / timeout
    },
    # Human approved; ready to execute staged effects
    RunState.APPROVED: {
        RunState.COMMITTING,
        RunState.FAILED,
    },
    # Executing staged effects in sequential order
    RunState.COMMITTING: {
        RunState.COMMITTED,          # All effects committed successfully
        RunState.ROLLING_BACK,       # Failure during commit; unwinding compensations
        RunState.FAILED,             # Catastrophic failure
    },
    # Unwinding compensations in reverse order
    RunState.ROLLING_BACK: {
        RunState.ROLLED_BACK,        # All compensations executed cleanly
        RunState.ROLLBACK_FAILED,    # A compensation itself failed; requires manual intervention
        RunState.FAILED,             # Unhandled rollback error
    },
    # Terminal States (no outgoing transitions allowed)
    RunState.COMMITTED: set(),
    RunState.REJECTED: set(),
    RunState.ROLLED_BACK: set(),
    RunState.ROLLBACK_FAILED: set(),
    RunState.FAILED: set(),
    RunState.DEAD_LETTERED: set(),
}

TERMINAL_STATES: Set[RunState] = {
    RunState.COMMITTED,
    RunState.REJECTED,
    RunState.ROLLED_BACK,
    RunState.ROLLBACK_FAILED,
    RunState.FAILED,
    RunState.DEAD_LETTERED,
}


def validate_transition(current_state: RunState, target_state: RunState, run_id: str | None = None) -> None:
    """Validate that moving from current_state to target_state is permitted.

    Raises:
        InvalidStateTransitionError: If the transition is not in the legal transition table.
    """
    if current_state == target_state:
        # Idempotent no-op
        return

    allowed = LEGAL_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise InvalidStateTransitionError(current_state, target_state, run_id)


def is_terminal_state(state: RunState) -> bool:
    """Check if the given state is terminal."""
    return state in TERMINAL_STATES

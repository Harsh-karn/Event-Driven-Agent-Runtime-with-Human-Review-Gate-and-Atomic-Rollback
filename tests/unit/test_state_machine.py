"""Unit tests for the Tether Finite State Machine."""

import pytest
from tether.models import RunState
from tether.state_machine import (
    InvalidStateTransitionError,
    is_terminal_state,
    validate_transition,
)


def test_valid_happy_path_transitions():
    """Verify normal execution lifecycle transitions."""
    # queued -> running
    validate_transition(RunState.QUEUED, RunState.RUNNING)

    # running -> awaiting_approval
    validate_transition(RunState.RUNNING, RunState.AWAITING_APPROVAL)

    # awaiting_approval -> approved
    validate_transition(RunState.AWAITING_APPROVAL, RunState.APPROVED)

    # approved -> committing
    validate_transition(RunState.APPROVED, RunState.COMMITTING)

    # committing -> committed
    validate_transition(RunState.COMMITTING, RunState.COMMITTED)


def test_pure_read_agent_transition():
    """Verify an agent with zero side effects can commit directly."""
    validate_transition(RunState.RUNNING, RunState.COMMITTED)


def test_rejection_flow_transitions():
    """Verify reviewer rejection terminates execution without committing."""
    validate_transition(RunState.RUNNING, RunState.AWAITING_APPROVAL)
    validate_transition(RunState.AWAITING_APPROVAL, RunState.REJECTED)


def test_rollback_lifecycle_transitions():
    """Verify atomic rollback lifecycle transitions."""
    # committing -> rolling_back
    validate_transition(RunState.COMMITTING, RunState.ROLLING_BACK)

    # rolling_back -> rolled_back (clean reverse compensation)
    validate_transition(RunState.ROLLING_BACK, RunState.ROLLED_BACK)


def test_rollback_failed_transition():
    """Verify compensation failure transitions to rollback_failed."""
    validate_transition(RunState.ROLLING_BACK, RunState.ROLLBACK_FAILED)


def test_dead_letter_transitions():
    """Verify max attempt exhaustion transitions to dead_lettered."""
    validate_transition(RunState.QUEUED, RunState.DEAD_LETTERED)
    validate_transition(RunState.RUNNING, RunState.DEAD_LETTERED)


def test_idempotent_self_transitions():
    """Verify transitioning to the same state is an idempotent no-op."""
    for state in RunState:
        validate_transition(state, state)


def test_invalid_illegal_transitions():
    """Verify illegal jumps are strictly prohibited."""
    # Cannot jump from queued straight to committed
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(RunState.QUEUED, RunState.COMMITTED)

    # Cannot commit from awaiting_approval without approved state
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(RunState.AWAITING_APPROVAL, RunState.COMMITTING)

    # Cannot approve a run that has already been rejected
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(RunState.REJECTED, RunState.APPROVED)

    # Terminal states cannot transition anywhere else
    terminal_states = [
        RunState.COMMITTED,
        RunState.REJECTED,
        RunState.ROLLED_BACK,
        RunState.ROLLBACK_FAILED,
        RunState.FAILED,
        RunState.DEAD_LETTERED,
    ]
    for terminal in terminal_states:
        assert is_terminal_state(terminal) is True
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(terminal, RunState.RUNNING)

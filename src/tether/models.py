"""Core Pydantic domain models and enumerations for Tether."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


class RunState(str, Enum):
    """Finite State Machine states for an Agent Run."""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    COMMITTING = "committing"
    COMMITTED = "committed"
    REJECTED = "rejected"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


class StepStatus(str, Enum):
    """Execution status of an individual step."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EffectStatus(str, Enum):
    """Lifecycle status of a staged side effect."""

    STAGED = "staged"
    COMMITTING = "committing"
    COMMITTED = "committed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"


class ApprovalDecision(str, Enum):
    """Decision made at the human review gate."""

    APPROVED = "approved"
    REJECTED = "rejected"


class EventType(str, Enum):
    """Categories of structured telemetry events."""

    RUN_ENQUEUED = "run_enqueued"
    RUN_STARTED = "run_started"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    EFFECT_STAGED = "effect_staged"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVAL_DECISION = "approval_decision"
    COMMIT_STARTED = "commit_started"
    EFFECT_COMMITTED = "effect_committed"
    ROLLBACK_STARTED = "rollback_started"
    EFFECT_COMPENSATED = "effect_compensated"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    RUN_DEAD_LETTERED = "run_dead_lettered"


class Run(BaseModel):
    """Root execution entity representing an agent invocation."""

    id: UUID = Field(default_factory=uuid4)
    agent_id: str
    trigger: str = "api"
    payload: dict[str, Any] = Field(default_factory=dict)
    dedup_key: Optional[str] = None
    state: RunState = RunState.QUEUED
    attempt: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: Optional[str] = None


class Step(BaseModel):
    """Checkpointed step of execution (model call, tool execution, decision)."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    idx: int
    kind: str  # 'model_call', 'read_tool', 'effect_staging', 'decision'
    input: dict[str, Any] = Field(default_factory=dict)
    output: Optional[dict[str, Any]] = None
    status: StepStatus = StepStatus.PENDING
    idempotency_key: str
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: Optional[datetime] = None
    error: Optional[str] = None


class StagedEffect(BaseModel):
    """Deferred external mutation with a compensating action."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    idx: int
    target_system: str  # e.g., 'slack', 'github', 'stripe', 'database'
    action: str  # e.g., 'send_message', 'create_branch', 'refund'
    params: dict[str, Any] = Field(default_factory=dict)
    compensation: dict[str, Any] = Field(default_factory=dict)
    status: EffectStatus = EffectStatus.STAGED
    idempotency_key: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    staged_at: datetime = Field(default_factory=utc_now)
    committed_at: Optional[datetime] = None
    compensated_at: Optional[datetime] = None


class Approval(BaseModel):
    """Human review gate sign-off or rejection record."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    decided_by: str
    decision: ApprovalDecision
    reason: Optional[str] = None
    decided_at: datetime = Field(default_factory=utc_now)
    effects_hash: str


class Event(BaseModel):
    """Structured outbox event emitted for warehouse logging and reconstruction."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_id: Optional[UUID] = None
    ts: datetime = Field(default_factory=utc_now)
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: Optional[str] = None
    exported_at: Optional[datetime] = None


class EnqueueRequest(BaseModel):
    """API request payload to enqueue an agent run."""

    agent_id: str
    trigger: str = "api"
    payload: dict[str, Any] = Field(default_factory=dict)
    dedup_key: Optional[str] = None


class EnqueueResponse(BaseModel):
    """API response for run enqueueing."""

    run_id: UUID
    is_duplicate: bool
    state: RunState

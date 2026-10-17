"""Tether: A durable, event-triggered, model-agnostic agent runtime.

Features:
- Event-triggered queue with Postgres/pgmq leases and heartbeat.
- Durable step engine with cryptographic idempotency keys.
- Staged effects with zero side-effects prior to human review.
- Atomic rollback with reverse compensating actions.
- Outbox event streaming to BigQuery for run reconstruction and cost tracking.
"""

__version__ = "0.1.0"

# ADR 002: Queue Architecture — Supabase pgmq on PostgreSQL with SKIP LOCKED Fallback

- **Status**: Accepted
- **Deciders**: Harsh Karn, Architecture & Runtime Team
- **Date**: 2026-10-17
- **Technical Area**: Infrastructure / Messaging & Queueing / Ingestion

---

## Context & Problem Statement

Tether requires a queuing mechanism to buffer webhook events, scheduled executions, and API triggers. The worker processes runs asynchronously from the queue.

Key queuing requirements:
1. **Atomic Ingestion**: Inserting a `runs` row and scheduling it on the queue must happen atomically within a single database transaction. If the transaction rolls back, no orphaned queue message must exist; if the queue fails, no orphaned database row must persist (solving the Dual-Write Problem).
2. **Visibility Timeouts (Leases)**: When a worker picks up a run, it must lease the job for a configurable duration (e.g., 60 seconds). If the worker crashes (`kill -9`), the visibility timeout expires, and another worker picks it up automatically.
3. **Heartbeat Extensions**: Long-running model calls or complex reasoning steps must extend the lease proactively.
4. **Dead-Letter Queue (DLQ)**: Poison runs that fail repeatedly must be moved to an archive / DLQ after $N$ attempts (default: 5) to prevent infinite retry loops.
5. **Operational Simplicity**: Minimize the infrastructure footprint and avoid managing separate external broker clusters.

---

## Considered Options

1. **Option A**: Supabase Queues (`pgmq` extension on PostgreSQL) with `SELECT ... FOR UPDATE SKIP LOCKED` Fallback (**Selected**)
2. **Option B**: Redis with Celery / ARQ
3. **Option C**: AWS SQS / GCP Cloud Tasks / PubSub
4. **Option D**: RabbitMQ

---

## Detailed Comparison

| Evaluation Dimension | Option A: pgmq (PostgreSQL) | Option B: Redis / Celery | Option C: SQS / Cloud Tasks | Option D: RabbitMQ |
| :--- | :--- | :--- | :--- | :--- |
| **Transactional Atomicity** | **Native**: `INSERT INTO runs` + `pgmq.send()` in the same ACID transaction. Zero dual-write hazard. | Impossible: Database and Redis cannot participate in a shared 2-phase commit without complex 2PC overhead. | Impossible: External cloud service cannot participate in a local DB transaction. | Impossible: Dual-write risk requires complex transactional outbox for queueing. |
| **Visibility Timeout & Leases** | **Built-in**: `pgmq.read(queue, vt=60, limit=1)` and `pgmq.set_vt(queue, msg_id, vt)`. | Moderate: Requires Redis lock managers or Celery visibility timeout hacks. | Built-in: Native message lease visibility. | Complex: Requires manual ack rejection and requeue. |
| **Dead-Letter Handling** | **Built-in**: `pgmq.archive()` moves completed messages to an archive table; DLQ tables capture exhausted attempts. | High manual code: Must configure dead-letter queues explicitly in Celery/Redis. | Built-in: Configurable max-receive-count and dead-letter queues. | Built-in: Dead-letter exchanges (DLX). |
| **Operational Simplicity** | **Zero Extra Infrastructure**: Uses the existing Supabase / PostgreSQL instance. | High: Must manage, monitor, back up, and secure a Redis instance. | High: Cloud vendor lock-in; requires IAM, VPC connectors, and cloud credentials in local dev. | Very High: Erlang VM, clustering, persistent disk, connection tuning. |
| **Throughput & Latency** | Thousands of jobs/sec (more than sufficient for agent workloads). | Tens of thousands of jobs/sec (overkill for agent orchestration). | Cloud-scale. | Very High. |

---

## Decision Outcome

**Chosen Option**: **Option A — `pgmq` on PostgreSQL with a `SELECT FOR UPDATE SKIP LOCKED` fallback engine.**

### Implementation Details:
1. **Primary Queue**: `agent_runs` queue created via `SELECT pgmq.create('agent_runs');`.
2. **Dead-Letter Queue**: `agent_runs_dlq` queue created via `SELECT pgmq.create('agent_runs_dlq');`.
3. **Atomic Enqueue**:
   ```sql
   BEGIN;
     INSERT INTO runs (id, agent_id, trigger, payload, state)
     VALUES (v_run_id, p_agent_id, p_trigger, p_payload, 'queued');

     PERFORM pgmq.send('agent_runs', jsonb_build_object('run_id', v_run_id));
   COMMIT;
   ```
4. **Worker Lease Loop**:
   - `pgmq.read('agent_runs', vt => 60, qty => 1)` retrieves the next available message.
   - A concurrent background `asyncio` task sends heartbeats every 20 seconds via `pgmq.set_vt(...)`.
   - On completion, `pgmq.archive('agent_runs', msg_id)` cleanly archives the message.
   - On exhaustion of `MAX_ATTEMPTS`, the message is routed to `agent_runs_dlq`.
5. **Fallback Safety**:
   If the `pgmq` extension is unavailable in a target PostgreSQL environment, Tether seamlessly switches to a pure SQL table-based queue utilizing:
   ```sql
   SELECT id FROM jobs
   WHERE status = 'queued' AND visible_at <= NOW()
   ORDER BY id
   FOR UPDATE SKIP LOCKED
   LIMIT 1;
   ```

---

## Consequences & Trade-offs

- **Database Load**: Queues on PostgreSQL generate write and vacuum overhead on the message tables. For agent runtime workloads (where individual runs take seconds to minutes and total volume is thousands of events per hour rather than millions per second), PostgreSQL easily handles the workload without performance degradation.
- **Single Source of Truth**: All operational state—runs, steps, staged effects, approvals, and queue messages—resides in one ACID-compliant database.

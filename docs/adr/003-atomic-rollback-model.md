# ADR 003: Compensation-Based Atomic Rollback Across External Systems

- **Status**: Accepted
- **Deciders**: Harsh Karn, Architecture & Runtime Team
- **Date**: 2026-10-17
- **Technical Area**: Execution Engine / Side Effect Management / Failure Recovery

---

## Context & Problem Statement

LLM agents are tasked with making changes in the real world: sending emails, posting messages to Slack, provisioning cloud infrastructure, opening pull requests, or updating CRM records.

A critical failure mode arises when an agent executes 4 distinct external actions, but action 3 fails (e.g., API rate limit, 500 server error, expired OAuth token). Without a rollback mechanism:
- The system is left in an inconsistent, half-mutated state.
- Retrying the entire batch from the beginning risks duplicating actions 1 and 2 (e.g., sending the same customer two emails).
- Rolling back external SaaS systems is fundamentally challenging because third-party APIs (Stripe, Slack, GitHub, Salesforce) do **not** participate in distributed two-phase commit (2PC) transactions.

We must formulate an architectural guarantee for "atomic rollback" in an environment composed of distributed, heterogeneous external APIs.

---

## Architectural Reality: The Myth of External ACID Atomicity

**True ACID atomicity across arbitrary external APIs does not exist.**
Distributed transactions requiring 2PC (`PREPARE -> COMMIT`) are unsupported by commercial REST/GraphQL APIs. Once an email is sent via SendGrid or a message is posted to Slack, the database transaction boundary has been breached.

Therefore, Tether adopts the **Saga Pattern (Compensating Transactions)** paired with **Client-Side Idempotency** and **Operational Alerting**.

---

## The Tether Solution: Staging + Reverse Compensation Unwind

Tether decomposes side effect execution into three distinct phases:

### Phase 1: Staging (Reasoning Phase)
- Tools designated as *effect tools* are intercepted by the runtime.
- The runtime **never** executes the effect tool immediately.
- Instead, it generates a row in `staged_effects` containing:
  - `idx`: Order index.
  - `target_system`: e.g., `slack`, `github`, `database`.
  - `action`: Forward mutation name (e.g., `send_message`, `create_branch`).
  - `params`: JSON payload for the forward action.
  - `compensation`: JSON definition of the reverse compensating action (e.g., `delete_message`, `delete_branch`).
  - `idempotency_key`: Deterministic hash `sha256(run_id + idx + action + params)`.
  - `status`: `staged`.
- The tool returns a synthetic staging response to the LLM context (e.g., `{"status": "staged", "staged_idx": 1}`).

### Phase 2: Cryptographic Human Review Gate
- When agent reasoning completes, the system computes:
  $$\text{effects\_hash} = \text{SHA256}\left(\sum_{i=1}^N \text{canonical\_json}(e_i)\right)$$
- The human reviewer inspects the proposed actions.
- The approval record stores `effects_hash`.
- The commit engine asserts that the database hash matches the approved hash before firing any API calls. If any parameter was tampered with, the commit is aborted.

### Phase 3: In-Order Commit & Reverse Compensation
- During the commit phase, the runtime executes effects $1, 2, \dots, N$ in sequential order.
- Each external API call is dispatched with its unique `idempotency_key` (via HTTP headers where supported).
- When forward action $k$ completes, any dynamic return IDs (e.g., `message_id = "msg_123"`) are backfilled into the compensation payload.
- **On Failure of Effect $k$**:
  1. Forward execution halts immediately.
  2. The runtime switches state from `committing` to `rolling_back`.
  3. The engine invokes the compensation actions for completed effects in strict reverse order:
     $$\text{Compensate}(k-1) \longrightarrow \text{Compensate}(k-2) \longrightarrow \dots \longrightarrow \text{Compensate}(1)$$
  4. Each successfully compensated row is updated to `status = 'compensated'`.
  5. The run state is marked `rolled_back`.

---

## Handling Compensation Failures: The `rollback_failed` Deadlock

What happens if a compensating action itself fails (e.g., Slack returns 503 while trying to delete a message)?

1. The runtime catches the exception.
2. The specific effect is marked `status = 'compensation_failed'`.
3. The overall run state transitions to `rollback_failed`.
4. A high-priority PagerDuty/Slack incident alert is fired with the complete run context, the exact failed compensation payload, and the remote entity IDs.
5. All context is permanently preserved in the PostgreSQL state store and exported to BigQuery so a human operator can manually complete the remediation.

---

## Comparison of Rollback Models

| Approach | Resilience | Implementation Complexity | Honest Assessment |
| :--- | :--- | :--- | :--- |
| **Direct Execution (No Staging)** | None | Zero | Leaves broken state on failures; repeats side effects on worker retry. |
| **All-or-Nothing 2PC** | Theoretical only | Impossible | Unsupported by external SaaS vendors. |
| **Staging + Saga Compensation (Tether)** | **High** | Moderate | Handles 99% of transient and business failures cleanly; explicit escape hatch for catastrophic compensation failures. |

---

## Decision Outcome

**Adopt Staging + Reverse Saga Compensations + Cryptographic Gate Verification.**
This model guarantees that:
1. No side effects occur before human approval.
2. Retries do not duplicate side effects.
3. Partial failures unwind gracefully.
4. Non-compensable edge cases trigger explicit human operational alerts rather than failing silently.

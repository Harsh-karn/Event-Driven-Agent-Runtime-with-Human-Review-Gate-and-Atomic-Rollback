# ADR 001: Own Execution Loop vs Adopting an Agent Framework

- **Status**: Accepted
- **Deciders**: Harsh Karn, Architecture & Runtime Team
- **Date**: 2026-10-17
- **Technical Area**: Runtime Core / Execution Engine / LLM Abstraction

---

## Context & Problem Statement

Building an enterprise-grade agent runtime requires deciding whether to build on top of an established agent orchestration framework (such as LangChain, CrewAI, AutoGen, or LlamaIndex) or to implement a purpose-built, headless execution loop with a thin abstraction over a unified provider client (LiteLLM).

The runtime must fulfill strict production requirements:
1. **Durable step-level checkpointing**: Every thought, tool call, and state transition must persist to PostgreSQL with cryptographic idempotency keys so that worker crashes (`kill -9`) resume seamlessly without re-executing completed work.
2. **Deterministic run reconstruction**: Runs must be 100% reconstructible from warehouse logs alone.
3. **Staged side effects with human review**: Effect tools must be intercepted and staged—never executed directly during reasoning—and only committed upon cryptographic approval of `effects_hash`.
4. **Atomic rollback via reverse compensation**: If effect $k$ fails during commit, effects $k-1 \dots 1$ must unwind in reverse order.
5. **Zero framework leakage**: Provider-specific SDK models (OpenAI, Anthropic, Bedrock) must never leak into domain models or database rows.

---

## Decision Drivers

* **Control over State & Checkpointing**: Ability to inject transactional persistence before and after each discrete step (model call, tool execution, decision).
* **Determinism for Replay**: Replaying an execution requires zero hidden framework state, magical callbacks, or implicit memory buffers.
* **Ease of Attaching Human Gate & Rollback**: Clean separation between immediate *read tools* and deferred *staged effect tools*.
* **Dependency Weight & Security Perimeter**: Frameworks pull hundreds of transitive dependencies, leading to bloat, dependency conflicts, and vulnerability surface area.
* **Debuggability & Observability**: Complete stack trace clarity without navigating deep framework abstraction layers.

---

## Considered Options

1. **Option A**: Build a Custom Headless Loop over `Runtime` Protocol (LiteLLM) (**Selected**)
2. **Option B**: Adopt LangChain / LangGraph
3. **Option C**: Adopt CrewAI
4. **Option D**: Adopt LlamaIndex Workflows

---

## Detailed Comparison

| Evaluation Dimension | Option A: Custom Headless Loop (Tether) | Option B: LangGraph / LangChain | Option C: CrewAI | Option D: LlamaIndex |
| :--- | :--- | :--- | :--- | :--- |
| **Control over Checkpointing** | **Total**: Explicit pre-step and post-step transactional writes in PostgreSQL with idempotency keys. | Partial: Relies on framework checkpointer adapters; schema is coupled to framework internals. | Low: In-memory state by default; external persistence is an afterthought. | Moderate: Event-based checkpointing, but tightly coupled to LlamaIndex abstractions. |
| **Side Effect Staging & Interception** | **Native**: Tools declare intent (`read` vs `effect`); effects write to `staged_effects` and halt prior to execution. | Complex: Requires custom tool interceptors or monkey-patching tool execution nodes. | Difficult: Assumes tools run immediately when invoked by the agent. | Difficult: Requires custom event handlers and manual pipeline halts. |
| **Atomic Rollback & Compensation** | **Native**: Compensations stored alongside staged params; unwinds $k-1 \dots 1$ automatically on commit failure. | Non-existent: No native concept of compensating transactions for external systems. | Non-existent: Rollback must be built outside the framework entirely. | Non-existent: Rollbacks must be custom coded outside workflow boundaries. |
| **Determinism for Replay** | **100% Deterministic**: Pure state machine persisted to PostgreSQL outbox and BigQuery. Replay CLI runs from warehouse alone. | Fragile: Hidden runnables, implicit memory classes, and stateful callbacks obscure historical reconstruction. | Fragile: High degree of nondeterministic inter-agent messaging overhead. | Moderate: Workflow event logs can be dumped, but reconstruction requires framework runtime. |
| **Dependency Weight** | **Minimal**: `litellm`, `psycopg 3`, `pydantic v2`, `fastapi`. Installs cleanly under 10 seconds. | **Very High**: Hundreds of transitive dependencies, frequent breaking changes across minor versions. | **High**: Heavy dependency chain, rigid agent abstractions. | **High**: Heavy index and parser dependencies even when unused. |
| **Provider Independence** | Complete: LiteLLM handles OpenAI, Anthropic, Gemini, Vertex, Bedrock behind `complete(...) -> Completion`. | Moderate: Abstracted, but framework-specific message and document wrappers dominate. | Poor: Heavily opinionated role/crew structures. | Moderate: Custom node and schema formats. |

---

## Decision Outcome

**Chosen Option**: **Option A — Custom Headless Loop over LiteLLM.**

### Concrete Architecture
- We define a strict Python `Protocol`:
  ```python
  class Runtime(Protocol):
      async def complete(
          self,
          messages: list[dict[str, Any]],
          tools: list[dict[str, Any]] | None = None,
          *,
          model: str,
          run_id: str,
          step_id: str,
      ) -> Completion: ...
  ```
- The loop is governed by an explicit finite state machine (`queued -> running -> awaiting_approval -> approved -> committing -> committed`).
- Tools are partitioned:
  - **Read tools** return information directly to the LLM context.
  - **Effect tools** stage parameters and a designated compensating action into `staged_effects` table with a computed `idempotency_key`. They return a synthetic staging acknowledgment to the LLM.
- After reasoning halts, the runtime computes `effects_hash = sha256(staged_effects)` and transitions to `awaiting_approval`.

---

## Conditions Under Which This Decision Would Be Revisited

We will re-evaluate adopting an external framework if:
1. Multi-agent negotiation and decentralized consensus topologies (e.g., dynamic swarms with autonomous role negotiation) become the primary operational requirement rather than reliable workflow orchestration.
2. An industry-standard framework stabilizes an open, vendor-neutral specification for compensating transactions and external effect staging with zero runtime overhead.
3. The maintenance cost of the headless loop exceeds the maintenance overhead of tracking upstream framework deprecations and breaking changes.

-- ==============================================================================
-- Tether: Initial PostgreSQL / Supabase Schema Migration
-- Migration: 001_initial_schema.sql
-- ==============================================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ------------------------------------------------------------------------------
-- 1. Custom Types & State Enumerations
-- ------------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE run_state AS ENUM (
        'queued',
        'running',
        'awaiting_approval',
        'approved',
        'committing',
        'committed',
        'rejected',
        'rolling_back',
        'rolled_back',
        'failed',
        'dead_lettered'
    );
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE step_status AS ENUM (
        'pending',
        'running',
        'completed',
        'failed',
        'skipped'
    );
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE effect_status AS ENUM (
        'staged',
        'committing',
        'committed',
        'compensating',
        'compensated',
        'failed'
    );
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE approval_decision AS ENUM (
        'approved',
        'rejected'
    );
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- ------------------------------------------------------------------------------
-- 2. Core Tables
-- ------------------------------------------------------------------------------

-- Runs: Root execution entity
CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    trigger VARCHAR(50) NOT NULL, -- 'webhook', 'schedule', 'api', 'manual'
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedup_key VARCHAR(255) UNIQUE, -- Deduplication key (e.g., webhook event ID)
    state run_state NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);

-- Steps: Checkpointed steps of execution (model call, tool call, decision)
CREATE TABLE IF NOT EXISTS steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    kind VARCHAR(50) NOT NULL, -- 'model_call', 'read_tool', 'effect_staging', 'decision'
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    output JSONB,
    status step_status NOT NULL DEFAULT 'pending',
    idempotency_key VARCHAR(255) NOT NULL UNIQUE, -- SHA256(run_id + idx + input_hash)
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    error TEXT,
    CONSTRAINT uq_run_step_idx UNIQUE(run_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_steps_run_id ON steps(run_id, idx);

-- Staged Effects: Deferred external mutations with compensations
CREATE TABLE IF NOT EXISTS staged_effects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    target_system VARCHAR(100) NOT NULL, -- e.g., 'slack', 'github', 'database', 'stripe'
    action VARCHAR(100) NOT NULL,        -- e.g., 'send_message', 'create_branch', 'refund'
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    compensation JSONB NOT NULL DEFAULT '{}'::jsonb, -- Action & params to unwind this effect
    status effect_status NOT NULL DEFAULT 'staged',
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,    -- SHA256(run_id + idx + action + params)
    result JSONB,                                    -- Execution return value (e.g., remote IDs)
    error TEXT,
    staged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    committed_at TIMESTAMPTZ,
    compensated_at TIMESTAMPTZ,
    CONSTRAINT uq_run_effect_idx UNIQUE(run_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_staged_effects_run ON staged_effects(run_id, idx);
CREATE INDEX IF NOT EXISTS idx_staged_effects_status ON staged_effects(status);

-- Approvals: Human review gate decisions
CREATE TABLE IF NOT EXISTS approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    decided_by VARCHAR(255) NOT NULL, -- Reviewer email or ID
    decision approval_decision NOT NULL,
    reason TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effects_hash VARCHAR(64) NOT NULL, -- Cryptographic SHA256 hash of staged effects seen by human
    CONSTRAINT uq_run_approval UNIQUE(run_id)
);

CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id);

-- Events: Outbox for structured logging, BigQuery export, and replay reconstruction
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES steps(id) ON DELETE SET NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type VARCHAR(50) NOT NULL, -- 'run_started', 'model_call', 'tool_call', 'state_transition', 'approval', 'compensation', 'run_finished'
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    model VARCHAR(100),
    exported_at TIMESTAMPTZ -- NULL until exported to BigQuery warehouse
);

CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, ts ASC);
CREATE INDEX IF NOT EXISTS idx_events_unexported ON events(exported_at) WHERE exported_at IS NULL;

-- ------------------------------------------------------------------------------
-- 3. Queue Architecture (pgmq with Table-based Fallback)
-- ------------------------------------------------------------------------------

-- Fallback queue table (used if pgmq extension is not installed)
CREATE TABLE IF NOT EXISTS tether_queue_messages (
    msg_id BIGSERIAL PRIMARY KEY,
    queue_name VARCHAR(100) NOT NULL,
    message JSONB NOT NULL,
    vt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_ct INTEGER NOT NULL DEFAULT 0,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tether_queue_poll 
ON tether_queue_messages(queue_name, vt) 
WHERE archived_at IS NULL;

-- Setup pgmq queues if extension exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgmq') THEN
        CREATE EXTENSION IF NOT EXISTS pgmq CASCADE;
        PERFORM pgmq.create('agent_runs');
        PERFORM pgmq.create('agent_runs_dlq');
    END IF;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'pgmq extension not available; using tether_queue_messages fallback';
END $$;

-- ------------------------------------------------------------------------------
-- 4. Atomic Transactional Enqueue Function
-- ------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION tether_enqueue(
    p_agent_id VARCHAR(100),
    p_trigger VARCHAR(50),
    p_payload JSONB DEFAULT '{}'::jsonb,
    p_dedup_key VARCHAR(255) DEFAULT NULL
)
RETURNS TABLE (
    run_id UUID,
    is_duplicate BOOLEAN,
    state run_state
) 
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_run_id UUID;
    v_existing_state run_state;
    v_has_pgmq BOOLEAN := FALSE;
BEGIN
    -- Check deduplication key first
    IF p_dedup_key IS NOT NULL THEN
        SELECT id, runs.state INTO v_run_id, v_existing_state
        FROM runs
        WHERE dedup_key = p_dedup_key;

        IF FOUND THEN
            RETURN QUERY SELECT v_run_id, TRUE, v_existing_state;
            RETURN;
        END IF;
    END IF;

    -- Generate new Run ID
    v_run_id := gen_random_uuid();

    -- Insert new run safely guarded against concurrent deduplication races
    BEGIN
        INSERT INTO runs (id, agent_id, trigger, payload, dedup_key, state, attempt, created_at, updated_at)
        VALUES (v_run_id, p_agent_id, p_trigger, p_payload, p_dedup_key, 'queued', 0, NOW(), NOW());
    EXCEPTION WHEN unique_violation THEN
        SELECT id, runs.state INTO v_run_id, v_existing_state
        FROM runs
        WHERE dedup_key = p_dedup_key;
        RETURN QUERY SELECT v_run_id, TRUE, v_existing_state;
        RETURN;
    END;

    -- Log run_started event to outbox
    INSERT INTO events (run_id, type, payload)
    VALUES (
        v_run_id,
        'run_enqueued',
        jsonb_build_object(
            'agent_id', p_agent_id,
            'trigger', p_trigger,
            'payload', p_payload,
            'dedup_key', p_dedup_key
        )
    );

    -- Send message to queue
    SELECT EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'pgmq'
    ) INTO v_has_pgmq;

    IF v_has_pgmq THEN
        PERFORM pgmq.send('agent_runs', jsonb_build_object('run_id', v_run_id));
    ELSE
        INSERT INTO tether_queue_messages (queue_name, message, vt)
        VALUES ('agent_runs', jsonb_build_object('run_id', v_run_id), NOW());
    END IF;

    RETURN QUERY SELECT v_run_id, FALSE, 'queued'::run_state;
END;
$$;

-- ------------------------------------------------------------------------------
-- 5. Row Level Security (RLS) Policies
-- ------------------------------------------------------------------------------

ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE staged_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tether_queue_messages ENABLE ROW LEVEL SECURITY;

-- Service Role (Worker and Backend API) has full access bypass
DROP POLICY IF EXISTS service_role_all_runs ON runs;
CREATE POLICY service_role_all_runs ON runs FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS service_role_all_steps ON steps;
CREATE POLICY service_role_all_steps ON steps FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS service_role_all_effects ON staged_effects;
CREATE POLICY service_role_all_effects ON staged_effects FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS service_role_all_approvals ON approvals;
CREATE POLICY service_role_all_approvals ON approvals FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS service_role_all_events ON events;
CREATE POLICY service_role_all_events ON events FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS service_role_all_queue ON tether_queue_messages;
CREATE POLICY service_role_all_queue ON tether_queue_messages FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Authenticated Users (Review UI Reviewers) can read runs, steps, effects, events and submit approvals
DROP POLICY IF EXISTS auth_read_runs ON runs;
CREATE POLICY auth_read_runs ON runs FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS auth_read_steps ON steps;
CREATE POLICY auth_read_steps ON steps FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS auth_read_effects ON staged_effects;
CREATE POLICY auth_read_effects ON staged_effects FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS auth_read_events ON events;
CREATE POLICY auth_read_events ON events FOR SELECT TO authenticated USING (true);

DROP POLICY IF EXISTS auth_manage_approvals ON approvals;
CREATE POLICY auth_manage_approvals ON approvals FOR ALL TO authenticated USING (true) WITH CHECK (true);

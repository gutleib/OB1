-- OB1 Russian Fork — Self-Hosted Schema
-- PostgreSQL 16 + pgvector
-- Adapted from NateBJones-Projects/OB1 (FSL-1.1-MIT)
-- Changes: vector(1536) → vector(1024) for USER-bge-m3, no Supabase RLS/service_role

BEGIN;

-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- Core thoughts table (adapted from docs/01-getting-started.md)
-- ============================================================================

CREATE TABLE IF NOT EXISTS thoughts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content TEXT NOT NULL,
  embedding vector(1024),
  metadata JSONB DEFAULT '{}'::jsonb,
  content_fingerprint TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Vector similarity search index
CREATE INDEX IF NOT EXISTS idx_thoughts_embedding
  ON thoughts USING hnsw (embedding vector_cosine_ops);

-- Metadata filtering index
CREATE INDEX IF NOT EXISTS idx_thoughts_metadata
  ON thoughts USING gin (metadata);

-- Date range index
CREATE INDEX IF NOT EXISTS idx_thoughts_created_at
  ON thoughts (created_at DESC);

-- Fingerprint dedup index
CREATE UNIQUE INDEX IF NOT EXISTS idx_thoughts_fingerprint
  ON thoughts (content_fingerprint)
  WHERE content_fingerprint IS NOT NULL;

-- Auto-update timestamp
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_thoughts_updated_at ON thoughts;
CREATE TRIGGER trg_thoughts_updated_at
  BEFORE UPDATE ON thoughts
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ============================================================================
-- Semantic search function
-- ============================================================================

CREATE OR REPLACE FUNCTION match_thoughts(
  query_embedding vector(1024),
  match_threshold FLOAT DEFAULT 0.5,
  match_count INT DEFAULT 10,
  filter JSONB DEFAULT '{}'::jsonb
)
RETURNS TABLE (
  id UUID,
  content TEXT,
  metadata JSONB,
  similarity FLOAT,
  created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    t.id,
    t.content,
    t.metadata,
    1 - (t.embedding <=> query_embedding) AS similarity,
    t.created_at
  FROM thoughts t
  WHERE 1 - (t.embedding <=> query_embedding) > match_threshold
    AND (filter = '{}'::jsonb OR t.metadata @> filter)
  ORDER BY t.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

-- ============================================================================
-- Upsert function (content dedup)
-- ============================================================================

DROP FUNCTION IF EXISTS upsert_thought(TEXT, JSONB);
CREATE OR REPLACE FUNCTION upsert_thought(p_content TEXT, p_payload JSONB DEFAULT '{}')
RETURNS TABLE(id UUID, fingerprint TEXT) AS $$
DECLARE
  v_fingerprint TEXT;
  v_id UUID;
BEGIN
  v_fingerprint := encode(sha256(convert_to(
    lower(trim(regexp_replace(p_content, '\s+', ' ', 'g'))),
    'UTF8'
  )), 'hex');

  INSERT INTO thoughts (content, content_fingerprint, metadata)
  VALUES (p_content, v_fingerprint, COALESCE(p_payload->'metadata', '{}'::jsonb))
  ON CONFLICT (content_fingerprint) WHERE content_fingerprint IS NOT NULL DO UPDATE
  SET updated_at = now(),
      metadata = thoughts.metadata || COALESCE(EXCLUDED.metadata, '{}'::jsonb)
  RETURNING thoughts.id INTO v_id;

  RETURN QUERY SELECT v_id AS id, v_fingerprint AS fingerprint;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Agent Memory Schema (from schemas/agent-memory/schema.sql)
-- No RLS, no service_role — direct PostgreSQL access
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_memories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thought_id UUID REFERENCES thoughts(id) ON DELETE SET NULL,
  workspace_id TEXT NOT NULL,
  project_id TEXT,
  channel_kind TEXT,
  channel_id TEXT,
  channel_thread_id TEXT,
  visibility TEXT NOT NULL DEFAULT 'project' CHECK (
    visibility IN ('personal', 'channel', 'project', 'workspace', 'organization')
  ),
  memory_type TEXT NOT NULL CHECK (
    memory_type IN (
      'decision', 'output', 'lesson', 'constraint',
      'open_question', 'failure', 'artifact_reference', 'work_log'
    )
  ),
  summary TEXT NOT NULL,
  content TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL DEFAULT 'active' CHECK (
    lifecycle_status IN ('active', 'stale', 'superseded', 'disputed', 'rejected')
  ),
  provenance_status TEXT NOT NULL DEFAULT 'generated' CHECK (
    provenance_status IN (
      'observed', 'inferred', 'user_confirmed',
      'imported', 'generated', 'superseded', 'disputed'
    )
  ),
  confidence NUMERIC(3,2) NOT NULL DEFAULT 0.50 CHECK (confidence >= 0 AND confidence <= 1),
  created_by TEXT NOT NULL DEFAULT 'agent' CHECK (created_by IN ('user', 'agent', 'system', 'import')),
  runtime_name TEXT,
  runtime_version TEXT,
  provider TEXT,
  model TEXT,
  task_id TEXT,
  flow_id TEXT,
  can_use_as_instruction BOOLEAN NOT NULL DEFAULT false,
  can_use_as_evidence BOOLEAN NOT NULL DEFAULT true,
  requires_user_confirmation BOOLEAN NOT NULL DEFAULT true,
  review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
    review_status IN (
      'pending', 'confirmed', 'evidence_only',
      'restricted', 'rejected', 'stale', 'merged'
    )
  ),
  last_confirmed_at TIMESTAMPTZ,
  stale_after TIMESTAMPTZ,
  idempotency_key TEXT,
  content_hash TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    can_use_as_instruction = false
    OR provenance_status IN ('user_confirmed', 'imported')
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_memories_idempotency_key
  ON agent_memories (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_memories_scope
  ON agent_memories (workspace_id, project_id, visibility);

CREATE INDEX IF NOT EXISTS idx_agent_memories_review
  ON agent_memories (review_status, lifecycle_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_memories_runtime_task
  ON agent_memories (runtime_name, task_id, flow_id);

CREATE INDEX IF NOT EXISTS idx_agent_memories_content_hash
  ON agent_memories (workspace_id, content_hash) WHERE content_hash IS NOT NULL;

-- Source references
CREATE TABLE IF NOT EXISTS agent_memory_source_refs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  uri TEXT,
  title TEXT,
  source_timestamp TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_source_refs_memory
  ON agent_memory_source_refs (memory_id);

-- Artifacts
CREATE TABLE IF NOT EXISTS agent_memory_artifacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
  artifact_kind TEXT NOT NULL,
  uri TEXT NOT NULL,
  description TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_artifacts_memory
  ON agent_memory_artifacts (memory_id);

-- Relations
CREATE TABLE IF NOT EXISTS agent_memory_relations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
  to_memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
  relation TEXT NOT NULL CHECK (
    relation IN ('related_to', 'supersedes', 'superseded_by', 'conflicts_with', 'merged_into')
  ),
  confidence NUMERIC(3,2) DEFAULT 0.50 CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (from_memory_id, to_memory_id, relation),
  CHECK (from_memory_id <> to_memory_id)
);

-- Review actions
CREATE TABLE IF NOT EXISTS agent_memory_review_actions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (
    action IN (
      'confirm', 'edit', 'evidence_only', 'restrict_scope',
      'mark_stale', 'merge', 'reject', 'dispute', 'supersede'
    )
  ),
  actor_id TEXT,
  actor_label TEXT,
  notes TEXT,
  before JSONB,
  after JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_review_actions_memory
  ON agent_memory_review_actions (memory_id, created_at DESC);

-- Recall traces
CREATE TABLE IF NOT EXISTS agent_memory_recall_traces (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id UUID NOT NULL DEFAULT gen_random_uuid(),
  workspace_id TEXT NOT NULL,
  project_id TEXT,
  runtime_name TEXT,
  runtime_version TEXT,
  task_id TEXT,
  flow_id TEXT,
  channel_kind TEXT,
  channel_id TEXT,
  query TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (request_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_recall_traces_scope
  ON agent_memory_recall_traces (workspace_id, project_id, created_at DESC);

-- Recall items
CREATE TABLE IF NOT EXISTS agent_memory_recall_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id UUID NOT NULL REFERENCES agent_memory_recall_traces(id) ON DELETE CASCADE,
  memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  similarity NUMERIC(5,4),
  ranking_score NUMERIC(7,4),
  returned BOOLEAN NOT NULL DEFAULT true,
  used BOOLEAN,
  ignored_reason TEXT,
  use_policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (trace_id, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_recall_items_trace
  ON agent_memory_recall_items (trace_id, rank);

-- Audit events
CREATE TABLE IF NOT EXISTS agent_memory_audit_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL CHECK (
    event_type IN (
      'recall_requested', 'memory_returned', 'memory_used',
      'memory_ignored', 'memory_written', 'memory_confirmed',
      'memory_edited', 'memory_rejected', 'memory_superseded',
      'memory_disputed'
    )
  ),
  workspace_id TEXT,
  project_id TEXT,
  memory_id UUID REFERENCES agent_memories(id) ON DELETE SET NULL,
  trace_id UUID REFERENCES agent_memory_recall_traces(id) ON DELETE SET NULL,
  actor_kind TEXT NOT NULL DEFAULT 'system' CHECK (actor_kind IN ('user', 'agent', 'system', 'import')),
  actor_label TEXT,
  runtime_name TEXT,
  task_id TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_audit_scope
  ON agent_memory_audit_events (workspace_id, project_id, created_at DESC);

-- Hash function for content dedup
CREATE OR REPLACE FUNCTION agent_memory_hash_text(p_content TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
  RETURN encode(sha256(convert_to(
    lower(trim(regexp_replace(coalesce(p_content, ''), '\s+', ' ', 'g'))),
    'UTF8'
  )), 'hex');
END;
$$;

-- Auto-update for agent_memories
CREATE OR REPLACE FUNCTION agent_memories_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_memories_updated_at ON agent_memories;
CREATE TRIGGER trg_agent_memories_updated_at
  BEFORE UPDATE ON agent_memories
  FOR EACH ROW EXECUTE FUNCTION agent_memories_set_updated_at();

COMMIT;

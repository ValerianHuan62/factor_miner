-- 研究演化与正式记忆的只读投影；JSON/JSONL 主存储不依赖本表。
BEGIN;

CREATE TABLE IF NOT EXISTS research_evolution_batches (
    context_id TEXT PRIMARY KEY,
    context_sha256 CHAR(64) NOT NULL UNIQUE,
    discovery_family_id TEXT NOT NULL,
    approval_batch_hash CHAR(64),
    coverage_graph_id TEXT,
    coverage_graph_manifest_hash CHAR(64),
    memory_snapshot_hash CHAR(64),
    gap_report_hash CHAR(64),
    design_policy_hash CHAR(64),
    hypothesis_count INTEGER NOT NULL CHECK (hypothesis_count >= 0),
    slot_count INTEGER NOT NULL CHECK (slot_count = 120),
    approval_count INTEGER NOT NULL DEFAULT 0 CHECK (approval_count >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    source_manifest_sha256 CHAR(64) NOT NULL
);

-- 旧库没有 approval_count：保留 NULL 作为 legacy marker，投影时只回填 NULL。
-- 这样不会把旧行的真实值误判成默认占位值，也不会覆盖已有非 NULL 值。
ALTER TABLE research_evolution_batches
    ADD COLUMN IF NOT EXISTS approval_count INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_evolution_batches'::regclass
          AND conname = 'research_evolution_batches_approval_count_nonnegative'
    ) THEN
        ALTER TABLE research_evolution_batches
            ADD CONSTRAINT research_evolution_batches_approval_count_nonnegative
            CHECK (approval_count IS NULL OR approval_count >= 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS research_gap_summaries (
    gap_report_hash CHAR(64) PRIMARY KEY,
    context_id TEXT NOT NULL,
    structural_count INTEGER NOT NULL CHECK (structural_count >= 0),
    market_regime_count INTEGER NOT NULL CHECK (market_regime_count >= 0),
    data_availability_count INTEGER NOT NULL CHECK (data_availability_count >= 0),
    sanitized_labels JSONB NOT NULL CHECK (jsonb_typeof(sanitized_labels) = 'object'),
    truncated_count INTEGER NOT NULL DEFAULT 0 CHECK (truncated_count >= 0),
    coverage_graph_manifest_hash CHAR(64),
    memory_snapshot_hash CHAR(64),
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE research_gap_summaries
    ADD COLUMN IF NOT EXISTS truncated_count INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_gap_summaries'::regclass
          AND conname = 'research_gap_summaries_sanitized_labels_object'
    ) THEN
        ALTER TABLE research_gap_summaries
            ADD CONSTRAINT research_gap_summaries_sanitized_labels_object
            CHECK (jsonb_typeof(sanitized_labels) = 'object');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_gap_summaries'::regclass
          AND conname = 'research_gap_summaries_truncated_count_nonnegative'
    ) THEN
        ALTER TABLE research_gap_summaries
            ADD CONSTRAINT research_gap_summaries_truncated_count_nonnegative
            CHECK (truncated_count IS NULL OR truncated_count >= 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS research_memory_snapshots (
    memory_snapshot_id TEXT PRIMARY KEY,
    snapshot_sha256 CHAR(64) NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    coverage_graph_id TEXT,
    source_family_ids TEXT[] NOT NULL,
    entry_count INTEGER NOT NULL
        CONSTRAINT research_memory_snapshots_entry_count_nonnegative CHECK (entry_count >= 0),
    terminal_counts JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(terminal_counts) = 'object'),
    cutoff_at TIMESTAMPTZ NOT NULL,
    source_manifest_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE research_memory_snapshots
    ADD COLUMN IF NOT EXISTS terminal_counts JSONB;

-- 旧安装可能使用自动命名或历史命名的 entry_count 固定值约束。
-- 只按规范化后的精确表达式删除该旧约束，不触碰同表的其他约束。
DO $$
DECLARE
    old_constraint RECORD;
BEGIN
    FOR old_constraint IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'research_memory_snapshots'::regclass
          AND contype = 'c'
          AND regexp_replace(pg_get_expr(conbin, conrelid), '[[:space:]()]', '', 'g') = 'entry_count=120'
    LOOP
        EXECUTE format(
            'ALTER TABLE research_memory_snapshots DROP CONSTRAINT %I',
            old_constraint.conname
        );
    END LOOP;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_memory_snapshots'::regclass
          AND conname = 'research_memory_snapshots_entry_count_nonnegative'
    ) THEN
        ALTER TABLE research_memory_snapshots
            ADD CONSTRAINT research_memory_snapshots_entry_count_nonnegative
            CHECK (entry_count >= 0);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_memory_snapshots'::regclass
          AND conname = 'research_memory_snapshots_terminal_counts_object'
    ) THEN
        ALTER TABLE research_memory_snapshots
            ADD CONSTRAINT research_memory_snapshots_terminal_counts_object
            CHECK (terminal_counts IS NULL OR jsonb_typeof(terminal_counts) = 'object');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS research_memory_entries (
    memory_entry_id TEXT PRIMARY KEY,
    discovery_family_id TEXT NOT NULL,
    generation_seal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    hypothesis_slot_id TEXT NOT NULL,
    candidate_slot_id TEXT NOT NULL,
    candidate_spec_hash CHAR(64) NOT NULL,
    ast_hash CHAR(64) NOT NULL,
    coverage_graph_id TEXT,
    memory_snapshot_sha256 CHAR(64) NOT NULL,
    memory_snapshot_id TEXT NOT NULL REFERENCES research_memory_snapshots(memory_snapshot_id) NOT DEFERRABLE,
    terminal_state TEXT NOT NULL,
    failure_reason TEXT,
    duplicate_of TEXT,
    structure_labels JSONB NOT NULL,
    summary_json JSONB NOT NULL,
    entry_sha256 CHAR(64) NOT NULL UNIQUE,
    source_manifest_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- 生产环境由 DBA 授予专用 Dashboard 账号 SELECT；应用不自动提权。
COMMIT;

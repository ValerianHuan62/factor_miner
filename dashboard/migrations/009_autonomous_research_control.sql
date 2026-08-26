-- Dashboard 自主研究中文读模型；JSON/JSONL 仍是正式状态主存储。
BEGIN;

ALTER TABLE research_runs
    ADD COLUMN IF NOT EXISTS run_kind TEXT NOT NULL DEFAULT 'published_snapshot',
    ADD COLUMN IF NOT EXISTS sequence BIGINT,
    ADD COLUMN IF NOT EXISTS stage TEXT,
    ADD COLUMN IF NOT EXISTS stage_label_zh TEXT,
    ADD COLUMN IF NOT EXISTS state_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS state_created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error_zh TEXT,
    ADD COLUMN IF NOT EXISTS decision_count INTEGER,
    ADD COLUMN IF NOT EXISTS approved_hypothesis_count INTEGER,
    ADD COLUMN IF NOT EXISTS rejected_hypothesis_count INTEGER,
    ADD COLUMN IF NOT EXISTS candidate_family_size INTEGER,
    ADD COLUMN IF NOT EXISTS worker_heartbeat_at TIMESTAMPTZ;

ALTER TABLE research_runs
    ALTER COLUMN snapshot_sha256 DROP NOT NULL,
    ALTER COLUMN artifact_manifest_sha256 DROP NOT NULL,
    ALTER COLUMN snapshot_json DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'research_runs'::regclass
          AND conname = 'research_runs_kind_contract'
    ) THEN
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_kind_contract CHECK (
            (run_kind = 'published_snapshot'
             AND published
             AND snapshot_sha256 IS NOT NULL
             AND artifact_manifest_sha256 IS NOT NULL
             AND snapshot_json IS NOT NULL)
            OR
            (run_kind = 'autonomous_control'
             AND run_id ~ '^autrun_[0-9a-f]{24}$'
             AND NOT published
             AND sequence IS NOT NULL
             AND stage IS NOT NULL
             AND stage_label_zh IS NOT NULL
             AND state_sha256 IS NOT NULL
             AND state_created_at IS NOT NULL
             AND state_updated_at IS NOT NULL)
        );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS research_runs_one_active_autonomous
    ON research_runs ((run_kind))
    WHERE run_kind = 'autonomous_control'
      AND stage NOT IN ('completed', 'no_approved_hypothesis', 'failed');

CREATE TABLE IF NOT EXISTS research_hypotheses (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE RESTRICT,
    logical_slot_id TEXT NOT NULL CHECK (logical_slot_id ~ '^H(0[1-9]|10)$'),
    context_sha256 CHAR(64) NOT NULL,
    hypothesis_batch_sha256 CHAR(64) NOT NULL,
    draft_sha256 CHAR(64) NOT NULL,
    claim_zh TEXT NOT NULL,
    mechanism_zh TEXT NOT NULL,
    expected_direction TEXT NOT NULL,
    observable_proxy_zh TEXT NOT NULL,
    independent_verification_zh TEXT NOT NULL,
    competing_explanations_zh TEXT[] NOT NULL,
    failure_modes_zh TEXT[] NOT NULL,
    falsification_path_zh TEXT NOT NULL,
    source_records_zh JSONB NOT NULL CHECK (jsonb_typeof(source_records_zh) = 'array'),
    mechanism_status TEXT NOT NULL DEFAULT 'mechanism_unverified'
        CHECK (mechanism_status = 'mechanism_unverified'),
    decision TEXT CHECK (decision IN ('approved', 'rejected')),
    approval_role TEXT,
    decided_at TIMESTAMPTZ,
    decision_sha256 CHAR(64),
    PRIMARY KEY (run_id, logical_slot_id),
    UNIQUE (run_id, draft_sha256),
    CHECK (
        (decision IS NULL AND approval_role IS NULL AND decided_at IS NULL AND decision_sha256 IS NULL)
        OR
        (decision IS NOT NULL AND approval_role IS NOT NULL AND decided_at IS NOT NULL AND decision_sha256 IS NOT NULL)
    )
);

CREATE OR REPLACE VIEW latest_research_run_zh AS
SELECT
    run_id,
    sequence,
    stage,
    CASE stage
        WHEN 'context_preparing' THEN '准备上下文'
        WHEN 'hypothesis_generating' THEN '生成假设'
        WHEN 'awaiting_review' THEN '等待审批'
        WHEN 'review_frozen' THEN '审批已冻结'
        WHEN 'expression_generating' THEN '生成表达式'
        WHEN 'manifest_frozen' THEN '候选已登记'
        WHEN 'evaluating' THEN '计算与评价'
        WHEN 'published' THEN '产物已发布'
        WHEN 'projected' THEN '数据库已投影'
        WHEN 'evolution_refreshed' THEN '记忆与图谱已刷新'
        WHEN 'completed' THEN '运行完成'
        WHEN 'no_approved_hypothesis' THEN '没有批准假设'
        WHEN 'failed' THEN '运行失败'
        ELSE stage_label_zh
    END AS stage_label_zh,
    last_error_zh,
    COALESCE(decision_count, 0) AS decision_count,
    COALESCE(approved_hypothesis_count, 0) AS approved_hypothesis_count,
    COALESCE(rejected_hypothesis_count, 0) AS rejected_hypothesis_count,
    COALESCE(candidate_family_size, 0) AS candidate_family_size,
    worker_heartbeat_at,
    (worker_heartbeat_at >= CURRENT_TIMESTAMP - INTERVAL '90 seconds') AS worker_online,
    state_created_at,
    state_updated_at,
    state_sha256
FROM research_runs
WHERE run_kind = 'autonomous_control'
ORDER BY state_updated_at DESC, sequence DESC
LIMIT 1;

COMMENT ON TABLE research_hypotheses IS 'Dashboard 审批所需的十条中文假设全文；不保存模型原始响应和调用计量。';
COMMENT ON VIEW latest_research_run_zh IS '最近自主研究批次的中文阶段、审批进度与 Worker 心跳。';

COMMIT;

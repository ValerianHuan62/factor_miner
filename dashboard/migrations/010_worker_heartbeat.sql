-- 独立 Worker 心跳；即使尚无研究批次，Dashboard 也能显示真实在线状态。
BEGIN;

CREATE TABLE IF NOT EXISTS research_worker_status (
    singleton_id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton_id),
    heartbeat_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE VIEW latest_research_worker_zh AS
SELECT
    heartbeat_at,
    heartbeat_at >= CURRENT_TIMESTAMP - INTERVAL '90 seconds' AS worker_online
FROM research_worker_status
WHERE singleton_id = TRUE;

COMMENT ON TABLE research_worker_status IS '单机研究 Worker 的独立心跳，不承载研究状态。';
COMMENT ON VIEW latest_research_worker_zh IS 'Dashboard 使用的 Worker 在线状态。';

COMMIT;

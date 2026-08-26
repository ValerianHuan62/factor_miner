-- 将 Dashboard 业务引用编号统一为 huan001、huan002……。
-- source_candidate_id 保留原始 Pilot 槽位，保证研究产物和数据库记录可追溯。

BEGIN;

ALTER TABLE candidates
    ADD COLUMN IF NOT EXISTS source_candidate_id TEXT;

UPDATE candidates
SET source_candidate_id = candidate_id
WHERE source_candidate_id IS NULL;

UPDATE candidates
SET candidate_id = 'huan' || lpad(split_part(source_candidate_id, '_', 3), 3, '0')
WHERE source_candidate_id LIKE 'pilot_fixed_%'
  AND split_part(source_candidate_id, '_', 3) ~ '^[0-9]+$';

UPDATE candidate_metrics
SET candidate_id = 'huan' || lpad(split_part(candidate_id, '_', 3), 3, '0')
WHERE candidate_id LIKE 'pilot_fixed_%'
  AND split_part(candidate_id, '_', 3) ~ '^[0-9]+$';

UPDATE candidate_ic_horizons
SET candidate_id = 'huan' || lpad(split_part(candidate_id, '_', 3), 3, '0')
WHERE candidate_id LIKE 'pilot_fixed_%'
  AND split_part(candidate_id, '_', 3) ~ '^[0-9]+$';

UPDATE portfolio_metrics
SET candidate_id = 'huan' || lpad(split_part(candidate_id, '_', 3), 3, '0')
WHERE candidate_id LIKE 'pilot_fixed_%'
  AND split_part(candidate_id, '_', 3) ~ '^[0-9]+$';

UPDATE portfolio_daily
SET candidate_id = 'huan' || lpad(split_part(candidate_id, '_', 3), 3, '0')
WHERE candidate_id LIKE 'pilot_fixed_%'
  AND split_part(candidate_id, '_', 3) ~ '^[0-9]+$';

UPDATE barra_exposure_summary
SET candidate_id = 'huan' || lpad(split_part(candidate_id, '_', 3), 3, '0')
WHERE candidate_id LIKE 'pilot_fixed_%'
  AND split_part(candidate_id, '_', 3) ~ '^[0-9]+$';

UPDATE barra_attribution
SET candidate_id = 'huan' || lpad(split_part(candidate_id, '_', 3), 3, '0')
WHERE candidate_id LIKE 'pilot_fixed_%'
  AND split_part(candidate_id, '_', 3) ~ '^[0-9]+$';

COMMENT ON COLUMN candidates.source_candidate_id IS '原始产物中的候选槽位，例如 pilot_fixed_001；业务引用请使用 candidate_id。';
COMMENT ON COLUMN candidates.candidate_id IS '稳定业务引用编号，例如 huan001；不承载金融含义。';

COMMIT;

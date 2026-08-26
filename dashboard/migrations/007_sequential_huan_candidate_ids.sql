-- Dashboard 候选统一使用稳定顺延的 huanNNN 业务编号。
-- source_candidate_id 保留正式槽位或内容寻址 ID，研究身份不依赖展示编号。

BEGIN;

LOCK TABLE candidates IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE candidate_id_repair (
    old_candidate_id TEXT PRIMARY KEY,
    new_candidate_id TEXT NOT NULL UNIQUE
) ON COMMIT DROP;

WITH current_max AS (
    SELECT COALESCE(
        MAX(substring(candidate_id FROM 5)::integer)
            FILTER (WHERE candidate_id ~ '^huan[0-9]{3,}$'),
        0
    ) AS value
    FROM candidates
), numbered AS (
    SELECT
        candidate_id,
        row_number() OVER (ORDER BY first_seen_run_id, source_candidate_id, candidate_id) AS sequence
    FROM candidates
    WHERE candidate_id !~ '^huan[0-9]{3,}$'
)
INSERT INTO candidate_id_repair (old_candidate_id, new_candidate_id)
SELECT
    numbered.candidate_id,
    'huan' || lpad((current_max.value + numbered.sequence)::text, 3, '0')
FROM numbered CROSS JOIN current_max;

UPDATE candidate_metrics AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

UPDATE candidate_ic_horizons AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

UPDATE portfolio_metrics AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

UPDATE portfolio_daily AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

UPDATE barra_attribution AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

UPDATE barra_exposure_summary AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

UPDATE candidates AS target
SET candidate_id = repair.new_candidate_id
FROM candidate_id_repair AS repair
WHERE target.candidate_id = repair.old_candidate_id;

CREATE UNIQUE INDEX IF NOT EXISTS candidates_source_candidate_id_unique
    ON candidates (source_candidate_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'candidates'::regclass
          AND conname = 'candidates_huan_reference_format'
    ) THEN
        ALTER TABLE candidates
            ADD CONSTRAINT candidates_huan_reference_format
            CHECK (candidate_id ~ '^huan[0-9]{3,}$');
    END IF;
END $$;

COMMIT;

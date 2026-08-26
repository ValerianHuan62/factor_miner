-- 强制刷新 candidates 的物理列顺序，解决客户端缓存旧列顺序的问题。
-- 只重建 Dashboard 候选目录，不触碰原始研究产物和其他指标表。

BEGIN;

CREATE TABLE candidates_rebuild_005 (
    candidate_id TEXT PRIMARY KEY,
    factor_name_zh TEXT NOT NULL,
    formula_text TEXT,
    calculation_method TEXT,
    hypothesis_claim TEXT,
    factor_category TEXT,
    expected_sign TEXT,
    formula_operator TEXT,
    formula_field TEXT,
    formula_period INTEGER,
    formula_window INTEGER,
    required_fields TEXT[],
    availability TEXT,
    hypothesis_mechanism TEXT,
    observable_proxy TEXT,
    independent_verification TEXT,
    competing_explanations TEXT[],
    failure_modes TEXT[],
    falsification_path TEXT,
    mechanism_status TEXT,
    source_kind TEXT,
    source_refs TEXT[],
    formula_center BOOLEAN,
    max_lookback INTEGER,
    quality_status TEXT NOT NULL DEFAULT 'not_assessed',
    first_seen_run_id TEXT,
    spec_hash CHAR(64) NOT NULL,
    source_candidate_id TEXT
);

INSERT INTO candidates_rebuild_005 (
    candidate_id, factor_name_zh, formula_text, calculation_method,
    hypothesis_claim, factor_category, expected_sign,
    formula_operator, formula_field, formula_period, formula_window,
    required_fields, availability, hypothesis_mechanism,
    observable_proxy, independent_verification, competing_explanations,
    failure_modes, falsification_path, mechanism_status, source_kind,
    source_refs, formula_center, max_lookback, quality_status,
    first_seen_run_id, spec_hash, source_candidate_id
)
SELECT
    candidate_id, factor_name_zh, formula_text, calculation_method,
    hypothesis_claim, factor_category, expected_sign,
    formula_operator, formula_field, formula_period, formula_window,
    required_fields, availability, hypothesis_mechanism,
    observable_proxy, independent_verification, competing_explanations,
    failure_modes, falsification_path, mechanism_status, source_kind,
    source_refs, formula_center, max_lookback, quality_status,
    first_seen_run_id, spec_hash, source_candidate_id
FROM candidates;

DROP TABLE candidates;
ALTER TABLE candidates_rebuild_005 RENAME TO candidates;
ALTER TABLE candidates RENAME CONSTRAINT candidates_rebuild_005_pkey TO candidates_pkey;

COMMENT ON TABLE candidates IS '因子目录：先展示中文因子名、公式、计算方式和事前假设，后展示来源与审计字段。';
COMMENT ON COLUMN candidates.factor_name_zh IS '因子中文名称：根据因子含义和窗口的简要总结。';
COMMENT ON COLUMN candidates.formula_text IS '面向人的完整计算公式。';
COMMENT ON COLUMN candidates.calculation_method IS '中文计算方式摘要，说明输入、运算和交易时点。';
COMMENT ON COLUMN candidates.hypothesis_claim IS '结果揭晓前冻结的金融主张，不代表因果成立。';
COMMENT ON COLUMN candidates.factor_category IS '中文因子类别。';
COMMENT ON COLUMN candidates.expected_sign IS '事前预期方向，例如 positive 或 negative。';
COMMENT ON COLUMN candidates.formula_operator IS '类型化 DSL 顶层算子。';
COMMENT ON COLUMN candidates.formula_field IS '公式涉及的主要数据字段。';
COMMENT ON COLUMN candidates.formula_period IS '差分或滞后周期，单位为交易日。';
COMMENT ON COLUMN candidates.formula_window IS '滚动计算窗口，单位为交易日。';
COMMENT ON COLUMN candidates.required_fields IS '服务器数据合同要求的字段集合。';
COMMENT ON COLUMN candidates.availability IS '信号最早可交易时点，例如 next_open。';
COMMENT ON COLUMN candidates.hypothesis_mechanism IS '待独立验证的经济机制，不等于因果结论。';
COMMENT ON COLUMN candidates.source_kind IS '候选来源，例如 human 或 deepseek。';
COMMENT ON COLUMN candidates.source_refs IS '可核验的来源引用。';
COMMENT ON COLUMN candidates.first_seen_run_id IS '首次进入 Dashboard 的运行 ID。';
COMMENT ON COLUMN candidates.spec_hash IS '候选 Spec 内容哈希，用于审计，不是业务展示字段。';
COMMENT ON COLUMN candidates.source_candidate_id IS '原始产物候选编号，例如 pilot_fixed_001；业务引用请使用 candidate_id。';

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.candidates
    TO factor_miner_dashboard_ro;

COMMIT;

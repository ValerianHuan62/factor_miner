-- 重构候选目录和因子指标读模型，便于 SlateTable 直接阅读和编辑。
-- 原始候选 Spec、IC 诊断和组合产物仍是正式研究主存储。

BEGIN;

-- 多期限 IC 从候选总览拆出。h1/h3/h5/h10/h20 表示标签持有期限，单位为交易日。
CREATE TABLE candidate_ic_horizons (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    horizon_days SMALLINT NOT NULL,
    valid_dates INTEGER,
    ic_mean DOUBLE PRECISION,
    rank_ic_mean DOUBLE PRECISION,
    metrics_sha256 CHAR(64),
    PRIMARY KEY (run_id, candidate_id, horizon_days)
);

INSERT INTO candidate_ic_horizons
    (run_id, candidate_id, horizon_days, valid_dates, ic_mean, rank_ic_mean)
SELECT run_id, candidate_id, 1, valid_dates_h1, ic_mean_h1, rank_ic_mean_h1
FROM candidate_metrics
WHERE ic_mean_h1 IS NOT NULL OR rank_ic_mean_h1 IS NOT NULL
UNION ALL
SELECT run_id, candidate_id, 3, valid_dates_h3, ic_mean_h3, rank_ic_mean_h3
FROM candidate_metrics
WHERE ic_mean_h3 IS NOT NULL OR rank_ic_mean_h3 IS NOT NULL
UNION ALL
SELECT run_id, candidate_id, 5, valid_dates_h5, ic_mean_h5, rank_ic_mean_h5
FROM candidate_metrics
WHERE ic_mean_h5 IS NOT NULL OR rank_ic_mean_h5 IS NOT NULL
UNION ALL
SELECT run_id, candidate_id, 10, valid_dates_h10, ic_mean_h10, rank_ic_mean_h10
FROM candidate_metrics
WHERE ic_mean_h10 IS NOT NULL OR rank_ic_mean_h10 IS NOT NULL
UNION ALL
SELECT run_id, candidate_id, 20, valid_dates_h20, ic_mean_h20, rank_ic_mean_h20
FROM candidate_metrics
WHERE ic_mean_h20 IS NOT NULL OR rank_ic_mean_h20 IS NOT NULL;

-- candidates：按阅读顺序重建，先放因子名字、公式和假设，再放审计字段。
ALTER TABLE candidates RENAME TO candidates_legacy_003;

CREATE TABLE candidates (
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
    spec_hash CHAR(64) NOT NULL
);

INSERT INTO candidates (
    candidate_id, factor_name_zh, formula_text, calculation_method,
    hypothesis_claim, factor_category, expected_sign,
    formula_operator, formula_field, formula_period, formula_window,
    required_fields, availability, hypothesis_mechanism,
    observable_proxy, independent_verification, competing_explanations,
    failure_modes, falsification_path, mechanism_status, source_kind,
    source_refs, formula_center, max_lookback, quality_status,
    first_seen_run_id, spec_hash
)
SELECT
    candidate_id,
    '待重新投影的候选因子 ' || candidate_id,
    formula_text,
    NULL,
    hypothesis_claim,
    factor_category,
    expected_sign,
    formula_operator,
    formula_field,
    formula_period,
    formula_window,
    required_fields,
    availability,
    hypothesis_mechanism,
    observable_proxy,
    independent_verification,
    competing_explanations,
    failure_modes,
    falsification_path,
    COALESCE(mechanism_status, 'mechanism_unverified'),
    source_kind,
    source_refs,
    formula_center,
    max_lookback,
    quality_status,
    first_seen_run_id,
    spec_hash
FROM candidates_legacy_003;

DROP TABLE candidates_legacy_003;

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
COMMENT ON COLUMN candidates.spec_hash IS '候选 Spec 内容哈希，用于审计，不是业务展示字段。';
COMMENT ON COLUMN candidates.first_seen_run_id IS '首次进入 Dashboard 的运行 ID。';
COMMENT ON COLUMN candidates.quality_status IS '候选状态，不代表生产有效性。';

-- candidate_metrics：只保留一个主期限的核心指标，删除 ir/t_statistic 重复别名。
-- 空指标行不迁移；失败或未执行候选必须留在原始账本，不在 Dashboard 造空指标行。
ALTER TABLE candidate_metrics RENAME TO candidate_metrics_legacy_003;

CREATE TABLE candidate_metrics (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    primary_horizon SMALLINT,
    valid_dates INTEGER,
    coverage_mean DOUBLE PRECISION,
    factor_return DOUBLE PRECISION,
    win_rate DOUBLE PRECISION,
    annualized_return DOUBLE PRECISION,
    max_drawdown DOUBLE PRECISION,
    sharpe DOUBLE PRECISION,
    information_ratio DOUBLE PRECISION,
    ic_mean DOUBLE PRECISION,
    rank_ic_mean DOUBLE PRECISION,
    ic_std DOUBLE PRECISION,
    rank_ic_std DOUBLE PRECISION,
    ic_ir DOUBLE PRECISION,
    rank_ic_ir DOUBLE PRECISION,
    ic_hac_t DOUBLE PRECISION,
    rank_ic_hac_t DOUBLE PRECISION,
    p_ic_lt_neg_002 DOUBLE PRECISION,
    p_ic_gt_pos_002 DOUBLE PRECISION,
    metrics_sha256 CHAR(64),
    PRIMARY KEY (run_id, candidate_id)
);

INSERT INTO candidate_metrics (
    run_id, candidate_id, primary_horizon, valid_dates, coverage_mean,
    factor_return, win_rate, annualized_return, max_drawdown, sharpe,
    information_ratio, ic_mean, rank_ic_mean, ic_std, rank_ic_std,
    ic_ir, rank_ic_ir, ic_hac_t, rank_ic_hac_t,
    p_ic_lt_neg_002, p_ic_gt_pos_002, metrics_sha256
)
SELECT
    run_id, candidate_id, primary_horizon, valid_dates, coverage_mean,
    factor_return, win_rate, annualized_return, max_drawdown, sharpe,
    information_ratio, ic_mean, rank_ic_mean, ic_std, rank_ic_std,
    ic_ir, rank_ic_ir, ic_hac_t, rank_ic_hac_t,
    p_ic_lt_neg_002, p_ic_gt_pos_002, metrics_sha256
FROM candidate_metrics_legacy_003
WHERE factor_return IS NOT NULL
   OR annualized_return IS NOT NULL
   OR sharpe IS NOT NULL
   OR ic_mean IS NOT NULL
   OR rank_ic_mean IS NOT NULL;

DROP TABLE candidate_metrics_legacy_003;

COMMENT ON TABLE candidate_metrics IS '因子核心指标：每个运行、每个候选一行，只保留主期限和核心风险收益指标。';
COMMENT ON COLUMN candidate_metrics.primary_horizon IS '主标签期限，单位为交易日；当前 Pilot 为 5 日。';
COMMENT ON COLUMN candidate_metrics.factor_return IS '核心 Q10-Q1 净收益区间收益。';
COMMENT ON COLUMN candidate_metrics.annualized_return IS '核心 Q10-Q1 净收益年化收益。';
COMMENT ON COLUMN candidate_metrics.max_drawdown IS '核心 Q10-Q1 净收益最大回撤，正数幅度。';
COMMENT ON COLUMN candidate_metrics.sharpe IS '核心 Q10-Q1 净收益 Sharpe。';
COMMENT ON COLUMN candidate_metrics.information_ratio IS '核心 Q10-Q1 相对基准的信息比率；不再另设 ir 别名。';
COMMENT ON COLUMN candidate_metrics.rank_ic_hac_t IS '主期限 RankIC 的 HAC t 统计量。';
COMMENT ON COLUMN candidate_metrics.metrics_sha256 IS '本行指标内容哈希，用于审计，不是业务展示字段。';

COMMENT ON TABLE candidate_ic_horizons IS '多期限 IC：horizon_days 为未来标签期限，单位为交易日；从 candidate_metrics 主表拆出。';
COMMENT ON COLUMN candidate_ic_horizons.horizon_days IS '未来收益标签期限，1/3/5/10/20 表示交易日数，不是因子计算窗口。';
COMMENT ON COLUMN candidate_ic_horizons.valid_dates IS '该期限实际有效的交易日数量。';

CREATE INDEX candidate_metrics_candidate_idx
    ON candidate_metrics (candidate_id, run_id);
CREATE INDEX candidate_ic_horizons_candidate_idx
    ON candidate_ic_horizons (candidate_id, run_id, horizon_days);

-- SlateTable 使用的账号只可编辑四张研究读模型表，不获得修改表结构、
-- 删除整个表或改写运行账本、原始日收益和 Barra 审计表的权限。
GRANT USAGE ON SCHEMA public TO factor_miner_dashboard_ro;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE public.candidates,
              public.candidate_metrics,
              public.candidate_ic_horizons,
              public.portfolio_metrics
    TO factor_miner_dashboard_ro;

COMMIT;

-- 因子候选、因子总览指标和组合分组指标的类型化投影。
-- 正式 JSON/JSONL/Parquet 产物仍是研究主存储；本迁移只改 Dashboard 读模型。

BEGIN;

-- candidates：因子目录，计算公式和来源假设拆成可读列。
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source_kind TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS hypothesis_claim TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS hypothesis_mechanism TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS expected_sign TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS observable_proxy TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS independent_verification TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS competing_explanations TEXT[];
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS failure_modes TEXT[];
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS falsification_path TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS mechanism_status TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source_refs TEXT[];
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS formula_operator TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS formula_text TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS formula_field TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS formula_period INTEGER;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS formula_window INTEGER;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS formula_center BOOLEAN;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS required_fields TEXT[];
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS max_lookback INTEGER;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS availability TEXT;

UPDATE candidates
SET source_kind = COALESCE(source_kind, 'unknown'),
    mechanism_status = COALESCE(mechanism_status, 'mechanism_unverified');

ALTER TABLE candidates DROP COLUMN IF EXISTS hypothesis_json;
ALTER TABLE candidates DROP COLUMN IF EXISTS formula_summary;

COMMENT ON TABLE candidates IS '因子目录：每行一个候选因子，保存类别、来源假设和类型化计算公式。';
COMMENT ON COLUMN candidates.source_kind IS '候选来源：human、deepseek 或其他受控来源。';
COMMENT ON COLUMN candidates.factor_category IS '因子类别：momentum、liquidity、volatility、value、quality 等。';
COMMENT ON COLUMN candidates.hypothesis_claim IS '结果揭晓前冻结的金融主张。';
COMMENT ON COLUMN candidates.hypothesis_mechanism IS '待独立验证的经济机制，不等于因果结论。';
COMMENT ON COLUMN candidates.formula_operator IS '类型化 DSL 顶层算子，例如 delta、rolling_mean、rolling_std。';
COMMENT ON COLUMN candidates.formula_text IS '面向人的计算公式，例如 close[t] - close[t-20]。';
COMMENT ON COLUMN candidates.formula_field IS '公式使用的主要字段。';
COMMENT ON COLUMN candidates.formula_period IS '差分或滞后算子的周期。';
COMMENT ON COLUMN candidates.formula_window IS '滚动算子的窗口长度。';
COMMENT ON COLUMN candidates.required_fields IS '服务器数据合同要求的字段集合。';
COMMENT ON COLUMN candidates.max_lookback IS '最大历史回看长度，不能使用未来数据。';
COMMENT ON COLUMN candidates.availability IS '信号最早可交易时点，例如 next_open。';

-- candidate_metrics：每个运行、每个候选一行的核心总览指标。
-- 多期限衰减也使用明确列；Q1-Q10 明细不写入这里。
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS primary_horizon SMALLINT;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS valid_dates INTEGER;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS coverage_mean DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS factor_return DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS win_rate DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS annualized_return DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS max_drawdown DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS sharpe DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS information_ratio DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ir DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_mean DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_mean DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_std DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_std DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_ir DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_ir DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_hac_t DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_hac_t DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS t_statistic DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS p_ic_lt_neg_002 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS p_ic_gt_pos_002 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_mean_h1 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_mean_h1 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS valid_dates_h1 INTEGER;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_mean_h3 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_mean_h3 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS valid_dates_h3 INTEGER;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_mean_h5 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_mean_h5 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS valid_dates_h5 INTEGER;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_mean_h10 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_mean_h10 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS valid_dates_h10 INTEGER;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS ic_mean_h20 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS rank_ic_mean_h20 DOUBLE PRECISION;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS valid_dates_h20 INTEGER;
ALTER TABLE candidate_metrics ADD COLUMN IF NOT EXISTS metrics_sha256 CHAR(64);

UPDATE candidate_metrics
SET primary_horizon = COALESCE(primary_horizon, 5);

ALTER TABLE candidate_metrics DROP COLUMN IF EXISTS metrics_json;

COMMENT ON TABLE candidate_metrics IS '因子总览指标：每个运行、每个候选一行，包含 IC、RankIC、HAC、显著性和核心多空摘要。';
COMMENT ON COLUMN candidate_metrics.factor_return IS '核心 Q10-Q1 净收益序列的区间收益摘要。';
COMMENT ON COLUMN candidate_metrics.win_rate IS '核心 Q10-Q1 净收益逐期为正的比例。';
COMMENT ON COLUMN candidate_metrics.annualized_return IS '核心 Q10-Q1 净收益年化收益。';
COMMENT ON COLUMN candidate_metrics.sharpe IS '核心 Q10-Q1 净收益 Sharpe。';
COMMENT ON COLUMN candidate_metrics.max_drawdown IS '核心 Q10-Q1 净收益最大回撤，正数幅度。';
COMMENT ON COLUMN candidate_metrics.information_ratio IS '核心 Q10-Q1 相对基准的信息比率。';
COMMENT ON COLUMN candidate_metrics.rank_ic_hac_t IS '主期限 RankIC 的 HAC t 统计量。';

-- portfolio_metrics：每个候选、每个组合序列一行，和 candidate_metrics 分开。
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS benchmark TEXT;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS observations INTEGER;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS period_return DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS annualized_return DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS excess_return DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS excess_annualized_return DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS annualized_volatility DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS excess_annualized_volatility DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS sharpe DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS information_ratio DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS max_drawdown DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS excess_max_drawdown DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS annualization_factor DOUBLE PRECISION;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS return_basis TEXT;
ALTER TABLE portfolio_metrics ADD COLUMN IF NOT EXISTS metrics_sha256 CHAR(64);

UPDATE portfolio_metrics
SET benchmark = COALESCE(benchmark, 'CSI300'),
    observations = COALESCE(observations, NULLIF(metrics_json->>'observations', '')::INTEGER),
    period_return = COALESCE(period_return, NULLIF(metrics_json->>'period_return', '')::DOUBLE PRECISION),
    annualized_return = COALESCE(annualized_return, NULLIF(metrics_json->>'annualized_return', '')::DOUBLE PRECISION),
    excess_return = COALESCE(excess_return, NULLIF(metrics_json->>'excess_return', '')::DOUBLE PRECISION),
    excess_annualized_return = COALESCE(excess_annualized_return, NULLIF(metrics_json->>'excess_annualized_return', '')::DOUBLE PRECISION),
    annualized_volatility = COALESCE(annualized_volatility, NULLIF(metrics_json->>'annualized_volatility', '')::DOUBLE PRECISION),
    excess_annualized_volatility = COALESCE(excess_annualized_volatility, NULLIF(metrics_json->>'excess_annualized_volatility', '')::DOUBLE PRECISION),
    sharpe = COALESCE(sharpe, NULLIF(metrics_json->>'sharpe', '')::DOUBLE PRECISION),
    information_ratio = COALESCE(information_ratio, NULLIF(metrics_json->>'information_ratio', '')::DOUBLE PRECISION),
    max_drawdown = COALESCE(max_drawdown, NULLIF(metrics_json->>'max_drawdown', '')::DOUBLE PRECISION),
    excess_max_drawdown = COALESCE(excess_max_drawdown, NULLIF(metrics_json->>'excess_max_drawdown', '')::DOUBLE PRECISION),
    annualization_factor = COALESCE(annualization_factor, NULLIF(metrics_json->>'annualization_factor', '')::DOUBLE PRECISION),
    return_basis = COALESCE(return_basis, metrics_json->>'return_basis'),
    metrics_sha256 = COALESCE(metrics_sha256, metrics_json->>'metrics_sha256');

ALTER TABLE portfolio_metrics DROP COLUMN IF EXISTS metrics_json;

COMMENT ON TABLE portfolio_metrics IS '组合分组指标：每个候选、每个 Q 组/多空/基准序列一行，所有指标为明确列。';
COMMENT ON COLUMN portfolio_metrics.series_name IS '组合序列名，例如 Q1_net_return、Q10_Q1_net_return、CSI300。';
COMMENT ON COLUMN portfolio_metrics.sharpe IS '该组合序列的 Sharpe。';
COMMENT ON COLUMN portfolio_metrics.max_drawdown IS '该组合序列最大回撤，正数幅度。';

COMMENT ON TABLE portfolio_daily IS '组合逐期收益：与组合汇总指标分开保存，便于画曲线和查单日。';

CREATE INDEX IF NOT EXISTS candidate_metrics_candidate_idx
    ON candidate_metrics (candidate_id, run_id);
CREATE INDEX IF NOT EXISTS portfolio_metrics_candidate_idx
    ON portfolio_metrics (candidate_id, run_id);

COMMIT;

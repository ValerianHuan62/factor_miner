-- 中文候选目录与每个因子最新的完整评价。
-- 旧运行和空指标继续保留在基础表，只从默认完整评价视图排除。

BEGIN;

CREATE OR REPLACE VIEW candidate_catalog_zh AS
SELECT
    candidate_id,
    factor_name_zh,
    formula_text,
    calculation_method,
    hypothesis_claim,
    factor_category,
    CASE expected_sign
        WHEN 'positive' THEN '正向'
        WHEN 'negative' THEN '负向'
        ELSE expected_sign
    END AS expected_sign_zh,
    CASE availability
        WHEN 'next_open' THEN '当日收盘观察，下一交易日开盘使用'
        WHEN 'next_close' THEN '下一交易日收盘使用'
        ELSE availability
    END AS availability_zh,
    hypothesis_mechanism,
    observable_proxy,
    independent_verification,
    competing_explanations,
    failure_modes,
    falsification_path,
    CASE mechanism_status
        WHEN 'mechanism_unverified' THEN '机制尚未独立验证'
        ELSE mechanism_status
    END AS mechanism_status_zh,
    CASE source_kind
        WHEN 'human' THEN '人工'
        WHEN 'deepseek' THEN 'LLM'
        WHEN 'llm' THEN 'LLM'
        WHEN 'shadow' THEN '确定性对照'
        ELSE source_kind
    END AS source_kind_zh,
    CASE quality_status
        WHEN 'not_assessed' THEN '待评价'
        WHEN 'evaluated' THEN '评价完整'
        WHEN 'failed' THEN '计算失败'
        ELSE quality_status
    END AS quality_status_zh,
    formula_operator,
    formula_field,
    formula_period,
    formula_window,
    required_fields,
    formula_center,
    max_lookback,
    source_refs,
    first_seen_run_id,
    source_candidate_id,
    spec_hash
FROM candidates;

CREATE OR REPLACE VIEW latest_complete_candidate_metrics AS
WITH complete AS (
    SELECT
        metrics.*,
        runs.projected_at
    FROM candidate_metrics AS metrics
    JOIN research_runs AS runs USING (run_id)
    WHERE metrics.ic_mean IS NOT NULL
      AND metrics.rank_ic_mean IS NOT NULL
      AND metrics.ic_std IS NOT NULL
      AND metrics.rank_ic_std IS NOT NULL
      AND metrics.ic_ir IS NOT NULL
      AND metrics.rank_ic_ir IS NOT NULL
      AND metrics.ic_hac_t IS NOT NULL
      AND metrics.rank_ic_hac_t IS NOT NULL
      AND metrics.win_rate IS NOT NULL
      AND metrics.annualized_return IS NOT NULL
      AND metrics.max_drawdown IS NOT NULL
      AND metrics.sharpe IS NOT NULL
      AND metrics.information_ratio IS NOT NULL
), ranked AS (
    SELECT
        complete.*,
        row_number() OVER (
            PARTITION BY candidate_id
            ORDER BY projected_at DESC, run_id DESC
        ) AS evaluation_recency_rank
    FROM complete
)
SELECT *
FROM ranked
WHERE evaluation_recency_rank = 1;

COMMENT ON VIEW candidate_catalog_zh IS '候选因子的中文业务目录；技术字段和审计身份保持原值。';
COMMENT ON VIEW latest_complete_candidate_metrics IS '每个 huanNNN 最新且核心指标完整的一次评价；旧半空运行保留在基础表。';

GRANT SELECT ON candidate_catalog_zh TO factor_miner_dashboard_ro;
GRANT SELECT ON latest_complete_candidate_metrics TO factor_miner_dashboard_ro;

COMMIT;

-- 为 A 股与美股建立同构但严格隔离的 Dashboard 读模型。
-- 已有 367 个因子明确归属 A 股；开发期仅完成 IC/HAC 的候选进入独立评价表，
-- 不伪造组合收益、回撤或 Sharpe。
BEGIN;

ALTER TABLE public.factors
    ADD COLUMN market_id TEXT NOT NULL DEFAULT 'a_share';
ALTER TABLE public.factors
    ALTER COLUMN market_id DROP DEFAULT;

ALTER TABLE public.research_runs
    ADD COLUMN market_id TEXT NOT NULL DEFAULT 'a_share';
ALTER TABLE public.research_runs
    ALTER COLUMN market_id DROP DEFAULT;

ALTER TABLE factor_miner_internal.factor_id_map
    ADD COLUMN market_id TEXT NOT NULL DEFAULT 'a_share';
ALTER TABLE factor_miner_internal.factor_id_map
    ALTER COLUMN market_id DROP DEFAULT;
ALTER TABLE factor_miner_internal.factor_id_map
    DROP CONSTRAINT factor_id_map_pkey;
ALTER TABLE factor_miner_internal.factor_id_map
    ADD PRIMARY KEY (market_id, source_candidate_id);

CREATE INDEX factors_market_id_idx
    ON public.factors (market_id, factor_id);
CREATE INDEX research_runs_market_id_updated_idx
    ON public.research_runs (market_id, updated_at DESC);

CREATE TABLE public.visible_candidate_evaluations (
    market_id TEXT NOT NULL,
    factor_id TEXT NOT NULL REFERENCES public.factors(factor_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    evaluation_scope TEXT NOT NULL CHECK (evaluation_scope ~ '[一-龥]'),
    horizon_days SMALLINT NOT NULL CHECK (horizon_days > 0),
    valid_dates INTEGER NOT NULL CHECK (valid_dates > 0),
    coverage_mean DOUBLE PRECISION NOT NULL CHECK (coverage_mean BETWEEN 0.0 AND 1.0),
    rank_ic_mean DOUBLE PRECISION NOT NULL,
    rank_ic_std DOUBLE PRECISION NOT NULL CHECK (rank_ic_std >= 0.0),
    rank_ic_ir DOUBLE PRECISION NOT NULL,
    rank_ic_hac_t DOUBLE PRECISION NOT NULL,
    raw_p_value DOUBLE PRECISION NOT NULL CHECK (raw_p_value BETWEEN 0.0 AND 1.0),
    bonferroni_p_value DOUBLE PRECISION NOT NULL CHECK (bonferroni_p_value BETWEEN 0.0 AND 1.0),
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market_id, factor_id)
);

DROP VIEW public.factor_metrics_detail;

CREATE OR REPLACE VIEW public.factor_metrics_detail AS
SELECT
    f.market_id,
    m.factor_id,
    f.hypothesis,
    f.mechanism,
    f.formula,
    f.calculation,
    f.discovered_direction,
    f.direction_relation,
    f.status,
    m.horizon_days,
    m.valid_dates,
    m.coverage_mean,
    m.ic_mean,
    m.rank_ic_mean,
    m.ic_std,
    m.rank_ic_std,
    m.ic_ir,
    m.rank_ic_ir,
    m.ic_hac_t,
    m.rank_ic_hac_t,
    m.annualized_return,
    m.max_drawdown,
    m.sharpe,
    m.information_ratio,
    TRUE AS has_portfolio,
    '完整评价与组合回测'::TEXT AS evaluation_scope,
    NULL::TEXT AS run_id,
    m.evaluated_at
FROM public.factor_metrics AS m
JOIN public.factors AS f ON f.factor_id = m.factor_id

UNION ALL

SELECT
    f.market_id,
    e.factor_id,
    f.hypothesis,
    f.mechanism,
    f.formula,
    f.calculation,
    f.discovered_direction,
    f.direction_relation,
    f.status,
    e.horizon_days,
    e.valid_dates,
    e.coverage_mean,
    NULL::DOUBLE PRECISION AS ic_mean,
    e.rank_ic_mean,
    NULL::DOUBLE PRECISION AS ic_std,
    e.rank_ic_std,
    NULL::DOUBLE PRECISION AS ic_ir,
    e.rank_ic_ir,
    NULL::DOUBLE PRECISION AS ic_hac_t,
    e.rank_ic_hac_t,
    NULL::DOUBLE PRECISION AS annualized_return,
    NULL::DOUBLE PRECISION AS max_drawdown,
    NULL::DOUBLE PRECISION AS sharpe,
    NULL::DOUBLE PRECISION AS information_ratio,
    FALSE AS has_portfolio,
    e.evaluation_scope,
    e.run_id,
    e.evaluated_at
FROM public.visible_candidate_evaluations AS e
JOIN public.factors AS f ON f.factor_id = e.factor_id
WHERE NOT EXISTS (
    SELECT 1 FROM public.factor_metrics AS m WHERE m.factor_id = e.factor_id
);

GRANT SELECT ON public.visible_candidate_evaluations TO factor_miner_dashboard_ro;
GRANT SELECT ON public.factor_metrics_detail TO factor_miner_dashboard_ro;

COMMENT ON COLUMN public.factors.market_id IS '因子所属研究市场；A 股与美股页面必须按此列过滤。';
COMMENT ON TABLE public.visible_candidate_evaluations IS '尚未完成组合回测的开发期候选 IC/HAC 摘要；不保存或补造组合指标。';
COMMENT ON VIEW public.factor_metrics_detail IS '按市场隔离的因子目录；完整因子与仅完成开发期评价的候选共用展示合同。';

COMMIT;

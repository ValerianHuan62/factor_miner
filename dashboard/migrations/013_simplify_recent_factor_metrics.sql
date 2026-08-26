-- PostgreSQL 只展示 2025-01-01 至 2026-06-30 最近期指标。
-- 发现期与确认期继续保留在正式文件产物中，不进入日常查询表。
BEGIN;

ALTER TABLE public.factor_metrics RENAME TO factor_metrics_previous;

CREATE TABLE public.factor_metrics (
    factor_id TEXT PRIMARY KEY,
    horizon_days SMALLINT NOT NULL CHECK (horizon_days > 0),
    valid_dates INTEGER NOT NULL CHECK (valid_dates > 0),
    coverage_mean DOUBLE PRECISION NOT NULL CHECK (coverage_mean >= 0.0),
    ic_mean DOUBLE PRECISION NOT NULL,
    rank_ic_mean DOUBLE PRECISION NOT NULL,
    ic_std DOUBLE PRECISION NOT NULL CHECK (ic_std >= 0.0),
    rank_ic_std DOUBLE PRECISION NOT NULL CHECK (rank_ic_std >= 0.0),
    ic_ir DOUBLE PRECISION NOT NULL,
    rank_ic_ir DOUBLE PRECISION NOT NULL,
    ic_hac_t DOUBLE PRECISION NOT NULL,
    rank_ic_hac_t DOUBLE PRECISION NOT NULL,
    annualized_return DOUBLE PRECISION NOT NULL,
    max_drawdown DOUBLE PRECISION NOT NULL CHECK (max_drawdown >= 0.0),
    sharpe DOUBLE PRECISION NOT NULL,
    information_ratio DOUBLE PRECISION NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT factor_metrics_factor_id_fkey
        FOREIGN KEY (factor_id) REFERENCES public.factors(factor_id) ON DELETE CASCADE
);

INSERT INTO public.factor_metrics (
    factor_id, horizon_days, valid_dates, coverage_mean,
    ic_mean, rank_ic_mean, ic_std, rank_ic_std,
    ic_ir, rank_ic_ir, ic_hac_t, rank_ic_hac_t,
    annualized_return, max_drawdown, sharpe, information_ratio, evaluated_at
)
SELECT
    factor_id, recent_horizon_days, recent_valid_dates, recent_coverage_mean,
    recent_ic_mean, recent_rank_ic_mean, recent_ic_std, recent_rank_ic_std,
    recent_ic_ir, recent_rank_ic_ir, recent_ic_hac_t, recent_rank_ic_hac_t,
    recent_annualized_return, recent_max_drawdown, recent_sharpe,
    recent_information_ratio, evaluated_at
FROM public.factor_metrics_previous;

DROP TABLE public.factor_metrics_previous;

CREATE OR REPLACE VIEW public.factor_metrics_detail AS
SELECT
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
    m.evaluated_at
FROM public.factor_metrics AS m
JOIN public.factors AS f ON f.factor_id = m.factor_id;

GRANT SELECT ON public.factor_metrics TO factor_miner_dashboard_ro;
GRANT SELECT ON public.factor_metrics_detail TO factor_miner_dashboard_ro;

COMMENT ON TABLE public.factor_metrics IS '2025-01-01 至 2026-06-30 最近期目标多头指标。';
COMMENT ON VIEW public.factor_metrics_detail IS '因子指标与中文假设、机制、公式的联查视图。';
COMMENT ON CONSTRAINT factor_metrics_factor_id_fkey ON public.factor_metrics
    IS '支持从指标因子编号跳转到因子目录。';

COMMIT;

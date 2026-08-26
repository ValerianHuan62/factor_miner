-- Dashboard 因子指标改为目标多头口径：发现期定方向，确认期作正式评价，最近期只作展示。
-- 非汇总研究明细和全部内容身份继续只保存在正式文件产物中。
BEGIN;

ALTER TABLE public.factors
    RENAME COLUMN expected_direction TO hypothesis_direction;

ALTER TABLE public.factors
    ADD COLUMN discovered_direction TEXT NOT NULL DEFAULT '正向'
        CHECK (discovered_direction IN ('正向', '负向')),
    ADD COLUMN direction_relation TEXT NOT NULL DEFAULT '与假设一致'
        CHECK (direction_relation IN ('与假设一致', '与假设相反'));

ALTER TABLE public.factors
    ALTER COLUMN discovered_direction DROP DEFAULT,
    ALTER COLUMN direction_relation DROP DEFAULT;

DROP TABLE public.factor_metrics;

CREATE TABLE public.factor_metrics (
    factor_id TEXT PRIMARY KEY REFERENCES public.factors(factor_id) ON DELETE CASCADE,
    discovery_rank_ic_mean DOUBLE PRECISION NOT NULL,
    confirmation_horizon_days SMALLINT NOT NULL CHECK (confirmation_horizon_days > 0),
    confirmation_valid_dates INTEGER NOT NULL CHECK (confirmation_valid_dates > 0),
    confirmation_coverage_mean DOUBLE PRECISION NOT NULL CHECK (confirmation_coverage_mean >= 0.0),
    confirmation_ic_mean DOUBLE PRECISION NOT NULL,
    confirmation_rank_ic_mean DOUBLE PRECISION NOT NULL,
    confirmation_ic_std DOUBLE PRECISION NOT NULL CHECK (confirmation_ic_std >= 0.0),
    confirmation_rank_ic_std DOUBLE PRECISION NOT NULL CHECK (confirmation_rank_ic_std >= 0.0),
    confirmation_ic_ir DOUBLE PRECISION NOT NULL,
    confirmation_rank_ic_ir DOUBLE PRECISION NOT NULL,
    confirmation_ic_hac_t DOUBLE PRECISION NOT NULL,
    confirmation_rank_ic_hac_t DOUBLE PRECISION NOT NULL,
    confirmation_win_rate DOUBLE PRECISION NOT NULL CHECK (confirmation_win_rate BETWEEN 0.0 AND 1.0),
    confirmation_annualized_return DOUBLE PRECISION NOT NULL,
    confirmation_max_drawdown DOUBLE PRECISION NOT NULL CHECK (confirmation_max_drawdown >= 0.0),
    confirmation_sharpe DOUBLE PRECISION NOT NULL,
    confirmation_information_ratio DOUBLE PRECISION NOT NULL,
    recent_horizon_days SMALLINT NOT NULL CHECK (recent_horizon_days > 0),
    recent_valid_dates INTEGER NOT NULL CHECK (recent_valid_dates > 0),
    recent_coverage_mean DOUBLE PRECISION NOT NULL CHECK (recent_coverage_mean >= 0.0),
    recent_ic_mean DOUBLE PRECISION NOT NULL,
    recent_rank_ic_mean DOUBLE PRECISION NOT NULL,
    recent_ic_std DOUBLE PRECISION NOT NULL CHECK (recent_ic_std >= 0.0),
    recent_rank_ic_std DOUBLE PRECISION NOT NULL CHECK (recent_rank_ic_std >= 0.0),
    recent_ic_ir DOUBLE PRECISION NOT NULL,
    recent_rank_ic_ir DOUBLE PRECISION NOT NULL,
    recent_ic_hac_t DOUBLE PRECISION NOT NULL,
    recent_rank_ic_hac_t DOUBLE PRECISION NOT NULL,
    recent_annualized_return DOUBLE PRECISION NOT NULL,
    recent_max_drawdown DOUBLE PRECISION NOT NULL CHECK (recent_max_drawdown >= 0.0),
    recent_sharpe DOUBLE PRECISION NOT NULL,
    recent_information_ratio DOUBLE PRECISION NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

GRANT SELECT ON public.factor_metrics TO factor_miner_dashboard_ro;

COMMENT ON COLUMN public.factors.hypothesis_direction IS '结果揭晓前冻结的事前预期方向。';
COMMENT ON COLUMN public.factors.discovered_direction IS '仅由冻结发现期 RankIC 决定的目标多头方向。';
COMMENT ON COLUMN public.factors.direction_relation IS '发现方向与事前假设方向的关系，不代表机制得到证明。';
COMMENT ON TABLE public.factor_metrics IS '确认期和最近期的目标多头核心指标；不保存逐期收益或分组收益。';

COMMIT;

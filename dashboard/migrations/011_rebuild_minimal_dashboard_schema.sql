-- 一次性重建 Dashboard 数据库：旧数据均为测试数据，不做迁移。
-- 研究序列、分组收益、风险归因明细与因子图谱继续保存在正式文件产物中。
BEGIN;

DROP SCHEMA public CASCADE;
CREATE SCHEMA public;

CREATE TABLE public.research_runs (
    run_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    hypothesis_count INTEGER NOT NULL DEFAULT 0 CHECK (hypothesis_count >= 0),
    factor_count INTEGER NOT NULL DEFAULT 0 CHECK (factor_count >= 0),
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (stage ~ '[一-龥]'),
    CHECK (status ~ '[一-龥]'),
    CHECK (error_message IS NULL OR error_message ~ '[一-龥]')
);

CREATE TABLE public.research_hypotheses (
    run_id TEXT NOT NULL REFERENCES public.research_runs(run_id) ON DELETE CASCADE,
    hypothesis_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (title ~ '[一-龥]'),
    claim TEXT NOT NULL CHECK (claim ~ '[一-龥]'),
    mechanism TEXT NOT NULL CHECK (mechanism ~ '[一-龥]'),
    expected_direction TEXT NOT NULL CHECK (expected_direction IN ('正向', '负向', '中性')),
    observable_proxy TEXT NOT NULL CHECK (observable_proxy ~ '[一-龥]'),
    independent_verification TEXT NOT NULL CHECK (independent_verification ~ '[一-龥]'),
    competing_explanations TEXT NOT NULL CHECK (competing_explanations ~ '[一-龥]'),
    failure_modes TEXT NOT NULL CHECK (failure_modes ~ '[一-龥]'),
    falsification_path TEXT NOT NULL CHECK (falsification_path ~ '[一-龥]'),
    source_description TEXT NOT NULL CHECK (source_description ~ '[一-龥]'),
    status TEXT NOT NULL CHECK (status ~ '[一-龥]'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, hypothesis_id)
);

CREATE TABLE public.factors (
    factor_id TEXT PRIMARY KEY CHECK (factor_id ~ '^huan[0-9]{3,}$'),
    hypothesis TEXT NOT NULL CHECK (hypothesis ~ '[一-龥]'),
    mechanism TEXT NOT NULL CHECK (mechanism ~ '[一-龥]'),
    expected_direction TEXT NOT NULL CHECK (expected_direction IN ('正向', '负向', '中性')),
    formula TEXT NOT NULL,
    calculation TEXT NOT NULL CHECK (calculation ~ '[一-龥]'),
    category TEXT NOT NULL CHECK (category ~ '[一-龥]'),
    trading_timing TEXT NOT NULL CHECK (trading_timing ~ '[一-龥]'),
    status TEXT NOT NULL CHECK (status ~ '[一-龥]'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE public.factor_metrics (
    factor_id TEXT PRIMARY KEY REFERENCES public.factors(factor_id) ON DELETE CASCADE,
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
    win_rate DOUBLE PRECISION NOT NULL CHECK (win_rate BETWEEN 0.0 AND 1.0),
    annualized_return DOUBLE PRECISION NOT NULL,
    max_drawdown DOUBLE PRECISION NOT NULL CHECK (max_drawdown >= 0.0),
    sharpe DOUBLE PRECISION NOT NULL,
    information_ratio DOUBLE PRECISION NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP SCHEMA IF EXISTS factor_miner_internal CASCADE;
CREATE SCHEMA factor_miner_internal;

CREATE TABLE factor_miner_internal.factor_id_map (
    source_candidate_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL UNIQUE CHECK (factor_id ~ '^huan[0-9]{3,}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

REVOKE ALL ON SCHEMA factor_miner_internal FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO factor_miner_dashboard_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO factor_miner_dashboard_ro;

COMMENT ON TABLE public.factors IS 'Dashboard 因子目录：只保存中文研究说明与公式。';
COMMENT ON TABLE public.factor_metrics IS '每个因子的最新完整指标，不保存分组收益序列。';
COMMENT ON TABLE public.research_runs IS '不定时研究批次的简要阶段与结果计数。';
COMMENT ON TABLE public.research_hypotheses IS 'Dashboard 待审批及已审批的中文研究假设。';
COMMENT ON SCHEMA factor_miner_internal IS '仅供写入程序维持因子编号稳定，不授权 Dashboard 读取。';

COMMIT;

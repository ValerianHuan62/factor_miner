-- V1 Dashboard 只读投影。JSON/JSONL/Parquet 产物仍是正式主存储。
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY,
    snapshot_sha256 CHAR(64) NOT NULL,
    artifact_manifest_sha256 CHAR(64) NOT NULL,
    published BOOLEAN NOT NULL,
    snapshot_json JSONB NOT NULL,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifact_refs (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    relative_path TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    PRIMARY KEY (run_id, relative_path)
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    spec_hash CHAR(64) NOT NULL,
    hypothesis_json JSONB NOT NULL,
    factor_category TEXT,
    formula_summary TEXT,
    first_seen_run_id TEXT,
    quality_status TEXT NOT NULL DEFAULT 'not_assessed'
);

CREATE TABLE IF NOT EXISTS candidate_metrics (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    metrics_json JSONB NOT NULL,
    PRIMARY KEY (run_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS portfolio_metrics (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    series_name TEXT NOT NULL,
    metrics_json JSONB NOT NULL,
    PRIMARY KEY (run_id, candidate_id, series_name)
);

CREATE TABLE IF NOT EXISTS portfolio_daily (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    exit_date DATE NOT NULL,
    series_name TEXT NOT NULL,
    return_value DOUBLE PRECISION,
    PRIMARY KEY (run_id, candidate_id, exit_date, series_name)
);

CREATE TABLE IF NOT EXISTS barra_exposure_summary (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    signal_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    portfolio TEXT NOT NULL,
    exposure_json JSONB NOT NULL,
    PRIMARY KEY (run_id, candidate_id, signal_date, entry_date, portfolio)
);

CREATE TABLE IF NOT EXISTS barra_attribution (
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    candidate_id TEXT NOT NULL,
    signal_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    portfolio TEXT NOT NULL,
    factor TEXT NOT NULL,
    contribution DOUBLE PRECISION,
    attribution_json JSONB NOT NULL,
    PRIMARY KEY (run_id, candidate_id, signal_date, entry_date, portfolio, factor)
);

-- 生产环境由 DBA 创建并授权只读 Dashboard 账号；不在应用启动时自动提权。

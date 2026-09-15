"""Dashboard 部署层的 PostgreSQL 只读投影适配器。"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from typing import Any, Iterable
import json

from factor_miner.canonical import sha256_json
from factor_miner.autonomous_schema import AutonomousResearchState
from factor_miner.dashboard_store import DashboardStore
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisBatch,
    LightweightReviewBatch,
)


def _as_dict(value: object) -> dict[str, Any]:
    """把可选对象安全地收窄为字典。"""

    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """把可选数组安全地收窄为列表。"""

    return value if isinstance(value, list) else []


def _text_list(value: object) -> list[str]:
    """提取可写入 PostgreSQL text[] 的字符串数组。"""

    return [str(item) for item in _as_list(value)]


def _number(value: object) -> float | None:
    """将数值字段转换为可空浮点数。"""

    return float(value) if isinstance(value, (int, float)) else None


def _integer(value: object) -> int | None:
    """将整数型字段转换为可空整数。"""

    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _chinese_or(value: object, fallback: str) -> str:
    """数据库描述字段只保留中文；外部枚举使用明确中文默认值。"""

    text = str(value or "").strip()
    return text if any("\u3400" <= char <= "\u9fff" for char in text) else fallback


def _candidate_ids(snapshot: dict[str, object]) -> list[str]:
    """提取有候选定义、IC 或组合结果的候选 ID。"""

    result: set[str] = set()
    for name in (
        "candidate_definitions",
        "ic_diagnostics",
        "recent_ic_diagnostics",
        "direction_decisions",
        "portfolio_metrics",
        "recent_portfolio_metrics",
        "portfolio_daily",
    ):
        value = snapshot.get(name)
        if isinstance(value, dict):
            result.update(str(key) for key in _candidate_payload(value))
    summaries = snapshot.get("candidate_metrics")
    if isinstance(summaries, dict):
        for candidate_id, summary in summaries.items():
            if isinstance(summary, dict) and (
                summary.get("candidate_id") == candidate_id
                or candidate_id in result
            ):
                result.add(str(candidate_id))
    return sorted(result)


def _candidate_payload(value: object) -> dict[str, Any]:
    """读取候选维度；兼容带 version/candidates 包装的正式产物。"""

    payload = _as_dict(value)
    candidates = payload.get("candidates")
    return candidates if isinstance(candidates, dict) else payload


def _reference_candidate_id(
    candidate_id: str,
    snapshot: dict[str, object] | None = None,
) -> str:
    """读取投影事务已经分配的稳定 huan 业务编号。"""

    alias = _as_dict((snapshot or {}).get("candidate_aliases")).get(candidate_id)
    if isinstance(alias, str):
        return alias

    prefix = "pilot_fixed_"
    if candidate_id.startswith(prefix):
        suffix = candidate_id[len(prefix):]
        if suffix.isdigit():
            return f"huan{int(suffix):03d}"
    return candidate_id


def _candidate_definition(
    snapshot: dict[str, object],
    candidate_id: str,
) -> dict[str, Any]:
    """读取已扁平化的候选定义，并兼容旧投影的嵌套结构。"""

    definitions = snapshot.get("candidate_definitions")
    definition = _as_dict(definitions).get(candidate_id)
    result = dict(_as_dict(definition))
    hypothesis = _as_dict(result.get("hypothesis"))
    formula = _as_dict(result.get("formula"))
    for field, source in (
        ("hypothesis_claim", "claim"),
        ("hypothesis_mechanism", "mechanism"),
        ("expected_sign", "expected_sign"),
        ("observable_proxy", "observable_proxy"),
        ("independent_verification", "independent_verification"),
        ("competing_explanations", "competing_explanations"),
        ("failure_modes", "failure_modes"),
        ("falsification_path", "falsification_path"),
    ):
        if field not in result and source in hypothesis:
            result[field] = hypothesis[source]
    for field in (
        "formula_operator",
        "formula_text",
        "formula_field",
        "formula_period",
        "formula_window",
        "formula_center",
    ):
        if field not in result and field in formula:
            result[field] = formula[field]
    return result


def _ic_summary(diagnostics: object) -> dict[str, object]:
    """将 IC 诊断压平成候选总览的一行明确列。"""

    values = _as_dict(diagnostics)
    daily = [row for row in _as_list(values.get("daily")) if isinstance(row, dict)]
    eligible = [row.get("eligible_count") for row in daily if isinstance(row.get("eligible_count"), int)]
    coverage_mean = mean(eligible) if eligible else None
    primary_horizon = _integer(values.get("primary_horizon")) or 5
    decay_rows = [row for row in _as_list(values.get("decay")) if isinstance(row, dict)]
    if not decay_rows and any(key in values for key in ("ic_mean", "rank_ic_mean")):
        decay_rows = [{"horizon": primary_horizon, "valid_dates": len(daily)}]
    row: dict[str, object] = {
        "primary_horizon": primary_horizon,
        "valid_dates": len(daily),
        "coverage_mean": coverage_mean,
        "ic_mean": _number(values.get("ic_mean")),
        "rank_ic_mean": _number(values.get("rank_ic_mean")),
        "ic_std": _number(values.get("ic_std")),
        "rank_ic_std": _number(values.get("rank_ic_std")),
        "ic_ir": _number(values.get("ic_ir")),
        "rank_ic_ir": _number(values.get("rank_ic_ir")),
        "ic_hac_t": _number(values.get("ic_hac_t")),
        "rank_ic_hac_t": _number(values.get("rank_ic_hac_t")),
        "p_ic_lt_neg_002": _number(values.get("p_ic_lt_neg_002")),
        "p_ic_gt_pos_002": _number(values.get("p_ic_gt_pos_002")),
    }
    for decay in decay_rows:
        horizon = _integer(decay.get("horizon"))
        if horizon is None:
            continue
        row[f"ic_mean_h{horizon}"] = _number(decay.get("ic_mean"))
        row[f"rank_ic_mean_h{horizon}"] = _number(decay.get("rank_ic_mean"))
        row[f"valid_dates_h{horizon}"] = _integer(decay.get("valid_dates"))
    return row


def _candidate_backtest_headline(
    snapshot: dict[str, object],
    candidate_id: str,
) -> dict[str, object]:
    """提取候选总览所需的 Q10-Q1 净收益摘要，不吞掉组合明细。"""

    portfolio = _candidate_payload(snapshot.get("portfolio_metrics")).get(candidate_id)
    series = _as_dict(_as_dict(portfolio).get("series"))
    headline = _as_dict(series.get("Q10_Q1_net_return"))
    daily = _portfolio_daily_rows(snapshot, candidate_id)
    returns = [row.get("Q10_Q1_net_return") for row in daily]
    valid_returns = [float(value) for value in returns if isinstance(value, (int, float))]
    return {
        "factor_return": _number(headline.get("period_return")),
        "win_rate": (sum(value > 0 for value in valid_returns) / len(valid_returns)) if valid_returns else None,
        "annualized_return": _number(headline.get("annualized_return")),
        "max_drawdown": _number(headline.get("max_drawdown")),
        "sharpe": _number(headline.get("sharpe")),
        "information_ratio": _number(headline.get("information_ratio")),
    }


def _candidate_summary(
    snapshot: dict[str, object],
    candidate_id: str,
) -> dict[str, Any]:
    """读取运行指标中的候选摘要。"""

    return _as_dict(
        _candidate_payload(snapshot.get("candidate_metrics")).get(candidate_id)
    )


def _direction_summary(
    snapshot: dict[str, object],
    candidate_id: str,
) -> dict[str, object]:
    """读取只由冻结发现期决定的方向，不使用确认期或最近期结果。"""

    payload = _candidate_payload(snapshot.get("direction_decisions")).get(candidate_id)
    decision = _as_dict(_as_dict(payload).get("decision", payload))
    discovery = _as_dict(decision.get("discovery_summary"))
    selected = {
        "positive": "正向",
        "negative": "负向",
    }.get(str(decision.get("selected_direction")))
    relation = {
        "supported": "与假设一致",
        "reversed": "与假设相反",
    }.get(str(decision.get("hypothesis_relation")))
    rank_ic = _number(discovery.get("rank_ic"))
    if selected is None or relation is None or rank_ic is None:
        raise FactorMinerError(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"候选 {candidate_id} 缺少冻结发现方向",
        )
    return {
        "discovered_direction": selected,
        "direction_relation": relation,
        "discovery_rank_ic_mean": rank_ic,
    }


def _target_long_headline(
    snapshot: dict[str, object],
    candidate_id: str,
    *,
    recent: bool,
) -> dict[str, object]:
    """提取确认期或最近期的目标多头净收益摘要。"""

    source_name = "recent_portfolio_metrics" if recent else "portfolio_metrics"
    portfolio = _candidate_payload(snapshot.get(source_name)).get(candidate_id)
    series = _as_dict(_as_dict(portfolio).get("series"))
    headline = _as_dict(series.get("target_long_net_return"))
    return {
        "annualized_return": _number(headline.get("annualized_return")),
        "max_drawdown": _number(headline.get("max_drawdown")),
        "sharpe": _number(headline.get("sharpe")),
        "information_ratio": _number(headline.get("information_ratio")),
    }


def _governance_win_rate(
    snapshot: dict[str, object],
    candidate_id: str,
) -> float | None:
    """读取冻结极端组差治理摘要中的确认期胜率。"""

    governance = _candidate_payload(snapshot.get("portfolio_governance")).get(
        candidate_id
    )
    return _number(_as_dict(governance).get("win_rate"))


def _target_long_metric_row(
    snapshot: dict[str, object],
    candidate_id: str,
) -> dict[str, object]:
    """提取 2025-01-01 至 2026-06-30 最近期的公开核心指标。"""

    diagnostics = _candidate_payload(snapshot.get("recent_ic_diagnostics")).get(
        candidate_id
    )
    values = _ic_summary(diagnostics)
    values.update(_target_long_headline(snapshot, candidate_id, recent=True))
    result = {
        target: values.get(source)
        for source, target in (
            ("primary_horizon", "horizon_days"),
            ("valid_dates", "valid_dates"),
            ("coverage_mean", "coverage_mean"),
            ("ic_mean", "ic_mean"),
            ("rank_ic_mean", "rank_ic_mean"),
            ("ic_std", "ic_std"),
            ("rank_ic_std", "rank_ic_std"),
            ("ic_ir", "ic_ir"),
            ("rank_ic_ir", "rank_ic_ir"),
            ("ic_hac_t", "ic_hac_t"),
            ("rank_ic_hac_t", "rank_ic_hac_t"),
            ("annualized_return", "annualized_return"),
            ("max_drawdown", "max_drawdown"),
            ("sharpe", "sharpe"),
            ("information_ratio", "information_ratio"),
        )
    }
    missing = [
        name
        for name, value in result.items()
        if name not in {"discovered_direction", "direction_relation"}
        and not isinstance(value, (int, float))
    ]
    if missing:
        raise FactorMinerError(
            FailureCode.PILOT_INPUT_CONTRACT_INVALID,
            f"候选 {candidate_id} 缺少 PostgreSQL 目标多头核心指标：{missing}",
        )
    return result


def _portfolio_daily_rows(
    snapshot: dict[str, object],
    candidate_id: str,
) -> list[dict[str, Any]]:
    """统一读取 Pilot 裸列表与 Stage C ``{daily: [...]}`` 包装。"""

    value = _candidate_payload(snapshot.get("portfolio_daily")).get(candidate_id)
    payload = _as_dict(value).get("daily", value)
    return [row for row in _as_list(payload) if isinstance(row, dict)]


class PostgresDashboardStore:
    """把正式文件产物投影为简洁的人类可读索引。"""

    def __init__(self, dsn: str, *, market_id: str = "a_share") -> None:
        if not dsn.strip():
            raise FactorMinerError(
                FailureCode.RUNTIME_BOUNDARY_ERROR,
                "Dashboard 数据库连接配置不能为空",
            )
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError:
            raise FactorMinerError(
                FailureCode.RUNTIME_BOUNDARY_ERROR,
                "服务器尚未安装 Dashboard PostgreSQL 依赖",
            ) from None
        self._psycopg = psycopg
        self._dsn = dsn
        self._market_id = market_id.strip()
        if not self._market_id:
            raise FactorMinerError(
                FailureCode.RUNTIME_BOUNDARY_ERROR,
                "Dashboard market_id 不能为空",
            )

    def replace_run_snapshot(self, run_id: str, snapshot: dict[str, object]) -> None:
        """只投影因子说明与最新完整指标；图表数据仍留在正式文件。"""

        if snapshot.get("run_id") != run_id:
            raise FactorMinerError(
                FailureCode.LEDGER_CORRUPT,
                "Dashboard 投影 run_id 与快照身份不一致",
            )
        self._require_complete_campaign_metrics(snapshot)
        with self._psycopg.connect(self._dsn) as connection:
            aliases = self._allocate_candidate_references(
                connection,
                tuple(_candidate_ids(snapshot)),
            )
            snapshot = dict(snapshot)
            snapshot["candidate_aliases"] = aliases
            self._project_candidates(connection, run_id, snapshot)
            self._project_ic_metrics(connection, run_id, snapshot)

    def load_factor_aliases(self) -> dict[str, str]:
        """读取内部身份映射；Dashboard 只向用户显示稳定 huanNNN。"""

        with self._psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                """
                SELECT source_candidate_id, factor_id
                FROM factor_miner_internal.factor_id_map
                WHERE market_id = %s
                  AND factor_id ~ '^huan[0-9]{3,}$'
                ORDER BY substring(factor_id FROM 5)::integer
                """,
                (self._market_id,),
            ).fetchall()
        return {str(source_id): str(factor_id) for source_id, factor_id in rows}

    def load_factor_metrics_detail(self) -> list[dict[str, object]]:
        """原指标读取完成后，按显式上下文附加状态分类。"""
        rows = self._load_factor_metrics_detail()
        from dashboard.market_profiles import profile_by_id
        from factor_miner_pg.regime_store import attach_pg_records
        profile = profile_by_id(self._market_id)
        context = profile.regime_context_id if profile else None
        if not context:
            from factor_miner.regime import record_fields
            return [{**row,**record_fields(None),'regime_record':None} for row in rows]
        with self._psycopg.connect(self._dsn) as connection:
            enriched = attach_pg_records(connection, rows, self._market_id, context)
            if profile.regime_manifest_path:
                from factor_miner.regime import load_manifest, file_sha
                manifest = load_manifest(profile.regime_manifest_path,self._market_id)
                digests = connection.execute('SELECT DISTINCT manifest_sha256 FROM public.factor_regime_current WHERE market_id=%s AND context_id=%s',(self._market_id,context)).fetchall()
                if manifest.context_id != context or digests != [(file_sha(profile.regime_manifest_path),)]:
                    raise ValueError('状态文件与 PostgreSQL 发布不同步，请重新投影')
            return enriched

    def _load_factor_metrics_detail(self) -> list[dict[str, object]]:
        """读取因子定义与最新聚合指标联查视图，不读取逐日或个股数据。"""

        columns = (
            "market_id",
            "factor_id",
            "hypothesis",
            "mechanism",
            "formula",
            "calculation",
            "discovered_direction",
            "direction_relation",
            "status",
            "horizon_days",
            "valid_dates",
            "coverage_mean",
            "ic_mean",
            "rank_ic_mean",
            "ic_std",
            "rank_ic_std",
            "ic_ir",
            "rank_ic_ir",
            "ic_hac_t",
            "rank_ic_hac_t",
            "annualized_return",
            "max_drawdown",
            "sharpe",
            "information_ratio",
            "has_portfolio",
            "evaluation_scope",
            "run_id",
            "evaluated_at",
        )
        with self._psycopg.connect(self._dsn) as connection:
            if self._market_id == "us_equity" and connection.execute(
                "SELECT to_regclass('public.screened_factors')"
            ).fetchone()[0] is not None:
                from dashboard.screened_store import metric_rows
                return metric_rows(connection)
            rows = connection.execute(
                """
                SELECT
                    market_id, factor_id, hypothesis, mechanism, formula, calculation,
                    discovered_direction, direction_relation, status,
                    horizon_days, valid_dates, coverage_mean,
                    ic_mean, rank_ic_mean, ic_std, rank_ic_std,
                    ic_ir, rank_ic_ir, ic_hac_t, rank_ic_hac_t,
                    annualized_return, max_drawdown, sharpe,
                    information_ratio, has_portfolio, evaluation_scope,
                    run_id, evaluated_at
                FROM public.factor_metrics_detail
                WHERE market_id = %s
                ORDER BY substring(factor_id FROM 5)::integer
                """,
                (self._market_id,),
            ).fetchall()
            result = [dict(zip(columns, row, strict=True)) for row in rows]
            if self._market_id == "us_equity" and connection.execute(
                "SELECT to_regclass('factor_miner_internal.causal_reports')"
            ).fetchone()[0] is not None:
                reports = connection.execute("""SELECT DISTINCT ON (factor_id)
                    factor_id, report->'evaluation', report->'reference_metrics',
                    report->'execution_audit'->'target'->>'valuation_status',
                    report->'execution_audit'->'benchmark'->>'valuation_status', run_id, created_at
                    FROM factor_miner_internal.causal_reports ORDER BY factor_id, created_at DESC""").fetchall()
                from dashboard.accounting_projection import overlay_accounting_metrics

                result = overlay_accounting_metrics(result, reports)
        return result

    def replace_visible_candidate_evaluations(
        self,
        run_id: str,
        candidates: Iterable[dict[str, object]],
    ) -> dict[str, str]:
        """投影仅完成开发期 IC/HAC 的候选，不补造组合回测指标。"""

        rows = tuple(dict(row) for row in candidates)
        source_ids = tuple(str(row["source_candidate_id"]) for row in rows)
        with self._psycopg.connect(self._dsn) as connection:
            aliases = self._allocate_candidate_references(connection, source_ids)
            for row in rows:
                source_id = str(row["source_candidate_id"])
                factor_id = aliases[source_id]
                existing_market = connection.execute(
                    "SELECT market_id FROM public.factors WHERE factor_id = %s",
                    (factor_id,),
                ).fetchone()
                if existing_market is not None and str(existing_market[0]) != self._market_id:
                    raise FactorMinerError(
                        FailureCode.LEDGER_CORRUPT,
                        f"因子编号 {factor_id} 已属于其他市场",
                    )
                connection.execute(
                    """
                    INSERT INTO public.factors (
                        market_id, factor_id, hypothesis, mechanism,
                        discovered_direction, direction_relation, formula,
                        calculation, hypothesis_direction, category,
                        trading_timing, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (factor_id) DO UPDATE SET
                        hypothesis = EXCLUDED.hypothesis,
                        mechanism = EXCLUDED.mechanism,
                        discovered_direction = EXCLUDED.discovered_direction,
                        direction_relation = EXCLUDED.direction_relation,
                        formula = EXCLUDED.formula,
                        calculation = EXCLUDED.calculation,
                        hypothesis_direction = EXCLUDED.hypothesis_direction,
                        category = EXCLUDED.category,
                        trading_timing = EXCLUDED.trading_timing,
                        status = EXCLUDED.status,
                        updated_at = NOW()
                    """,
                    (
                        self._market_id,
                        factor_id,
                        row["hypothesis"],
                        row["mechanism"],
                        row["discovered_direction"],
                        row["direction_relation"],
                        row["formula"],
                        row["calculation"],
                        row["hypothesis_direction"],
                        row["category"],
                        row["trading_timing"],
                        row["status"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.visible_candidate_evaluations (
                        market_id, factor_id, run_id, evaluation_scope,
                        horizon_days, valid_dates, coverage_mean,
                        rank_ic_mean, rank_ic_std, rank_ic_ir,
                        rank_ic_hac_t, raw_p_value, bonferroni_p_value,
                        evaluated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (market_id, factor_id) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        evaluation_scope = EXCLUDED.evaluation_scope,
                        horizon_days = EXCLUDED.horizon_days,
                        valid_dates = EXCLUDED.valid_dates,
                        coverage_mean = EXCLUDED.coverage_mean,
                        rank_ic_mean = EXCLUDED.rank_ic_mean,
                        rank_ic_std = EXCLUDED.rank_ic_std,
                        rank_ic_ir = EXCLUDED.rank_ic_ir,
                        rank_ic_hac_t = EXCLUDED.rank_ic_hac_t,
                        raw_p_value = EXCLUDED.raw_p_value,
                        bonferroni_p_value = EXCLUDED.bonferroni_p_value,
                        evaluated_at = EXCLUDED.evaluated_at
                    """,
                    (
                        self._market_id,
                        factor_id,
                        run_id,
                        row["evaluation_scope"],
                        row["horizon_days"],
                        row["valid_dates"],
                        row["coverage_mean"],
                        row["rank_ic_mean"],
                        row["rank_ic_std"],
                        row["rank_ic_ir"],
                        row["rank_ic_hac_t"],
                        row["raw_p_value"],
                        row["bonferroni_p_value"],
                        row["evaluated_at"],
                    ),
                )
        return aliases

    def project_control_state(
        self,
        state: AutonomousResearchState,
        *,
        hypotheses: LightweightHypothesisBatch | None = None,
        review: LightweightReviewBatch | None = None,
        heartbeat_at: datetime | None = None,
    ) -> None:
        """投影中文批次摘要与假设全文；控制身份仍以文件账本为准。"""

        if hypotheses is not None and hypotheses.run_id != state.run_id:
            raise ValueError("假设批次与控制状态的 run_id 不一致")
        if review is not None and review.run_id != state.run_id:
            raise ValueError("审批批次与控制状态的 run_id 不一致")
        status = "运行失败" if state.stage.value == "failed" else (
            "运行结束" if state.stage.terminal else "运行中"
        )
        with self._psycopg.connect(self._dsn) as connection:
            connection.execute(
                """
                INSERT INTO research_runs (
                    run_id, market_id, stage, status, hypothesis_count, factor_count,
                    error_message, started_at, finished_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    stage = EXCLUDED.stage,
                    status = EXCLUDED.status,
                    hypothesis_count = GREATEST(research_runs.hypothesis_count, EXCLUDED.hypothesis_count),
                    factor_count = research_runs.factor_count,
                    error_message = EXCLUDED.error_message,
                    finished_at = EXCLUDED.finished_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    state.run_id,
                    self._market_id,
                    state.stage_label,
                    status,
                    len(hypotheses.hypotheses) if hypotheses else 0,
                    0,
                    _chinese_or(state.last_error, "运行失败，详细原因请查看正式文件账本")
                    if state.last_error else None,
                    state.created_at,
                    state.updated_at if state.stage.terminal else None,
                    state.updated_at,
                ),
            )
            if hypotheses is not None:
                self._project_research_hypotheses(
                    connection,
                    hypotheses,
                    review=review,
                )

    def update_research_run_factor_count(self, run_id: str, factor_count: int) -> None:
        """评价发布后记录实际成功因子数，不使用预留候选槽数量。"""

        if factor_count < 0:
            raise ValueError("实际成功因子数不能为负")
        with self._psycopg.connect(self._dsn) as connection:
            connection.execute(
                "UPDATE research_runs SET factor_count = %s, updated_at = NOW() "
                "WHERE run_id = %s",
                (factor_count, run_id),
            )

    @staticmethod
    def _project_research_hypotheses(
        connection: Any,
        hypotheses: LightweightHypothesisBatch,
        *,
        review: LightweightReviewBatch | None,
    ) -> None:
        """把通过 Schema 的十条假设全文投影为中文描述列。"""

        decisions = (
            {item.logical_slot_id: item for item in review.decisions}
            if review is not None
            else {}
        )
        for draft in hypotheses.hypotheses:
            decision = decisions.get(draft.logical_slot_id)
            sources = "；".join(
                f"{item.claim_fragment}（{item.rationale}）"
                for item in draft.source_records
            ) or "无外部来源，仅作为待检验研究假设"
            decision_status = {
                "approved": "已批准",
                "rejected": "已拒绝",
            }.get(str(decision.decision) if decision else "", "待审批")
            connection.execute(
                """
                INSERT INTO research_hypotheses (
                    run_id, hypothesis_id, title, claim, mechanism,
                    expected_direction, observable_proxy,
                    independent_verification, competing_explanations,
                    failure_modes, falsification_path, source_description,
                    status, reviewed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (run_id, hypothesis_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    claim = EXCLUDED.claim,
                    mechanism = EXCLUDED.mechanism,
                    expected_direction = EXCLUDED.expected_direction,
                    observable_proxy = EXCLUDED.observable_proxy,
                    independent_verification = EXCLUDED.independent_verification,
                    competing_explanations = EXCLUDED.competing_explanations,
                    failure_modes = EXCLUDED.failure_modes,
                    falsification_path = EXCLUDED.falsification_path,
                    source_description = EXCLUDED.source_description,
                    status = EXCLUDED.status,
                    reviewed_at = EXCLUDED.reviewed_at
                """,
                (
                    hypotheses.run_id,
                    draft.logical_slot_id,
                    f"{draft.logical_slot_id} 因子假设",
                    draft.prior_claim,
                    draft.mechanism,
                    {"positive": "正向", "negative": "负向", "neutral": "中性"}.get(
                        str(draft.expected_direction), "中性"
                    ),
                    draft.observable_proxy,
                    draft.independent_verification,
                    "；".join(draft.competing_explanations),
                    "；".join(draft.failure_modes),
                    draft.falsification_path,
                    sources,
                    decision_status,
                    decision.decided_at if decision else None,
                ),
            )

    def load_latest_research_control(self) -> dict[str, object] | None:
        """读取最近研究批次的中文摘要。"""

        columns = (
            "run_id", "stage", "status", "hypothesis_count", "factor_count",
            "error_message", "started_at", "finished_at", "updated_at",
        )
        with self._psycopg.connect(self._dsn) as connection:
            row = connection.execute(
                "SELECT " + ", ".join(columns)
                + " FROM research_runs WHERE market_id = %s "
                  "ORDER BY updated_at DESC LIMIT 1",
                (self._market_id,),
            ).fetchone()
        return dict(zip(columns, row, strict=True)) if row is not None else None

    def project_worker_heartbeat(self, heartbeat_at: datetime | None = None) -> None:
        """兼容旧 Worker 调用；精简版不保存在线状态。"""

        return None

    def load_worker_status(self) -> dict[str, object]:
        """精简版不再跟踪 Worker 在线状态。"""

        return {}

    def load_research_hypotheses(self, run_id: str) -> tuple[dict[str, object], ...]:
        """按 H01–H10 返回完整中文假设与审批状态。"""

        columns = (
            "hypothesis_id", "title", "claim", "mechanism", "expected_direction",
            "observable_proxy", "independent_verification",
            "competing_explanations", "failure_modes", "falsification_path",
            "source_description", "status", "created_at", "reviewed_at",
        )
        with self._psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                "SELECT " + ", ".join(columns)
                + " FROM research_hypotheses WHERE run_id = %s ORDER BY hypothesis_id",
                (run_id,),
            ).fetchall()
        return tuple(dict(zip(columns, row, strict=True)) for row in rows)

    @staticmethod
    def _require_complete_campaign_metrics(snapshot: dict[str, object]) -> None:
        """正式确认指标须完整，但 PostgreSQL 只展示最近期指标。"""

        if not isinstance(snapshot.get("campaign_metadata"), dict):
            return
        for candidate_id in _candidate_ids(snapshot):
            confirmation = _ic_summary(
                _candidate_payload(snapshot.get("ic_diagnostics")).get(candidate_id)
            )
            confirmation.update(
                _target_long_headline(snapshot, candidate_id, recent=False)
            )
            confirmation["win_rate"] = _governance_win_rate(snapshot, candidate_id)
            required_confirmation = (
                "primary_horizon", "valid_dates", "coverage_mean",
                "ic_mean", "rank_ic_mean", "ic_std", "rank_ic_std",
                "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t",
                "win_rate", "annualized_return", "max_drawdown", "sharpe",
                "information_ratio",
            )
            missing = [
                name for name in required_confirmation
                if not isinstance(confirmation.get(name), (int, float))
            ]
            if missing:
                raise FactorMinerError(
                    FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                    f"候选 {candidate_id} 缺少正式确认核心指标：{missing}",
                )
            _target_long_metric_row(snapshot, candidate_id)

    def _allocate_candidate_references(
        self,
        connection: Any,
        source_candidate_ids: Iterable[str],
    ) -> dict[str, str]:
        """在数据库事务内顺延分配 huanNNN，重复投影复用原编号。"""

        sources = tuple(sorted(set(str(item) for item in source_candidate_ids)))
        if not sources:
            return {}
        connection.execute(
            "LOCK TABLE factor_miner_internal.factor_id_map "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
        maximum_row = connection.execute(
            """
            SELECT MAX(value) FROM (
                SELECT substring(factor_id FROM 5)::integer AS value
                FROM factor_miner_internal.factor_id_map
                WHERE factor_id ~ '^huan[0-9]{3,}$'
                UNION ALL
                SELECT substring(factor_id FROM 5)::integer AS value
                FROM public.factors
                WHERE factor_id ~ '^huan[0-9]{3,}$'
            ) AS ids
            """
        ).fetchone()
        maximum = int(maximum_row[0] or 0) if maximum_row is not None else 0
        aliases: dict[str, str] = {}
        for source_id in sources:
            existing = connection.execute(
                """
                SELECT factor_id
                FROM factor_miner_internal.factor_id_map
                WHERE market_id = %s
                  AND source_candidate_id = %s
                  AND factor_id ~ '^huan[0-9]{3,}$'
                """,
                (self._market_id, source_id),
            ).fetchone()
            if existing is not None:
                reference = str(existing[0])
                aliases[source_id] = reference
                if reference.startswith("huan") and reference[4:].isdigit():
                    maximum = max(maximum, int(reference[4:]))
                continue
            if source_id.startswith("huan") and source_id[4:].isdigit():
                reference = source_id
                maximum = max(maximum, int(source_id[4:]))
            else:
                maximum += 1
                reference = f"huan{maximum:03d}"
            aliases[source_id] = reference
            connection.execute(
                """
                INSERT INTO factor_miner_internal.factor_id_map
                    (market_id, source_candidate_id, factor_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (market_id, source_candidate_id) DO NOTHING
                """,
                (self._market_id, source_id, reference),
            )
        return aliases

    @staticmethod
    def _project_evolution_memory(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """投影脱敏演化身份和记忆摘要；不写原始结果。"""
        memory = _as_dict(snapshot.get("memory_snapshot"))
        if not memory:
            return
        evolution = _as_dict(snapshot.get("evolution_metadata"))
        if evolution:
            family_id = (
                snapshot.get("campaign_metadata", {}).get("family_id")
                if isinstance(snapshot.get("campaign_metadata"), dict)
                else None
            )
            batch_identity = (
                ("context_sha256", evolution.get("context_sha256")),
                ("discovery_family_id", family_id),
                ("approval_batch_hash", evolution.get("approval_batch_hash")),
                ("coverage_graph_id", evolution.get("coverage_graph_id")),
                ("coverage_graph_manifest_hash", evolution.get("coverage_graph_manifest_hash")),
                ("memory_snapshot_hash", evolution.get("memory_snapshot_hash")),
                ("gap_report_hash", evolution.get("gap_report_hash")),
                ("design_policy_hash", evolution.get("design_policy_hash")),
                ("hypothesis_count", evolution.get("hypothesis_count", 10)),
                ("slot_count", evolution.get("slot_count", 120)),
                ("approval_count", evolution.get("approval_count", 0)),
            )
            existing_context = connection.execute(
                """
                SELECT context_sha256, discovery_family_id, approval_batch_hash,
                       coverage_graph_id, coverage_graph_manifest_hash,
                       memory_snapshot_hash, gap_report_hash, design_policy_hash,
                       hypothesis_count, slot_count, approval_count, source_manifest_sha256
                FROM research_evolution_batches
                WHERE context_id = %s
                """,
                (evolution.get("context_id"),),
            ).fetchone()
            if existing_context is not None:
                # source_manifest_sha256 描述某次发布，不是 evolution context
                # 的内容身份；同一上下文允许由补全指标后的新 run 再次投影。
                existing_identity = tuple(existing_context[:len(batch_identity)])
                expected_identity = tuple(value for _, value in batch_identity)
                differences = ", ".join(
                    name for (name, expected), actual in zip(batch_identity, existing_identity)
                    if actual != expected and not (name == "approval_count" and actual is None)
                )
                if differences:
                    raise FactorMinerError(
                        FailureCode.LEDGER_CORRUPT,
                        f"同一 evolution context 的 PostgreSQL 投影身份冲突：{differences}",
                    )
            connection.execute(
                """
                INSERT INTO research_evolution_batches
                    (context_id, context_sha256, discovery_family_id, approval_batch_hash,
                     coverage_graph_id, coverage_graph_manifest_hash, memory_snapshot_hash,
                     gap_report_hash, design_policy_hash, hypothesis_count, slot_count,
                     approval_count, created_at, source_manifest_sha256)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::timestamptz,%s)
                ON CONFLICT (context_id) DO UPDATE SET
                    approval_count = COALESCE(research_evolution_batches.approval_count, EXCLUDED.approval_count)
                """,
                (evolution.get("context_id"), evolution.get("context_sha256"),
                 family_id,
                 evolution.get("approval_batch_hash"), evolution.get("coverage_graph_id"),
                 evolution.get("coverage_graph_manifest_hash"), evolution.get("memory_snapshot_hash"),
                 evolution.get("gap_report_hash"), evolution.get("design_policy_hash"),
                 evolution.get("hypothesis_count", 10), evolution.get("slot_count", 120),
                 evolution.get("approval_count", 0),
                 memory.get("published_at"), memory.get("source_manifest_sha256")),
            )
            gap = _as_dict(evolution.get("gap_summary"))
            category_counts = _as_dict(gap.get("category_counts"))
            truncated_count = int(gap.get("truncated_count", 0))
            if evolution.get("gap_report_hash"):
                existing_gap = connection.execute(
                    """
                    SELECT context_id, structural_count, market_regime_count,
                           data_availability_count, sanitized_labels, truncated_count,
                           coverage_graph_manifest_hash, memory_snapshot_hash
                    FROM research_gap_summaries
                    WHERE gap_report_hash = %s
                    """,
                    (evolution.get("gap_report_hash"),),
                ).fetchone()
                gap_labels = {"labels": gap.get("sanitized_labels", [])}
                gap_identity = (
                    evolution.get("context_id"),
                    int(category_counts.get("structural", 0)),
                    int(category_counts.get("market_regime", 0)),
                    int(category_counts.get("data_availability", 0)),
                    gap_labels,
                    truncated_count,
                    evolution.get("coverage_graph_manifest_hash"),
                    evolution.get("memory_snapshot_hash"),
                )
                if existing_gap is not None:
                    differences = tuple(
                        actual != expected and not (index == 5 and actual is None)
                        for index, (actual, expected) in enumerate(zip(existing_gap, gap_identity))
                    )
                    if any(differences):
                        raise FactorMinerError(FailureCode.LEDGER_CORRUPT, "同一 gap report 的 PostgreSQL 投影内容冲突")
                connection.execute(
                    """
                    INSERT INTO research_gap_summaries
                        (gap_report_hash, context_id, structural_count, market_regime_count,
                         data_availability_count, sanitized_labels, truncated_count, coverage_graph_manifest_hash,
                         memory_snapshot_hash, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::timestamptz)
                    ON CONFLICT (gap_report_hash) DO UPDATE SET
                        truncated_count = COALESCE(research_gap_summaries.truncated_count, EXCLUDED.truncated_count)
                    """,
                    (evolution.get("gap_report_hash"), gap_identity[0], gap_identity[1], gap_identity[2],
                     gap_identity[3], json.dumps(gap_labels, ensure_ascii=False, separators=(",", ":")),
                     gap_identity[5], gap_identity[6], gap_identity[7], memory.get("published_at")),
                )
        existing = connection.execute(
            "SELECT snapshot_sha256, terminal_counts FROM research_memory_snapshots WHERE memory_snapshot_id = %s",
            (memory.get("memory_snapshot_id"),),
        ).fetchone()
        terminal_counts = _as_dict(memory.get("terminal_counts"))
        if existing is not None:
            existing_hash, existing_terminal_counts = tuple(existing)
            if existing_hash != memory.get("memory_snapshot_sha256") or (
                existing_terminal_counts is not None and existing_terminal_counts != terminal_counts
            ):
                raise FactorMinerError(FailureCode.LEDGER_CORRUPT, "同一 memory snapshot 的 PostgreSQL 投影内容冲突")
        # migration 006 使用非 deferrable FK；父表必须在任何 entry child 之前存在。
        connection.execute(
            """
            INSERT INTO research_memory_snapshots
                (memory_snapshot_id, snapshot_sha256, run_id, coverage_graph_id,
                 source_family_ids, entry_count, terminal_counts, cutoff_at,
                 source_manifest_sha256, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::timestamptz,%s,%s::timestamptz)
            ON CONFLICT (memory_snapshot_id) DO UPDATE SET
                terminal_counts = COALESCE(research_memory_snapshots.terminal_counts, EXCLUDED.terminal_counts)
            """,
            (memory.get("memory_snapshot_id"), memory.get("memory_snapshot_sha256"), run_id,
             memory.get("coverage_graph_id"), memory.get("source_family_ids", []), memory.get("entry_count"),
             json.dumps(_as_dict(memory.get("terminal_counts")), ensure_ascii=False, separators=(",", ":")),
             memory.get("cutoff_at", memory.get("published_at")), memory.get("source_manifest_sha256"),
             memory.get("published_at")),
        )
        entries = _as_list(snapshot.get("memory_entries"))
        for entry in entries:
            row = _as_dict(entry)
            data_identity = _as_dict(row.get("data_identity_summary"))
            structure_labels = {
                "field_signature": _as_list(row.get("field_signature")),
                "operator_signature": _as_list(row.get("operator_signature")),
                "temporal_signature": _as_list(row.get("temporal_signature")),
                "structure_signature": _as_list(row.get("structure_signature")),
                "gap_labels": _as_list(row.get("gap_labels")),
            }
            evaluation_summary = _as_dict(row.get("evaluation_summary"))
            summary_json = {
                "evaluation_summary": evaluation_summary,
                "data_identity_summary": data_identity,
            }
            source_manifest_sha256 = data_identity.get("manifest_hash") or memory.get("source_manifest_sha256")
            created_at = row.get("created_at") or memory.get("published_at")
            coverage_graph_id = row.get("coverage_graph_id") or memory.get("coverage_graph_id") or "unknown"
            existing_entry = connection.execute(
                "SELECT entry_sha256 FROM research_memory_entries WHERE memory_entry_id = %s",
                (row.get("memory_entry_id"),),
            ).fetchone()
            if existing_entry is not None and existing_entry[0] != row.get("entry_sha256"):
                raise FactorMinerError(FailureCode.LEDGER_CORRUPT, "同一 memory entry 的 PostgreSQL 投影 hash 冲突")
            connection.execute(
                """
                INSERT INTO research_memory_entries
                    (memory_entry_id, discovery_family_id, generation_seal_id, run_id,
                     hypothesis_slot_id, candidate_slot_id, candidate_spec_hash, ast_hash,
                     coverage_graph_id, memory_snapshot_sha256, memory_snapshot_id,
                     terminal_state, failure_reason, duplicate_of, structure_labels, summary_json,
                     entry_sha256, source_manifest_sha256, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::timestamptz)
                ON CONFLICT (memory_entry_id) DO NOTHING
                """,
                (row.get("memory_entry_id"), row.get("discovery_family_id") or "unknown", row.get("generation_seal_id") or "unknown",
                 row.get("run_id"), row.get("hypothesis_slot_id"), row.get("candidate_slot_id"),
                 row.get("candidate_spec_hash") or "unknown", row.get("ast_hash") or "unknown", coverage_graph_id,
                 memory.get("memory_snapshot_sha256"), memory.get("memory_snapshot_id"),
                 row.get("terminal_state") or "unknown", row.get("failure_reason"), row.get("duplicate_of"),
                 json.dumps(structure_labels, ensure_ascii=False, separators=(",", ":")),
                 json.dumps(summary_json, ensure_ascii=False, separators=(",", ":")),
                 row.get("entry_sha256") or "unknown", source_manifest_sha256 or "unknown", created_at),
            )

    @staticmethod
    def _project_artifacts(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """投影产物清单。"""

        for artifact in snapshot.get("artifact_refs", ()):
            if not isinstance(artifact, dict):
                raise FactorMinerError(FailureCode.LEDGER_CORRUPT, "Dashboard artifact_refs 结构无效")
            connection.execute(
                """
                INSERT INTO artifact_refs (run_id, relative_path, sha256, size_bytes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, relative_path) DO NOTHING
                """,
                (run_id, str(artifact["relative_path"]), str(artifact["sha256"]), int(artifact["size_bytes"])),
            )

    def _project_candidates(
        self,
        connection: Any,
        run_id: str,
        snapshot: dict[str, object],
    ) -> None:
        """投影因子目录；描述性文字统一转换为中文。"""

        for candidate_id in _candidate_ids(snapshot):
            reference_id = _reference_candidate_id(str(candidate_id), snapshot)
            definition = _candidate_definition(snapshot, candidate_id)
            summary = _candidate_summary(snapshot, str(candidate_id))
            discovered = _direction_summary(snapshot, str(candidate_id))
            formula = str(definition.get("formula_text") or "未提供公式")
            hypothesis_direction = {
                "positive": "正向",
                "negative": "负向",
                "neutral": "中性",
                "正向": "正向",
                "负向": "负向",
                "中性": "中性",
            }.get(str(definition.get("expected_sign")), "中性")
            category = _chinese_or({
                "momentum": "动量",
                "reversal": "反转",
                "liquidity": "流动性",
                "volatility": "波动率",
                "quality": "质量",
                "value": "价值",
            }.get(str(definition.get("factor_category")), definition.get("factor_category")), "未分类")
            timing = _chinese_or({
                "next_open": "下一交易日开盘",
                "next_close": "下一交易日收盘",
            }.get(str(definition.get("availability")), definition.get("availability")), "下一交易日开盘")
            confirmation_passed = summary.get("confirmation_passed")
            status = (
                "确认通过"
                if confirmation_passed is True
                else "确认未通过" if confirmation_passed is False else "待评价"
            )
            connection.execute(
                """
                INSERT INTO factors (
                    market_id, factor_id, hypothesis, mechanism, discovered_direction,
                    direction_relation, formula, calculation,
                    hypothesis_direction, category, trading_timing, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (factor_id) DO UPDATE SET
                    hypothesis = EXCLUDED.hypothesis,
                    mechanism = EXCLUDED.mechanism,
                    discovered_direction = EXCLUDED.discovered_direction,
                    direction_relation = EXCLUDED.direction_relation,
                    formula = EXCLUDED.formula,
                    calculation = EXCLUDED.calculation,
                    hypothesis_direction = EXCLUDED.hypothesis_direction,
                    category = EXCLUDED.category,
                    trading_timing = EXCLUDED.trading_timing,
                    status = EXCLUDED.status,
                    updated_at = NOW()
                """,
                (
                    self._market_id,
                    reference_id,
                    str(definition.get("hypothesis_claim") or "尚未提供中文假设"),
                    str(definition.get("hypothesis_mechanism") or "机制尚未独立验证"),
                    discovered["discovered_direction"],
                    discovered["direction_relation"],
                    formula,
                    str(definition.get("calculation_method") or f"按公式 {formula} 计算"),
                    hypothesis_direction,
                    category,
                    timing,
                    status,
                ),
            )

    @staticmethod
    def _project_ic_metrics(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """每个因子仅投影 2025-01-01 至 2026-06-30 的完整核心指标。"""

        diagnostics = snapshot.get("recent_ic_diagnostics")
        if not isinstance(diagnostics, dict):
            return
        for candidate_id in _candidate_payload(diagnostics):
            reference_id = _reference_candidate_id(str(candidate_id), snapshot)
            row = _target_long_metric_row(snapshot, str(candidate_id))
            connection.execute(
                """
                INSERT INTO factor_metrics (
                    factor_id, horizon_days, valid_dates, coverage_mean,
                    ic_mean, rank_ic_mean, ic_std, rank_ic_std,
                    ic_ir, rank_ic_ir, ic_hac_t, rank_ic_hac_t,
                    annualized_return, max_drawdown, sharpe, information_ratio
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (factor_id) DO UPDATE SET
                    horizon_days = EXCLUDED.horizon_days,
                    valid_dates = EXCLUDED.valid_dates,
                    coverage_mean = EXCLUDED.coverage_mean,
                    ic_mean = EXCLUDED.ic_mean,
                    rank_ic_mean = EXCLUDED.rank_ic_mean,
                    ic_std = EXCLUDED.ic_std,
                    rank_ic_std = EXCLUDED.rank_ic_std,
                    ic_ir = EXCLUDED.ic_ir,
                    rank_ic_ir = EXCLUDED.rank_ic_ir,
                    ic_hac_t = EXCLUDED.ic_hac_t,
                    rank_ic_hac_t = EXCLUDED.rank_ic_hac_t,
                    annualized_return = EXCLUDED.annualized_return,
                    max_drawdown = EXCLUDED.max_drawdown,
                    sharpe = EXCLUDED.sharpe,
                    information_ratio = EXCLUDED.information_ratio,
                    evaluated_at = NOW()
                """,
                (
                    reference_id,
                    *(row[field] for field in (
                        "horizon_days", "valid_dates", "coverage_mean",
                        "ic_mean", "rank_ic_mean", "ic_std", "rank_ic_std",
                        "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t",
                        "annualized_return", "max_drawdown", "sharpe",
                        "information_ratio",
                    )),
                ),
            )

    @staticmethod
    def _project_ic_horizons(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """投影多期限 IC；每个候选、每个标签期限一行。"""

        diagnostics = snapshot.get("ic_diagnostics")
        if not isinstance(diagnostics, dict):
            return
        for candidate_id, value in _candidate_payload(diagnostics).items():
            reference_id = _reference_candidate_id(str(candidate_id), snapshot)
            values = _as_dict(value)
            for decay in _as_list(values.get("decay")):
                if not isinstance(decay, dict):
                    continue
                horizon_days = _integer(decay.get("horizon"))
                if horizon_days is None:
                    continue
                horizon_row = {
                    "run_id": run_id,
                    "candidate_id": reference_id,
                    "horizon_days": horizon_days,
                    "valid_dates": _integer(decay.get("valid_dates")),
                    "ic_mean": _number(decay.get("ic_mean")),
                    "rank_ic_mean": _number(decay.get("rank_ic_mean")),
                }
                horizon_row["metrics_sha256"] = sha256_json(horizon_row)
                connection.execute(
                    """
                    INSERT INTO candidate_ic_horizons
                        (run_id, candidate_id, horizon_days, valid_dates,
                         ic_mean, rank_ic_mean, metrics_sha256)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, candidate_id, horizon_days) DO UPDATE SET
                        valid_dates = EXCLUDED.valid_dates,
                        ic_mean = EXCLUDED.ic_mean,
                        rank_ic_mean = EXCLUDED.rank_ic_mean,
                        metrics_sha256 = EXCLUDED.metrics_sha256
                    """,
                    (
                        run_id,
                        reference_id,
                        horizon_days,
                        horizon_row["valid_dates"],
                        horizon_row["ic_mean"],
                        horizon_row["rank_ic_mean"],
                        horizon_row["metrics_sha256"],
                    ),
                )

    @staticmethod
    def _project_portfolio_metrics(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """投影组合指标；每个候选、每个组合序列一行，所有指标为独立列。"""

        portfolio = snapshot.get("portfolio_metrics")
        if not isinstance(portfolio, dict):
            return
        for candidate_id, value in _candidate_payload(portfolio).items():
            reference_id = _reference_candidate_id(str(candidate_id), snapshot)
            payload = _as_dict(value)
            series = payload.get("series")
            if not isinstance(series, dict):
                continue
            for series_name, metrics in series.items():
                values = _as_dict(metrics)
                connection.execute(
                    """
                    INSERT INTO portfolio_metrics (
                        run_id, candidate_id, series_name, benchmark, observations,
                        period_return, annualized_return, excess_return,
                        excess_annualized_return, annualized_volatility,
                        excess_annualized_volatility, sharpe, information_ratio,
                        max_drawdown, excess_max_drawdown, annualization_factor,
                        return_basis, metrics_sha256
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, candidate_id, series_name) DO UPDATE SET
                        benchmark = EXCLUDED.benchmark,
                        observations = EXCLUDED.observations,
                        period_return = EXCLUDED.period_return,
                        annualized_return = EXCLUDED.annualized_return,
                        excess_return = EXCLUDED.excess_return,
                        excess_annualized_return = EXCLUDED.excess_annualized_return,
                        annualized_volatility = EXCLUDED.annualized_volatility,
                        excess_annualized_volatility = EXCLUDED.excess_annualized_volatility,
                        sharpe = EXCLUDED.sharpe,
                        information_ratio = EXCLUDED.information_ratio,
                        max_drawdown = EXCLUDED.max_drawdown,
                        excess_max_drawdown = EXCLUDED.excess_max_drawdown,
                        annualization_factor = EXCLUDED.annualization_factor,
                        return_basis = EXCLUDED.return_basis,
                        metrics_sha256 = EXCLUDED.metrics_sha256
                    """,
                    (
                        run_id,
                        reference_id,
                        str(series_name),
                        str(payload.get("benchmark", "CSI300")),
                        _integer(values.get("observations")),
                        _number(values.get("period_return")),
                        _number(values.get("annualized_return")),
                        _number(values.get("excess_return")),
                        _number(values.get("excess_annualized_return")),
                        _number(values.get("annualized_volatility")),
                        _number(values.get("excess_annualized_volatility")),
                        _number(values.get("sharpe")),
                        _number(values.get("information_ratio")),
                        _number(values.get("max_drawdown")),
                        _number(values.get("excess_max_drawdown")),
                        _number(values.get("annualization_factor")),
                        values.get("return_basis"),
                        str(values.get("metrics_sha256", sha256_json(values))),
                    ),
                )

    @staticmethod
    def _project_portfolio_daily(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """投影逐期组合收益长表。"""

        daily = snapshot.get("portfolio_daily")
        if not isinstance(daily, dict):
            return
        for candidate_id in _candidate_payload(daily):
            reference_id = _reference_candidate_id(str(candidate_id), snapshot)
            for row in _portfolio_daily_rows(snapshot, str(candidate_id)):
                if not isinstance(row, dict) or "exit_date" not in row:
                    continue
                for series_name, value in row.items():
                    if not series_name.endswith("_return") or not isinstance(value, (int, float)):
                        continue
                    connection.execute(
                        """
                        INSERT INTO portfolio_daily
                            (run_id, candidate_id, exit_date, series_name, return_value)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (run_id, candidate_id, exit_date, series_name) DO NOTHING
                        """,
                        (run_id, reference_id, row["exit_date"], series_name, value),
                    )

    @staticmethod
    def _project_barra(connection: Any, run_id: str, snapshot: dict[str, object]) -> None:
        """投影可选 Barra 结果；Barra 原始明细暂保持 JSON 审计格式。"""

        barra = snapshot.get("barra_attribution")
        if not isinstance(barra, dict):
            return
        for candidate_id, result in _candidate_payload(barra).items():
            reference_id = _reference_candidate_id(str(candidate_id), snapshot)
            if not isinstance(result, dict):
                continue
            for exposure in _as_list(result.get("exposure_summary")):
                if not isinstance(exposure, dict):
                    continue
                connection.execute(
                    """
                    INSERT INTO barra_exposure_summary
                        (run_id, candidate_id, signal_date, entry_date, portfolio, exposure_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        run_id,
                        reference_id,
                        exposure["signal_date"],
                        exposure["entry_date"],
                        exposure["portfolio"],
                        json.dumps(exposure, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            for attribution in _as_list(result.get("attribution")):
                if not isinstance(attribution, dict):
                    continue
                connection.execute(
                    """
                    INSERT INTO barra_attribution
                        (run_id, candidate_id, signal_date, entry_date,
                         portfolio, factor, contribution, attribution_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        run_id,
                        reference_id,
                        attribution["signal_date"],
                        attribution["entry_date"],
                        attribution["portfolio"],
                        attribution["factor"],
                        attribution.get("contribution"),
                        json.dumps(attribution, ensure_ascii=False, separators=(",", ":")),
                    ),
                )

    def load_run_snapshot(self, run_id: str) -> dict[str, object] | None:
        """完整快照已迁回正式文件，PostgreSQL 不再提供该接口。"""

        return None

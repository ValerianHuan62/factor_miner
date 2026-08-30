"""从不可变运行产物构建并发布候选筛选报告。"""

from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import date, datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from factor_miner.candidate_screening import (
    CandidateScreeningInput,
    CandidateScreeningPolicy,
    CandidateScreeningReport,
    UnavailableCandidateRecord,
    screen_candidates,
)
from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.ledger import _atomic_write_immutable
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.regime_snapshot import verify_regime_snapshot


class CandidateScreeningPublication(BaseModel):
    """一次不可变筛选报告发布的摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str = Field(pattern=r"^screening_[0-9a-f]{24}$")
    report_root: Path
    candidate_count: int = Field(ge=1)
    selected_count: int = Field(ge=0)
    target_satisfied: bool
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_candidate_map(path: Path) -> dict[str, str]:
    """读取服务器导出的稳定 source candidate 到 huanNNN 映射。"""

    result: dict[str, str] = {}
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["source_candidate_id", "factor_id"]:
            raise ValueError("候选编号映射必须精确包含 source_candidate_id,factor_id")
        for row in reader:
            source = str(row["source_candidate_id"])
            business = str(row["factor_id"])
            if source in result or business in result.values():
                raise ValueError("候选编号映射存在重复")
            if not business.startswith("huan") or not business[4:].isdigit():
                raise ValueError("候选业务编号必须使用 huanNNN")
            result[source] = business
    if not result:
        raise ValueError("候选编号映射不能为空")
    return result


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    """读取 JSON object 并保留明确失败语义。"""

    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 无法解析") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 根节点必须是 object")
    return payload


def _candidate_payload(payload: Mapping[str, Any], candidate_id: str, *, label: str) -> dict[str, Any]:
    """读取版本化产物中的候选对象。"""

    candidates = payload.get("candidates")
    if not isinstance(candidates, dict) or not isinstance(candidates.get(candidate_id), dict):
        raise ValueError(f"{label} 缺少候选 {candidate_id}")
    return dict(candidates[candidate_id])


def _series_headline(payload: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    """读取目标多头净收益摘要。"""

    series = payload.get("series")
    if not isinstance(series, dict):
        raise ValueError(f"{label} 缺少 series")
    headline = series.get("target_long_net_return")
    if not isinstance(headline, dict):
        raise ValueError(f"{label} 缺少 target_long_net_return")
    return dict(headline)


def _number(payload: Mapping[str, Any], field: str, *, label: str) -> float:
    """读取有限数值字段。"""

    value = payload.get(field)
    if not isinstance(value, int | float) or not float("-inf") < float(value) < float("inf"):
        raise ValueError(f"{label}.{field} 必须是有限数值")
    return float(value)


def _load_regime_calendar(snapshot_root: Path | None) -> tuple[list[date], list[str], str | None]:
    """核验并读取最早可用日状态日历。"""

    if snapshot_root is None:
        return [], [], None
    verified = verify_regime_snapshot(snapshot_root)
    frame = (
        pl.read_parquet(verified.snapshot_root / "filtered_regimes.parquet")
        .filter(pl.col("status") == "available")
        .select(["earliest_use_date", "canonical_state_id"])
        .drop_nulls()
        .sort("earliest_use_date")
    )
    dates = frame["earliest_use_date"].to_list()
    states = [f"状态{int(item)}" for item in frame["canonical_state_id"].to_list()]
    return dates, states, sha256_json(verified.manifest.model_dump(mode="json"))


def _regime_summary(
    daily_rows: list[dict[str, Any]],
    regime_dates: list[date],
    regime_states: list[str],
) -> tuple[float | None, dict[str, float]]:
    """按当日最早可用状态汇总确认期 RankIC；覆盖不足时仍只作描述。"""

    if not regime_dates:
        return None, {}
    grouped: dict[str, list[float]] = {}
    matched = 0
    for row in daily_rows:
        observed = date.fromisoformat(str(row["date"]))
        position = bisect_right(regime_dates, observed) - 1
        if position < 0:
            continue
        state = regime_states[position]
        grouped.setdefault(state, []).append(float(row["rank_ic"]))
        matched += 1
    coverage = matched / len(daily_rows) if daily_rows else 0.0
    return coverage, {state: mean(values) for state, values in sorted(grouped.items())}


def _barra_summary(payload: dict[str, Any]) -> dict[str, float | str | None]:
    """压缩 Barra 暴露、贡献和风险，避免把归因误当 Alpha。"""

    status = str(payload.get("status") or "not_available")
    result: dict[str, float | str | None] = {
        "status": status,
        "common_contribution_share": None,
        "factor_risk_share": None,
        "max_abs_style_exposure": None,
        "max_abs_industry_exposure": None,
    }
    attribution = payload.get("attribution")
    if status != "available" or not isinstance(attribution, dict):
        return result
    exposures = attribution.get("exposure_summary")
    rows = exposures if isinstance(exposures, list) else []
    style_values: list[float] = []
    industry_values: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field, value in row.items():
            if not isinstance(value, int | float):
                continue
            if field == "Size" or field.startswith("Style_"):
                style_values.append(abs(float(value)))
            elif field.startswith("industry_"):
                industry_values.append(abs(float(value)))
    result["max_abs_style_exposure"] = max(style_values) if style_values else None
    result["max_abs_industry_exposure"] = max(industry_values) if industry_values else None
    contribution_rows = attribution.get("attribution")
    common_abs = 0.0
    residual_abs = 0.0
    for row in contribution_rows if isinstance(contribution_rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("contribution"), int | float):
            continue
        value = abs(float(row["contribution"]))
        if row.get("factor") == "specific_residual":
            residual_abs += value
        else:
            common_abs += value
    denominator = common_abs + residual_abs
    result["common_contribution_share"] = common_abs / denominator if denominator else None
    risk_rows = attribution.get("risk_decomposition")
    risk_shares = []
    for row in risk_rows if isinstance(risk_rows, list) else []:
        if not isinstance(row, dict):
            continue
        factor_variance = row.get("factor_variance")
        total_variance = row.get("total_variance")
        if isinstance(factor_variance, int | float) and isinstance(total_variance, int | float) and total_variance > 0:
            risk_shares.append(float(factor_variance) / float(total_variance))
    result["factor_risk_share"] = mean(risk_shares) if risk_shares else None
    return result


def _find_spec(run_root: Path, candidate_id: str, manifest: Any) -> dict[str, Any]:
    """只从已核验清单引用中读取候选 Spec。"""

    relative = f"candidates/{candidate_id}/spec.json"
    if relative not in {item.relative_path for item in manifest.artifacts}:
        raise ValueError(f"运行 {run_root.name} 缺少候选 {candidate_id} 的 Spec 引用")
    return _read_json(run_root / relative, label="候选 Spec")


def _build_input(
    *,
    candidate_id: str,
    business_id: str,
    run_root: Path,
    manifest: Any,
    payloads: Mapping[str, dict[str, Any]],
    regime_dates: list[date],
    regime_states: list[str],
) -> CandidateScreeningInput:
    """从一组同运行候选产物构造筛选输入。"""

    summary = _candidate_payload(payloads["metrics"], candidate_id, label="运行指标")
    diagnostics = _candidate_payload(payloads["ic"], candidate_id, label="确认期 IC")
    recent = _candidate_payload(payloads["recent_ic"], candidate_id, label="最近期 IC")
    decision_wrapper = _candidate_payload(payloads["directions"], candidate_id, label="方向决定")
    decision = decision_wrapper.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("方向决定缺少 decision")
    portfolio = _candidate_payload(payloads["portfolio"], candidate_id, label="确认期组合")
    recent_portfolio = _candidate_payload(payloads["recent_portfolio"], candidate_id, label="最近期组合")
    daily_wrapper = _candidate_payload(payloads["daily"], candidate_id, label="逐期组合")
    daily = daily_wrapper.get("daily")
    if not isinstance(daily, list) or len(daily) < 2 or any(not isinstance(row, dict) for row in daily):
        raise ValueError("逐期组合 daily 无效")
    governance = _candidate_payload(payloads["governance"], candidate_id, label="组合治理")
    spread_daily = governance.get("extreme_spread_daily")
    if not isinstance(spread_daily, list) or any(not isinstance(row, dict) for row in spread_daily):
        raise ValueError("组合治理缺少极端组差逐期序列")
    spread_returns = {
        str(row["exit_date"]): _number(row, "extreme_spread_net_return", label="极端组差")
        for row in spread_daily
        if isinstance(row.get("exit_date"), str)
    }
    if len(spread_returns) != len(spread_daily):
        raise ValueError("极端组差退出日期缺失或重复")
    barra_payload = _candidate_payload(payloads["barra"], candidate_id, label="Barra 归因")
    spec = _find_spec(run_root, candidate_id, manifest)
    hypothesis = spec.get("hypothesis")
    if not isinstance(hypothesis, dict) or not isinstance(hypothesis.get("claim"), str):
        raise ValueError("候选 Spec 缺少中文假设")
    discovery = decision.get("discovery_summary")
    if not isinstance(discovery, dict):
        raise ValueError("方向决定缺少发现期摘要")
    selected_direction = str(decision.get("selected_direction"))
    relation = str(decision.get("hypothesis_relation"))
    if selected_direction not in {"positive", "negative"} or relation not in {"supported", "reversed"}:
        raise ValueError("方向决定字段无效")
    headline = _series_headline(portfolio, label="确认期组合")
    recent_headline = _series_headline(recent_portfolio, label="最近期组合")
    annual = diagnostics.get("annual_summary")
    decay = diagnostics.get("decay")
    if not isinstance(annual, dict) or not isinstance(decay, list):
        raise ValueError("确认期 IC 缺少年度或期限摘要")
    annual_rank_ic = {
        str(year): _number(value, "rank_ic_mean", label=f"年度 {year}")
        for year, value in annual.items()
        if isinstance(value, dict)
    }
    horizon_rank_ic = {
        int(row["horizon"]): _number(row, "rank_ic_mean", label="期限摘要")
        for row in decay
        if isinstance(row, dict) and isinstance(row.get("horizon"), int)
    }
    annualization_factor = _number(headline, "annualization_factor", label="确认期组合")
    mean_turnover = mean(
        float(row["target_long_turnover"])
        for row in daily
        if isinstance(row.get("target_long_turnover"), int | float)
    )
    regime_coverage, regime_rank_ic = _regime_summary(
        [row for row in diagnostics.get("daily", []) if isinstance(row, dict)],
        regime_dates,
        regime_states,
    )
    barra = _barra_summary(barra_payload)
    return CandidateScreeningInput(
        candidate_id=candidate_id,
        business_id=business_id,
        source_run_id=manifest.run_id,
        source_manifest_sha256=manifest.manifest_sha256,
        spec_sha256=str(summary.get("spec_sha256")),
        formula_sha256=sha256_json(spec.get("expression")),
        hypothesis=str(hypothesis["claim"]),
        selected_direction=selected_direction,
        hypothesis_relation=relation,
        confirmation_passed=bool(summary.get("confirmation_passed")),
        confirmation_rank_ic_mean=_number(diagnostics, "rank_ic_mean", label="确认期 IC"),
        confirmation_rank_ic_hac_t=_number(diagnostics, "rank_ic_hac_t", label="确认期 IC"),
        confirmation_ic_mean=_number(diagnostics, "ic_mean", label="确认期 IC"),
        confirmation_ic_hac_t=_number(diagnostics, "ic_hac_t", label="确认期 IC"),
        confirmation_information_ratio=_number(headline, "information_ratio", label="确认期组合"),
        confirmation_sharpe=_number(headline, "sharpe", label="确认期组合"),
        confirmation_max_drawdown=_number(headline, "max_drawdown", label="确认期组合"),
        confirmation_annualized_return=_number(headline, "annualized_return", label="确认期组合"),
        annual_rank_ic=annual_rank_ic,
        horizon_rank_ic=horizon_rank_ic,
        recent_rank_ic_mean=_number(recent, "rank_ic_mean", label="最近期 IC"),
        recent_information_ratio=_number(recent_headline, "information_ratio", label="最近期组合"),
        recent_sharpe=_number(recent_headline, "sharpe", label="最近期组合"),
        mean_turnover=mean_turnover,
        annualization_factor=annualization_factor,
        portfolio_rows=tuple(daily),
        governance_spread_returns=spread_returns,
        barra_status=str(barra["status"]),
        barra_common_contribution_share=barra["common_contribution_share"],
        barra_factor_risk_share=barra["factor_risk_share"],
        barra_max_abs_style_exposure=barra["max_abs_style_exposure"],
        barra_max_abs_industry_exposure=barra["max_abs_industry_exposure"],
        regime_coverage=regime_coverage,
        regime_oriented_rank_ic=regime_rank_ic,
    )


def load_screening_inputs(
    *,
    artifact_root: Path,
    candidate_map: Mapping[str, str],
    policy: CandidateScreeningPolicy,
    regime_snapshot_root: Path | None = None,
) -> tuple[tuple[CandidateScreeningInput, ...], tuple[UnavailableCandidateRecord, ...]]:
    """在冻结截止时点前汇总每个 source candidate 的最新完整发布运行。"""

    root = Path(artifact_root).expanduser().resolve(strict=False)
    runs_root = root / "artifacts" / "runs"
    if not runs_root.is_dir():
        raise ValueError("正式运行产物目录不存在")
    regime_dates, regime_states, _ = _load_regime_calendar(regime_snapshot_root)
    selected: dict[str, tuple[int, CandidateScreeningInput]] = {}
    required = {
        "metrics": "run/metrics.json",
        "ic": "ic/diagnostics.json",
        "recent_ic": "ic/recent.json",
        "directions": "direction/decisions.json",
        "portfolio": "portfolio/metrics.json",
        "recent_portfolio": "portfolio/recent_metrics.json",
        "daily": "portfolio/daily.json",
        "governance": "portfolio/governance.json",
        "barra": "barra/attribution.json",
    }
    cutoff_ns = int(policy.cutoff_at.timestamp() * 1_000_000_000)
    for run_root in sorted(runs_root.glob("run_*")):
        manifest_path = run_root / "run_manifest.json"
        if not manifest_path.is_file() or manifest_path.stat().st_mtime_ns > cutoff_ns:
            continue
        manifest = verify_published_run(root, run_root.name)
        available = {item.relative_path for item in manifest.artifacts}
        if not set(required.values()).issubset(available):
            continue
        payloads = {
            key: _read_json(run_root / relative, label=relative)
            for key, relative in required.items()
        }
        metrics_candidates = payloads["metrics"].get("candidates")
        if not isinstance(metrics_candidates, dict):
            continue
        for candidate_id in sorted(set(metrics_candidates).intersection(candidate_map)):
            item = _build_input(
                candidate_id=candidate_id,
                business_id=candidate_map[candidate_id],
                run_root=run_root,
                manifest=manifest,
                payloads=payloads,
                regime_dates=regime_dates,
                regime_states=regime_states,
            )
            previous = selected.get(candidate_id)
            identity = (manifest_path.stat().st_mtime_ns, manifest.run_id)
            if previous is None or identity > (previous[0], previous[1].source_run_id):
                selected[candidate_id] = (identity[0], item)
    missing = sorted(set(candidate_map).difference(selected), key=lambda item: candidate_map[item])
    unavailable = tuple(
        UnavailableCandidateRecord(
            candidate_id=candidate_id,
            business_id=candidate_map[candidate_id],
            failure_reason="稳定编号存在，但截止时点前找不到完整不可变 long-only 发布产物",
        )
        for candidate_id in missing
    )
    inputs = tuple(sorted((item for _, item in selected.values()), key=lambda item: item.business_id))
    return inputs, unavailable


def _format_number(value: float | None, *, percent: bool = False) -> str:
    """格式化报告数字。"""

    if value is None:
        return "暂无可靠数据"
    return f"{value:.2%}" if percent else f"{value:.4f}"


def _markdown(report: CandidateScreeningReport) -> str:
    """生成中文筛选与稳健性报告。"""

    lines = [
        "# 候选筛选与稳健性报告",
        "",
        "## 研究边界",
        "",
        "本报告在冻结候选全集上进行方向感知筛选、成本压力和主动收益相关簇去重。结果只能称为通过当前协议筛选的候选因子，不是认证 Alpha、生产结论或实盘建议。Barra 与市场状态只用于解释风险和稳定性，不证明经济机制。",
        "",
        "## 冻结政策",
        "",
        f"- 截止时点：`{report.policy.cutoff_at.isoformat()}`",
        f"- 政策哈希：`{report.policy_sha256}`",
        f"- 目标数量：{report.policy.target_min}–{report.policy.target_max}",
        f"- 确认期方向校正 RankIC 下限：{report.policy.minimum_oriented_rank_ic:.4f}",
        f"- 年度为正占比下限：{report.policy.minimum_positive_year_fraction:.2%}",
        f"- 多期限为正占比下限：{report.policy.minimum_positive_horizon_fraction:.2%}",
        f"- 主动收益相关簇阈值：{report.policy.cluster_abs_active_return_correlation:.2f}",
        f"- 相关去重序列：`{report.policy.correlation_series}`",
        "- 成本压力：28bp 信息比率为正，42bp 净 Sharpe 为正",
        "- 市场状态：仅作描述性诊断，不改变主筛选状态",
        "",
        "## 筛选结果",
        "",
        f"- 候选全集：{report.candidate_count}",
        f"- 可从不可变产物完整核验：{report.auditable_candidate_count}",
        f"- 输入不可审计：{report.unavailable_candidate_count}",
        f"- 通过全部硬门槛：{report.statistical_eligible_count}",
        f"- 最终保留：{report.selected_count}",
        f"- 是否落入目标区间：{'是' if report.target_satisfied else '否'}",
        "",
        "| 排名 | 因子 | 状态 | 方向校正 RankIC | HAC t | 信息比率 | 净 Sharpe | 最大回撤 | 28bp IR | 42bp Sharpe | 相关簇 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.selected:
        lines.append(
            "| {rank} | {factor} | {status} | {rank_ic} | {hac} | {ir} | {sharpe} | {drawdown} | {ir28} | {sharpe42} | {cluster} |".format(
                rank=item.selection_rank,
                factor=item.business_id,
                status=item.status,
                rank_ic=_format_number(item.oriented_rank_ic_mean),
                hac=_format_number(item.oriented_rank_ic_hac_t),
                ir=_format_number(item.confirmation_information_ratio),
                sharpe=_format_number(item.confirmation_sharpe),
                drawdown=_format_number(item.confirmation_max_drawdown, percent=True),
                ir28=_format_number(item.stress_metrics["28"].information_ratio),
                sharpe42=_format_number(item.stress_metrics["42"].sharpe),
                cluster=item.correlation_cluster,
            )
        )
    reason_counts: dict[str, int] = {}
    for item in report.candidates:
        for reason in item.failure_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    lines.extend(["", "## 主要淘汰原因", ""])
    for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {reason}：{count}")
    if report.unavailable_candidates:
        lines.append(f"- 稳定编号存在但缺少完整不可变发布产物：{len(report.unavailable_candidates)}")
    lines.extend(
        [
            "",
            "## Barra 与市场状态解释",
            "",
            "Barra 主动暴露、共同因子贡献占比和风险占比只作为并列诊断；高暴露不自动淘汰，但需要在后续组合构建中比较原组合与行业/风格中性组合。市场状态快照若覆盖不足八成，报告只显示局部结果，不得宣称跨状态稳健。",
            "",
            "## 下一阶段",
            "",
            "对保留候选先构建透明的等权标准化组合，再比较固定权重与走步式权重；组合收益形成后运行 CPCV 和 Deflated Sharpe Ratio，并从报告截止时点之后开始纸面样本外跟踪。",
            "",
        ]
    )
    return "\n".join(lines)


def _csv_bytes(report: CandidateScreeningReport) -> bytes:
    """生成完整候选明细 CSV。"""

    output = io.StringIO(newline="")
    fields = [
        "business_id", "status", "selection_rank", "correlation_cluster",
        "selected_direction", "hypothesis_relation", "oriented_rank_ic_mean",
        "oriented_rank_ic_hac_t", "confirmation_information_ratio",
        "confirmation_sharpe", "confirmation_max_drawdown", "positive_year_fraction",
        "positive_horizon_fraction", "recent_oriented_rank_ic_mean",
        "recent_information_ratio", "mean_turnover", "stress_28_information_ratio",
        "stress_42_sharpe", "barra_status", "barra_common_contribution_share",
        "barra_factor_risk_share", "failure_reasons", "warnings", "source_run_id",
        "source_manifest_sha256", "spec_sha256", "formula_sha256", "hypothesis",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in report.candidates:
        writer.writerow(
            {
                **{field: getattr(item, field) for field in fields if hasattr(item, field)},
                "stress_28_information_ratio": item.stress_metrics["28"].information_ratio,
                "stress_42_sharpe": item.stress_metrics["42"].sharpe,
                "failure_reasons": "；".join(item.failure_reasons),
                "warnings": "；".join(item.warnings),
            }
        )
    return output.getvalue().encode("utf-8-sig")


def _unavailable_csv_bytes(report: CandidateScreeningReport) -> bytes:
    """生成不能进入排名的输入不可审计候选清单。"""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=["business_id", "candidate_id", "failure_reason"],
    )
    writer.writeheader()
    for item in report.unavailable_candidates:
        writer.writerow(item.model_dump(mode="python"))
    return output.getvalue().encode("utf-8-sig")


def publish_screening_report(
    report: CandidateScreeningReport,
    *,
    output_root: Path,
) -> CandidateScreeningPublication:
    """以内容寻址目录原子写入 JSON、中文 Markdown、CSV 和清单。"""

    report_id = f"screening_{report.report_sha256[:24]}"
    root = Path(output_root).expanduser().resolve(strict=False) / report_id
    files = {
        "policy.json": canonical_json_bytes(report.policy.model_dump(mode="json")),
        "report.json": canonical_json_bytes(report.model_dump(mode="json")),
        "候选筛选与稳健性报告.md": _markdown(report).encode("utf-8"),
        "候选明细.csv": _csv_bytes(report),
        "输入不可审计候选.csv": _unavailable_csv_bytes(report),
    }
    refs = {
        name: {"sha256": sha256(content).hexdigest(), "size_bytes": len(content)}
        for name, content in sorted(files.items())
    }
    manifest_payload = {
        "version": "candidate-screening-publication-v1",
        "report_id": report_id,
        "status": "published",
        "policy_sha256": report.policy_sha256,
        "report_sha256": report.report_sha256,
        "files": refs,
    }
    manifest_payload["manifest_sha256"] = sha256_json(manifest_payload)
    files["manifest.json"] = canonical_json_bytes(manifest_payload)
    for name, content in files.items():
        _atomic_write_immutable(root / name, content)
    return CandidateScreeningPublication(
        report_id=report_id,
        report_root=root,
        candidate_count=report.candidate_count,
        selected_count=report.selected_count,
        target_satisfied=report.target_satisfied,
        policy_sha256=report.policy_sha256,
        report_sha256=report.report_sha256,
        manifest_sha256=str(manifest_payload["manifest_sha256"]),
    )


def build_and_publish_screening_report(
    *,
    artifact_root: Path,
    candidate_map_path: Path,
    policy_path: Path,
    output_root: Path,
    regime_snapshot_root: Path | None = None,
) -> CandidateScreeningPublication:
    """核验输入、执行筛选并发布不可变报告。"""

    policy = CandidateScreeningPolicy.model_validate_json(Path(policy_path).read_bytes())
    candidate_map = load_candidate_map(candidate_map_path)
    inputs, unavailable = load_screening_inputs(
        artifact_root=artifact_root,
        candidate_map=candidate_map,
        policy=policy,
        regime_snapshot_root=regime_snapshot_root,
    )
    report = screen_candidates(
        inputs,
        policy,
        unavailable_candidates=unavailable,
    )
    return publish_screening_report(report, output_root=output_root)

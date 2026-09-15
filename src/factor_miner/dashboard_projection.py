"""从已发布不可变产物构建 Dashboard 只读投影。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from factor_miner.canonical import sha256_json
from factor_miner.dashboard_store import DashboardStore
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.portfolio_artifacts import verify_published_run
from factor_miner.research_memory import ResearchMemoryStore
from factor_miner.schema import RegisteredCandidate, RegisteredTrustedCandidate


class DashboardSnapshot(BaseModel):
    """Dashboard 页面可读取的非写入快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    published: bool = True
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_refs: tuple[dict[str, object], ...] = Field(min_length=1)
    candidate_metrics: dict[str, object] = Field(default_factory=dict)
    candidate_definitions: dict[str, object] = Field(default_factory=dict)
    candidate_aliases: dict[str, str] = Field(default_factory=dict)
    portfolio_metrics: dict[str, object] | None = None
    portfolio_governance: dict[str, object] | None = None
    recent_portfolio_metrics: dict[str, object] | None = None
    portfolio_daily: dict[str, object] | None = None
    ic_diagnostics: dict[str, object] | None = None
    recent_ic_diagnostics: dict[str, object] | None = None
    direction_decisions: dict[str, object] | None = None
    barra_attribution: dict[str, object] | None = None
    campaign_metadata: dict[str, object] | None = None
    evolution_metadata: dict[str, object] | None = None
    memory_snapshot: dict[str, object] | None = None
    memory_entries: tuple[dict[str, object], ...] = ()
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        artifact_manifest_sha256: str,
        artifact_refs: tuple[dict[str, object], ...],
        candidate_metrics: dict[str, object] | None = None,
        candidate_definitions: dict[str, object] | None = None,
        portfolio_metrics: dict[str, object] | None = None,
        portfolio_governance: dict[str, object] | None = None,
        recent_portfolio_metrics: dict[str, object] | None = None,
        portfolio_daily: dict[str, object] | None = None,
        ic_diagnostics: dict[str, object] | None = None,
        recent_ic_diagnostics: dict[str, object] | None = None,
        direction_decisions: dict[str, object] | None = None,
        barra_attribution: dict[str, object] | None = None,
        campaign_metadata: dict[str, object] | None = None,
        evolution_metadata: dict[str, object] | None = None,
        memory_snapshot: dict[str, object] | None = None,
        memory_entries: tuple[dict[str, object], ...] = (),
    ) -> DashboardSnapshot:
        """构造内容寻址 Dashboard 快照。"""

        # candidate_definitions 是从已发布候选 Spec 派生的读模型字段。
        # 不把它加入旧版 snapshot 身份哈希，保证只升级 Dashboard 读模型时
        # 不会伪造一个新的研究运行身份，也不会覆盖不可变研究产物。
        payload = {
            "run_id": run_id,
            "published": True,
            "artifact_manifest_sha256": artifact_manifest_sha256,
            "artifact_refs": artifact_refs,
            "candidate_metrics": candidate_metrics or {},
            "portfolio_metrics": portfolio_metrics,
            "portfolio_daily": portfolio_daily,
            "ic_diagnostics": ic_diagnostics,
            "barra_attribution": barra_attribution,
        }
        # 兼容没有 campaign_metadata 字段的旧版读模型哈希。
        if campaign_metadata is not None:
            payload["campaign_metadata"] = campaign_metadata
        if evolution_metadata is not None:
            payload["evolution_metadata"] = evolution_metadata
        if memory_snapshot is not None:
            payload["memory_snapshot"] = memory_snapshot
        return cls(
            **payload,
            candidate_definitions=candidate_definitions or {},
            portfolio_governance=portfolio_governance,
            recent_portfolio_metrics=recent_portfolio_metrics,
            recent_ic_diagnostics=recent_ic_diagnostics,
            direction_decisions=direction_decisions,
            candidate_aliases={
                str(candidate_id): candidate_reference_id(str(candidate_id))
                for candidate_id in (candidate_definitions or {})
            },
            memory_entries=memory_entries,
            snapshot_sha256=sha256_json(payload),
        )


def candidate_reference_id(candidate_id: str) -> str:
    """将历史 Pilot 槽位转换为稳定的业务引用编号。"""

    prefix = "pilot_fixed_"
    if candidate_id.startswith(prefix):
        suffix = candidate_id[len(prefix):]
        if suffix.isdigit():
            return f"huan{int(suffix):03d}"
    return candidate_id


def _projection_error(message: str) -> FactorMinerError:
    """构造 Dashboard 投影错误。"""

    return FactorMinerError(FailureCode.LEDGER_CORRUPT, message)


def _read_optional_json(root: Path, relative_path: str) -> dict[str, object] | None:
    """读取已发布的可选 JSON 摘要，拒绝非法根类型。"""

    path = root / relative_path
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _projection_error("Dashboard 引用的 JSON 产物无法解析") from None
    if not isinstance(value, dict):
        raise _projection_error("Dashboard JSON 产物根节点必须是 object")
    return value


def _candidate_metrics(run_metrics: dict[str, Any]) -> dict[str, object]:
    """把运行级候选摘要规范化为 candidate_id 到摘要的映射。"""

    summaries = run_metrics.get("candidate_summaries")
    if not isinstance(summaries, list):
        return run_metrics
    result: dict[str, object] = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            raise _projection_error("Pilot candidate_summaries 结构无效")
        key = summary.get("candidate_id") or summary.get("slot_id")
        if not isinstance(key, str):
            raise _projection_error("Pilot candidate_summaries 缺少 candidate_id 或 slot_id")
        result[key] = dict(summary)
    return result


def _first_expression_field(expression: object) -> str | None:
    """提取类型化表达式中的第一个字段名。"""

    if not isinstance(expression, dict):
        return None
    if expression.get("op") == "field" and isinstance(expression.get("field"), str):
        return str(expression["field"])
    for argument in expression.get("args", ()):
        field = _first_expression_field(argument)
        if field is not None:
            return field
    return None


def _expression_details(expression: object) -> dict[str, object]:
    """将白名单 DSL 表达式转换为可读公式和独立列。"""

    if not isinstance(expression, dict):
        return {}
    operator = expression.get("op")
    if not isinstance(operator, str):
        return {}

    def render(node: object) -> str:
        if not isinstance(node, dict):
            return "输入"
        node_operator = node.get("op")
        if node_operator == "field" and isinstance(node.get("field"), str):
            return str(node["field"])
        arguments = [render(argument) for argument in node.get("args", ())]
        period_value = node.get("period")
        window_value = node.get("window")
        period_text = str(period_value) if isinstance(period_value, int) else "1"
        window_text = str(window_value) if isinstance(window_value, int) else "?"
        if node_operator == "delta" and arguments:
            return f"Δ_{period_text}({arguments[0]})"
        if node_operator == "delay" and arguments:
            return f"Delay_{period_text}({arguments[0]})"
        if node_operator == "rolling_mean" and arguments:
            return f"MA_{window_text}({arguments[0]})"
        if node_operator == "rolling_std" and arguments:
            return f"STD_{window_text}({arguments[0]})"
        if node_operator == "rolling_sum" and arguments:
            return f"Sum_{window_text}({arguments[0]})"
        if node_operator == "rolling_max" and arguments:
            return f"Max_{window_text}({arguments[0]})"
        if node_operator == "rolling_min" and arguments:
            return f"Min_{window_text}({arguments[0]})"
        if node_operator == "rolling_corr" and len(arguments) >= 2:
            return f"Corr_{window_text}({arguments[0]}, {arguments[1]})"
        if node_operator == "neg" and arguments:
            return f"−({arguments[0]})"
        if node_operator == "abs" and arguments:
            return f"Abs({arguments[0]})"
        if node_operator == "const" and isinstance(node.get("value"), (int, float)):
            return str(node["value"])
        symbols = {"add": "+", "sub": "−", "mul": "×", "div": "/"}
        if node_operator in symbols and len(arguments) >= 2:
            return f"({arguments[0]} {symbols[node_operator]} {arguments[1]})"
        return str(node_operator or "输入")

    def collect(node: object, field_names: list[str], periods: list[int], windows: list[int]) -> None:
        if not isinstance(node, dict):
            return
        field_name = node.get("field")
        if isinstance(field_name, str) and field_name not in field_names:
            field_names.append(field_name)
        if isinstance(node.get("period"), int):
            periods.append(int(node["period"]))
        if isinstance(node.get("window"), int):
            windows.append(int(node["window"]))
        for argument in node.get("args", ()):
            collect(argument, field_names, periods, windows)

    fields: list[str] = []
    periods: list[int] = []
    windows: list[int] = []
    collect(expression, fields, periods, windows)
    formula_field = ", ".join(fields) if fields else "输入"
    formula_text = render(expression)
    period = max(periods) if periods else None
    window = max(windows) if windows else None
    center = expression.get("center")
    return {
        "formula_operator": operator,
        "formula_text": formula_text,
        "formula_field": formula_field,
        "formula_period": period,
        "formula_window": window,
        "formula_center": center if isinstance(center, bool) else None,
    }


def _expression_operators(expression: object) -> set[str]:
    """收集表达式实际使用的算子，供业务分类使用。"""

    if not isinstance(expression, dict):
        return set()
    operators = {str(expression["op"])} if isinstance(expression.get("op"), str) else set()
    for argument in expression.get("args", ()):
        operators.update(_expression_operators(argument))
    return operators


def _expression_nodes(expression: object, operator: str) -> list[dict[str, object]]:
    """收集指定算子节点，不根据候选编号猜测因子属性。"""

    if not isinstance(expression, dict):
        return []
    nodes: list[dict[str, object]] = []
    if expression.get("op") == operator:
        nodes.append(expression)
    for argument in expression.get("args", ()):
        nodes.extend(_expression_nodes(argument, operator))
    return nodes


def _factor_descriptor(
    expression: object,
    formula: dict[str, object],
    availability: object,
) -> tuple[str, str, str]:
    """从真实 AST 派生中文名称、类别和计算方式。"""

    operators = _expression_operators(expression)
    root = expression.get("op") if isinstance(expression, dict) else None
    windows = [
        int(node["window"])
        for node in _expression_nodes(expression, "rolling_mean")
        + _expression_nodes(expression, "rolling_corr")
        if isinstance(node.get("window"), int)
    ]
    mean_windows = [
        int(node["window"])
        for node in _expression_nodes(expression, "rolling_mean")
        if isinstance(node.get("window"), int)
    ]
    delta_periods = [
        int(node["period"])
        for node in _expression_nodes(expression, "delta")
        if isinstance(node.get("period"), int)
    ]
    mean_window = mean_windows[0] if mean_windows else None
    window = max(windows) if windows else formula.get("formula_window")
    period = max(delta_periods) if delta_periods else formula.get("formula_period")
    window_text = f"{window}日" if isinstance(window, int) else "滚动窗口"
    period_text = f"{period}日" if isinstance(period, int) else "对应周期"
    availability_text = {
        "next_open": "下一交易日开盘",
        "next_close": "下一交易日收盘",
    }.get(str(availability), str(availability) or "下一交易时点")

    if root == "mul" and "neg" in operators and mean_window is not None:
        name = f"成交量强度—下跌收益反转交互因子（均量{mean_window}日、跌幅{period_text}）"
        category = "成交量与短期反转"
        method = (
            f"计算当日成交量相对{mean_window}日成交量均值的比值，"
            f"再与{period_text}收盘价变化的负向部分相乘；信号在{availability_text}使用。"
        )
    elif root == "rolling_corr" and "neg" in operators:
        name = f"成交量—下跌收益相关因子（{window_text}、跌幅{period_text}）"
        category = "成交量与短期反转"
        method = (
            f"计算{window_text}内成交量与{period_text}收盘价变化负向部分的滚动相关系数；"
            f"信号在{availability_text}使用。"
        )
    elif root == "sub" and "rolling_corr" in operators:
        corr_nodes = _expression_nodes(expression, "rolling_corr")
        corr_args = corr_nodes[0].get("args", ()) if corr_nodes else ()
        corr_fields = {
            str(node.get("field"))
            for node in corr_args
            if isinstance(node, dict) and isinstance(node.get("field"), str)
        }
        if "delay" in operators:
            name = f"成交量强度—收益自相关差异因子（{window_text}）"
            category = "成交量与收益自相关"
            method = (
                f"计算成交量相对{mean_window or window}日均量的比值，"
                f"减去{window_text}内收益与其滞后收益的滚动相关系数；"
                f"信号在{availability_text}使用。"
            )
        elif "delta" in operators and "volume" in corr_fields:
            name = f"成交量变化—收益相关差异因子（{window_text}）"
            category = "成交量与价格联动"
            method = (
                f"计算成交量相对{mean_window or window}日均量的比值，"
                f"减去{window_text}内成交量变化与收盘价变化的滚动相关系数；"
                f"信号在{availability_text}使用。"
            )
        else:
            name = f"成交量强度—收益联动差异因子（{window_text}）"
            category = "成交量与价格联动"
            method = (
                f"计算成交量相对{mean_window or window}日均量的比值，"
                f"减去{window_text}内成交量与收盘价变化的滚动相关系数；"
                f"信号在{availability_text}使用。"
            )
    elif root == "mul" and mean_window is not None and "delta" in operators:
        name = f"成交量强度×短期收益交互因子（{mean_window}日）"
        category = "成交量与短期动量"
        method = (
            f"计算成交量相对{mean_window}日成交量均值的比值，再与{period_text}收盘价变化相乘；"
            f"信号在{availability_text}使用。"
        )
    else:
        fields = str(formula.get("formula_field", "输入字段"))
        operator = str(root or formula.get("formula_operator", "组合"))
        name = f"{fields}—{operator}组合因子（{window_text}）"
        category = "基于表达式的组合因子"
        method = f"按公式 {formula.get('formula_text', '未提供')} 计算，信号在{availability_text}使用。"
    return name, category, method


def _source_kind(spec: dict[str, Any], provenance: dict[str, Any]) -> str:
    """从候选来源信息提取稳定的来源类别。"""

    source = str(spec.get("source_kind") or provenance.get("source_kind") or provenance.get("source") or "human")
    return "deepseek" if "deepseek" in source.lower() else source


def _has_chinese(value: object) -> bool:
    """判断自由文本是否至少包含一个中文字符。"""

    return isinstance(value, str) and any("\u3400" <= char <= "\u9fff" for char in value)


def _validate_readable_hypothesis(hypothesis: dict[str, Any], candidate_id: str) -> None:
    """投影前阻止英文叙事和来源占位符进入 Dashboard。"""

    required_text = (
        "claim",
        "mechanism",
        "observable_proxy",
        "independent_verification",
        "falsification_path",
    )
    for field in required_text:
        if not _has_chinese(hypothesis.get(field)):
            raise _projection_error(f"候选 {candidate_id} 的 hypothesis.{field} 不是中文，拒绝投影")
    for field in ("competing_explanations", "failure_modes"):
        values = hypothesis.get(field)
        if not isinstance(values, list) or any(not _has_chinese(value) for value in values):
            raise _projection_error(f"候选 {candidate_id} 的 hypothesis.{field} 不是中文，拒绝投影")
    source_refs = hypothesis.get("source_refs")
    forbidden = {
        "public_source_pending_review",
        "source_pending_review",
        "pending_review",
        "unknown_source",
    }
    if not isinstance(source_refs, list) or any(str(value).strip().lower() in forbidden for value in source_refs):
        raise _projection_error(f"候选 {candidate_id} 的来源引用是占位符，拒绝投影")


def _read_candidate_definitions(
    root: Path,
    manifest: Any,
    candidate_metrics: dict[str, object],
) -> dict[str, object]:
    """读取清单引用的候选 Spec，并扁平化为数据库列所需字段。"""

    result: dict[str, object] = {}
    for artifact in manifest.artifacts:
        relative_path = str(artifact.relative_path)
        parts = relative_path.split("/")
        if len(parts) != 3 or parts[0] != "candidates" or parts[2] != "spec.json":
            continue
        candidate_id = parts[1]
        path = root / relative_path
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise _projection_error(f"候选 Spec 无法解析：{relative_path}") from None
        if not isinstance(spec, dict):
            raise _projection_error(f"候选 Spec 根节点不是 object：{relative_path}")
        summary = candidate_metrics.get(candidate_id, {})
        if not summary:
            summary = next(
                (
                    item
                    for item in candidate_metrics.values()
                    if isinstance(item, dict) and item.get("slot_id") == candidate_id
                ),
                {},
            )
        result[candidate_id] = _flatten_candidate_definition(spec, candidate_id, summary)
    return result


def _flatten_candidate_definition(
    spec: dict[str, Any],
    candidate_id: str,
    summary: object,
) -> dict[str, object]:
    """把一个已核验候选 Spec 展开为 Dashboard 业务列。"""

    hypothesis = spec.get("hypothesis") if isinstance(spec.get("hypothesis"), dict) else {}
    provenance = spec.get("provenance") if isinstance(spec.get("provenance"), dict) else {}
    _validate_readable_hypothesis(hypothesis, candidate_id)
    summary_dict = summary if isinstance(summary, dict) else {}
    formula = _expression_details(spec.get("expression"))
    required_fields = spec.get("required_fields", [])
    factor_name, category, calculation_method = _factor_descriptor(
        spec.get("expression"), formula, spec.get("availability")
    )
    declared_category = spec.get("factor_category", provenance.get("factor_category"))
    if isinstance(declared_category, str) and declared_category not in {"", "unclassified"}:
        category = declared_category
    availability = spec.get("availability")
    if isinstance(availability, dict):
        availability_text = "观察{}，决策{}，交易{}".format(
            availability.get("observation", ""),
            availability.get("decision", ""),
            availability.get("earliest_trade", ""),
        )
    else:
        availability_text = str(availability or "")
    return {
        "spec_hash": summary_dict.get("spec_sha256", summary_dict.get("candidate_spec_hash", "0" * 64)),
        "source_kind": _source_kind(spec, provenance),
        "factor_category": category,
        "factor_name_zh": factor_name,
        "calculation_method": calculation_method,
        "hypothesis_claim": hypothesis.get("claim"),
        "hypothesis_mechanism": hypothesis.get("mechanism"),
        "expected_sign": hypothesis.get("expected_sign"),
        "observable_proxy": hypothesis.get("observable_proxy"),
        "independent_verification": hypothesis.get("independent_verification"),
        "competing_explanations": hypothesis.get("competing_explanations", []),
        "failure_modes": hypothesis.get("failure_modes", []),
        "falsification_path": hypothesis.get("falsification_path"),
        "mechanism_status": hypothesis.get("mechanism_status", "mechanism_unverified"),
        "source_refs": hypothesis.get("source_refs", []),
        **formula,
        "required_fields": required_fields,
        "max_lookback": spec.get("max_lookback"),
        "availability": availability_text,
    }


def _read_state_candidate_definitions(
    root: Path,
    candidate_metrics: dict[str, object],
) -> dict[str, object]:
    """从正式候选登记目录恢复严格研究族的槽位详情。"""

    expected: dict[str, tuple[str, str, object]] = {}
    for candidate_key, summary in candidate_metrics.items():
        if not isinstance(summary, dict):
            continue
        candidate_id = summary.get("candidate_id")
        spec_hash = summary.get("candidate_spec_hash")
        if isinstance(candidate_id, str) and isinstance(spec_hash, str):
            slot_id = summary.get("slot_id") or candidate_key
            if candidate_id in expected and expected[candidate_id][1] != spec_hash:
                raise _projection_error(f"严格研究族候选 ID 对应多个 Spec：{candidate_id}")
            expected[candidate_id] = (str(slot_id), spec_hash, summary)
    if not expected:
        return {}
    candidate_root = root / "state" / "candidates"
    if not candidate_root.is_dir():
        return {}
    found: dict[str, object] = {}
    for path in sorted(candidate_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise _projection_error(f"正式候选登记无法解析：{path.name}") from None
        if not isinstance(payload, dict):
            raise _projection_error(f"正式候选登记根节点不是 object：{path.name}")
        candidate_id = payload.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in expected:
            continue
        try:
            registered = (
                RegisteredTrustedCandidate.model_validate(payload)
                if payload.get("spec", {}).get("spec_version") in {"2", "3"}
                else RegisteredCandidate.model_validate(payload)
            )
        except (AttributeError, ValueError) as error:
            raise _projection_error(f"正式候选登记 Schema 无效：{path.name}：{error}") from error
        slot_id, expected_hash, summary = expected[candidate_id]
        if registered.spec_hash != expected_hash:
            raise _projection_error(f"候选 Spec hash 与运行结果不一致：{candidate_id}")
        spec_payload = registered.spec.model_dump(mode="json")
        found[slot_id] = _flatten_candidate_definition(
            spec_payload,
            slot_id,
            summary,
        )
    found_pairs = {
        (str(slot_id), str(value.get("spec_hash")))
        for slot_id, value in found.items()
        if isinstance(value, dict)
    }
    missing = sorted(
        (slot_id, spec_hash)
        for slot_id, spec_hash, _summary in expected.values()
        if (slot_id, spec_hash) not in found_pairs
    )
    if missing:
        raise _projection_error(f"严格研究族缺少候选 Spec：{missing[:5]}")
    return found


def project_run_artifacts(
    artifact_root: Path,
    run_id: str,
    store: DashboardStore,
) -> DashboardSnapshot:
    """只投影已发布运行；暂存目录和未发布运行一律拒绝。"""

    manifest = verify_published_run(artifact_root, run_id)
    run_root = artifact_root.expanduser().resolve(strict=False) / "artifacts" / "runs" / run_id
    portfolio = _read_optional_json(run_root, "portfolio/metrics.json")
    portfolio_governance = _read_optional_json(run_root, "portfolio/governance.json")
    recent_portfolio = _read_optional_json(run_root, "portfolio/recent_metrics.json")
    portfolio_daily = _read_optional_json(run_root, "portfolio/daily.json")
    ic = _read_optional_json(run_root, "ic/diagnostics.json")
    recent_ic = _read_optional_json(run_root, "ic/recent.json")
    direction_decisions = _read_optional_json(run_root, "direction/decisions.json")
    barra = _read_optional_json(run_root, "barra/attribution.json")
    run_metrics = _read_optional_json(run_root, "run/metrics.json")
    # 快照中的 candidate_metrics 保持旧版运行摘要形状，以维护既有
    # run_id/snapshot_sha256；投影器内部另行规范化候选摘要用于写列。
    candidate_metrics = run_metrics or {}
    candidate_summary_map = _candidate_metrics(candidate_metrics)
    candidate_definitions = _read_candidate_definitions(
        run_root,
        manifest,
        candidate_summary_map,
    )
    if len(candidate_definitions) < len(candidate_summary_map):
        strict_definitions = _read_state_candidate_definitions(
            artifact_root.expanduser().resolve(strict=False),
            candidate_summary_map,
        )
        if strict_definitions:
            candidate_definitions = strict_definitions
    campaign_metadata = None
    if run_metrics is not None and isinstance(run_metrics.get("campaign_metadata"), dict):
        campaign_metadata = run_metrics["campaign_metadata"]
    memory_snapshot = None
    evolution_metadata = None
    memory_root = artifact_root.expanduser().resolve(strict=False) / "research_memory"
    reference_path = memory_root / "references" / f"{run_id}.json"
    memory_entries: tuple[dict[str, object], ...] = ()
    if reference_path.exists():
        try:
            reference = json.loads(reference_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise _projection_error("研究记忆 reference 无法解析") from None
        if not isinstance(reference, dict):
            raise _projection_error("研究记忆 reference 必须是 object")
        memory_snapshot = {key: reference[key] for key in (
            "memory_snapshot_id", "memory_snapshot_sha256", "entry_count", "terminal_counts",
            "published_at", "source_manifest_sha256", "projection_status",
            "coverage_graph_id", "source_family_ids", "cutoff_at",
            "coverage_graph_manifest_hash", "source_memory_snapshot_hash", "gap_report_hash",
            "context_sha256", "approval_batch_hash", "design_policy_hash", "approval_count", "gap_summary",
        ) if key in reference}
        evolution_metadata = reference.get("evolution_metadata") if isinstance(reference.get("evolution_metadata"), dict) else None
        try:
            formal_store = ResearchMemoryStore(research_memory_root=memory_root)
            formal_store.verify()
            loaded_snapshot = formal_store.load_snapshot(str(reference["memory_snapshot_id"]))
        except (KeyError, FactorMinerError) as error:
            raise _projection_error(f"研究记忆正式 snapshot 无法核验：{error}") from error
        if loaded_snapshot.snapshot_sha256 != reference.get("memory_snapshot_sha256"):
            raise _projection_error("研究记忆 reference 与正式 snapshot 身份不一致")
        loaded_entries = [formal_store.load_entry(entry_id) for entry_id in loaded_snapshot.entry_ids]
        memory_entries = tuple(entry.model_dump(mode="json") for entry in loaded_entries)
    snapshot = DashboardSnapshot.build(
        run_id=run_id,
        artifact_manifest_sha256=manifest.manifest_sha256,
        artifact_refs=tuple(item.model_dump(mode="json") for item in manifest.artifacts),
        candidate_metrics=candidate_metrics,
        candidate_definitions=candidate_definitions,
        portfolio_metrics=portfolio,
        portfolio_governance=portfolio_governance,
        recent_portfolio_metrics=recent_portfolio,
        portfolio_daily=portfolio_daily,
        ic_diagnostics=ic,
        recent_ic_diagnostics=recent_ic,
        direction_decisions=direction_decisions,
        barra_attribution=barra,
        campaign_metadata=campaign_metadata,
        evolution_metadata=evolution_metadata,
        memory_snapshot=memory_snapshot,
        memory_entries=memory_entries,
    )
    store.replace_run_snapshot(run_id, snapshot.model_dump(mode="json"))
    return snapshot


def load_dashboard_snapshot(
    run_id: str,
    store: DashboardStore,
) -> DashboardSnapshot:
    """只从读模型读取已投影快照，不触碰原始数据和研究账本。"""

    payload = store.load_run_snapshot(run_id)
    if payload is None:
        raise _projection_error("Dashboard 只能读取已发布且已投影的运行")
    try:
        return DashboardSnapshot.model_validate(payload)
    except ValueError:
        raise _projection_error("Dashboard 读模型快照 Schema 无效") from None

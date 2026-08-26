"""阶段 C 正式研究族的治理编排与统一 Pilot 评价端口。

本模块只处理 family、slot、seal、统计预算和发布元数据。候选真正的因子值、
标签、组合、Barra 计算由传入的统一 Pilot runner 提供，避免正式研究族复制
阶段 A 的金融计算器。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_hypothesis import RegisteredCoverageGapHypothesis
from factor_miner.lightweight_schema import LightweightBatchManifest
from factor_miner.llm_ledger import LLMDiscoveryLedger
from factor_miner.llm_orchestrator import (
    ApprovedBatchDependencies,
    ApprovedBatchResult,
    _ensure_generation_started,
    finalize_unexecuted_slots,
    run_approved_batch,
)
from factor_miner.llm_schema import RegisteredLLMDiscoveryResearchFamily
from factor_miner.llm_seal import RegisteredGenerationSeal, verify_generation_seal
from factor_miner.llm_state import (
    CANDIDATE_TERMINALS,
    CandidateSlotState,
    FamilyGenerationState,
    expected_logical_hypothesis_slot_ids,
    expected_candidate_slot_ids,
    initial_discovery_family_state,
)
from factor_miner.portfolio_artifacts import PublishedRunManifest, publish_run_artifacts
from factor_miner.research_memory import publish_campaign_memory
from factor_miner.research_evolution import require_approved_evolution_batch
from factor_miner.research_evolution_schema import (
    ApprovedEvolutionHypothesisBatch,
    CoverageGapReport,
    GapCategory,
    ResearchEvolutionContext,
)
from factor_miner.shadow_generators import ShadowGenerationSummary, generate_shadow_arms
from factor_miner.statistics import (
    FrozenStatisticalPolicy,
    HacInference,
    hac_mean_test_frozen,
)


def lightweight_statistical_policy(
    manifest: LightweightBatchManifest,
    *,
    alpha: float = 0.05,
    hac_max_lags: int = 5,
    min_valid_dates: int = 60,
) -> FrozenStatisticalPolicy:
    """按轻量 manifest 的完整槽数冻结 HAC 与 Bonferroni 分母。"""

    return FrozenStatisticalPolicy(
        policy_id=f"lightweight_{manifest.evaluation_policy_hash[:24]}",
        alpha=alpha,
        hac_max_lags=hac_max_lags,
        min_valid_dates=min_valid_dates,
        bonferroni_denominator=manifest.family_size,
        multiplicity_policy="bonferroni_over_frozen_family_budget",
    )


class CampaignPilotRunner(Protocol):
    """阶段 A 统一评价器的最小 slot 端口。"""

    def run_slot(self, slot_id: str) -> object:
        """执行一个已经通过 generation seal 的候选槽。"""


class CampaignSlotEvaluation(BaseModel):
    """一个预登记槽的评价状态和非原始结果摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_id: str
    slot_state: CandidateSlotState
    status: str
    candidate_id: str | None = None
    candidate_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ast_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_reason: str | None = None
    hac: HacInference | None = None
    output_correlation: float | None = None
    output: dict[str, object] = Field(default_factory=dict)


class CampaignEvaluationResult(BaseModel):
    """正式研究族完整 120 槽评价结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_id: str = Field(pattern=r"^llmfamily_[0-9a-f]{24}$")
    generation_seal_id: str = Field(pattern=r"^llmseal_[0-9a-f]{24}$")
    evaluation_policy_id: str
    status: str
    registered_slot_count: int = Field(ge=0)
    evaluated_slot_count: int = Field(ge=0)
    failed_slot_count: int = Field(ge=0)
    bonferroni_denominator: int = Field(ge=1)
    statistical_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    slot_evaluations: tuple[CampaignSlotEvaluation, ...] = Field(
        min_length=120,
        max_length=120,
    )
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_counts(self) -> CampaignEvaluationResult:
        """槽位集合、统计分母和计数必须相互一致。"""

        if self.registered_slot_count != len(self.slot_evaluations):
            raise ValueError("registered_slot_count 与槽位结果数量不一致")
        if self.registered_slot_count != 120:
            raise ValueError("正式研究族必须保留完整 120 个预登记槽位")
        if self.bonferroni_denominator != self.registered_slot_count:
            raise ValueError("Bonferroni 分母必须覆盖全部预登记槽位")
        if self.evaluated_slot_count + self.failed_slot_count != self.registered_slot_count:
            raise ValueError("评价成功和失败槽位计数不完整")
        slot_ids = tuple(item.slot_id for item in self.slot_evaluations)
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("正式评价槽位不能重复")
        return self

    @classmethod
    def build(
        cls,
        *,
        family_id: str,
        generation_seal_id: str,
        evaluation_policy_id: str,
        status: str,
        slot_evaluations: tuple[CampaignSlotEvaluation, ...],
        statistical_policy: FrozenStatisticalPolicy,
    ) -> CampaignEvaluationResult:
        """从完整槽结果构造内容寻址摘要。"""

        evaluated = sum(item.status == "evaluated" for item in slot_evaluations)
        failed = len(slot_evaluations) - evaluated
        payload = {
            "family_id": family_id,
            "generation_seal_id": generation_seal_id,
            "evaluation_policy_id": evaluation_policy_id,
            "status": status,
            "registered_slot_count": len(slot_evaluations),
            "evaluated_slot_count": evaluated,
            "failed_slot_count": failed,
            "bonferroni_denominator": statistical_policy.bonferroni_denominator,
            "statistical_policy_hash": sha256_json(
                statistical_policy.model_dump(mode="json")
            ),
            "slot_evaluations": [
                item.model_dump(mode="json") for item in slot_evaluations
            ],
        }
        return cls(**payload, result_sha256=sha256_json(payload))


class LightweightCampaignEvaluationResult(BaseModel):
    """轻量批次完整槽位评价；统计分母由冻结 manifest 决定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "lightweight-campaign-evaluation-v1"
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = "completed"
    registered_slot_count: int = Field(gt=0)
    evaluated_slot_count: int = Field(ge=0)
    failed_slot_count: int = Field(ge=0)
    bonferroni_denominator: int = Field(gt=0)
    frozen_slot_ids: tuple[str, ...] = Field(min_length=1)
    slot_evaluations: tuple[CampaignSlotEvaluation, ...] = Field(min_length=1)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_complete_family(self) -> LightweightCampaignEvaluationResult:
        """缺失、重复或额外槽位都不得离开冻结检验族。"""

        if len(self.frozen_slot_ids) != self.registered_slot_count:
            raise ValueError("轻量评价冻结槽位数与登记数不一致")
        if len(set(self.frozen_slot_ids)) != self.registered_slot_count:
            raise ValueError("轻量评价冻结槽位不能重复")
        evaluated_ids = tuple(item.slot_id for item in self.slot_evaluations)
        if evaluated_ids != self.frozen_slot_ids:
            raise ValueError("轻量评价必须按 manifest 顺序完整覆盖全部冻结槽位")
        if self.bonferroni_denominator != self.registered_slot_count:
            raise ValueError("轻量评价 Bonferroni 分母必须覆盖全部冻结槽位")
        if self.evaluated_slot_count + self.failed_slot_count != self.registered_slot_count:
            raise ValueError("轻量评价成功和失败槽位计数不完整")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != sha256_json(payload):
            raise ValueError("轻量评价 result_sha256 与内容不一致")
        return self

    @classmethod
    def build(
        cls,
        *,
        manifest: LightweightBatchManifest,
        slot_evaluations: tuple[CampaignSlotEvaluation, ...],
    ) -> LightweightCampaignEvaluationResult:
        """从冻结 manifest 和全部槽位终态构造内容寻址结果。"""

        frozen_slot_ids = tuple(
            binding.slot_id for binding in manifest.candidate_bindings
        )
        evaluated = sum(item.status == "evaluated" for item in slot_evaluations)
        payload = {
            "version": "lightweight-campaign-evaluation-v1",
            "manifest_sha256": manifest.manifest_sha256,
            "status": "completed",
            "registered_slot_count": manifest.family_size,
            "evaluated_slot_count": evaluated,
            "failed_slot_count": manifest.family_size - evaluated,
            "bonferroni_denominator": manifest.family_size,
            "frozen_slot_ids": frozen_slot_ids,
            "slot_evaluations": [
                item.model_dump(mode="json") for item in slot_evaluations
            ],
        }
        return cls(**payload, result_sha256=sha256_json(payload))


class CampaignGenerationResult(BaseModel):
    """登记、shadow 和终态收口的服务器摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_id: str
    status: str
    terminal_slot_count: int = Field(ge=0)
    non_terminal_slot_ids: tuple[str, ...]
    approved_batch_results: tuple[dict[str, object], ...]
    shadow_summary_sha256: str | None = None
    finalization_summary_sha256: str | None = None
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MappingCampaignPilotRunner:
    """把服务器已生成的 slot 结果接到统一评价器的合成/受控适配器。"""

    def __init__(
        self,
        results_by_slot: Mapping[str, object],
        *,
        approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
        evolution_context: ResearchEvolutionContext | None = None,
    ) -> None:
        if approval_batch is None and evolution_context is None:
            self._results = dict(results_by_slot)
            return
        _validated_strict_evolution_inputs(
            approval_batch=approval_batch,
            evolution_context=evolution_context,
            family_id=evolution_context.discovery_family_id
            if evolution_context is not None
            else None,
        )
        self._results = dict(results_by_slot)

    def run_slot(self, slot_id: str) -> object:
        """按不可变 slot ID 返回阶段 A 统一评价结果。"""

        if slot_id not in self._results:
            raise FactorMinerError(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"统一 Pilot 缺少候选槽结果：{slot_id}",
            )
        return self._results[slot_id]


def _runner_error(code: FailureCode, message: str) -> FactorMinerError:
    """构造研究族治理错误。"""

    return FactorMinerError(code, message)


def _validated_strict_evolution_inputs(
    *,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    evolution_context: ResearchEvolutionContext | None,
    family_id: str | None,
) -> ApprovedEvolutionHypothesisBatch | None:
    """核验 strict evolution 路径的十槽预算与核心上下文 hash。"""

    if approval_batch is None and evolution_context is None:
        return None
    if approval_batch is None or evolution_context is None:
        raise _runner_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "严格演化路径必须同时提供 approval_batch 与 evolution_context",
        )
    expected_family_id = family_id or evolution_context.discovery_family_id
    validated = require_approved_evolution_batch(
        approval_batch,
        context_sha256=evolution_context.context_sha256,
        discovery_family_id=expected_family_id,
    )
    if tuple(item.logical_slot_id for item in validated.approvals) != expected_logical_hypothesis_slot_ids():
        raise _runner_error(
            FailureCode.APPROVAL_COUNT_INVALID,
            "严格演化批准批次必须按固定顺序覆盖 H01-H10",
        )
    for name in (
        "coverage_graph_manifest_hash",
        "memory_snapshot_hash",
        "gap_report_hash",
        "design_policy_hash",
        "context_sha256",
    ):
        value = getattr(evolution_context, name)
        if not isinstance(value, str) or len(value) != 64:
            raise _runner_error(
                FailureCode.EVOLUTION_CONTEXT_INVALID,
                f"严格演化上下文缺少有效 {name}",
            )
    return validated


def _validate_strict_llm_hypothesis_coverage(
    approved_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
) -> None:
    """strict path 必须一次性覆盖两条 LLM arm 的十个逻辑槽。"""

    expected_slots = {
        f"{arm_id}:{logical_slot_id}"
        for arm_id in ("coverage_outcome_llm", "literature_only_llm")
        for logical_slot_id in expected_logical_hypothesis_slot_ids()
    }
    actual_slots = {item.draft.slot_id for item in approved_hypotheses}
    if actual_slots != expected_slots:
        raise _runner_error(
            FailureCode.APPROVAL_COUNT_INVALID,
            "严格演化 campaign generation 必须同时覆盖两条 LLM arm 的 H01-H10",
        )


def _jsonable(value: object) -> object:
    """将阶段 A 返回的模型或 dataclass 转成不含原始行情的 JSON 摘要。"""

    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("评价摘要包含非有限数字")
        return value
    return str(value)


def _as_mapping(value: object) -> dict[str, object]:
    """读取阶段 A 结果的公开摘要字段。"""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, Mapping):
        raise TypeError("统一 Pilot 结果必须是 object、Pydantic 模型或 dataclass")
    return {str(key): item for key, item in value.items()}


def _nested(value: Mapping[str, object], *keys: str) -> object | None:
    """按候选结果的公开嵌套层读取字段。"""

    current: object = value
    for key in keys:
        if isinstance(current, BaseModel):
            current = current.model_dump(mode="json")
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _candidate_metadata(raw: Mapping[str, object]) -> tuple[str | None, str | None, str | None]:
    """提取候选 ID、Spec hash 和规范 AST hash。"""

    candidate_id = raw.get("candidate_id")
    candidate_spec_hash = raw.get("candidate_spec_hash") or raw.get("spec_hash")
    expression = raw.get("expression") or raw.get("ast")
    if expression is None:
        candidate = raw.get("candidate")
        if isinstance(candidate, Mapping):
            candidate_id = candidate_id or candidate.get("candidate_id")
            candidate_spec_hash = candidate_spec_hash or candidate.get("spec_hash")
            expression = candidate.get("expression") or _nested(candidate, "spec", "expression")
    ast_hash = raw.get("ast_hash")
    if ast_hash is None and expression is not None:
        ast_hash = sha256_json(_jsonable(expression))
    return (
        str(candidate_id) if candidate_id is not None else None,
        str(candidate_spec_hash) if candidate_spec_hash is not None else None,
        str(ast_hash) if ast_hash is not None else None,
    )


def _rank(values: Sequence[float]) -> list[float]:
    """计算确定性 average tie rank。"""

    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for index in range(cursor, end):
            ranks[indexed[index][0]] = rank
        cursor = end
    return ranks


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    """计算一组已经对齐截面的 Spearman 相关。"""

    if len(left) != len(right) or len(left) < 2:
        raise ValueError("输出相关性截面样本不足")
    left_rank = _rank(left)
    right_rank = _rank(right)
    left_mean = sum(left_rank) / len(left_rank)
    right_mean = sum(right_rank) / len(right_rank)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left_rank, right_rank, strict=True)
    )
    left_var = sum((item - left_mean) ** 2 for item in left_rank)
    right_var = sum((item - right_mean) ** 2 for item in right_rank)
    if left_var == 0 or right_var == 0:
        raise ValueError("输出相关性截面存在常数序列")
    return numerator / math.sqrt(left_var * right_var)


def _panel_by_date(raw: Mapping[str, object]) -> dict[str, dict[str, float]]:
    """提取可见区间逐日截面输出，用于冗余检查。"""

    value = raw.get("factor_panel") or raw.get("factor_outputs")
    if isinstance(value, Mapping):
        result: dict[str, dict[str, float]] = {}
        for current_date, values in value.items():
            if not isinstance(values, Mapping):
                continue
            result[str(current_date)] = {
                str(asset): float(number)
                for asset, number in values.items()
                if isinstance(number, (int, float)) and math.isfinite(float(number))
            }
        return result
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    result = {}
    for row in value:
        if not isinstance(row, Mapping):
            continue
        current_date = row.get("date") or row.get("signal_date")
        asset = row.get("asset") or row.get("security_id")
        number = (
            row.get("factor_value")
            if "factor_value" in row
            else row.get("value")
        )
        if current_date is None or asset is None or not isinstance(number, (int, float)):
            continue
        if not math.isfinite(float(number)):
            continue
        result.setdefault(str(current_date), {})[str(asset)] = float(number)
    return result


def _panel_correlation(
    left: Mapping[str, Mapping[str, float]],
    right: Mapping[str, Mapping[str, float]],
) -> float | None:
    """计算重叠可见日的平均逐日截面 Spearman。"""

    values: list[float] = []
    for current_date in sorted(set(left).intersection(right)):
        assets = sorted(set(left[current_date]).intersection(right[current_date]))
        if len(assets) < 2:
            continue
        try:
            values.append(
                _correlation(
                    [left[current_date][asset] for asset in assets],
                    [right[current_date][asset] for asset in assets],
                )
            )
        except ValueError:
            continue
    return sum(values) / len(values) if values else None


def _invoke_runner(pilot_runner: object, slot_id: str) -> object:
    """兼容正式 runner、测试 runner 和简单 callable。"""

    if hasattr(pilot_runner, "run_slot"):
        return pilot_runner.run_slot(slot_id)  # type: ignore[attr-defined]
    if hasattr(pilot_runner, "evaluate_slot"):
        return pilot_runner.evaluate_slot(slot_id)  # type: ignore[attr-defined]
    if callable(pilot_runner):
        return pilot_runner(slot_id)
    raise TypeError("pilot_runner 必须提供 run_slot、evaluate_slot 或 callable")


def evaluate_sealed_campaign(
    seal: RegisteredGenerationSeal,
    campaign: RegisteredLLMDiscoveryResearchFamily,
    pilot_runner: object,
    *,
    statistical_policy: FrozenStatisticalPolicy | None = None,
    max_abs_output_correlation: float = 0.8,
    evolution_context: ResearchEvolutionContext | None = None,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    slot_objects: Mapping[str, Mapping[str, object]] | None = None,
) -> CampaignEvaluationResult:
    """在 generation seal 后逐槽调用阶段 A 统一评价器。

    所有 120 个预登记槽都进入结果和统计分母。失败、未执行和冗余槽只改变
    槽状态与失败说明，不会从多重检验族中删除。
    """

    if seal.manifest.discovery_family_id != campaign.discovery_family_id:
        raise _runner_error(
            FailureCode.LLM_DATA_IDENTITY_OVERLAP,
            "generation seal 与研究族身份不一致",
        )
    expected_slots = expected_candidate_slot_ids(campaign.spec)
    states = dict(seal.manifest.candidate_slot_states)
    # 规范 JSON 会按 key 排序，反序列化后的 dict 顺序不再代表 arm 顺序；
    # 槽位身份集合才是 seal 的不变量。
    if set(states) != set(expected_slots) or len(states) != 120:
        raise _runner_error(
            FailureCode.LLM_FAMILY_NOT_SEALED,
            "正式评价必须接收完整 120 槽 generation seal",
        )
    if any(state not in CANDIDATE_TERMINALS for state in states.values()):
        raise _runner_error(
            FailureCode.LLM_FAMILY_NOT_SEALED,
            "generation seal 中存在未终结槽位",
        )
    projected = initial_discovery_family_state(campaign).model_copy(
        update={
            "family_generation_state": FamilyGenerationState.GENERATION_SEALED,
            "candidate_slot_states": states,
        }
    )
    verify_generation_seal(
        campaign,
        projected,
        seal,
        evolution_context=evolution_context,
        approval_batch=approval_batch,
        slot_objects=slot_objects,
    )
    if not 0 <= max_abs_output_correlation <= 1:
        raise ValueError("输出冗余阈值必须位于 [0, 1]")
    policy = statistical_policy or FrozenStatisticalPolicy.formal_120(
        policy_id=campaign.spec.evaluation_policy_id
    )
    if (
        policy.bonferroni_denominator != campaign.spec.global_statistical_trial_budget
        or campaign.spec.global_statistical_trial_budget != 120
    ):
        raise _runner_error(
            FailureCode.STAT_FAMILY_NOT_FROZEN,
            "正式统计分母必须冻结为 120 个预登记槽",
        )

    evaluations: list[CampaignSlotEvaluation] = []
    seen_ast: dict[str, str] = {}
    seen_panels: dict[str, dict[str, dict[str, float]]] = {}
    for slot_id in expected_slots:
        slot_state = states[slot_id]
        if slot_state is not CandidateSlotState.READY_FOR_REGISTRATION:
            evaluations.append(
                CampaignSlotEvaluation(
                    slot_id=slot_id,
                    slot_state=slot_state,
                    status="not_executed",
                    failure_reason=f"槽位终态为 {slot_state.value}，没有候选结果",
                )
            )
            continue
        try:
            raw = _as_mapping(_invoke_runner(pilot_runner, slot_id))
            candidate_id, spec_hash, ast_hash = _candidate_metadata(raw)
            if ast_hash is not None and ast_hash in seen_ast:
                evaluations.append(
                    CampaignSlotEvaluation(
                        slot_id=slot_id,
                        slot_state=slot_state,
                        status="redundant",
                        candidate_id=candidate_id,
                        candidate_spec_hash=spec_hash,
                        ast_hash=ast_hash,
                        failure_reason=(
                            f"规范 AST 与 {seen_ast[ast_hash]} 重复"
                        ),
                        output=_jsonable(raw),
                    )
                )
                continue
            if ast_hash is not None:
                seen_ast[ast_hash] = slot_id
            panel = _panel_by_date(raw)
            output_correlation: float | None = None
            redundant_reason: str | None = None
            if panel:
                for previous_slot, previous_panel in seen_panels.items():
                    correlation = _panel_correlation(panel, previous_panel)
                    if correlation is not None:
                        output_correlation = correlation
                        if abs(correlation) > max_abs_output_correlation:
                            redundant_reason = (
                                f"与 {previous_slot} 的逐日截面 Spearman "
                                f"相关性 {correlation:.6f} 超过阈值"
                            )
                            break
                seen_panels[slot_id] = panel
            rank_values = raw.get("rank_ic_values") or raw.get("rank_ic_sequence")
            if rank_values is None:
                rank_values = _nested(raw, "ic", "rank_ic_sequence")
            if not isinstance(rank_values, Sequence) or isinstance(rank_values, (str, bytes)):
                raise _runner_error(
                    FailureCode.STAT_FAMILY_NOT_FROZEN,
                    f"{slot_id} 缺少统一 Pilot RankIC 序列",
                )
            hac = hac_mean_test_frozen(
                tuple(float(item) for item in rank_values),
                policy,
            )
            output = _jsonable(raw)
            evaluations.append(
                CampaignSlotEvaluation(
                    slot_id=slot_id,
                    slot_state=slot_state,
                    status="redundant" if redundant_reason else "evaluated",
                    candidate_id=candidate_id,
                    candidate_spec_hash=spec_hash,
                    ast_hash=ast_hash,
                    failure_reason=redundant_reason,
                    hac=hac,
                    output_correlation=output_correlation,
                    output=output,
                )
            )
        except Exception as error:
            evaluations.append(
                CampaignSlotEvaluation(
                    slot_id=slot_id,
                    slot_state=slot_state,
                    status="failed",
                    failure_reason=str(error),
                )
            )
    return CampaignEvaluationResult.build(
        family_id=campaign.discovery_family_id,
        generation_seal_id=seal.generation_seal_id,
        evaluation_policy_id=campaign.spec.evaluation_policy_id,
        status="completed",
        slot_evaluations=tuple(evaluations),
        statistical_policy=policy,
    )


def run_approved_campaign_generation(
    family_id: str,
    family: RegisteredLLMDiscoveryResearchFamily,
    dependencies: ApprovedBatchDependencies,
    approved_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...],
    *,
    coverage_hypotheses: tuple[RegisteredCoverageGapHypothesis, ...] | None = None,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    evolution_context: ResearchEvolutionContext | None = None,
) -> CampaignGenerationResult:
    """执行登记、LLM 主 arm、两条 shadow arm 和终态收口。"""

    if family.discovery_family_id != family_id:
        raise _runner_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "family 身份不一致")
    strict_batch = _validated_strict_evolution_inputs(
        approval_batch=approval_batch,
        evolution_context=evolution_context,
        family_id=family_id,
    )
    if strict_batch is not None:
        _validate_strict_llm_hypothesis_coverage(approved_hypotheses)
    ledger = LLMDiscoveryLedger(dependencies.artifact_root)
    ledger.register_family(family)
    if not approved_hypotheses:
        _ensure_generation_started(
            ledger,
            family,
            "campaign-generation",
            dependencies.now or datetime.now(timezone.utc),
        )
    approved_coverage = coverage_hypotheses or tuple(
        item
        for item in approved_hypotheses
        if item.draft.slot_id.startswith("coverage_outcome_llm:")
    )
    batch_results: list[ApprovedBatchResult] = []
    if approved_hypotheses:
        batch_results.append(
            run_approved_batch(
                family_id,
                approved_hypotheses,
                dependencies,
                approval_batch=approval_batch,
                evolution_context=evolution_context,
            )
        )
    if any(item.pending_request_hashes for item in batch_results):
        state = LLMDiscoveryLedger(dependencies.artifact_root).project_state(family_id)
        pending = tuple(
            slot_id
            for slot_id, slot_state in state.candidate_slot_states.items()
            if slot_state not in CANDIDATE_TERMINALS
        )
        payload = {
            "family_id": family_id,
            "status": "awaiting_authorization",
            "terminal_slot_count": sum(
                state_value in CANDIDATE_TERMINALS
                for state_value in state.candidate_slot_states.values()
            ),
            "non_terminal_slot_ids": pending,
            "approved_batch_results": [
                item.model_dump(mode="json") for item in batch_results
            ],
            "shadow_summary_sha256": None,
            "finalization_summary_sha256": None,
        }
        return CampaignGenerationResult(
            **payload,
            summary_sha256=sha256_json(payload),
        )
    shadow_summary: ShadowGenerationSummary | None = None
    if approved_coverage:
        if dependencies.field_registry is None:
            raise _runner_error(
                FailureCode.SPEC_SCHEMA_INVALID,
                "shadow 生成缺少服务器字段注册表",
            )
        shadow_summary = generate_shadow_arms(
            family_id,
            family,
            dependencies.artifact_root,
            approved_coverage,
            dependencies.field_registry,
            approval_batch=strict_batch,
            evolution_context=evolution_context,
            now=dependencies.now,
        )
    finalization = finalize_unexecuted_slots(
        family_id,
        dependencies,
        approved_hypotheses,
    )
    state = LLMDiscoveryLedger(dependencies.artifact_root).project_state(family_id)
    status = "completed" if not finalization.remaining_non_terminal_slot_ids else "partial"
    payload = {
        "family_id": family_id,
        "status": status,
        "terminal_slot_count": sum(
            item in CANDIDATE_TERMINALS
            for item in state.candidate_slot_states.values()
        ),
        "non_terminal_slot_ids": finalization.remaining_non_terminal_slot_ids,
        "approved_batch_results": [item.model_dump(mode="json") for item in batch_results],
        "shadow_summary_sha256": (
            shadow_summary.summary_sha256 if shadow_summary is not None else None
        ),
        "finalization_summary_sha256": finalization.summary_sha256,
    }
    return CampaignGenerationResult(**payload, summary_sha256=sha256_json(payload))


@dataclass(frozen=True, slots=True)
class CampaignPublication:
    """正式研究族发布产物。"""

    run_id: str
    manifest: PublishedRunManifest
    artifact_root: Path

    @property
    def manifest_path(self) -> Path:
        """返回不可变运行清单路径。"""

        return self.artifact_root / "artifacts" / "runs" / self.run_id / "run_manifest.json"


def _evolution_publication_metadata(
    *,
    seal: RegisteredGenerationSeal,
    context: ResearchEvolutionContext | None,
    approval_batch: ApprovedEvolutionHypothesisBatch | None,
    gap_report: CoverageGapReport | None,
) -> dict[str, object]:
    """冻结并展开 strict evolution 的四表身份链为脱敏读模型。"""
    strict_present = any(item is not None for item in (context, approval_batch, gap_report))
    strict_sealed = seal.manifest.evolution_context_hash is not None
    if strict_present != strict_sealed or (strict_sealed and any(item is None for item in (context, approval_batch, gap_report))):
        raise _runner_error(
            FailureCode.EVOLUTION_CONTEXT_INVALID,
            "strict evolution 发布必须同时提供 seal、context、approval batch 和 gap report",
        )
    if not strict_sealed:
        return {}
    assert context is not None and approval_batch is not None and gap_report is not None
    if context.context_sha256 != seal.manifest.evolution_context_hash:
        raise _runner_error(FailureCode.EVOLUTION_HASH_MISMATCH, "evolution context 与 seal hash 不一致")
    if approval_batch.approval_batch_sha256 != seal.manifest.approval_batch_hash:
        raise _runner_error(FailureCode.EVOLUTION_HASH_MISMATCH, "approval batch 与 seal hash 不一致")
    if context.gap_report_hash != gap_report.report_sha256 or gap_report.report_sha256 != seal.manifest.gap_report_hash:
        raise _runner_error(FailureCode.EVOLUTION_HASH_MISMATCH, "gap report 与 context/seal hash 不一致")
    if context.coverage_graph_manifest_hash != seal.manifest.coverage_graph_manifest_hash:
        raise _runner_error(FailureCode.EVOLUTION_HASH_MISMATCH, "coverage graph 与 seal hash 不一致")
    if context.memory_snapshot_hash != seal.manifest.memory_snapshot_hash:
        raise _runner_error(FailureCode.EVOLUTION_HASH_MISMATCH, "source memory snapshot 与 seal hash 不一致")
    if context.design_policy_hash != seal.manifest.design_policy_hash:
        raise _runner_error(FailureCode.EVOLUTION_HASH_MISMATCH, "design policy 与 seal hash 不一致")
    category_counts = {category.value: 0 for category in GapCategory}
    labels: set[str] = set()
    for card in gap_report.gap_cards:
        category_counts[card.gap_category.value] = category_counts.get(card.gap_category.value, 0) + 1
        labels.update(card.sanitized_labels)
    gap_summary = {
        "gap_report_hash": gap_report.report_sha256,
        "coverage_graph_id": gap_report.coverage_graph_id,
        "category_counts": category_counts,
        "sanitized_labels": sorted(labels),
        "truncated_count": gap_report.truncated_count,
    }
    return {
        "context_id": f"evolutionctx_{context.context_sha256[:24]}",
        "context_sha256": context.context_sha256,
        "approval_batch_hash": approval_batch.approval_batch_sha256,
        "approval_count": len(approval_batch.approvals),
        "coverage_graph_id": gap_report.coverage_graph_id,
        "coverage_graph_manifest_hash": context.coverage_graph_manifest_hash,
        "memory_snapshot_hash": context.memory_snapshot_hash,
        "gap_report_hash": context.gap_report_hash,
        "design_policy_hash": context.design_policy_hash,
        "hypothesis_count": context.hypotheses_per_round,
        "slot_count": context.total_slots,
        "gap_summary": gap_summary,
    }


def _campaign_candidate_spec_artifacts(
    artifact_root: Path,
    slot_objects: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, bytes]:
    """把严格研究族的已登记候选 Spec 纳入运行清单。"""

    if slot_objects is None:
        return {}
    expected: dict[str, tuple[str, str]] = {}
    for slot_id, payload in slot_objects.items():
        candidate_id = payload.get("candidate_id")
        spec_hash = payload.get("candidate_spec_hash")
        if isinstance(candidate_id, str) and isinstance(spec_hash, str):
            expected[str(slot_id)] = (candidate_id, spec_hash)
    if not expected:
        return {}
    candidate_root = artifact_root.expanduser().resolve(strict=False) / "state" / "candidates"
    if not candidate_root.is_dir():
        raise _runner_error(FailureCode.LEDGER_CORRUPT, "严格研究族发布缺少候选登记目录")
    registered: dict[str, dict[str, object]] = {}
    for path in sorted(candidate_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise _runner_error(FailureCode.LEDGER_CORRUPT, f"候选登记无法解析：{path.name}") from error
        if not isinstance(payload, dict):
            raise _runner_error(FailureCode.LEDGER_CORRUPT, f"候选登记根节点无效：{path.name}")
        candidate_id = payload.get("candidate_id")
        if isinstance(candidate_id, str):
            registered[candidate_id] = payload
    artifacts: dict[str, bytes] = {}
    missing: list[str] = []
    for slot_id, (candidate_id, spec_hash) in sorted(expected.items()):
        payload = registered.get(candidate_id)
        spec = payload.get("spec") if payload is not None else None
        if payload is None or not isinstance(spec, dict):
            missing.append(slot_id)
            continue
        if payload.get("spec_hash") != spec_hash or sha256_json(spec) != spec_hash:
            raise _runner_error(FailureCode.LEDGER_CORRUPT, f"候选登记 hash 不一致：{slot_id}")
        artifacts[f"candidates/{slot_id}/spec.json"] = canonical_json_bytes(spec)
    if missing:
        raise _runner_error(FailureCode.LEDGER_CORRUPT, f"严格研究族缺少候选 Spec：{missing[:5]}")
    return artifacts


def _require_complete_dashboard_metrics(result: CampaignEvaluationResult) -> None:
    """拒绝把缺少核心 IC 或逐期组合收益的已评价槽发布到读模型。"""

    required_ic_fields = {
        "ic_mean",
        "rank_ic_mean",
        "ic_std",
        "rank_ic_std",
        "ic_ir",
        "rank_ic_ir",
        "ic_hac_t",
        "rank_ic_hac_t",
    }
    for item in result.slot_evaluations:
        if item.status not in {"evaluated", "redundant"}:
            continue
        ic = item.output.get("ic")
        if not isinstance(ic, Mapping):
            raise _runner_error(FailureCode.PILOT_INPUT_CONTRACT_INVALID, f"{item.slot_id} 缺少 IC 摘要")
        missing = sorted(field for field in required_ic_fields if not isinstance(ic.get(field), (int, float)))
        if missing:
            raise _runner_error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{item.slot_id} 缺少 Dashboard 核心 IC 指标：{missing}",
            )
        daily = item.output.get("portfolio_daily")
        if not isinstance(daily, Sequence) or isinstance(daily, (str, bytes)) or not daily:
            raise _runner_error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{item.slot_id} 缺少计算 win_rate 所需的逐期组合收益",
            )
        if any(not isinstance(row, Mapping) or not isinstance(row.get("Q10_Q1_net_return"), (int, float)) for row in daily):
            raise _runner_error(
                FailureCode.PILOT_INPUT_CONTRACT_INVALID,
                f"{item.slot_id} 的逐期组合收益缺少 Q10_Q1_net_return",
            )


def publish_campaign_evaluation(
    artifact_root: Path,
    campaign: RegisteredLLMDiscoveryResearchFamily,
    seal: RegisteredGenerationSeal,
    result: CampaignEvaluationResult,
    *,
    evolution_context: ResearchEvolutionContext | None = None,
    approval_batch: ApprovedEvolutionHypothesisBatch | None = None,
    gap_report: CoverageGapReport | None = None,
    slot_objects: Mapping[str, Mapping[str, object]] | None = None,
    dashboard_store: object | None = None,
) -> CampaignPublication:
    """发布正式 family 的非原始诊断，并保留完整 120 槽失败摘要。"""

    if result.family_id != campaign.discovery_family_id:
        raise _runner_error(FailureCode.LLM_DATA_IDENTITY_OVERLAP, "评价结果 family 身份不一致")
    if result.generation_seal_id != seal.generation_seal_id:
        raise _runner_error(FailureCode.LLM_FAMILY_NOT_SEALED, "评价结果 seal 身份不一致")
    _require_complete_dashboard_metrics(result)
    states = dict(seal.manifest.candidate_slot_states)
    projected = initial_discovery_family_state(campaign).model_copy(
        update={"family_generation_state": FamilyGenerationState.GENERATION_SEALED,
                "candidate_slot_states": states}
    )
    verify_generation_seal(
        campaign,
        projected,
        seal,
        evolution_context=evolution_context,
        approval_batch=approval_batch,
        slot_objects=slot_objects,
    )
    evolution_metadata = _evolution_publication_metadata(
        seal=seal, context=evolution_context, approval_batch=approval_batch, gap_report=gap_report,
    )
    run_identity = {
        "family_id": campaign.discovery_family_id,
        "generation_seal_id": seal.generation_seal_id,
        "result_sha256": result.result_sha256,
    }
    run_id = f"run_{sha256_json(run_identity)[:24]}"
    slot_payload = [item.model_dump(mode="json") for item in result.slot_evaluations]
    candidate_summaries = [
        {
            "slot_id": item.slot_id,
            "candidate_id": item.candidate_id,
            "candidate_spec_hash": item.candidate_spec_hash,
            "ast_hash": item.ast_hash,
            "status": item.status,
            "failure_reason": item.failure_reason,
            "hac": item.hac.model_dump(mode="json") if item.hac else None,
        }
        for item in result.slot_evaluations
    ]
    ic_candidates: dict[str, object] = {}
    portfolio_daily: dict[str, object] = {}
    portfolio_metrics: dict[str, object] = {}
    barra_candidates: dict[str, object] = {}
    for item in result.slot_evaluations:
        key = item.slot_id
        output = item.output
        ic_candidates[key] = output.get("ic", {"status": item.status, "hac": item.hac.model_dump(mode="json") if item.hac else None})
        portfolio_daily[key] = {"daily": output.get("portfolio_daily", [])}
        portfolio_metrics[key] = output.get("portfolio_metrics", output.get("portfolio", {"status": item.status}))
        barra_candidates[key] = output.get("barra", {"status": "not_available", "missing_inputs": ["campaign_result_not_provided"]})
    published_at = datetime.now(timezone.utc)
    manifest_payload = {
        "family_id": campaign.discovery_family_id,
        "generation_seal_id": seal.generation_seal_id,
        "generation_manifest_sha256": seal.manifest_sha256,
        "evaluation_policy_id": campaign.spec.evaluation_policy_id,
        "data_contract_identity_hash": seal.manifest.data_contract_identity_hash,
        "statistical_budget_hash": result.statistical_policy_hash,
        "published_at": published_at.isoformat(),
    }
    metrics_payload = {
        "version": "research-campaign-v1",
        "status": result.status,
        "run_id": run_id,
        "family_id": campaign.discovery_family_id,
        "generation_seal_id": seal.generation_seal_id,
        "evaluation_policy_id": campaign.spec.evaluation_policy_id,
        "registered_slot_count": result.registered_slot_count,
        "evaluated_slot_count": result.evaluated_slot_count,
        "failed_slot_count": result.failed_slot_count,
        "bonferroni_denominator": result.bonferroni_denominator,
        "candidate_summaries": candidate_summaries,
        "campaign_metadata": {
            "family_id": campaign.discovery_family_id,
            "generation_seal_id": seal.generation_seal_id,
            "registered_slot_count": result.registered_slot_count,
            "failed_slot_count": result.failed_slot_count,
        },
    }
    artifacts = {
        "run/metrics.json": canonical_json_bytes(metrics_payload),
        "run/input_manifest.json": canonical_json_bytes(manifest_payload),
        "campaign/slot_evaluations.json": canonical_json_bytes({"slots": slot_payload}),
        "ic/diagnostics.json": canonical_json_bytes({"candidates": ic_candidates}),
        "portfolio/daily.json": canonical_json_bytes({"candidates": portfolio_daily}),
        "portfolio/metrics.json": canonical_json_bytes({"candidates": portfolio_metrics}),
        "barra/attribution.json": canonical_json_bytes({"candidates": barra_candidates}),
    }
    artifacts.update(_campaign_candidate_spec_artifacts(artifact_root, slot_objects))
    manifest = publish_run_artifacts(artifact_root, run_id, artifacts)
    # immutable publication 已经核验成功；此后记忆失败会暴露错误，不会把运行伪装成
    # 已完成。数据库投影由 Dashboard projector 重放，绝不参与正式主存储事务。
    publish_campaign_memory(
        artifact_root,
        run_id=run_id,
        family_id=campaign.discovery_family_id,
        generation_seal_id=seal.generation_seal_id,
        source_manifest_sha256=manifest.manifest_sha256,
        data_contract_identity_hash=seal.manifest.data_contract_identity_hash,
        slot_evaluations=slot_payload,
        published_at=published_at,
        metadata={"campaign_metadata": metrics_payload["campaign_metadata"]},
        evolution_metadata=evolution_metadata,
    )
    if dashboard_store is not None:
        # Dashboard 是可重放的只读投影；数据库失败时正式 JSON/JSONL 已保留，
        # reference 仍是 pending，并将数据库错误暴露给调用方，禁止伪装成功。
        from factor_miner.dashboard_projection import project_run_artifacts
        project_run_artifacts(artifact_root, run_id, dashboard_store)  # type: ignore[arg-type]
    return CampaignPublication(run_id=run_id, manifest=manifest, artifact_root=artifact_root)

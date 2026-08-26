"""配对局部变异的影子计划、可靠性门控与不可变产物。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factor_miner.canonical import canonical_json_bytes, sha256_json
from factor_miner.compiler import compile_candidate
from factor_miner.dsl import canonical_ast_hash, validate_ast
from factor_miner.field_registry import FieldAvailabilityRegistry
from factor_miner.ledger import atomic_write_immutable
from factor_miner.long_only_protocol import LongOnlyResearchProtocol
from factor_miner.schema import FactorNode, RegisteredTrustedCandidate, registered_trusted_candidate


_HASH = r"^[0-9a-f]{64}$"
_WINDOWS = (5, 10, 20, 40, 60, 120)
_ROLLING_REPLACEMENTS = (
    "rolling_mean",
    "rolling_std",
    "rolling_min",
    "rolling_max",
)


class PairedShadowPolicy(BaseModel):
    """默认关闭的轻量影子政策。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    max_parent_count: int = Field(default=3, ge=1, le=10)
    block_count: Literal[4] = 4
    minimum_winner_agreement: float = Field(default=0.75, ge=0.5, le=1.0)
    lcb_z: float = Field(default=1.0, ge=0.0, le=3.0)
    minimum_common_dates: int = Field(default=120, ge=20)
    memory_retention_batches: int = Field(default=3, ge=1, le=10)


class LocalMutation(BaseModel):
    """只改变一个 AST 节点属性的局部变异。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mutation_kind: Literal["window", "operator"]
    node_path: tuple[int, ...]
    before_value: str
    after_value: str
    description_cn: str = Field(min_length=1)
    preference_code: Literal[
        "优先尝试更长窗口",
        "优先尝试更短窗口",
        "同单位场景优先波动算子",
        "同单位场景优先极值算子",
        "同单位场景优先均值算子",
    ]


class PairedShadowVariant(BaseModel):
    """父表达式或一个局部兄弟表达式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["parent", "sibling_1", "sibling_2"]
    candidate: RegisteredTrustedCandidate
    mutation: LocalMutation | None = None

    @model_validator(mode="after")
    def validate_role(self) -> PairedShadowVariant:
        if (self.role == "parent") != (self.mutation is None):
            raise ValueError("影子父候选不得含变异，兄弟候选必须含一个变异")
        return self


class PairedShadowProbe(BaseModel):
    """同一父候选的三条配对表达式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(pattern=r"^psprobe_[0-9a-f]{24}$")
    source_slot_id: str
    variants: tuple[PairedShadowVariant, PairedShadowVariant, PairedShadowVariant]

    @model_validator(mode="after")
    def validate_variants(self) -> PairedShadowProbe:
        if tuple(item.role for item in self.variants) != (
            "parent",
            "sibling_1",
            "sibling_2",
        ):
            raise ValueError("影子探针必须按父候选、兄弟一、兄弟二排序")
        hashes = tuple(canonical_ast_hash(item.candidate.spec.expression) for item in self.variants)
        if len(set(hashes)) != 3:
            raise ValueError("影子探针的三个 AST 必须互不相同")
        expected = sha256_json(
            {
                "source_slot_id": self.source_slot_id,
                "variants": [item.model_dump(mode="json") for item in self.variants],
            }
        )
        if self.probe_id != f"psprobe_{expected[:24]}":
            raise ValueError("影子探针身份与内容不一致")
        return self


class PairedShadowPlan(BaseModel):
    """读取发现标签前冻结的完整影子计划。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["paired-shadow-plan-v1"] = "paired-shadow-plan-v1"
    autonomous_run_id: str
    source_published_run_id: str
    discovery_start: date
    discovery_end: date
    selection_rule: Literal["ast_hash_ascending"] = "ast_hash_ascending"
    policy: PairedShadowPolicy
    probes: tuple[PairedShadowProbe, ...]
    created_at: datetime
    plan_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_plan(self) -> PairedShadowPlan:
        protocol = LongOnlyResearchProtocol()
        if (
            self.discovery_start != protocol.discovery_start
            or self.discovery_end != protocol.discovery_end
        ):
            raise ValueError("影子计划只能使用冻结发现区间")
        if len(self.probes) > self.policy.max_parent_count:
            raise ValueError("影子探针数超过冻结政策")
        payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        if self.plan_sha256 != sha256_json(payload):
            raise ValueError("影子计划内容身份不一致")
        return self


class PairedShadowProbeResult(BaseModel):
    """一个局部探针的可靠性门控终态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str
    status: Literal["accepted", "abstained", "failed"]
    common_date_count: int = Field(ge=0)
    block_winners: tuple[str, ...] = ()
    winner_candidate_id: str | None = None
    winner_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    paired_lcb: float | None = None
    preference_code: str | None = None
    reason_cn: str
    result_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_result(self) -> PairedShadowProbeResult:
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != sha256_json(payload):
            raise ValueError("影子探针结果身份不一致")
        if self.status == "accepted" and (
            self.winner_candidate_id is None
            or self.winner_agreement is None
            or self.paired_lcb is None
            or self.preference_code is None
        ):
            raise ValueError("接受的影子偏好必须包含完整门控结果")
        return self


class PairedShadowSummary(BaseModel):
    """一个自主批次的影子层汇总。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["paired-shadow-summary-v1"] = "paired-shadow-summary-v1"
    autonomous_run_id: str
    source_published_run_id: str
    plan_sha256: str = Field(pattern=_HASH)
    status: Literal["completed", "not_executed", "failed"]
    results: tuple[PairedShadowProbeResult, ...]
    accepted_preference_codes: tuple[str, ...]
    error_cn: str | None = None
    summary_sha256: str = Field(pattern=_HASH)

    @model_validator(mode="after")
    def validate_summary(self) -> PairedShadowSummary:
        accepted = tuple(
            sorted(
                {
                    str(item.preference_code)
                    for item in self.results
                    if item.status == "accepted" and item.preference_code is not None
                }
            )
        )
        if accepted != self.accepted_preference_codes:
            raise ValueError("影子汇总偏好与探针终态不一致")
        payload = self.model_dump(mode="json", exclude={"summary_sha256"})
        if self.summary_sha256 != sha256_json(payload):
            raise ValueError("影子汇总内容身份不一致")
        return self


def _walk(node: FactorNode, path: tuple[int, ...] = ()) -> tuple[tuple[tuple[int, ...], FactorNode], ...]:
    values = [(path, node)]
    for index, child in enumerate(node.args):
        values.extend(_walk(child, path + (index,)))
    return tuple(values)


def _replace(node: FactorNode, path: tuple[int, ...], replacement: FactorNode) -> FactorNode:
    if not path:
        return replacement
    index = path[0]
    children = list(node.args)
    children[index] = _replace(children[index], path[1:], replacement)
    return node.model_copy(update={"args": tuple(children)})


def _window_preference(before: int, after: int) -> str:
    return "优先尝试更长窗口" if after > before else "优先尝试更短窗口"


def _operator_preference(after: str) -> str:
    if after == "rolling_std":
        return "同单位场景优先波动算子"
    if after in {"rolling_min", "rolling_max"}:
        return "同单位场景优先极值算子"
    return "同单位场景优先均值算子"


def _raw_mutations(expression: FactorNode) -> tuple[tuple[FactorNode, LocalMutation], ...]:
    options: list[tuple[FactorNode, LocalMutation]] = []
    for path, node in _walk(expression):
        current = node.window if node.op.startswith("rolling_") else node.period if node.op in {"delay", "delta"} else None
        if current in _WINDOWS:
            position = _WINDOWS.index(current)
            neighbours = tuple(
                _WINDOWS[index]
                for index in (position - 1, position + 1)
                if 0 <= index < len(_WINDOWS)
            )
            for target in neighbours:
                replacement = node.model_copy(
                    update={
                        "window": target if node.op.startswith("rolling_") else None,
                        "period": target if node.op in {"delay", "delta"} else None,
                    }
                )
                options.append(
                    (
                        _replace(expression, path, replacement),
                        LocalMutation(
                            mutation_kind="window",
                            node_path=path,
                            before_value=str(current),
                            after_value=str(target),
                            description_cn=f"把节点窗口从{current}日改为{target}日",
                            preference_code=_window_preference(current, target),
                        ),
                    )
                )
        if node.op in _ROLLING_REPLACEMENTS and len(node.args) == 1:
            for target in _ROLLING_REPLACEMENTS:
                if target == node.op:
                    continue
                replacement = node.model_copy(update={"op": target})
                options.append(
                    (
                        _replace(expression, path, replacement),
                        LocalMutation(
                            mutation_kind="operator",
                            node_path=path,
                            before_value=node.op,
                            after_value=target,
                            description_cn=f"把局部算子从{node.op}改为{target}",
                            preference_code=_operator_preference(target),
                        ),
                    )
                )
    return tuple(options)


def _sibling_candidates(
    parent: RegisteredTrustedCandidate,
    *,
    source_run_id: str,
    registry: FieldAvailabilityRegistry,
    created_at: datetime,
) -> tuple[PairedShadowVariant, PairedShadowVariant] | None:
    allowed = tuple(item.field_id for item in registry.fields if item.eligible_for_factor)
    siblings: list[PairedShadowVariant] = []
    seen = {canonical_ast_hash(parent.spec.expression)}
    for expression, mutation in _raw_mutations(parent.spec.expression):
        try:
            metadata = validate_ast(
                expression,
                allowed_fields=allowed,
                forbidden_fields=("label", "future_return", "label_o2o_5d"),
            )
            if tuple(metadata.required_fields) != tuple(parent.spec.required_fields):
                continue
            spec = parent.spec.model_copy(
                update={
                    "expression": expression,
                    "required_fields": metadata.required_fields,
                    "max_lookback": metadata.lookback,
                    "created_at": created_at,
                    "provenance": {
                        **parent.spec.provenance,
                        "source": "paired_local_shadow",
                        "source_run_id": source_run_id,
                        "parent_candidate_id": parent.candidate_id,
                        "mutation_kind": mutation.mutation_kind,
                        "mutation_path": ".".join(str(item) for item in mutation.node_path) or "root",
                    },
                }
            )
            candidate = registered_trusted_candidate(spec)
            compile_candidate(candidate, allowed_fields=allowed)
        except Exception:
            continue
        ast_hash = canonical_ast_hash(candidate.spec.expression)
        if ast_hash in seen:
            continue
        seen.add(ast_hash)
        siblings.append(
            PairedShadowVariant(
                role=f"sibling_{len(siblings) + 1}",
                candidate=candidate,
                mutation=mutation,
            )
        )
        if len(siblings) == 2:
            return siblings[0], siblings[1]
    return None


def build_paired_shadow_plan(
    *,
    autonomous_run_id: str,
    source_published_run_id: str,
    candidates: Sequence[tuple[str, RegisteredTrustedCandidate]],
    registry: FieldAvailabilityRegistry,
    policy: PairedShadowPolicy,
    created_at: datetime,
) -> PairedShadowPlan:
    """按 AST 身份选父候选并在读取结果前冻结两个局部兄弟。"""

    protocol = LongOnlyResearchProtocol()
    ordered = sorted(
        candidates,
        key=lambda item: (canonical_ast_hash(item[1].spec.expression), item[0]),
    )
    probes: list[PairedShadowProbe] = []
    for slot_id, parent in ordered:
        siblings = _sibling_candidates(
            parent,
            source_run_id=source_published_run_id,
            registry=registry,
            created_at=created_at,
        )
        if siblings is None:
            continue
        variants = (
            PairedShadowVariant(role="parent", candidate=parent),
            siblings[0],
            siblings[1],
        )
        probe_payload = {
            "source_slot_id": slot_id,
            "variants": [item.model_dump(mode="json") for item in variants],
        }
        digest = sha256_json(probe_payload)
        probes.append(
            PairedShadowProbe(
                probe_id=f"psprobe_{digest[:24]}",
                source_slot_id=slot_id,
                variants=variants,
            )
        )
        if len(probes) == policy.max_parent_count:
            break
    payload = {
        "version": "paired-shadow-plan-v1",
        "autonomous_run_id": autonomous_run_id,
        "source_published_run_id": source_published_run_id,
        "discovery_start": protocol.discovery_start.isoformat(),
        "discovery_end": protocol.discovery_end.isoformat(),
        "selection_rule": "ast_hash_ascending",
        "policy": policy.model_dump(mode="json"),
        "probes": [item.model_dump(mode="json") for item in probes],
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }
    return PairedShadowPlan(**payload, plan_sha256=sha256_json(payload))


def _build_result(**payload: object) -> PairedShadowProbeResult:
    if isinstance(payload.get("block_winners"), tuple):
        payload["block_winners"] = list(payload["block_winners"])
    return PairedShadowProbeResult(**payload, result_sha256=sha256_json(payload))


def evaluate_paired_probe(
    probe: PairedShadowProbe,
    daily_rank_ic: Mapping[str, Mapping[date, float]],
    policy: PairedShadowPolicy,
) -> PairedShadowProbeResult:
    """在共同日期上评价三条序列并执行四区块可靠性门控。"""

    candidate_ids = tuple(item.candidate.candidate_id for item in probe.variants)
    if any(candidate_id not in daily_rank_ic for candidate_id in candidate_ids):
        return _build_result(
            probe_id=probe.probe_id,
            status="failed",
            common_date_count=0,
            reason_cn="影子评价缺少候选逐日 RankIC",
        )
    common = sorted(set.intersection(*(set(daily_rank_ic[item]) for item in candidate_ids)))
    if len(common) < policy.minimum_common_dates:
        return _build_result(
            probe_id=probe.probe_id,
            status="abstained",
            common_date_count=len(common),
            reason_cn="共同有效日期不足，影子层放弃学习",
        )
    oriented: dict[str, tuple[float, ...]] = {}
    for candidate_id in candidate_ids:
        raw = tuple(float(daily_rank_ic[candidate_id][day]) for day in common)
        if any(value != value or abs(value) == float("inf") for value in raw):
            return _build_result(
                probe_id=probe.probe_id,
                status="failed",
                common_date_count=len(common),
                reason_cn="逐日 RankIC 包含非有限值",
            )
        sign = 1.0 if mean(raw) >= 0 else -1.0
        oriented[candidate_id] = tuple(sign * value for value in raw)
    block_sizes = [len(common) // policy.block_count] * policy.block_count
    for index in range(len(common) % policy.block_count):
        block_sizes[index] += 1
    winners: list[str] = []
    block_means: dict[str, list[float]] = {item: [] for item in candidate_ids}
    start = 0
    for size in block_sizes:
        stop = start + size
        values = {
            candidate_id: mean(oriented[candidate_id][start:stop])
            for candidate_id in candidate_ids
        }
        best = max(values.values())
        tied = tuple(key for key, value in values.items() if abs(value - best) <= 1e-12)
        winners.append(tied[0] if len(tied) == 1 else "tie")
        for candidate_id, value in values.items():
            block_means[candidate_id].append(value)
        start = stop
    overall = {
        candidate_id: mean(oriented[candidate_id]) for candidate_id in candidate_ids
    }
    winner = max(overall, key=overall.get)  # type: ignore[arg-type]
    parent_id = candidate_ids[0]
    agreement = winners.count(winner) / policy.block_count
    differences = [
        left - right
        for left, right in zip(block_means[winner], block_means[parent_id], strict=True)
    ]
    standard_error = stdev(differences) / (len(differences) ** 0.5)
    lcb = mean(differences) - policy.lcb_z * standard_error
    winner_variant = next(item for item in probe.variants if item.candidate.candidate_id == winner)
    accepted = (
        winner != parent_id
        and agreement >= policy.minimum_winner_agreement
        and lcb > 0
        and winner_variant.mutation is not None
    )
    return _build_result(
        probe_id=probe.probe_id,
        status="accepted" if accepted else "abstained",
        common_date_count=len(common),
        block_winners=tuple(winners),
        winner_candidate_id=winner,
        winner_agreement=agreement,
        paired_lcb=lcb,
        preference_code=(winner_variant.mutation.preference_code if accepted and winner_variant.mutation else None),
        reason_cn=(
            "局部兄弟在四个区块中稳定胜出，接受离散结构偏好"
            if accepted
            else "胜者一致率或配对改善下界不足，影子层放弃学习"
        ),
    )


def build_paired_shadow_summary(
    plan: PairedShadowPlan,
    results: Sequence[PairedShadowProbeResult],
    *,
    status: Literal["completed", "not_executed", "failed"] = "completed",
    error_cn: str | None = None,
) -> PairedShadowSummary:
    accepted = tuple(
        sorted(
            {
                str(item.preference_code)
                for item in results
                if item.status == "accepted" and item.preference_code is not None
            }
        )
    )
    payload = {
        "version": "paired-shadow-summary-v1",
        "autonomous_run_id": plan.autonomous_run_id,
        "source_published_run_id": plan.source_published_run_id,
        "plan_sha256": plan.plan_sha256,
        "status": status,
        "results": [item.model_dump(mode="json") for item in results],
        "accepted_preference_codes": list(accepted),
        "error_cn": error_cn,
    }
    return PairedShadowSummary(**payload, summary_sha256=sha256_json(payload))


def publish_paired_shadow_plan(root: Path, plan: PairedShadowPlan) -> Path:
    """在接触发现结果前不可变发布计划。"""

    path = root / "state" / "autonomous_research" / "runs" / plan.autonomous_run_id / "paired_shadow" / "plan.json"
    atomic_write_immutable(path, canonical_json_bytes(plan.model_dump(mode="json")))
    return path


def publish_paired_shadow_summary(root: Path, summary: PairedShadowSummary) -> Path:
    """不可变发布完整影子终态。"""

    base = root / "state" / "autonomous_research" / "runs" / summary.autonomous_run_id / "paired_shadow"
    for result in summary.results:
        atomic_write_immutable(
            base / "probes" / f"{result.probe_id}.json",
            canonical_json_bytes(result.model_dump(mode="json")),
        )
    path = base / "summary.json"
    atomic_write_immutable(path, canonical_json_bytes(summary.model_dump(mode="json")))
    return path

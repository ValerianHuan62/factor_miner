"""Dashboard 自主研究命令与假设全文展示合同。"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Literal

from factor_miner.autonomous_schema import ResearchCommand, ResearchCommandType
from factor_miner.canonical import sha256_json
from factor_miner.lightweight_hypotheses import (
    LightweightHypothesisBatch,
    LightweightHypothesisDecision,
    LightweightReviewBatch,
)
from factor_miner.research_control import ResearchControlStore
from factor_miner.research_evolution import LogicalEvolutionHypothesisDraft
from factor_miner.semantic_coverage import (
    SemanticCoverage,
    SemanticPlanTags,
    build_semantic_coverage,
)
from factor_miner.lightweight_evolution import LightweightMemoryStore


def _direction(value: object) -> str:
    return {"positive": "正向", "negative": "负向", "neutral": "中性"}.get(
        str(value), str(value)
    )


def hypothesis_summary_payload(
    draft: LogicalEvolutionHypothesisDraft,
) -> dict[str, object]:
    """构造不含任何审批动作的左栏摘要。"""

    semantic = draft.semantic_plan.chinese_labels() if draft.semantic_plan else {}
    return {
        "槽位": draft.logical_slot_id,
        "主张": draft.prior_claim,
        "预期方向": _direction(draft.expected_direction),
        "机制状态": "尚未独立验证",
        "事件语义": semantic.get("事件", "未标注"),
        "上下文语义": semantic.get("上下文", "未标注"),
    }


def hypothesis_detail_payload(
    draft: LogicalEvolutionHypothesisDraft,
) -> dict[str, object]:
    """返回审批所需的九类完整正文，绝不截断。"""

    sources = tuple(
        {
            "任务编号": item.source_record_id,
            "主张片段": item.claim_fragment,
            "检索理由": item.rationale,
            "检索词": item.query_terms,
            "年份": f"{item.year_start}–{item.year_end}",
            "结果上限": item.result_limit,
        }
        for item in draft.source_records
    )
    return {
        "主张": draft.prior_claim,
        "机制": draft.mechanism,
        "预期方向": _direction(draft.expected_direction),
        "可观察代理": draft.observable_proxy,
        "机制验证方案（不改变统一回测协议）": draft.independent_verification,
        "竞争解释": draft.competing_explanations,
        "失效方式": draft.failure_modes,
        "证伪路径": draft.falsification_path,
        "来源与边界": sources,
        "金融语义标签": (
            draft.semantic_plan.chinese_labels()
            if draft.semantic_plan is not None
            else "未标注（不影响审批与候选评价）"
        ),
    }


def load_hypothesis_rows(
    artifact_root: Path,
    run_id: str,
) -> tuple[dict[str, object], ...]:
    """从 Worker 正式对象目录读取完整假设和审批决定。"""

    run_root = ResearchControlStore(artifact_root).runs_root / run_id
    objects = run_root / "objects"
    batch_path = objects / "hypotheses.json"
    if not batch_path.is_file():
        return ()
    batch = LightweightHypothesisBatch.model_validate_json(batch_path.read_bytes())
    review_path = objects / "review.json"
    review = (
        LightweightReviewBatch.model_validate_json(review_path.read_bytes())
        if review_path.is_file()
        else None
    )
    if review is not None:
        decision_items = review.decisions
    else:
        decision_items = tuple(
            LightweightHypothesisDecision.model_validate_json(path.read_bytes())
            for path in sorted((objects / "decisions").glob("H*.json"))
        )
    decisions = {item.logical_slot_id: item for item in decision_items}
    rows = []
    for draft in batch.hypotheses:
        detail = hypothesis_detail_payload(draft)
        decision = decisions.get(draft.logical_slot_id)
        rows.append(
            {
                "logical_slot_id": draft.logical_slot_id,
                "claim_zh": detail["主张"],
                "mechanism_zh": detail["机制"],
                "expected_direction": detail["预期方向"],
                "observable_proxy_zh": detail["可观察代理"],
                "independent_verification_zh": detail[
                    "机制验证方案（不改变统一回测协议）"
                ],
                "competing_explanations_zh": detail["竞争解释"],
                "failure_modes_zh": detail["失效方式"],
                "falsification_path_zh": detail["证伪路径"],
                "source_records_zh": detail["来源与边界"],
                "semantic_plan": (
                    draft.semantic_plan.model_dump(mode="json")
                    if draft.semantic_plan is not None
                    else None
                ),
                "semantic_labels_zh": detail["金融语义标签"],
                "context_sha256": batch.context_sha256,
                "hypothesis_batch_sha256": batch.batch_sha256,
                "draft_sha256": draft.draft_sha256,
                "decision": decision.decision if decision else None,
                "decision_sha256": decision.decision_sha256 if decision else None,
            }
        )
    return tuple(rows)


def load_semantic_coverage_view(
    artifact_root: Path,
    rows: tuple[dict[str, object], ...],
) -> SemanticCoverage:
    """从累计正式记忆叠加当前草案，构造 Dashboard 语义覆盖视图。"""

    historical_plans: list[SemanticPlanTags | None] = []
    historical_draft_hashes: set[str] = set()
    state_path = (
        artifact_root
        / "state"
        / "autonomous_research"
        / "evolution_current.json"
    )
    if state_path.is_file():
        payload = json.loads(state_path.read_text("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("当前演化状态必须是 JSON object")
        expected = sha256_json(
            {key: value for key, value in payload.items() if key != "state_sha256"}
        )
        if payload.get("state_sha256") != expected:
            raise ValueError("当前演化状态内容身份不一致")
        snapshot_id = payload.get("memory_snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("当前演化状态缺少 memory_snapshot_id")
        entries = LightweightMemoryStore(artifact_root).load_snapshot_entries(snapshot_id)
        for entry in entries:
            if entry.entry_kind != "hypothesis":
                continue
            historical_plans.append(entry.semantic_plan)
            historical_draft_hashes.add(entry.draft_sha256)

    current_plans: list[SemanticPlanTags | None] = []
    for row in rows:
        draft_hash = row.get("draft_sha256")
        if isinstance(draft_hash, str) and draft_hash in historical_draft_hashes:
            continue
        raw_plan = row.get("semantic_plan")
        current_plans.append(
            SemanticPlanTags.model_validate(raw_plan)
            if isinstance(raw_plan, dict)
            else None
        )
    return build_semantic_coverage(tuple(historical_plans + current_plans))


def build_review_decision(
    *,
    run_id: str,
    context_sha256: str,
    logical_slot_id: str,
    draft_sha256: str,
    decision: Literal["approved", "rejected"],
    approval_role: str,
    decided_at: datetime,
) -> LightweightHypothesisDecision:
    """构造可被规范 JSON 序列化且绑定完整身份的审批决定。"""

    payload = {
        "run_id": run_id,
        "context_sha256": context_sha256,
        "logical_slot_id": logical_slot_id,
        "draft_sha256": draft_sha256,
        "decision": decision,
        "approval_role": approval_role,
        "decided_at": decided_at,
    }
    draft_model = LightweightHypothesisDecision.model_construct(
        decision_sha256="0" * 64,
        **payload,
    )
    digest = sha256_json(
        draft_model.model_dump(mode="json", exclude={"decision_sha256"})
    )
    return LightweightHypothesisDecision(**payload, decision_sha256=digest)


def submit_start_command(
    artifact_root: Path,
    *,
    requested_by: str,
    requested_at: datetime,
) -> ResearchCommand:
    """提交一次幂等的新批次命令。"""

    command = ResearchCommand.start(
        requested_by=requested_by,
        requested_at=requested_at,
    )
    ResearchControlStore(artifact_root).submit(command)
    return command


def submit_review_decision(
    artifact_root: Path,
    *,
    decision: LightweightHypothesisDecision,
    requested_by: str,
    requested_at: datetime,
) -> ResearchCommand:
    """提交与假设全文哈希绑定的批准或拒绝决定。"""

    command = ResearchCommand.build(
        command_type=ResearchCommandType.REVIEW_DECISION,
        target_run_id=decision.run_id,
        requested_by=requested_by,
        requested_at=requested_at,
        body=decision.model_dump(mode="json"),
    )
    ResearchControlStore(artifact_root).submit(command)
    return command


def submit_freeze_command(
    artifact_root: Path,
    *,
    review: LightweightReviewBatch | None = None,
    run_id: str | None = None,
    hypothesis_batch_sha256: str | None = None,
    decision_sha256: tuple[str, ...] = (),
    requested_by: str,
    requested_at: datetime,
) -> ResearchCommand:
    """十条决定齐全后提交冻结命令。"""

    target_run_id = review.run_id if review is not None else run_id
    batch_hash = (
        review.hypothesis_batch_sha256
        if review is not None
        else hypothesis_batch_sha256
    )
    hashes = (
        tuple(item.decision_sha256 for item in review.decisions)
        if review is not None
        else decision_sha256
    )
    if target_run_id is None or batch_hash is None or len(hashes) != 10:
        raise ValueError("十条假设决定未齐，不能冻结审批")
    command = ResearchCommand.build(
        command_type=ResearchCommandType.FREEZE_REVIEW,
        target_run_id=target_run_id,
        requested_by=requested_by,
        requested_at=requested_at,
        body={
            "hypothesis_batch_sha256": batch_hash,
            "review_sha256": review.review_sha256 if review is not None else None,
            "decision_sha256": hashes,
        },
    )
    ResearchControlStore(artifact_root).submit(command)
    return command


def submit_approve_remaining_and_freeze(
    artifact_root: Path,
    *,
    run_id: str,
    rows: tuple[dict[str, object], ...],
    approval_role: str,
    requested_by: str,
    requested_at: datetime,
) -> tuple[tuple[ResearchCommand, ...], ResearchCommand]:
    """一键批准所有未决定假设，并保留已有决定后冻结整批审批。"""

    expected_slots = tuple(f"H{index:02d}" for index in range(1, 11))
    ordered = tuple(sorted(rows, key=lambda item: str(item["logical_slot_id"])))
    if tuple(str(item["logical_slot_id"]) for item in ordered) != expected_slots:
        raise ValueError("批量审批必须包含且仅包含 H01–H10")
    batch_hashes = {str(item["hypothesis_batch_sha256"]) for item in ordered}
    context_hashes = {str(item["context_sha256"]) for item in ordered}
    if len(batch_hashes) != 1 or len(context_hashes) != 1:
        raise ValueError("十条假设不属于同一正式批次")

    commands = []
    decision_hashes = []
    for row in ordered:
        existing = row.get("decision")
        if existing in {"approved", "rejected"}:
            digest = row.get("decision_sha256")
            if not digest:
                raise ValueError("已有审批决定缺少正式身份，不能冻结")
            decision_hashes.append(str(digest))
            continue
        if existing is not None:
            raise ValueError(f"未知审批状态：{existing}")
        decision = build_review_decision(
            run_id=run_id,
            context_sha256=str(row["context_sha256"]),
            logical_slot_id=str(row["logical_slot_id"]),
            draft_sha256=str(row["draft_sha256"]),
            decision="approved",
            approval_role=approval_role,
            decided_at=requested_at,
        )
        commands.append(
            submit_review_decision(
                artifact_root,
                decision=decision,
                requested_by=requested_by,
                requested_at=requested_at,
            )
        )
        decision_hashes.append(decision.decision_sha256)

    freeze = submit_freeze_command(
        artifact_root,
        run_id=run_id,
        hypothesis_batch_sha256=next(iter(batch_hashes)),
        decision_sha256=tuple(decision_hashes),
        requested_by=requested_by,
        requested_at=requested_at + timedelta(microseconds=1),
    )
    return tuple(commands), freeze


def submit_resume_command(
    artifact_root: Path,
    *,
    run_id: str,
    failed_state_sha256: str,
    requested_by: str,
    requested_at: datetime,
) -> ResearchCommand:
    """提交与当前失败快照绑定的继续命令。"""

    command = ResearchCommand.build(
        command_type=ResearchCommandType.RESUME,
        target_run_id=run_id,
        requested_by=requested_by,
        requested_at=requested_at,
        body={"failed_state_sha256": failed_state_sha256},
    )
    ResearchControlStore(artifact_root).submit(command)
    return command

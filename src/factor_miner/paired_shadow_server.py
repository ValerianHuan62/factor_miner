"""公司 Linux 上配对局部变异影子层的发现期评价端口。"""

from __future__ import annotations

from datetime import date

from factor_miner.data_source import FactorInputRequest
from factor_miner.paired_shadow import (
    PairedShadowPlan,
    PairedShadowProbeResult,
    evaluate_paired_probe,
)
from factor_miner.pilot_runner import (
    _load_pilot_calendar,
    _load_pilot_market_open,
    build_open_to_open_ic_panel,
    compute_fixed_signal_panels,
    evaluate_discovery_daily_rank_ic,
)
from factor_miner.pilot_schema import (
    PilotFixedCandidate,
    PilotFixedCandidateFile,
    PilotRunRequest,
    PilotSourcePaths,
)
from factor_miner.pilot_sources import PilotQuantLakeFactorInputSource
from factor_miner.schema import EvaluationPolicySpec


def evaluate_paired_shadow_plan(
    plan: PairedShadowPlan,
    *,
    paths: PilotSourcePaths,
    request: PilotRunRequest,
    evaluation_policy: EvaluationPolicySpec,
) -> tuple[PairedShadowProbeResult, ...]:
    """只读取冻结发现区间，计算父候选与兄弟候选的逐日配对 RankIC。"""

    if not plan.policy.enabled:
        return ()
    if not plan.probes:
        return ()
    variants = tuple(
        variant
        for probe in plan.probes
        for variant in probe.variants
    )
    candidates = PilotFixedCandidateFile(
        version="pilot-candidates-v2",
        candidates=tuple(
            PilotFixedCandidate(
                candidate_id=variant.candidate.candidate_id,
                spec=variant.candidate.spec,
            )
            for variant in variants
        ),
    )
    required_fields = tuple(
        sorted(
            {
                field
                for variant in variants
                for field in variant.candidate.spec.required_fields
            }
        )
    )
    warmup = max(variant.candidate.spec.max_lookback for variant in variants)
    source = PilotQuantLakeFactorInputSource(
        paths,
        code_commit=request.code_commit,
        config_hash=request.config_hash,
        discovery_only=True,
    )
    provenance = source.inspect_inputs()
    if provenance.resolved_release_id != request.data_release_id:
        raise ValueError("影子计划的数据发布版本与 Pilot 请求不一致")
    factor_request = FactorInputRequest(
        start=plan.discovery_start,
        end=plan.discovery_end,
        required_fields=required_fields,
        warmup_observations=warmup,
    )
    panels = compute_fixed_signal_panels(
        candidates,
        source,
        factor_request,
        request.artifact_root / "work" / "paired_shadow" / plan.plan_sha256[:24],
    )
    calendar = _load_pilot_calendar(paths, plan.discovery_start, plan.discovery_end)
    market = _load_pilot_market_open(paths, plan.discovery_start, plan.discovery_end)
    daily: dict[str, dict[date, float]] = {}
    for panel in panels:
        labeled = build_open_to_open_ic_panel(
            panel,
            market,
            calendar,
            horizons=(5,),
        )
        daily[panel.candidate_id] = dict(
            evaluate_discovery_daily_rank_ic(labeled, evaluation_policy)
        )
    return tuple(
        evaluate_paired_probe(probe, daily, plan.policy)
        for probe in plan.probes
    )

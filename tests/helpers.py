"""schema 测试使用的稳定构造器。"""

from datetime import date, datetime, timezone
import re

from factor_miner.schema import (
    AvailabilitySpec,
    CampaignSpec,
    CandidateFactorSpec,
    FactorNode,
    HypothesisSpec,
    HypothesisSlot,
    IncrementalEvaluationPolicySpec,
    OrthogonalizationPolicySpec,
    ReferenceFactorLibrarySpec,
    RegisteredCandidate,
    RegisteredReferenceFactorLibrary,
    RegisteredTrustedCandidate,
    ResearchFamilySpec,
    TrustedCandidateFactorSpec,
    TrustedVisibleCampaignSpec,
    evaluation_policy_id,
    reference_factor_library_id,
    registered_reference_factor_library,
    registered_research_family,
    registered_candidate,
    registered_trusted_candidate,
)
from factor_miner.policy import company_a_share_visible_policy


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """移除 CLI 测试输出中与终端有关的 ANSI 样式码。"""

    return ANSI_ESCAPE.sub("", text)


def valid_candidate() -> CandidateFactorSpec:
    """构造一份合法的、可重复生成的候选因子 spec。

    返回：
        带完整事前假设、规范化 AST 和 provenance 的候选 spec。
    """

    return CandidateFactorSpec(
        hypothesis=HypothesisSpec(
            claim="过去二十日的收盘价趋势与下一阶段收益同向。",
            mechanism="价格趋势可能反映信息扩散速度差异。",
            expected_sign="positive",
            observable_proxy="二十日收盘价变化率。",
            independent_verification="使用独立成交量与公告数据检验趋势机制。",
            competing_explanations=("行业暴露", "市场风险暴露"),
            baseline_reference="close 的二十日简单动量。",
            failure_modes=("流动性不足", "价格序列缺失"),
            falsification_path="若独立机制检验失败或方向稳定反转，则否定该机制。",
            source_refs=("合成测试来源：仅用于单元测试，不作为研究引用。",),
        ),
        expression=FactorNode(
            op="delta",
            args=(FactorNode(op="field", field="close"),),
            period=20,
        ),
        required_fields=("close",),
        max_lookback=20,
        availability="next_open",
        created_at=datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
        provenance={
            "author": "人工登记",
            "source": "synthetic-test",
        },
    )


def valid_registered_candidate() -> RegisteredCandidate:
    """构造一份合法的已登记候选。"""

    return registered_candidate(valid_candidate())


def valid_campaign() -> CampaignSpec:
    """构造一份覆盖一个已登记候选的合法 campaign。

    返回：
        包含冻结评价协议的 campaign spec。
    """

    candidate_id = valid_registered_candidate().candidate_id
    return CampaignSpec(
        visible_start=date(2020, 1, 1),
        visible_end=date(2020, 12, 31),
        candidate_ids=(candidate_id,),
        max_hypotheses=1,
        alpha=0.05,
        label_column="label_o2o_5d",
        rank_mask_column="valid_for_factor_rank",
        hac_max_lags=5,
        min_valid_dates=60,
        min_names_per_date=20,
        min_median_coverage=0.8,
        max_abs_output_correlation=0.8,
        reference_factor_columns=("reference_momentum",),
    )


def valid_trusted_candidate() -> TrustedCandidateFactorSpec:
    """构造不使用自由文本可得性的 V0.1 候选。"""

    legacy = valid_candidate()
    return TrustedCandidateFactorSpec(
        hypothesis=legacy.hypothesis,
        expression=legacy.expression,
        required_fields=legacy.required_fields,
        max_lookback=legacy.max_lookback,
        availability=AvailabilitySpec(
            observation="close_t",
            decision="after_close_t",
            earliest_trade="open_t_plus_1",
        ),
        created_at=legacy.created_at,
        provenance=legacy.provenance,
    )


def valid_registered_trusted_candidate() -> RegisteredTrustedCandidate:
    """构造内容寻址的 V0.1 候选记录。"""

    return registered_trusted_candidate(valid_trusted_candidate())


def valid_research_family() -> ResearchFamilySpec:
    """构造覆盖单个 trusted candidate 的全局研究 family。"""

    candidate = valid_registered_trusted_candidate()
    policy = company_a_share_visible_policy()
    return ResearchFamilySpec(
        evaluation_policy_id=evaluation_policy_id(policy),
        global_hypothesis_budget=100,
        slots=(HypothesisSlot(slot_number=1, candidate_id=candidate.candidate_id),),
        created_at=datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc),
        provenance={"author": "人工登记", "source": "synthetic-test"},
    )


def valid_trusted_campaign() -> TrustedVisibleCampaignSpec:
    """构造引用冻结 policy 与 research family 的 V0.1 campaign。"""

    candidate = valid_registered_trusted_candidate()
    family = registered_research_family(valid_research_family())
    return TrustedVisibleCampaignSpec(
        visible_start=date(2020, 1, 1),
        visible_end=date(2020, 12, 31),
        candidate_ids=(candidate.candidate_id,),
        evaluation_policy_id=valid_research_family().evaluation_policy_id,
        research_family_id=family.research_family_id,
    )


def valid_reference_factor_library() -> ReferenceFactorLibrarySpec:
    """构造冻结且时点正确的 V0.2 参考因子库。"""

    return ReferenceFactorLibrarySpec(
        reference_manifest_id="company_a_share_reference_v1",
        reference_manifest_sha256="c" * 64,
        reference_factor_ids=(
            "reference_value",
            "reference_quality",
            "reference_momentum",
        ),
        factor_value_column="raw_factor",
        basis_selection="frozen_full_basis",
        point_in_time=True,
        created_at=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
        provenance={"author": "人工登记", "source": "synthetic-test"},
    )


def valid_registered_reference_factor_library() -> RegisteredReferenceFactorLibrary:
    """构造内容寻址的 V0.2 参考因子库记录。"""

    return registered_reference_factor_library(valid_reference_factor_library())


def valid_incremental_policy() -> IncrementalEvaluationPolicySpec:
    """构造与测试参考库严格匹配的 V0.2 评价政策。"""

    library = valid_registered_reference_factor_library()
    legacy = company_a_share_visible_policy()
    return IncrementalEvaluationPolicySpec(
        **legacy.model_dump(
            mode="python",
            exclude={
                "policy_version",
                "reference_manifest_id",
                "reference_factor_ids",
            },
        ),
        reference_manifest_id=library.spec.reference_manifest_id,
        reference_factor_ids=library.spec.reference_factor_ids,
        reference_factor_library_id=reference_factor_library_id(library.spec),
        orthogonalization=OrthogonalizationPolicySpec(
            method="cross_sectional_rank_ols",
            include_intercept=True,
            min_reference_coverage=0.8,
            min_cross_sectional_excess_names=20,
            max_condition_number=1e8,
            min_median_residual_variance_ratio=0.05,
            min_abs_mean_residual_rank_ic=0.01,
        ),
    )

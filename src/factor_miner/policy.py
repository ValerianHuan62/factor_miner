"""代码解析的公司 A 股可信可见评价协议。"""

from factor_miner.schema import (
    AvailabilitySpec,
    IncrementalEvaluationPolicySpec,
    OrthogonalizationPolicySpec,
    EvaluationPolicySpec,
    RegisteredReferenceFactorLibrary,
    UniverseSpec,
    validate_incremental_policy_library,
)


def company_a_share_visible_policy() -> EvaluationPolicySpec:
    """返回唯一受支持的公司 A 股 V0.1 visible policy。"""

    return EvaluationPolicySpec(
        label_id="company_a_share_o2o_5d_v1",
        label_column="label_o2o_5d",
        label_horizon_sessions=5,
        label_formula_version="open_t_plus_6_over_open_t_plus_1_v1",
        rank_mask_column="valid_for_factor_rank",
        universe=UniverseSpec(
            universe_id="company_a_share_all_pit",
            version="state_table_v1",
            membership_column="valid_for_factor_rank",
            membership_available_at="close_t",
            point_in_time=True,
        ),
        availability=AvailabilitySpec(
            observation="close_t",
            decision="after_close_t",
            earliest_trade="open_t_plus_1",
        ),
        alpha=0.05,
        hac_method="ols_hac",
        hac_max_lags=5,
        min_valid_dates=60,
        min_names_per_date=20,
        min_median_coverage=0.8,
        min_abs_mean_rank_ic=0.01,
        neutralization="none",
        max_abs_output_correlation=0.8,
        reference_manifest_id="company_a_share_reference_v1",
        reference_factor_ids=("reference_momentum_20d",),
    )


def company_a_share_incremental_policy(
    library: RegisteredReferenceFactorLibrary,
) -> IncrementalEvaluationPolicySpec:
    """返回与冻结参考库严格绑定的公司 A 股 V0.2 评价政策。"""

    legacy = company_a_share_visible_policy()
    payload = legacy.model_dump(
        mode="python",
        exclude={
            "policy_version",
            "reference_manifest_id",
            "reference_factor_ids",
        },
    )
    policy = IncrementalEvaluationPolicySpec(
        **payload,
        reference_manifest_id=library.spec.reference_manifest_id,
        reference_factor_ids=library.spec.reference_factor_ids,
        reference_factor_library_id=library.reference_factor_library_id,
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
    validate_incremental_policy_library(policy, library)
    return policy

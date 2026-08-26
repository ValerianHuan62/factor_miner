"""V0.4 覆盖图谱不可变合同测试。"""

from datetime import date, datetime, timezone
import unittest

from pydantic import ValidationError

from factor_miner.coverage_schema import (
    CoverageClusterPolicy,
    CoverageGraphSpec,
    CoveragePairPolicy,
    CoverageStructuralPolicy,
    registered_coverage_graph_spec,
)
from factor_miner.regime_schema import RegimeAnnotation


def _annotation(state: int, label: str) -> RegimeAnnotation:
    return RegimeAnnotation(
        regime_snapshot_id="regsnap_" + "a" * 24,
        canonical_state_id=state,
        economic_label=label,
        description=f"{label}的人工经济解释",
        author="researcher",
        created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


def _spec() -> CoverageGraphSpec:
    return CoverageGraphSpec(
        factor_catalog_sha256="1" * 64,
        daily_ic_manifest_sha256="2" * 64,
        regime_snapshot_id="regsnap_" + "a" * 24,
        regime_annotations=(
            _annotation(0, "高波动下跌·弱宽度"),
            _annotation(1, "低波动平稳·宽度中性"),
        ),
        evaluation_policy_id="evalpol_" + "3" * 24,
        data_release_id="quantlake-visible-v1",
        label_id="label_o2o_5d_v1",
        visible_start=date(2020, 1, 1),
        visible_end=date(2025, 12, 31),
        pair_policy=CoveragePairPolicy(),
        structural_policy=CoverageStructuralPolicy(),
        cluster_policy=CoverageClusterPolicy(),
        algorithm_version="coverage-v0.4.0",
        random_seed=0,
        runtime_provenance={
            "code_commit": "4" * 40,
            "config_hash": "5" * 64,
            "uv_lock_sha256": "6" * 64,
            "release_manifest_sha256": "7" * 64,
        },
        created_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


class CoverageSchemaTest(unittest.TestCase):
    """覆盖图谱 Spec 必须在结果产生前稳定且自洽。"""

    def test_registered_spec_is_content_addressed(self) -> None:
        first = registered_coverage_graph_spec(_spec())
        second = registered_coverage_graph_spec(_spec())

        self.assertEqual(first, second)
        self.assertTrue(first.coverage_spec_id.startswith("covspec_"))
        self.assertEqual(len(first.spec_hash), 64)

    def test_every_state_has_one_chinese_annotation_for_this_snapshot(self) -> None:
        payload = _spec().model_dump()
        payload["regime_annotations"] = (
            _annotation(0, "高波动下跌·弱宽度"),
            _annotation(0, "重复名称"),
        )

        with self.assertRaisesRegex(ValidationError, "canonical state"):
            CoverageGraphSpec.model_validate(payload)

    def test_annotation_cannot_reference_another_snapshot(self) -> None:
        payload = _spec().model_dump()
        wrong = _annotation(0, "错误快照").model_copy(
            update={"regime_snapshot_id": "regsnap_" + "b" * 24}
        )
        payload["regime_annotations"] = (wrong, _annotation(1, "正常"))

        with self.assertRaisesRegex(ValidationError, "状态注释"):
            CoverageGraphSpec.model_validate(payload)

    def test_visible_interval_must_be_forward(self) -> None:
        payload = _spec().model_dump()
        payload["visible_start"] = date(2026, 1, 1)
        payload["visible_end"] = date(2025, 12, 31)

        with self.assertRaisesRegex(ValidationError, "visible_end"):
            CoverageGraphSpec.model_validate(payload)


if __name__ == "__main__":
    unittest.main()

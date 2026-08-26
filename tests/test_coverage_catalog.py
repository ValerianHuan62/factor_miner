"""现有生产因子 YAML 到覆盖节点的确定性导入测试。"""

from pathlib import Path
import tempfile
import unittest

from factor_miner.coverage_catalog import load_legacy_factor_catalog
from factor_miner.errors import FactorMinerError


YAML_SAMPLE = """\
- factor_id: alpha_003_n10_v1
  factor_name_cn: Alpha#003 开盘价与成交量截面排名相关系数
  category: volume_price
  subcategory: volume_price_corr
  description: 价格与成交量关系
  formula_expr: factor = -Corr_10(Rank_cs(O), Rank_cs(V))
  lookback_window: 15
  lag_days: 1
  input_fields: [open, volume]
  preprocess_method: mad_zscore_cs
  neutralization: none
  status: active
  param_n: 10
  mean_ic: 0.031859
  icir: 0.500362
- factor_id: alpha_007_n50_v1
  factor_name_cn: Alpha#007 成交量加权价格差异信号
  category: momentum
  subcategory: price_momentum
  description: 量价门控动量
  formula_expr: 若 V > MA_50(V)，factor = -Rank_ts_50(abs(C-C_{t-50})) * sign(C-C_{t-50})
  lookback_window: 55
  lag_days: 1
  input_fields: [close, volume]
  preprocess_method: mad_zscore_cs
  neutralization: none
  status: active
  param_n: 50
  mean_ic: -0.037024
  icir: -0.426353
"""


class CoverageCatalogTest(unittest.TestCase):
    """自由文本公式只转成元数据，不会被当成可执行 AST。"""

    def test_loads_legacy_yaml_with_stable_structure_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factors.yaml"
            path.write_text(YAML_SAMPLE, encoding="utf-8")

            nodes = load_legacy_factor_catalog(path)

        self.assertEqual([node.factor_id for node in nodes], [
            "alpha_003_n10_v1",
            "alpha_007_n50_v1",
        ])
        first, second = nodes
        self.assertEqual(first.structure_source, "legacy_formula_metadata")
        self.assertIsNone(first.ast_hash)
        self.assertEqual(first.input_fields, ("open", "volume"))
        self.assertEqual(first.operator_tags, ("corr", "rank_cs"))
        self.assertEqual(first.windows, (10,))
        self.assertEqual(len(first.formula_hash), 64)
        self.assertEqual(first.orientation_sign, 1)
        self.assertEqual(first.orientation_source, "legacy_visible_mean_ic")
        self.assertEqual(second.orientation_sign, -1)
        self.assertEqual(
            second.operator_tags,
            ("abs", "ma", "rank_ts", "sign"),
        )
        self.assertEqual(second.windows, (50,))

    def test_rejects_duplicate_factor_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factors.yaml"
            path.write_text(YAML_SAMPLE + YAML_SAMPLE, encoding="utf-8")

            with self.assertRaisesRegex(FactorMinerError, "重复"):
                load_legacy_factor_catalog(path)

    def test_rejects_mapping_instead_of_yaml_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factors.yaml"
            path.write_text("factor_id: alpha_bad\n", encoding="utf-8")

            with self.assertRaisesRegex(FactorMinerError, "YAML list"):
                load_legacy_factor_catalog(path)


if __name__ == "__main__":
    unittest.main()

from datetime import date, timedelta
from pathlib import Path
import unittest

import polars as pl

from factor_miner.compiler import (
    build_polars_expr,
    compile_candidate,
)
from factor_miner.schema import FactorNode
from tests.helpers import valid_registered_candidate


def synthetic_panel() -> pl.DataFrame:
    """生成两资产、三十日期的程序合成行情面板。"""

    rows: list[dict[str, object]] = []
    start_date = date(2020, 1, 1)
    for asset_index, asset in enumerate(("A", "B")):
        for day_index in range(30):
            close = float(100 + asset_index * 10 + day_index)
            rows.append(
                {
                    "date": start_date + timedelta(days=day_index),
                    "asset": asset,
                    "close": close,
                    "volume": float(10 + day_index),
                    "denominator": 0.0 if day_index == 3 else 2.0,
                }
            )
    return pl.DataFrame(rows).sort(["asset", "date"])


class CompilerTest(unittest.TestCase):
    """Polars 表达式映射和确定性编译测试。"""

    def test_rolling_mean_matches_manual_grouped_result(self) -> None:
        """验证 rolling_mean 使用每个资产自身的历史窗口。"""
        node = FactorNode(
            op="rolling_mean",
            args=(FactorNode(op="field", field="close"),),
            window=5,
        )
        result = synthetic_panel().with_columns(
            build_polars_expr(node).alias("factor")
        )
        expected = (
            synthetic_panel()
            .with_columns(
                pl.col("close")
                .rolling_mean(window_size=5, min_samples=5)
                .over("asset")
                .alias("expected")
            )
            .get_column("expected")
        )
        self.assertTrue(result.get_column("factor").equals(expected))

    def test_delay_and_delta_match_manual_columns(self) -> None:
        """验证 delay 和 delta 的历史方向。"""
        panel = synthetic_panel()
        result = panel.with_columns(
            build_polars_expr(
                FactorNode(
                    op="delay",
                    args=(FactorNode(op="field", field="close"),),
                    period=1,
                )
            ).alias("delayed"),
            build_polars_expr(
                FactorNode(
                    op="delta",
                    args=(FactorNode(op="field", field="close"),),
                    period=5,
                )
            ).alias("changed"),
        )
        expected = panel.with_columns(
            pl.col("close").shift(1).over("asset").alias("expected_delayed"),
            pl.col("close").diff(5).over("asset").alias("expected_changed"),
        )
        self.assertTrue(result.get_column("delayed").equals(expected.get_column("expected_delayed")))
        self.assertTrue(result.get_column("changed").equals(expected.get_column("expected_changed")))

    def test_safe_division_returns_null_at_zero(self) -> None:
        """验证除数为零时安全除法返回 null。"""
        node = FactorNode(
            op="div",
            args=(
                FactorNode(op="field", field="close"),
                FactorNode(op="field", field="denominator"),
            ),
        )
        result = synthetic_panel().with_columns(
            build_polars_expr(node).alias("ratio")
        )
        zero_rows = result.filter(pl.col("denominator") == 0.0)
        self.assertEqual(zero_rows.get_column("ratio").null_count(), len(zero_rows))
        non_zero_rows = result.filter(pl.col("denominator") != 0.0)
        self.assertTrue((non_zero_rows.get_column("ratio") > 0).all())

    def test_compile_plan_is_deterministic(self) -> None:
        """验证同一个候选重复编译得到相同计划和哈希。"""
        candidate = valid_registered_candidate()
        first = compile_candidate(candidate, {"close", "volume"})
        second = compile_candidate(candidate, {"close", "volume"})
        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))
        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertEqual(first.ast_hash, second.ast_hash)

    def test_compiled_plan_records_metadata(self) -> None:
        """验证编译计划记录候选、字段、lookback 和可得性。"""
        plan = compile_candidate(valid_registered_candidate(), {"close", "volume"})
        self.assertTrue(plan.candidate_id.startswith("cand_"))
        self.assertEqual(plan.required_fields, ("close",))
        self.assertEqual(plan.required_lookback, 20)
        self.assertEqual(plan.availability, "next_open")
        self.assertEqual(plan.expression["op"], "delta")

    def test_compiler_source_has_no_dynamic_execution(self) -> None:
        """验证编译器源码不包含动态执行或动态导入。"""
        source = Path(__file__).parents[1].joinpath(
            "src", "factor_miner", "compiler.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("eval(", "exec(", "importlib", "__import__"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

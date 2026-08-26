"""动态未来依赖探针测试。"""

from datetime import date, timedelta
from unittest.mock import patch
import unittest

import polars as pl

from factor_miner.compiler import compile_candidate
from factor_miner.errors import FactorMinerError
from factor_miner.lookahead import run_lookahead_probes
from tests.helpers import valid_registered_candidate


def _input_frame() -> pl.DataFrame:
    """生成不包含任何标签的单资产输入。"""

    return pl.DataFrame(
        {
            "date": [date(2020, 1, 1) + timedelta(days=index) for index in range(35)],
            "asset": ["AAA"] * 35,
            "close": [float(index + 1) for index in range(35)],
            "valid_for_factor_compute": [True] * 35,
        }
    )


class LookaheadProbeTest(unittest.TestCase):
    """验证前缀截断和未来扰动能识别未来依赖。"""

    def test_whitelist_plan_passes_prefix_and_future_perturbation(self) -> None:
        """合法二十日动量在未来变化时历史输出保持不变。"""

        plan = compile_candidate(valid_registered_candidate(), {"close"})
        result = run_lookahead_probes(
            plan,
            _input_frame(),
            checkpoints=(date(2020, 1, 26), date(2020, 1, 30)),
            seed=17,
        )
        self.assertTrue(result.passed)
        self.assertGreater(result.compared_rows, 0)
        self.assertEqual(result.seed, 17)

    def test_future_dependent_runner_is_rejected(self) -> None:
        """人为注入的负位移表达式必须被动态探针拒绝。"""

        plan = compile_candidate(valid_registered_candidate(), {"close"})

        def future_runner(_plan, frame: pl.DataFrame) -> pl.DataFrame:
            return frame.sort(["date", "asset"]).with_columns(
                pl.col("close").shift(-1).over("asset").alias("raw_factor")
            ).select(["date", "asset", "raw_factor"])

        with patch("factor_miner.lookahead._evaluate_plan", side_effect=future_runner):
            with self.assertRaisesRegex(FactorMinerError, "LOOKAHEAD_DETECTED"):
                run_lookahead_probes(
                    plan,
                    _input_frame(),
                    checkpoints=(date(2020, 1, 26),),
                    seed=17,
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION_PATH = Path("dashboard/migrations/012_target_long_factor_metrics.sql")


class TargetLongDashboardSchemaMigrationTests(unittest.TestCase):
    """公开指标只表达发现方向、确认期和最近期目标多头结果。"""

    def test_factor_description_records_discovered_direction_in_chinese(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8").lower()
        self.assertIn("hypothesis_direction", sql)
        self.assertIn("discovered_direction", sql)
        self.assertIn("direction_relation", sql)
        self.assertIn("'正向', '负向'", sql)
        self.assertNotIn("sha256", sql)
        self.assertNotIn("hash", sql)

    def test_confirmation_and_recent_metrics_are_both_complete(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8").lower()
        metrics = (
            "ic_mean",
            "rank_ic_mean",
            "ic_std",
            "rank_ic_std",
            "ic_ir",
            "rank_ic_ir",
            "ic_hac_t",
            "rank_ic_hac_t",
            "win_rate",
            "annualized_return",
            "max_drawdown",
            "sharpe",
            "information_ratio",
        )
        for window in ("confirmation", "recent"):
            for metric in metrics:
                if window == "recent" and metric == "win_rate":
                    continue
                self.assertRegex(
                    sql,
                    rf"\b{window}_{metric}\b\s+double precision\s+not null",
                )
        self.assertNotIn("recent_win_rate", sql)
        for forbidden in (
            "portfolio_daily",
            "portfolio_metrics",
            "barra",
            "worker",
            "heartbeat",
        ):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()

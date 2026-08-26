from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION_PATH = Path("dashboard/migrations/013_simplify_recent_factor_metrics.sql")


class RecentFactorMetricsSchemaMigrationTests(unittest.TestCase):
    """数据库只展示最近期指标，并可联查中文因子说明。"""

    def test_new_table_uses_simple_complete_metric_names(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8").lower()
        create_table = sql.split(
            "create table public.factor_metrics (", maxsplit=1
        )[1].split(");", maxsplit=1)[0]
        for column in (
            "ic_mean", "rank_ic_mean", "ic_std", "rank_ic_std",
            "ic_ir", "rank_ic_ir", "ic_hac_t", "rank_ic_hac_t",
            "annualized_return", "max_drawdown", "sharpe",
            "information_ratio",
        ):
            self.assertRegex(
                create_table,
                rf"\b{column}\b\s+double precision\s+not null",
            )
        self.assertNotIn("confirmation_", create_table)
        self.assertNotIn("recent_", create_table)

    def test_existing_rows_are_copied_from_recent_window(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8").lower()
        self.assertIn("recent_ic_mean", sql)
        self.assertIn("recent_rank_ic_mean", sql)
        self.assertIn("recent_annualized_return", sql)
        self.assertIn("2025-01-01", sql)
        self.assertIn("2026-06-30", sql)

    def test_foreign_key_and_joined_view_expose_factor_descriptions(self) -> None:
        sql = re.sub(r"\s+", " ", MIGRATION_PATH.read_text("utf-8").lower())
        self.assertIn(
            "constraint factor_metrics_factor_id_fkey foreign key (factor_id) "
            "references public.factors(factor_id) on delete cascade",
            sql,
        )
        self.assertIn("create or replace view public.factor_metrics_detail as", sql)
        self.assertIn("join public.factors as f on f.factor_id = m.factor_id", sql)
        for column in ("f.hypothesis", "f.mechanism", "f.formula", "f.calculation"):
            self.assertIn(column, sql)


if __name__ == "__main__":
    unittest.main()

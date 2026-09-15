"""A 股与美股 Dashboard 读模型隔离迁移测试。"""

from pathlib import Path
import unittest


MIGRATION = Path("dashboard/migrations/014_market_isolation.sql")


class MarketIsolationSchemaMigrationTest(unittest.TestCase):
    """市场身份必须贯穿因子、运行、编号和可见评价。"""

    def test_market_identity_and_visible_evaluation_are_explicit(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8").lower()

        self.assertIn("alter table public.factors", sql)
        self.assertIn("add column market_id text not null", sql)
        self.assertIn("primary key (market_id, source_candidate_id)", sql)
        self.assertIn("create table public.visible_candidate_evaluations", sql)
        self.assertIn("false as has_portfolio", sql)
        self.assertIn("null::double precision as annualized_return", sql)
        self.assertIn("null::double precision as sharpe", sql)

    def test_factor_catalog_query_filters_market(self) -> None:
        source = Path("dashboard/pg_store.py").read_text(encoding="utf-8")

        self.assertIn("FROM public.factor_metrics_detail\n                WHERE market_id = %s", source)
        self.assertIn("WHERE market_id = %s\n                  AND source_candidate_id", source)


if __name__ == "__main__":
    unittest.main()

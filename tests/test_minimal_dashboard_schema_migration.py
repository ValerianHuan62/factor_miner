from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION_PATH = Path("dashboard/migrations/011_rebuild_minimal_dashboard_schema.sql")


class MinimalDashboardSchemaMigrationTests(unittest.TestCase):
    def test_public_schema_is_rebuilt_as_four_small_business_tables(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8")
        normalized = re.sub(r"\s+", " ", sql.lower())

        self.assertIn("drop schema public cascade", normalized)
        self.assertIn("create schema public", normalized)

        public_tables = set(
            re.findall(
                r"create table public\.([a-z_]+)\s*\(",
                normalized,
            )
        )
        self.assertEqual(
            public_tables,
            {
                "factors",
                "factor_metrics",
                "research_runs",
                "research_hypotheses",
            },
        )

        forbidden_public_terms = {
            "hash",
            "sha256",
            "snapshot",
            "portfolio",
            "barra",
            "memory",
            "worker",
            "heartbeat",
            "artifact",
        }
        public_section = normalized.split("create schema factor_miner_internal", maxsplit=1)[0]
        for term in forbidden_public_terms:
            self.assertNotIn(term, public_section)

    def test_factor_metrics_are_complete_and_never_null(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8").lower()
        required_metrics = {
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
        }
        for column in required_metrics:
            self.assertRegex(sql, rf"\b{column}\b\s+double precision\s+not null")

    def test_only_internal_id_map_keeps_source_identifier(self) -> None:
        sql = MIGRATION_PATH.read_text("utf-8").lower()
        self.assertIn("create schema factor_miner_internal", sql)
        self.assertIn("create table factor_miner_internal.factor_id_map", sql)
        self.assertRegex(sql, r"source_candidate_id\s+text\s+primary key")
        self.assertRegex(sql, r"factor_id\s+text\s+not null\s+unique")
        self.assertIn("check (factor_id ~ '^huan[0-9]{3,}$')", sql)


if __name__ == "__main__":
    unittest.main()

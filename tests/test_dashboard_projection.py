"""Dashboard 只读投影测试。"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from factor_miner.dashboard_projection import (
    _expression_details,
    _factor_descriptor,
    candidate_reference_id,
    load_dashboard_snapshot,
    project_run_artifacts,
)
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.errors import FactorMinerError
from factor_miner.portfolio_artifacts import publish_run_artifacts
from dashboard.ui import candidate_name
from dashboard.pg_store import (
    PostgresDashboardStore,
    _candidate_backtest_headline,
    _portfolio_daily_rows,
)


RUN_ID = "run_" + "2" * 24


class DashboardProjectionTest(unittest.TestCase):
    """Dashboard 只能读取已发布产物并且投影幂等。"""

    def test_projection_is_read_only_and_rebuildable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publish_run_artifacts(
                root,
                RUN_ID,
                {
                    "portfolio/metrics.json": b'{"Q10_net_return": {"sharpe": 1.2}}',
                    "ic/diagnostics.json": b'{"ic_mean": 0.03}',
                    "barra/attribution.json": b'{"risk_decomposition_status": "realized_attribution_only"}',
                },
            )
            store = InMemoryDashboardStore()
            first = project_run_artifacts(root, RUN_ID, store)
            second = project_run_artifacts(root, RUN_ID, store)
            self.assertEqual(first, second)
            self.assertEqual(load_dashboard_snapshot(RUN_ID, store), first)
            self.assertEqual(store.snapshots[RUN_ID]["portfolio_metrics"]["Q10_net_return"]["sharpe"], 1.2)

    def test_unpublished_run_cannot_be_loaded(self) -> None:
        with self.assertRaises(FactorMinerError):
            load_dashboard_snapshot(RUN_ID, InMemoryDashboardStore())

    def test_expression_details_render_nested_formula_for_read_model(self) -> None:
        expression = {
            "op": "sub",
            "args": [
                {
                    "op": "div",
                    "args": [
                        {"op": "field", "field": "volume"},
                        {"op": "rolling_mean", "window": 20, "args": [{"op": "field", "field": "volume"}]},
                    ],
                },
                {
                    "op": "rolling_corr",
                    "window": 20,
                    "args": [
                        {"op": "delta", "period": 1, "args": [{"op": "field", "field": "close"}]},
                        {"op": "delay", "period": 1, "args": [{"op": "field", "field": "close"}]},
                    ],
                },
            ],
        }
        details = _expression_details(expression)
        self.assertIn("MA_20(volume)", details["formula_text"])
        self.assertIn("Corr_20", details["formula_text"])
        self.assertEqual(details["formula_field"], "volume, close")
        self.assertEqual(details["formula_period"], 1)
        self.assertEqual(details["formula_window"], 20)

    def test_expression_details_keep_extreme_and_sum_windows_in_formula(self) -> None:
        expression = {
            "op": "sub",
            "args": [
                {
                    "op": "rolling_max",
                    "window": 40,
                    "args": [{"op": "field", "field": "high"}],
                },
                {
                    "op": "rolling_min",
                    "window": 20,
                    "args": [{
                        "op": "rolling_sum",
                        "window": 5,
                        "args": [{"op": "field", "field": "low"}],
                    }],
                },
            ],
        }
        details = _expression_details(expression)
        self.assertEqual(
            details["formula_text"],
            "(Max_40(high) − Min_20(Sum_5(low)))",
        )
        self.assertEqual(details["formula_window"], 40)

    def test_pilot_slot_gets_stable_huan_reference_id(self) -> None:
        self.assertEqual(candidate_reference_id("pilot_fixed_001"), "huan001")
        self.assertEqual(candidate_reference_id("huan017"), "huan017")
        self.assertEqual(candidate_name("huan017"), "huan017")

    def test_campaign_candidates_receive_sequential_huan_references(self) -> None:
        """新研究族必须从库内最大 huan 编号继续分配且重复投影保持不变。"""

        class Cursor:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def execute(self, sql, params=()):
                normalized = " ".join(sql.split())
                if "MAX" in normalized:
                    return Cursor((15,))
                if "source_candidate_id = %s" in normalized:
                    if params == ("a_share", "coverage_outcome_llm:001"):
                        return Cursor(("huan016",))
                    return Cursor(None)
                return Cursor(None)

        store = PostgresDashboardStore.__new__(PostgresDashboardStore)
        store._market_id = "a_share"
        aliases = store._allocate_candidate_references(
            Connection(),
            ("coverage_outcome_llm:001", "coverage_outcome_llm:002"),
        )
        self.assertEqual(
            aliases,
            {
                "coverage_outcome_llm:001": "huan016",
                "coverage_outcome_llm:002": "huan017",
            },
        )

    def test_nested_campaign_daily_returns_produce_win_rate(self) -> None:
        """Stage C 的 daily 包装不能让胜率静默变成 NULL。"""

        snapshot = {
            "portfolio_metrics": {
                "candidates": {
                    "C": {"series": {"Q10_Q1_net_return": {"period_return": 0.1}}}
                }
            },
            "portfolio_daily": {
                "candidates": {
                    "C": {
                        "daily": [
                            {"Q10_Q1_net_return": 0.01},
                            {"Q10_Q1_net_return": -0.02},
                            {"Q10_Q1_net_return": 0.03},
                        ]
                    }
                }
            },
        }
        headline = _candidate_backtest_headline(snapshot, "C")
        self.assertAlmostEqual(headline["win_rate"], 2 / 3)
        self.assertEqual(len(_portfolio_daily_rows(snapshot, "C")), 3)

    def test_postgres_rejects_campaign_snapshot_with_null_core_metrics(self) -> None:
        """即使绕过发布器，PostgreSQL 入口也不能接受核心指标为空。"""

        snapshot = {
            "campaign_metadata": {"family_id": "llmfamily_" + "1" * 24},
            "candidate_definitions": {"C": {"factor_name_zh": "测试因子"}},
            "ic_diagnostics": {"candidates": {"C": {"rank_ic_mean": 0.01}}},
            "portfolio_metrics": {"candidates": {"C": {"series": {}}}},
            "portfolio_daily": {"candidates": {"C": {"daily": []}}},
        }
        with self.assertRaises(FactorMinerError):
            PostgresDashboardStore._require_complete_campaign_metrics(snapshot)

    def test_descriptor_uses_actual_ast_instead_of_shared_template(self) -> None:
        reversal_expression = {
            "op": "mul",
            "args": [
                {
                    "op": "div",
                    "args": [
                        {"op": "field", "field": "volume"},
                        {"op": "rolling_mean", "window": 20, "args": [{"op": "field", "field": "volume"}]},
                    ],
                },
                {"op": "neg", "args": [{"op": "delta", "period": 5, "args": [{"op": "field", "field": "close"}]}]},
            ],
        }
        interaction_expression = {
            "op": "mul",
            "args": [
                {
                    "op": "div",
                    "args": [
                        {"op": "field", "field": "volume"},
                        {"op": "rolling_mean", "window": 120, "args": [{"op": "field", "field": "volume"}]},
                    ],
                },
                {"op": "delta", "period": 1, "args": [{"op": "field", "field": "close"}]},
            ],
        }
        reversal = _factor_descriptor(
            reversal_expression,
            _expression_details(reversal_expression),
            "next_open",
        )
        interaction = _factor_descriptor(
            interaction_expression,
            _expression_details(interaction_expression),
            "next_open",
        )
        self.assertNotEqual(reversal[0], interaction[0])
        self.assertNotEqual(reversal[1], interaction[1])
        self.assertIn("−(Δ_5(close))", _expression_details(reversal_expression)["formula_text"])

    def test_postgres_adapter_projects_only_parameterized_snapshot_values(self) -> None:
        class Cursor:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                if sql.lstrip().startswith("SELECT snapshot_sha256"):
                    return Cursor(None)
                return Cursor(None)

        connection = Connection()
        fake_psycopg = type(
            "FakePsycopg",
            (),
            {"connect": staticmethod(lambda _dsn: connection)},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publish_run_artifacts(
                root,
                RUN_ID,
                {
                    "portfolio/metrics.json": b'{"C": {"series": {}}}',
                    "ic/diagnostics.json": b'{"C": {"ic_mean": 0.01}}',
                    "barra/attribution.json": b'{"C": {}}',
                },
            )
            with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
                with self.assertRaises(FactorMinerError):
                    project_run_artifacts(root, RUN_ID, PostgresDashboardStore("dsn"))
        self.assertTrue(connection.statements)
        for sql, params in connection.statements:
            self.assertNotIn("snapshot_sha256='", sql)
            self.assertIsInstance(params, tuple)

    def test_postgres_adapter_projects_only_readable_factor_and_latest_metrics(self) -> None:
        """PG 只存因子说明和最新完整指标，不复制图表或审计数据。"""

        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                return Cursor()

        connection = Connection()
        fake_psycopg = type(
            "FakePsycopg",
            (),
            {"connect": staticmethod(lambda _dsn: connection)},
        )
        snapshot = {
            "run_id": RUN_ID,
            "snapshot_sha256": "a" * 64,
            "artifact_manifest_sha256": "b" * 64,
            "artifact_refs": [],
            "candidate_definitions": {
                "C": {
                    "spec_hash": "c" * 64,
                    "source_kind": "deepseek",
                    "factor_category": "novel_vendor_label",
                    "hypothesis_claim": "价格变化可能反映信息扩散速度。",
                    "hypothesis_mechanism": "信息逐步扩散。",
                    "expected_sign": "positive",
                    "formula_operator": "delta",
                    "formula_text": "close[t] - close[t-20]",
                    "formula_field": "close",
                    "formula_period": 20,
                    "required_fields": ["close"],
                    "max_lookback": 20,
                    "availability": "next_open",
                }
            },
            "candidate_metrics": {
                "candidates": {
                    "C": {
                        "quality_status": "not_assessed",
                        "confirmation_passed": True,
                    }
                }
            },
            "ic_diagnostics": {
                "C": {
                    "primary_horizon": 5,
                    "ic_mean": 0.01,
                    "rank_ic_mean": 0.02,
                    "ic_std": 0.03,
                    "rank_ic_std": 0.04,
                    "ic_ir": 0.3,
                    "rank_ic_ir": 0.4,
                    "ic_hac_t": 1.2,
                    "rank_ic_hac_t": 1.5,
                    "p_ic_lt_neg_002": 0.1,
                    "p_ic_gt_pos_002": 0.2,
                    "daily": [{"eligible_count": 100}],
                    "decay": [{"horizon": 5, "valid_dates": 20}],
                }
            },
            "portfolio_metrics": {
                "C": {
                    "benchmark": "CSI300",
                    "series": {
                        "target_long_net_return": {
                            "observations": 20,
                            "period_return": 0.1,
                            "annualized_return": 0.2,
                            "excess_return": 0.05,
                            "excess_annualized_return": 0.1,
                            "annualized_volatility": 0.2,
                            "excess_annualized_volatility": 0.15,
                            "sharpe": 1.0,
                            "information_ratio": 0.8,
                            "max_drawdown": 0.1,
                            "excess_max_drawdown": 0.08,
                            "annualization_factor": 50.0,
                            "return_basis": "net_or_gross_as_named",
                        }
                    },
                }
            },
            "portfolio_daily": {
                "C": [
                    {"exit_date": "2024-06-01", "target_long_net_return": -0.01},
                    {"exit_date": "2025-06-01", "target_long_net_return": 0.01},
                ]
            },
            "portfolio_governance": {
                "C": {
                    "win_rate": 0.6,
                    "extreme_spread_daily": [],
                }
            },
            "recent_ic_diagnostics": {
                "C": {
                    "primary_horizon": 5,
                    "ic_mean": 0.015,
                    "rank_ic_mean": 0.025,
                    "ic_std": 0.035,
                    "rank_ic_std": 0.045,
                    "ic_ir": 0.35,
                    "rank_ic_ir": 0.45,
                    "ic_hac_t": 1.3,
                    "rank_ic_hac_t": 1.6,
                    "daily": [{"eligible_count": 110}],
                    "decay": [{"horizon": 5, "valid_dates": 10}],
                }
            },
            "recent_portfolio_metrics": {
                "C": {
                    "benchmark": "CSI300",
                    "series": {
                        "target_long_net_return": {
                            "observations": 10,
                            "period_return": 0.08,
                            "annualized_return": 0.18,
                            "excess_return": 0.04,
                            "excess_annualized_return": 0.09,
                            "annualized_volatility": 0.19,
                            "excess_annualized_volatility": 0.14,
                            "sharpe": 0.9,
                            "information_ratio": 0.7,
                            "max_drawdown": 0.09,
                            "excess_max_drawdown": 0.07,
                            "annualization_factor": 50.0,
                            "return_basis": "net_or_gross_as_named",
                        }
                    },
                }
            },
            "direction_decisions": {
                "C": {
                    "decision": {
                        "hypothesis_direction": "positive",
                        "selected_direction": "negative",
                        "hypothesis_relation": "reversed",
                        "discovery_summary": {"rank_ic": -0.02},
                    }
                }
            },
            "barra_attribution": {
                "C": {"exposure_summary": None, "attribution": None},
            },
        }
        with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            PostgresDashboardStore("dsn").replace_run_snapshot(RUN_ID, snapshot)

        statements = "\n".join(sql for sql, _ in connection.statements)
        self.assertIn("INSERT INTO factors", statements)
        self.assertIn("formula", statements)
        self.assertIn("calculation", statements)
        self.assertIn("hypothesis", statements)
        self.assertIn("rank_ic_hac_t", statements)
        self.assertIn("discovered_direction", statements)
        factor_params = next(
            params for sql, params in connection.statements if "INSERT INTO factors" in sql
        )
        self.assertEqual(factor_params[4], "负向")
        self.assertEqual(factor_params[5], "与假设相反")
        self.assertEqual(factor_params[9], "未分类")
        metrics_sql, metrics_params = next(
            (sql, params)
            for sql, params in connection.statements
            if "INSERT INTO factor_metrics" in sql
        )
        self.assertEqual(metrics_sql.count("%s"), len(metrics_params))
        self.assertNotIn("confirmation_", metrics_sql)
        self.assertNotIn("recent_", metrics_sql)
        self.assertEqual(metrics_params[4], 0.015)
        self.assertEqual(metrics_params[5], 0.025)
        self.assertEqual(metrics_params[12], 0.18)
        self.assertEqual(metrics_params[13], 0.09)
        self.assertNotIn(0.6, metrics_params)
        for forbidden in (
            "snapshot_json",
            "artifact_refs",
            "portfolio_metrics",
            "portfolio_daily",
            "candidate_ic_horizons",
            "barra_attribution",
            "research_memory",
            "sha256",
        ):
            self.assertNotIn(forbidden, statements)


if __name__ == "__main__":
    unittest.main()

"""Dashboard 只读合同测试。"""

import os
from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import Mock, patch

from dashboard.read import load_optional_snapshot, load_server_barra_snapshot, load_server_snapshot
from dashboard.ui import daily_rows
from factor_miner.dashboard_labels import chinese_candidate_label
from factor_miner.errors import FactorMinerError
from factor_miner.portfolio_artifacts import publish_run_artifacts


REQUIRED_CORE_METRICS = (
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


class DashboardContractTest(unittest.TestCase):
    """Dashboard 在无运行时可浏览，在正式产物损坏时硬失败。"""

    def setUp(self) -> None:
        """这些旧投影合同显式测试 A 股，不依赖首页默认市场。"""
        for target in ("dashboard.read.current_market_id", "dashboard.market_profiles.current_market_id"):
            patcher = patch(target, return_value="a_share")
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dashboard_requires_private_server_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {"FM_ARTIFACT_ROOT": "", "FM_DASHBOARD_RUN_ID": ""},
            clear=False,
        ):
            with self.assertRaises(FactorMinerError):
                load_server_snapshot()

    def test_optional_dashboard_snapshot_allows_market_without_run(self) -> None:
        """配置本地市场但尚未研究时，诊断页应显示空状态而不是报错。"""

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "FM_DASHBOARD_ARTIFACT_ROOT": directory,
                "FM_ARTIFACT_ROOT": "",
                "FM_DASHBOARD_RUN_ID": "",
            },
            clear=False,
        ):
            self.assertIsNone(load_optional_snapshot())

    def test_dashboard_reads_charts_from_files_and_requires_postgres(self) -> None:
        """图表来自正式产物，同时 PostgreSQL 连接仍是硬要求。"""

        run_id = "run_" + "8" * 24
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publish_run_artifacts(
                root,
                run_id,
                {
                    "portfolio/metrics.json": b'{"C":{"series":{}}}',
                    "portfolio/daily.json": b'{"C":[{"exit_date":"2026-01-02","Q10_Q1_net_return":0.01}]}',
                    "ic/diagnostics.json": b'{"C":{"ic_mean":0.01}}',
                    "barra/attribution.json": b'{"C":{"status":"not_available"}}',
                },
            )
            with patch.dict(
                os.environ,
                {
                    "FM_DASHBOARD_ARTIFACT_ROOT_A_SHARE": str(root),
                    "FM_DASHBOARD_RUN_ID_A_SHARE": run_id,
                    "FM_DASHBOARD_DSN": "postgresql://local",
                },
                clear=False,
            ), patch("dashboard.read._attach_factor_aliases", side_effect=lambda value, _: value):
                snapshot = load_server_snapshot()

        self.assertEqual(snapshot.run_id, run_id)
        self.assertEqual(
            snapshot.portfolio_daily["C"][0]["Q10_Q1_net_return"],
            0.01,
        )

    def test_dashboard_rejects_missing_postgres_dsn(self) -> None:
        with patch.dict(os.environ, {"FM_DASHBOARD_DSN": ""}, clear=False):
            with self.assertRaisesRegex(FactorMinerError, "FM_DASHBOARD_DSN"):
                load_server_snapshot()

    def test_dashboard_reuses_parsed_artifacts_and_loads_huan_aliases(self) -> None:
        """同一正式运行只解析一次，并从最小 PG 索引附加 huan 编号。"""

        run_id = "run_" + "6" * 24
        snapshot = Mock(run_id=run_id, candidate_definitions={"cand_1": {}})
        snapshot.candidate_metrics = {}
        snapshot.ic_diagnostics = None
        snapshot.portfolio_metrics = None
        snapshot.portfolio_daily = None
        snapshot.barra_attribution = None
        snapshot.model_copy.return_value = "带编号快照"
        with patch.dict(
            os.environ,
            {
                "FM_DASHBOARD_ARTIFACT_ROOT_A_SHARE": "/tmp/dashboard-cache-contract",
                "FM_DASHBOARD_RUN_ID_A_SHARE": run_id,
                "FM_DASHBOARD_DSN": "postgresql://private",
            },
            clear=False,
        ), patch("dashboard.read._latest_projected_run", return_value=None), patch(
            "dashboard.read.project_run_artifacts", return_value=snapshot
        ) as project, patch("dashboard.read.PostgresDashboardStore") as store_type:
            store_type.return_value.load_factor_aliases.return_value = {"cand_1": "huan001"}
            from dashboard import read as dashboard_read

            dashboard_read._load_artifact_snapshot.cache_clear()
            first = load_server_snapshot()
            second = load_server_snapshot()

        self.assertEqual(first, "带编号快照")
        self.assertEqual(second, "带编号快照")
        self.assertEqual(project.call_count, 1)
        snapshot.model_copy.assert_called_with(
            update={"candidate_aliases": {"cand_1": "huan001"}}
        )

    def test_daily_rows_accepts_stage_c_daily_wrapper(self) -> None:
        """分组回测页必须读取 Stage C 的 daily 包装，而不是显示零观测。"""

        snapshot = Mock(
            candidate_aliases={"cand_1": "huan001"},
            portfolio_daily={
                "candidates": {
                    "cand_1": {
                        "daily": [
                            {"exit_date": "2026-01-03", "Q10_Q1_net_return": 0.02},
                            {"exit_date": "2026-01-02", "Q10_Q1_net_return": -0.01},
                        ]
                    }
                }
            },
        )

        rows = daily_rows(snapshot, "huan001")

        self.assertEqual([row["exit_date"] for row in rows], ["2026-01-02", "2026-01-03"])

    def test_barra_page_uses_explicit_completed_validation_run(self) -> None:
        """主运行无归因时，可显示显式配置的最近完成 Barra 验证运行。"""

        main = Mock(barra_attribution={})
        fallback = Mock(run_id="run_" + "5" * 24, candidate_definitions={"pilot_fixed_001": {}})
        fallback.candidate_metrics = {}
        fallback.ic_diagnostics = None
        fallback.portfolio_metrics = None
        fallback.portfolio_daily = None
        fallback.barra_attribution = {"candidates": {"pilot_fixed_001": {"status": "available", "attribution": {}}}}
        fallback.model_copy.return_value = "Barra 验证快照"
        with patch.dict(
            os.environ,
            {
                "FM_DASHBOARD_ARTIFACT_ROOT_A_SHARE": "/tmp/barra-fallback-contract",
                "FM_DASHBOARD_RUN_ID_A_SHARE": "run_" + "4" * 24,
                "FM_DASHBOARD_BARRA_RUN_ID_A_SHARE": fallback.run_id,
                "FM_DASHBOARD_DSN": "postgresql://private",
            },
            clear=False,
        ), patch("dashboard.read.load_server_snapshot", return_value=main), patch(
            "dashboard.read._load_artifact_snapshot", return_value=fallback
        ), patch("dashboard.read.PostgresDashboardStore") as store_type:
            store_type.return_value.load_factor_aliases.return_value = {"pilot_fixed_001": "huan001"}
            result = load_server_barra_snapshot()

        self.assertEqual(result, "Barra 验证快照")

    def test_dashboard_follows_latest_autonomous_projection(self) -> None:
        """按钮完成新批次后，Dashboard 不得继续读取环境里写死的旧 run。"""

        old_run = "run_" + "7" * 24
        latest_run = "run_" + "9" * 24
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory)
            for run_id, value in ((old_run, 0.01), (latest_run, 0.02)):
                publish_run_artifacts(
                    private_root,
                    run_id,
                    {
                        "portfolio/metrics.json": b'{"C":{"series":{}}}',
                        "portfolio/daily.json": json.dumps(
                            {"C": [{"exit_date": "2026-01-02", "Q10_Q1_net_return": value}]}
                        ).encode(),
                        "ic/diagnostics.json": b'{"C":{"ic_mean":0.01}}',
                        "barra/attribution.json": b'{"C":{"status":"not_available"}}',
                    },
                )
            projection = (
                private_root
                / "state/autonomous_research/runs/autrun_1234567890abcdef12345678/objects/projection.json"
            )
            projection.parent.mkdir(parents=True)
            projection.write_text(
                json.dumps({"projected_run_id": latest_run}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "FM_DASHBOARD_ARTIFACT_ROOT_A_SHARE": str(private_root),
                    "FM_DASHBOARD_RUN_ID_A_SHARE": old_run,
                    "FM_DASHBOARD_DSN": "postgresql://local",
                },
                clear=False,
            ), patch("dashboard.read._attach_factor_aliases", side_effect=lambda value, _: value):
                snapshot = load_server_snapshot()

        self.assertEqual(snapshot.run_id, latest_run)
        self.assertEqual(snapshot.portfolio_daily["C"][0]["Q10_Q1_net_return"], 0.02)

    def test_dashboard_service_exposes_src_package_path(self) -> None:
        """服务器服务必须让所有页面都能导入 src 下的核心包。"""

        service = Path("deploy/factor-miner-dashboard.service").read_text("utf-8")
        self.assertIn(
            "Environment=PYTHONPATH=/opt/factor-miner/src:/opt/factor-miner",
            service,
        )
        self.assertIn("WorkingDirectory=/opt/factor-miner", service)
        self.assertIn("-m streamlit run dashboard/app.py", service)

    def test_local_dashboard_exposes_repository_and_src_packages(self) -> None:
        """Mac 本地入口也必须能导入 dashboard 与 src 包。"""

        makefile = Path("Makefile").read_text("utf-8")
        self.assertIn(
            "PYTHONPATH=src:. uv run --frozen python -m streamlit run dashboard/app.py",
            makefile,
        )

    def test_dashboard_and_worker_wait_for_postgres(self) -> None:
        """服务器重启时两个服务必须等待数据库就绪。"""

        for path in (
            "deploy/factor-miner-dashboard.service",
            "deploy/factor-miner-worker.service",
        ):
            service = Path(path).read_text("utf-8")
            self.assertIn("ExecStartPre=/usr/bin/timeout 120", service)
            self.assertIn("pg_isready -h 127.0.0.1 -p 5432", service)

    def test_research_console_polls_only_with_connected_worker(self) -> None:
        """离线队列不伪装正在运行；交互生命周期由页面回归覆盖。"""

        page = Path("dashboard/pages/7_研究运行台.py").read_text("utf-8")
        self.assertIn('@st.fragment(run_every="5s")', page)
        self.assertIn("worker_running(artifact_root)", page)
        self.assertIn("if online and", page)
        self.assertIn("state.stage.terminal", page)

    def test_navigation_is_four_direct_tasks(self) -> None:
        """正式入口提供结果、研究、代表库和设置，历史页面不在导航中。"""

        app = Path("dashboard/app.py").read_text("utf-8")
        self.assertEqual(app.count("st.Page("), 4)
        for title in ("看结果", "挖因子", "研究代表库", "设置"):
            self.assertIn(f'title="{title}"', app)
        self.assertNotIn('st.Page("dashboard/pages/', app)

    def test_audit_page_does_not_render_entire_snapshot(self) -> None:
        """折叠区也不能序列化巨型完整快照，否则页面会耗尽内存。"""

        page = Path("dashboard/pages/5_运行审计.py").read_text("utf-8")
        self.assertNotIn("snapshot.model_dump", page)
        self.assertIn("snapshot.artifact_refs", page)

    def test_dashboard_labels_translate_descriptive_values(self) -> None:
        """面向人的候选描述必须显示中文，未知技术值保持原样。"""

        self.assertEqual(chinese_candidate_label("expected_sign", "positive"), "正向")
        self.assertEqual(
            chinese_candidate_label("availability", "next_open"),
            "当日收盘观察，下一交易日开盘使用",
        )
        self.assertEqual(
            chinese_candidate_label("mechanism_status", "mechanism_unverified"),
            "机制尚未独立验证",
        )
        self.assertEqual(chinese_candidate_label("quality_status", "not_assessed"), "待评价")
        self.assertEqual(chinese_candidate_label("source_kind", "deepseek"), "LLM")
        self.assertEqual(chinese_candidate_label("source_kind", "sol"), "sol")

    def test_complete_view_requires_every_core_metric(self) -> None:
        """默认视图只能选择核心指标全部完整的每候选最新运行。"""

        sql = Path(
            "dashboard/migrations/008_complete_chinese_candidate_views.sql"
        ).read_text(encoding="utf-8")
        for field in REQUIRED_CORE_METRICS:
            self.assertIn(f"{field} IS NOT NULL", sql)
        self.assertIn("row_number() OVER", sql)
        self.assertIn("PARTITION BY candidate_id", sql)
        self.assertIn("candidate_catalog_zh", sql)
        self.assertIn("latest_complete_candidate_metrics", sql)


if __name__ == "__main__":
    unittest.main()

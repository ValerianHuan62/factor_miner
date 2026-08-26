"""Dashboard 只读合同测试。"""

import os
from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import Mock, patch

from dashboard.read import load_server_barra_snapshot, load_server_snapshot
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
    """Dashboard 缺少运行环境私有连接时必须硬失败。"""

    def test_dashboard_requires_private_server_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {"FM_ARTIFACT_ROOT": "", "FM_DASHBOARD_RUN_ID": ""},
            clear=False,
        ):
            with self.assertRaises(FactorMinerError):
                load_server_snapshot()

    def test_dashboard_reads_charts_from_published_files_without_postgres_snapshot(self) -> None:
        """删除 snapshot_json 后，图表仍必须从正式发布产物读取。"""

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
                    "FM_ARTIFACT_ROOT": str(root),
                    "FM_DASHBOARD_RUN_ID": run_id,
                    "FM_DASHBOARD_DSN": "",
                },
                clear=False,
            ):
                snapshot = load_server_snapshot()

        self.assertEqual(snapshot.run_id, run_id)
        self.assertEqual(
            snapshot.portfolio_daily["C"][0]["Q10_Q1_net_return"],
            0.01,
        )

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
                "FM_ARTIFACT_ROOT": "/tmp/dashboard-cache-contract",
                "FM_DASHBOARD_RUN_ID": run_id,
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
                "FM_ARTIFACT_ROOT": "/tmp/barra-fallback-contract",
                "FM_DASHBOARD_RUN_ID": "run_" + "4" * 24,
                "FM_DASHBOARD_BARRA_RUN_ID": fallback.run_id,
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
                    "FM_ARTIFACT_ROOT": str(private_root),
                    "FM_DASHBOARD_RUN_ID": old_run,
                    "FM_DASHBOARD_DSN": "",
                },
                clear=False,
            ):
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

    def test_dashboard_and_worker_wait_for_postgres(self) -> None:
        """服务器重启时两个服务必须等待数据库就绪。"""

        for path in (
            "deploy/factor-miner-dashboard.service",
            "deploy/factor-miner-worker.service",
        ):
            service = Path(path).read_text("utf-8")
            self.assertIn("ExecStartPre=/usr/bin/timeout 120", service)
            self.assertIn("pg_isready -h 127.0.0.1 -p 5432", service)

    def test_research_console_auto_refreshes_only_during_live_work(self) -> None:
        """活动批次自动轮询，终态页面不得永久占用刷新循环。"""

        page = Path("dashboard/pages/7_研究运行台.py").read_text("utf-8")
        self.assertIn('@st.fragment(run_every="5s")', page)
        self.assertIn('st.session_state["research_auto_refresh"] = True', page)
        self.assertIn('st.session_state.pop("research_auto_refresh", None)', page)
        self.assertIn('state.stage.value in {"failed", "completed"}', page)
        self.assertIn(
            'state.stage.value not in {"failed", "completed"}',
            page,
        )
        self.assertIn('st.button("新建下一批研究"', page)

    def test_navigation_paths_are_relative_to_dashboard_entrypoint(self) -> None:
        """st.Page 会相对 app.py 所在目录解析页面文件。"""

        app = Path("dashboard/app.py").read_text("utf-8")
        self.assertNotIn('st.Page("dashboard/pages/', app)
        for page in range(1, 8):
            self.assertIn(f'st.Page("pages/{page}_', app)

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

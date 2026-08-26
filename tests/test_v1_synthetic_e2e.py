"""V1 合成验收：封存后的组合诊断可以发布并投影到只读 Dashboard。"""

from pathlib import Path
import tempfile
import unittest

from factor_miner.dashboard_projection import load_dashboard_snapshot, project_run_artifacts
from factor_miner.dashboard_store import InMemoryDashboardStore
from factor_miner.portfolio_schema import PortfolioEvaluationPolicy
from factor_miner.workflow import run_visible_portfolio_campaign
from tests.test_barra_schema import policy as barra_policy
from tests.test_portfolio_workflow import PortfolioWorkflowTest
from tests.helpers import valid_trusted_campaign


class V1SyntheticE2ETest(unittest.TestCase):
    """Mac 只验证程序生成的合成产物，不读取公司服务器数据。"""

    def test_published_portfolio_ic_barra_can_rebuild_dashboard_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sources = PortfolioWorkflowTest().sources(sealed=True)
            result = run_visible_portfolio_campaign(
                valid_trusted_campaign(),
                PortfolioEvaluationPolicy(),
                barra_policy(),
                sources,
                Path(directory),
            )
            store = InMemoryDashboardStore()
            snapshot = project_run_artifacts(Path(directory), result.run_id, store)
            rebuilt = load_dashboard_snapshot(result.run_id, store)

            self.assertEqual(snapshot, rebuilt)
            self.assertEqual(snapshot.run_id, result.run_id)
            self.assertIn(result.run_id, store.snapshots)
            self.assertIsNotNone(snapshot.portfolio_metrics)
            series = snapshot.portfolio_metrics or {}
            candidate_id = next(iter(series))
            self.assertEqual(
                set(series[candidate_id]["series"]),
                {"target_long_gross_return", "target_long_net_return", "CSI300"},
            )
            self.assertIsNotNone(snapshot.ic_diagnostics)
            self.assertIsNotNone(snapshot.barra_attribution)


if __name__ == "__main__":
    unittest.main()

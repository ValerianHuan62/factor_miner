from pathlib import Path
import tempfile
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.ledger import JsonlLedger
from factor_miner.regime_schema import (
    registered_regime_research,
)
from factor_miner.regime_workflow import validate_regime_deployment
from tests.test_regime_snapshot import research_fixture


class RegimeWorkflowTest(unittest.TestCase):
    """研究登记与正式部署之间的治理边界测试。"""

    def test_research_and_deployment_documents_are_immutable(self) -> None:
        """V0.3 研究与部署必须进入各自内容寻址目录。"""

        _, spec, _, deployment = research_fixture()
        research = registered_regime_research(spec)
        with tempfile.TemporaryDirectory() as directory:
            ledger = JsonlLedger(Path(directory))
            research_path = ledger.register_regime_research(research)
            deployment_path = ledger.register_regime_deployment(deployment)
            self.assertEqual(
                research_path,
                ledger.register_regime_research(research),
            )
            self.assertEqual(
                deployment_path,
                ledger.register_regime_deployment(deployment),
            )
            self.assertEqual(research_path.parent, ledger.paths.regime_research_root)
            self.assertEqual(
                deployment_path.parent,
                ledger.paths.regime_deployments_root,
            )

    def test_deployment_must_match_an_eligible_research_candidate(self) -> None:
        """未研究、配置漂移或不合格候选都不能登记为正式模型。"""

        _, spec, report, deployment = research_fixture()
        research = registered_regime_research(spec)
        validated = validate_regime_deployment(research, report, deployment)
        self.assertEqual(validated, deployment)

        unknown = deployment.model_copy(
            update={
                "spec": deployment.spec.model_copy(
                    update={"research_candidate_id": "regcand_" + "9" * 24}
                )
            }
        )
        with self.assertRaises(FactorMinerError) as raised:
            validate_regime_deployment(research, report, unknown)
        self.assertEqual(
            raised.exception.code,
            FailureCode.REGIME_DOWNSTREAM_MANIFEST_MISMATCH,
        )


if __name__ == "__main__":
    unittest.main()

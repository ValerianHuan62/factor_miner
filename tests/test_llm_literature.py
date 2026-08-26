"""V0.5 受限 Crossref 文献元数据网关测试。"""

from datetime import datetime, timezone
import unittest

from factor_miner.errors import FactorMinerError, FailureCode
from factor_miner.llm_literature import (
    LiteratureQuery,
    LiteratureSourceRecordTask,
    build_literature_source_record_tasks,
    parse_crossref_response,
    validate_literature_query,
)
from factor_miner.research_evolution import LogicalEvolutionHypothesisDraft


class LLMLiteratureTest(unittest.TestCase):
    """查询只能使用冻结词表，响应不能扩张成任意网页访问。"""

    def test_query_rejects_url_ip_and_terms_outside_vocabulary(self) -> None:
        # 以下 URL、内网地址和私有字段名都是纯字符串夹具；测试不会发起网络请求。
        allowed = frozenset({"price", "continuation", "information", "diffusion"})
        for terms in (
            ("https://example.com/paper",),
            ("192.168.1.1",),
            ("private_factor_name",),
        ):
            with self.subTest(terms=terms):
                with self.assertRaises(FactorMinerError) as context:
                    validate_literature_query(
                        LiteratureQuery(
                            query_terms=terms,
                            year_start=1990,
                            year_end=2026,
                            result_limit=3,
                        ),
                        allowed,
                    )
                self.assertEqual(
                    context.exception.code,
                    FailureCode.LLM_PRIVACY_VIOLATION,
                )

    def test_crossref_parser_keeps_bounded_public_metadata(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1234/SYNTHETIC.1",
                        "title": ["Information Diffusion and Prices"],
                        "author": [
                            {"given": "A", "family": "Researcher"},
                            {"given": "B", "family": "Scholar"},
                        ],
                        "published": {"date-parts": [[2020, 1, 1]]},
                        "container-title": ["Synthetic Journal"],
                        "abstract": (
                            "<jats:p>Public abstract about price "
                            "continuation.</jats:p>"
                        ),
                        "URL": "https://untrusted.example/path",
                    }
                ]
            }
        }

        record = parse_crossref_response(
            query=LiteratureQuery(
                query_terms=("information", "diffusion"),
                year_start=1990,
                year_end=2026,
                result_limit=3,
            ),
            response_payload=payload,
            retrieved_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(len(record.results), 1)
        item = record.results[0]
        self.assertEqual(item.public_identifier, "10.1234/synthetic.1")
        self.assertEqual(
            item.canonical_url,
            "https://doi.org/10.1234/synthetic.1",
        )
        self.assertNotIn("<", item.abstract_excerpt)
        self.assertNotIn("untrusted.example", item.canonical_url)
        self.assertEqual(item.verification_status, "identity_verified")

    def test_build_source_record_tasks_uses_logical_hypothesis_without_regeneration(
        self,
    ) -> None:
        draft = LogicalEvolutionHypothesisDraft(
            logical_slot_id="H01",
            mechanism_unverified=True,
            prior_claim="价格延续可能对应慢扩散。",
            mechanism="公开信息进入价格存在时滞。",
            expected_direction="positive",
            observable_proxy="过去二十日价格延续",
            independent_verification="比较公告密度分组。",
            competing_explanations=("短期流动性冲击",),
            failure_modes=("高波动反转",),
            falsification_path="方向显著反向。",
            gap_ids=("G001",),
            source_records=(
                {
                    "source_record_id": "src-01",
                    "claim_fragment": "价格延续与扩散相关",
                    "rationale": "验证信息扩散线索",
                    "query_terms": ("price", "continuation"),
                    "year_start": 1990,
                    "year_end": 2026,
                    "result_limit": 3,
                },
            ),
        )

        tasks = build_literature_source_record_tasks(draft)

        self.assertEqual(len(tasks), 1)
        self.assertIsInstance(tasks[0], LiteratureSourceRecordTask)
        self.assertEqual(tasks[0].logical_slot_id, "H01")
        self.assertEqual(tasks[0].source_record_id, "src-01")
        self.assertEqual(
            tasks[0].query.query_terms,
            ("price", "continuation"),
        )


if __name__ == "__main__":
    unittest.main()

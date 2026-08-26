"""目标多头五年研究合同测试。"""

from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import unittest

import polars as pl

from factor_miner.data_source import FactorInputRequest
from factor_miner.long_only_protocol import (
    DiscoveryRankICSummary,
    LongOnlyResearchProtocol,
    select_direction,
)
from factor_miner.pilot_sources import PilotQuantLakeFactorInputSource
from tests.test_pilot_sources import _partition_source_sha256, _write_pilot_inputs


class LongOnlyResearchProtocolTest(unittest.TestCase):
    """研究区间和发现方向必须在结果前具有确定的合同。"""

    def _discovery_summary(self, rank_ic: float) -> DiscoveryRankICSummary:
        """构造绑定到冻结发现区间的 RankIC 摘要。"""

        protocol = LongOnlyResearchProtocol()
        return DiscoveryRankICSummary(
            protocol=protocol,
            window_start=protocol.discovery_start,
            window_end=protocol.discovery_end,
            rank_ic=rank_ic,
        )

    def test_default_protocol_freezes_discovery_and_confirmation_windows(self) -> None:
        """默认协议冻结发现期和确认期，最近期作为确认期内的独立报告窗口。"""

        policy = LongOnlyResearchProtocol()

        self.assertEqual(policy.discovery_start, date(2021, 1, 1))
        self.assertEqual(policy.discovery_end, date(2023, 12, 31))
        self.assertEqual(policy.confirmation_start, date(2024, 1, 1))
        self.assertEqual(policy.confirmation_end, date(2026, 6, 30))
        self.assertEqual(policy.recent_start, date(2025, 1, 1))
        self.assertEqual(policy.recent_end, date(2026, 6, 30))
        self.assertEqual(policy.universe, "SSE_SZSE_WHOLE_MARKET")

    def test_direction_is_selected_without_overwriting_hypothesis(self) -> None:
        """发现区间负 RankIC 只能记录反向选择，不能改写事前正向假设。"""

        summary = self._discovery_summary(-0.01)
        decision = select_direction(summary, hypothesis_direction="positive")

        self.assertEqual(decision.selected_direction, "negative")
        self.assertEqual(decision.hypothesis_relation, "reversed")
        self.assertEqual(decision.discovery_summary, summary)

    def test_direction_rejects_bare_rank_ic_without_discovery_summary(self) -> None:
        """确认期或最近期的裸值不能绕过发现窗口身份决定方向。"""

        with self.assertRaises(TypeError):
            select_direction(-0.01, hypothesis_direction="positive")

    def test_direction_requires_the_actual_preregistered_hypothesis_direction(self) -> None:
        """方向决定不能以默认正向假设替代候选登记的 expected_sign。"""

        with self.assertRaises(TypeError):
            select_direction(self._discovery_summary(-0.01))

    def test_protocol_rejects_overridden_dates_universe_and_model_copy_mutation(self) -> None:
        """任何构造或复制都不能把冻结协议改成别的区间或股票池。"""

        with self.assertRaises(ValueError):
            LongOnlyResearchProtocol(discovery_start=date(2021, 1, 2))
        with self.assertRaises(ValueError):
            LongOnlyResearchProtocol(universe="CSI300")
        with self.assertRaises(ValueError):
            LongOnlyResearchProtocol().model_copy(
                update={"confirmation_start": date(2023, 12, 31)}
            )

    def test_discovery_summary_rejects_non_discovery_window_and_non_decisive_rank_ic(self) -> None:
        """确认/最近窗口、零值和非有限值均不得决定方向。"""

        protocol = LongOnlyResearchProtocol()
        invalid_summaries = (
            (protocol.confirmation_start, protocol.confirmation_end, -0.01),
            (protocol.recent_start, protocol.recent_end, -0.01),
            (protocol.discovery_start, protocol.discovery_end, 0.0),
            (protocol.discovery_start, protocol.discovery_end, math.nan),
            (protocol.discovery_start, protocol.discovery_end, math.inf),
            (protocol.discovery_start, protocol.discovery_end, -math.inf),
        )
        for window_start, window_end, rank_ic in invalid_summaries:
            with self.subTest(
                window_start=window_start,
                window_end=window_end,
                rank_ic=rank_ic,
            ):
                with self.assertRaises(ValueError):
                    DiscoveryRankICSummary(
                        protocol=protocol,
                        window_start=window_start,
                        window_end=window_end,
                        rank_ic=rank_ic,
                    )
        with self.assertRaises(ValueError):
            DiscoveryRankICSummary(
                protocol=protocol,
                window_start=protocol.discovery_start,
                window_end=protocol.discovery_end,
            )

    def test_discovery_summary_copy_cannot_mutate_the_discovery_identity(self) -> None:
        """不可变摘要的复制也必须重新验证发现窗口身份。"""

        summary = self._discovery_summary(-0.01)

        with self.assertRaises(ValueError):
            summary.model_copy(
                update={"window_start": summary.protocol.confirmation_start}
            )

    def test_source_uses_daily_market_state_intersection_for_sse_szse_only(self) -> None:
        """全市场候选池只保留两所交易所且每日按状态与行情交集变化。"""

        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            paths = _write_pilot_inputs(Path(directory))
            market_path = paths.market_uri
            state_path = paths.state_uri
            assert market_path is not None
            assert state_path is not None
            assets = ("600000.XSHG", "000001.XSHE", "00700.XHKG", "BAD")
            pl.DataFrame(
                {
                    "date": [date(2026, 7, 30)] * 4 + [date(2026, 7, 31)] * 4,
                    "code": list(assets) * 2,
                    "adj_open": [10.0] * 8,
                    "adj_close": [10.0] * 8,
                    "volume": [100.0] * 8,
                    "money": [1000.0] * 8,
                    "adjust_factor": [1.0] * 8,
                }
            ).write_parquet(market_path)
            pl.DataFrame(
                {
                    "date": [date(2026, 7, 30)] * 4 + [date(2026, 7, 31)] * 3,
                    "code": list(assets) + ["600000.XSHG", "00700.XHKG", "BAD"],
                    "is_st": [False] * 7,
                    "is_newly_listed": [False] * 7,
                    "is_suspended": [False] * 7,
                    "can_buy": [True] * 7,
                    "can_sell": [True] * 7,
                    "valid_for_factor_compute": [True] * 7,
                    "valid_for_factor_rank": [True] * 7,
                    "valid_for_trading": [True] * 7,
                }
            ).write_parquet(state_path)
            release_path = paths.release_manifest_uri
            release = json.loads(release_path.read_text("utf-8"))
            release["market_partitions"][0]["sha256"] = hashlib.sha256(
                market_path.read_bytes()
            ).hexdigest()
            release["state_partitions"][0]["sha256"] = hashlib.sha256(
                state_path.read_bytes()
            ).hexdigest()
            release["market_source_sha256"] = _partition_source_sha256(
                release["market_partitions"]
            )
            release["state_source_sha256"] = _partition_source_sha256(
                release["state_partitions"]
            )
            release_path.write_text(json.dumps(release), encoding="utf-8")
            source = PilotQuantLakeFactorInputSource(
                paths,
                code_commit="a" * 40,
                config_hash="b" * 64,
            )

            source.inspect_inputs()
            frame = source.scan_inputs(
                FactorInputRequest(
                    start=date(2026, 7, 31),
                    end=date(2026, 7, 31),
                    required_fields=("close",),
                    warmup_observations=1,
                )
            ).collect()

            visible_assets = frame.filter(
                pl.col("date") == date(2026, 7, 31)
            ).get_column("asset").to_list()
            self.assertEqual(visible_assets, ["600000.XSHG"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import polars as pl
import pandas as pd
from typer.testing import CliRunner

from factor_miner.cli import app
from factor_miner.errors import FactorMinerError
from tests.helpers import strip_ansi


class _FakeRiceQuantProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.security_ids_seen: list[tuple[str, ...]] = []

    def trading_dates(self, start: date, end: date) -> tuple[date, ...]:
        self.calls.append(("trading_dates", start, end))
        return (start, end) if start != end else (start,)

    def factor_exposure(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame:
        self.calls.append(("factor_exposure", start, end))
        self.security_ids_seen.append(security_ids)
        rows = []
        for current in (start, end) if start != end else (start,):
            for security_id in security_ids:
                rows.append(
                    {
                        "date": current,
                        "order_book_id": security_id,
                        "size": 1.0,
                        "beta": 0.5,
                        "银行": 1.0,
                    }
                )
        return pl.DataFrame(rows)

    def factor_return(self, start: date, end: date) -> pl.DataFrame:
        self.calls.append(("factor_return", start, end))
        return pl.DataFrame(
            {
                "date": [start, end],
                "size": [0.01, 0.02],
                "beta": [0.02, 0.03],
                "银行": [0.03, 0.04],
            }
        )

    def specific_return(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame:
        self.calls.append(("specific_return", start, end))
        return pl.DataFrame(
            {
                "date": [start, start],
                "order_book_id": [security_ids[0], "UNAVAILABLE.XSHG"],
                "specific_return": [0.001, float("nan")],
            }
        )

    def factor_covariance(self, dates: tuple[date, ...]) -> pl.DataFrame:
        self.calls.append(("factor_covariance", dates[0], dates[-1]))
        return pl.DataFrame(
            {
                "date": [dates[0], dates[0]],
                "factor_1": ["size", "银行"],
                "factor_2": ["size", "银行"],
                "covariance": [0.1, 0.2],
            }
        )

    def specific_risk(
        self,
        security_ids: tuple[str, ...],
        start: date,
        end: date,
        *,
        batch_size: int,
    ) -> pl.DataFrame:
        self.calls.append(("specific_risk", start, end))
        return pl.DataFrame(
            {
                "date": [start, start],
                "order_book_id": [security_ids[0], "UNAVAILABLE.XSHG"],
                "specific_risk": [0.2, float("nan")],
            }
        )

    def csi300_weights(self, start: date, end: date) -> pl.DataFrame:
        self.calls.append(("csi300_weights", start, end))
        return pl.DataFrame(
            {
                "date": [start, start],
                "order_book_id": ["000300_MEMBER_A.XSHG", "000300_MEMBER_B.XSHG"],
                "weight": [0.499, 0.5],
            }
        )


class RiceQuantBarraDownloadTests(unittest.TestCase):
    def _request(self, root: Path, universe_uri: Path):
        from factor_miner.ricequant_barra import RiceQuantBarraDownloadRequest

        return RiceQuantBarraDownloadRequest(
            derived_root=root,
            universe_uri=universe_uri,
            start_date=date(2025, 12, 31),
            end_date=date(2026, 1, 2),
            batch_size=2,
        )

    def test_fetch_publishes_normalized_partitions_catalog_and_hashed_manifest(self) -> None:
        """捕获中文因子名进入 Parquet 字段、文件未哈希或缺少六类数据。"""

        from factor_miner.ricequant_barra import fetch_ricequant_barra

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            universe_uri = temporary / "universe.parquet"
            pl.DataFrame(
                {"security_id": ["000001.SZ", "600000.SH", "000001.SZ"]}
            ).write_parquet(universe_uri)
            root = temporary / "derived" / "barra"

            result = fetch_ricequant_barra(
                self._request(root, universe_uri), _FakeRiceQuantProvider()
            )

            self.assertEqual(result.completed_years, (2025, 2026))
            for year in result.completed_years:
                exposure = pl.read_parquet(root / "exposure" / f"year={year}.parquet")
                self.assertEqual(
                    exposure.columns,
                    [
                        "date",
                        "security_id",
                        "Size",
                        "Style_Beta",
                        "industry_001",
                    ],
                )
                self.assertFalse(any("银行" in column for column in exposure.columns))
                factor_return = pl.read_parquet(
                    root / "factor_return" / f"year={year}.parquet"
                )
                self.assertEqual(
                    factor_return.columns,
                    ["date", "factor", "factor_return"],
                )
                covariance = pl.read_parquet(
                    root / "factor_covariance" / f"year={year}.parquet"
                )
                self.assertEqual(
                    covariance.columns,
                    ["date", "factor_a", "factor_b", "covariance"],
                )
                weights = pl.read_parquet(
                    root / "csi300_weight" / f"year={year}.parquet"
                )
                self.assertAlmostEqual(weights.get_column("weight").sum(), 1.0)
                specific_return = pl.read_parquet(
                    root / "specific_return" / f"year={year}.parquet"
                )
                self.assertTrue(
                    specific_return.get_column("specific_return").is_finite().all()
                )
                specific_risk = pl.read_parquet(
                    root / "specific_risk" / f"year={year}.parquet"
                )
                self.assertTrue(
                    (
                        specific_risk.get_column("specific_risk").is_finite()
                        & (specific_risk.get_column("specific_risk") > 0)
                    ).all()
                )

                partition_manifest = json.loads(
                    (root / "partitions" / f"year={year}.json").read_text()
                )
                for name, identity in partition_manifest["files"].items():
                    payload = (root / name).read_bytes()
                    self.assertEqual(identity["sha256"], hashlib.sha256(payload).hexdigest())
                    self.assertEqual(identity["size_bytes"], len(payload))

            catalog = json.loads((root / "factor_catalog.json").read_text())
            self.assertEqual(
                [(item["factor_code"], item["name_cn"]) for item in catalog["factors"]],
                [
                    ("industry_001", "银行"),
                    ("Style_Beta", "贝塔"),
                    ("Size", "规模"),
                ],
            )
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual(manifest["model"], "v2trd")
            self.assertEqual(manifest["industry_mapping"], "sws_2021")
            self.assertEqual(manifest["factor_return_method"], "implicit")
            self.assertEqual(len(manifest["partitions"]), 2)
            for partition_record in manifest["partitions"]:
                self.assertEqual(len(partition_record["files"]), 6)
                for relative_path, identity in partition_record["files"].items():
                    payload = (root / relative_path).read_bytes()
                    self.assertEqual(
                        identity["sha256"], hashlib.sha256(payload).hexdigest()
                    )
            self.assertNotIn("token", json.dumps(manifest).lower())
            self.assertNotIn("username", json.dumps(manifest).lower())

    def test_completed_partitions_are_reused_without_new_provider_calls(self) -> None:
        """捕获恢复运行时重复请求已完成年份。"""

        from factor_miner.ricequant_barra import fetch_ricequant_barra

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            universe_uri = temporary / "universe.parquet"
            pl.DataFrame({"security_id": ["000001.SZ"]}).write_parquet(universe_uri)
            request = self._request(temporary / "barra", universe_uri)
            provider = _FakeRiceQuantProvider()
            fetch_ricequant_barra(request, provider)
            first_call_count = len(provider.calls)

            result = fetch_ricequant_barra(request, provider)

            self.assertEqual(len(provider.calls), first_call_count)
            self.assertEqual(result.reused_years, (2025, 2026))

    def test_quantlake_state_code_column_is_accepted_as_universe(self) -> None:
        """捕获服务器状态表使用 code 字段时无法作为股票全集。"""

        from factor_miner.ricequant_barra import fetch_ricequant_barra

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            universe_uri = temporary / "daily_security_flags.parquet"
            pl.DataFrame(
                {"code": ["000001.SZ", "600000.SH", "430001.BJ"]}
            ).write_parquet(universe_uri)
            provider = _FakeRiceQuantProvider()

            result = fetch_ricequant_barra(
                self._request(temporary / "barra", universe_uri),
                provider,
            )

            self.assertEqual(result.completed_years, (2025, 2026))
            self.assertTrue(provider.security_ids_seen)
            self.assertEqual(
                provider.security_ids_seen[0],
                ("000001.XSHE", "600000.XSHG"),
            )

    def test_corrupt_completed_partition_fails_instead_of_silently_overwriting(self) -> None:
        """捕获已发布分片损坏后被静默覆盖。"""

        from factor_miner.ricequant_barra import fetch_ricequant_barra

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            universe_uri = temporary / "universe.parquet"
            pl.DataFrame({"security_id": ["000001.SZ"]}).write_parquet(universe_uri)
            request = self._request(temporary / "barra", universe_uri)
            fetch_ricequant_barra(request, _FakeRiceQuantProvider())
            (request.derived_root / "exposure" / "year=2025.parquet").write_bytes(b"corrupt")

            with self.assertRaises(FactorMinerError):
                fetch_ricequant_barra(request, _FakeRiceQuantProvider())

    def test_missing_file_in_committed_year_fails_instead_of_reusing(self) -> None:
        """捕获年度提交标记存在但任一数据文件缺失时被误复用。"""

        from factor_miner.ricequant_barra import fetch_ricequant_barra

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            universe_uri = temporary / "universe.parquet"
            pl.DataFrame({"security_id": ["000001.SZ"]}).write_parquet(universe_uri)
            request = self._request(temporary / "barra", universe_uri)
            fetch_ricequant_barra(request, _FakeRiceQuantProvider())
            (request.derived_root / "specific_risk" / "year=2025.parquet").unlink()

            with self.assertRaises(FactorMinerError):
                fetch_ricequant_barra(request, _FakeRiceQuantProvider())

    def test_credentials_loader_requires_private_values_without_returning_them_in_errors(self) -> None:
        """捕获凭据缺失时错误消息泄露已有敏感值。"""

        from factor_miner.ricequant_barra import load_ricequant_credentials

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("RQ_USERNAME=private-user\nOTHER=private-token\n")

            with self.assertRaises(FactorMinerError) as raised:
                load_ricequant_credentials(env_file)

            self.assertNotIn("private-user", str(raised.exception))
            self.assertNotIn("private-token", str(raised.exception))

    def test_cli_exposes_explicit_private_inputs_and_derived_root(self) -> None:
        """捕获 CLI 缺少显式凭据、股票全集或派生根参数。"""

        result = CliRunner().invoke(app, ["barra", "fetch-ricequant", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        plain_help = strip_ansi(result.output)
        self.assertIn("--env-file", plain_help)
        self.assertIn("--universe-uri", plain_help)
        self.assertIn("--derived-root", plain_help)
        self.assertIn("--start", plain_help)
        self.assertIn("--end", plain_help)

    def test_provider_skips_a_batch_with_no_listed_security_data(self) -> None:
        """未来才上市的整批证券返回 None 时，其他有效批次仍应继续。"""

        from factor_miner.ricequant_barra import RqdatacBarraProvider

        provider = object.__new__(RqdatacBarraProvider)

        def fetch(ids, start, end):
            if ids == ["000001.XSHE"]:
                return None
            return pd.DataFrame(
                {
                    "date": [pd.Timestamp("2010-01-04")],
                    "order_book_id": [ids[0]],
                    "size": [1.0],
                }
            )

        frame = provider._batched_panel(
            fetch,
            ("000001.XSHE", "600000.XSHG"),
            date(2010, 1, 1),
            date(2010, 1, 31),
            batch_size=1,
        )

        self.assertEqual(frame.height, 1)
        self.assertEqual(frame["order_book_id"].to_list(), ["600000.XSHG"])


if __name__ == "__main__":
    unittest.main()

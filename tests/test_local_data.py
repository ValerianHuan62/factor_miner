"""跨市场本地数据发布测试。"""

from pathlib import Path
import tempfile
import unittest

import polars as pl

from factor_miner.company_a_share import StandardPanelDataSource
from factor_miner.errors import FactorMinerError
from factor_miner.local_data import prepare_local_release
from factor_miner.runtime import load_runtime_profile


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for asset, base in (("AAPL", 100.0), ("MSFT", 200.0)):
        for offset in range(8):
            open_price = base + offset
            rows.append(
                {
                    "date": f"2026-01-{offset + 2:02d}",
                    "asset": asset,
                    "open": open_price,
                    "high": open_price + 2,
                    "low": open_price - 1,
                    "close": open_price + 1,
                    "volume": 1000 + offset,
                }
            )
    return rows


def _read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text("utf-8").splitlines())


class LocalDataReleaseTest(unittest.TestCase):
    """本地 CSV 可形成与操作系统和市场无关的标准发布。"""

    def test_us_style_csv_builds_visible_standard_panel_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "us.csv"
            pl.DataFrame(_rows()).write_csv(source)
            release = prepare_local_release(
                source,
                root / "release",
                artifact_root=root / "artifacts",
                adjustment_convention="split_adjusted",
                calendar_version="xnys-test-v1",
                assume_tradable=True,
                repository_root=Path(__file__).parents[1],
            )

            profile = load_runtime_profile(_read_env(release.config_path), platform_name="Darwin")
            provenance = StandardPanelDataSource(
                profile,
                profile.code_commit or "",
                profile.config_hash or "",
            ).inspect()

            self.assertEqual(provenance.resolved_release_id, release.release_id)
            state = pl.read_parquet(root / "release" / "state.parquet")
            self.assertEqual(
                set(state.columns),
                {
                    "date",
                    "asset",
                    "valid_for_factor_compute",
                    "valid_for_factor_rank",
                    "valid_for_trading",
                    "can_open_long",
                    "can_close_long",
                },
            )
            label = pl.read_parquet(root / "release" / "label.parquet")
            self.assertEqual(
                {"label_entry_date", "label_exit_date", "label_o2o_5d"},
                set(label.columns).difference({"date", "asset"}),
            )

    def test_missing_masks_require_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.parquet"
            pl.DataFrame(_rows()).write_parquet(source)
            with self.assertRaisesRegex(FactorMinerError, "assume-tradable"):
                prepare_local_release(
                    source,
                    root / "release",
                    artifact_root=root / "artifacts",
                    adjustment_convention="unadjusted",
                    calendar_version="calendar-v1",
                    repository_root=Path(__file__).parents[1],
                )


if __name__ == "__main__":
    unittest.main()

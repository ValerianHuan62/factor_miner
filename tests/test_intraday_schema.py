"""分钟正式源与观察时点合同测试。"""

from pathlib import Path
import tempfile
import unittest

import h5py
import numpy as np
from pydantic import ValidationError

from factor_miner.intraday_data import inspect_hdf5_contract
from factor_miner.intraday_schema import FROZEN_MINUTE_ROOT, IntradaySourceSpec


class IntradaySchemaTest(unittest.TestCase):
    """分钟源只能指向唯一聚合目录，信号只能次日使用。"""

    def test_intraday_source_accepts_only_frozen_directory(self) -> None:
        spec = IntradaySourceSpec(root=FROZEN_MINUTE_ROOT)
        self.assertEqual(
            spec.root,
            Path("/data/quantlake/raw/market/minbar_h5/equities_final_2005_20260630"),
        )
        self.assertEqual(spec.expected_minutes_per_complete_day, 240)
        self.assertEqual(spec.earliest_use, "next_trading_day_open")
        with self.assertRaises(ValidationError):
            IntradaySourceSpec(
                root=Path("/data/quantlake/raw/market/minbar_h5/equities")
            )

    def test_hdf5_contract_reads_metadata_without_rows(self) -> None:
        """合同探针只返回结构、范围和哈希，不返回分钟记录。"""

        dtype = np.dtype(
            [
                ("datetime", "<i8"),
                ("open", "<f8"),
                ("high", "<f8"),
                ("low", "<f8"),
                ("close", "<f8"),
                ("volume", "<f8"),
                ("total_turnover", "<f8"),
                ("num_trades", "<u8"),
            ]
        )
        values = np.zeros(2, dtype=dtype)
        values["datetime"] = [20260630093100, 20260630150000]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.h5"
            with h5py.File(path, "w") as handle:
                handle.create_dataset("data", data=values)
            contract = inspect_hdf5_contract(path)
        self.assertEqual(contract.dataset_key, "data")
        self.assertEqual(contract.row_count, 2)
        self.assertEqual(contract.datetime_min, 20260630093100)
        self.assertEqual(contract.datetime_max, 20260630150000)
        self.assertEqual(contract.columns, IntradaySourceSpec().required_fields)
        self.assertRegex(contract.file_sha256, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()

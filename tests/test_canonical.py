import math
import unittest

from factor_miner.canonical import canonical_json_bytes, sha256_json


class CanonicalJsonTest(unittest.TestCase):
    """规范化 JSON 和内容哈希测试。"""

    def test_key_order_does_not_change_hash(self):
        """验证对象键顺序不影响内容哈希。"""
        self.assertEqual(
            sha256_json({"b": 2, "a": 1}), sha256_json({"a": 1, "b": 2})
        )

    def test_non_finite_number_is_rejected(self):
        """验证非有限数字会被拒绝。"""
        with self.assertRaises(ValueError):
            canonical_json_bytes({"x": math.nan})

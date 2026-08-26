from pathlib import Path
import unittest


class ImportBoundaryTest(unittest.TestCase):
    """独立项目依赖边界测试。"""

    def test_source_does_not_reference_forbidden_dependencies_or_paths(self):
        """验证核心源码不引用禁止的项目、个人路径或动态执行文本。"""
        source_root = Path(__file__).parents[1] / "src" / "factor_miner"
        forbidden = (
            "huan_quant",
            "/Users/",
            "DolphinDB",
            "psycopg",
            "sqlalchemy",
            "eval(",
            "exec(",
        )
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(source_root.rglob("*.py"))
        )
        for text in forbidden:
            with self.subTest(text=text):
                self.assertNotIn(text, source)

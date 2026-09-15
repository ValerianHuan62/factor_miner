"""Dashboard 风格样例合同测试。"""

import unittest

from dashboard.style_gallery import THEMES, preview_html, theme_by_id


class StyleGalleryTest(unittest.TestCase):
    """六套主题必须使用同一研究内容并保持完全本地。"""

    def test_gallery_contains_all_requested_themes(self) -> None:
        self.assertEqual(
            [theme.theme_id for theme in THEMES],
            ["apple", "ferrari", "claude", "spacex", "mastercard", "binance"],
        )

    def test_every_preview_uses_raw_ic_hac_and_calmar(self) -> None:
        for theme in THEMES:
            html = preview_html(theme.theme_id)
            self.assertIn("IC 均值", html)
            self.assertIn("0.0218", html)
            self.assertIn("RankIC HAC t", html)
            self.assertIn("Calmar", html)
            self.assertNotIn("http://", html)
            self.assertNotIn("https://", html)

    def test_unknown_theme_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知 Dashboard 主题"):
            theme_by_id("unknown")


if __name__ == "__main__":
    unittest.main()

"""Frontend static contract tests."""
import ast
import re
import unittest
from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "index.html"
ROOT = INDEX_HTML.parent


def _index_html():
    return INDEX_HTML.read_text(encoding="utf-8")


class TestSourceFilterContract(unittest.TestCase):
    """Source filter: the menu's displayed values must stay consistent with the values JS recognizes."""

    def test_source_filter_values_match_menu_options(self):
        html = _index_html()
        values_match = re.search(r"const SOURCE_FILTER_VALUES = (\[[^\]]+\]);", html)
        self.assertIsNotNone(values_match)

        source_filter_values = ast.literal_eval(values_match.group(1))
        menu_values = re.findall(r'class="source-filter-option"[^>]+data-value="([^"]+)"', html)

        self.assertEqual(menu_values, source_filter_values)
        self.assertIn("antigravity", source_filter_values)


class TestOfflineAssetContract(unittest.TestCase):
    """The public dashboard must not load browser assets from external CDNs."""

    def test_index_has_no_external_asset_hosts(self):
        html = _index_html()
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)
        self.assertNotIn("cdn.jsdelivr.net", html)

    def test_vendor_scripts_are_local_and_present(self):
        html = _index_html()
        for src in ("/vendor/marked.umd.js", "/vendor/purify.min.js"):
            self.assertIn(f'<script src="{src}"></script>', html)
            self.assertTrue((ROOT / src.lstrip("/")).is_file(), src)

    def test_vendor_fonts_are_local_and_present(self):
        html = _index_html()
        for src in (
            "/vendor/fonts/inter-latin-wght-normal.woff2",
            "/vendor/fonts/jetbrains-mono-latin-wght-normal.woff2",
        ):
            self.assertIn(src, html)
            self.assertTrue((ROOT / src.lstrip("/")).is_file(), src)

    def test_mobile_web_app_capable_meta_is_current(self):
        self.assertIn(
            '<meta name="mobile-web-app-capable" content="yes">',
            _index_html(),
        )

    def test_document_language_is_english(self):
        self.assertIn('<html lang="en">', _index_html())


class TestPromptQueueContract(unittest.TestCase):
    """Prompt Queue is visible, but it must not intercept the browser print shortcut."""

    def test_prompt_queue_has_no_global_print_shortcut(self):
        html = _index_html()
        self.assertIn("Prompt Queue", html)
        self.assertNotIn("KeyP", html)
        self.assertNotIn("Cmd+P", html)
        self.assertNotIn("⌘P", html)
        self.assertNotIn("Ctrl+P", html)


class TestConversationMarkdownContract(unittest.TestCase):
    def test_raw_mode_does_not_persist_over_markdown_rendering(self):
        html = _index_html()
        self.assertIn("localStorage.removeItem('conv_raw')", html)
        self.assertNotIn("localStorage.getItem('conv_raw')", html)
        self.assertNotIn("localStorage.setItem('conv_raw'", html)
        self.assertIn("let _convRaw = false;", html)

    def test_folded_markdown_preview_uses_rendered_plain_text(self):
        html = _index_html()
        self.assertIn("function markdownPreviewText", html)
        self.assertIn("const preview = useMd ? (markdownPreviewText(fullInner) || t) : t;", html)
        self.assertNotIn("const head = t.slice(0, headLen);", html)


if __name__ == "__main__":
    unittest.main()

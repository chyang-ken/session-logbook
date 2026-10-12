"""Run the shipped markdown setup against the vendored marked build."""
from pathlib import Path
import shutil
import subprocess
import unittest


class MarkdownTildeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend behavior tests")
    def test_single_tilde_is_literal(self):
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("markdown_tilde_test.js"))],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""Paged search in the dashboard: run the shipped loop from ``index.html`` under Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


class SearchPagingFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend behavior tests")
    def test_list_is_redrawn_for_the_first_and_last_page_only(self):
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("search_paging_frontend_test.js"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

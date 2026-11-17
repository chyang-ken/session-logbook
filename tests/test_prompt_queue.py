"""Exercise shipped Prompt Queue helpers with synthetic browser storage."""
from pathlib import Path
import shutil
import subprocess
import unittest


class PromptQueueTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for frontend behavior tests')
    def test_storage_and_range_copy(self):
        result = subprocess.run(
            [shutil.which('node'), str(Path(__file__).with_name('prompt_queue_test.js'))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

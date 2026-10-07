"""Run the actual frontend refresh functions without a browser dependency."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ConversationLiveTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend behavior tests")
    def test_reader_lifecycle_and_request_races(self):
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("conversation_live_test.js"))],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""Launch the built file and inspect real HTTP responses without WoS access."""
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from scripts.build_dashboard import build


class BundleEntryTests(unittest.TestCase):
    def test_single_file_launch_and_empty_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            app = directory / "DEH_Dashboard.py"
            build(app)
            data_file = directory / "DEH_Research_Data.json"
            child = subprocess.Popen([sys.executable, str(app), "--no-browser", "--data-file", str(data_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: [lines.put(line.rstrip()) for line in child.stdout], daemon=True)
            reader.start()
            try:
                self.assertEqual(lines.get(timeout=10), "DEH research dashboard")
                address_line = lines.get(timeout=10)
                self.assertTrue(address_line.startswith("Open: http://127.0.0.1:"), address_line)
                address = address_line.removeprefix("Open: ").rstrip("/")
                with urllib.request.urlopen(address + "/", timeout=5) as response:
                    html = response.read().decode()
                found = re.search(r'<script id="deh-bootstrap" type="application/json">(.*?)</script>', html, re.S)
                self.assertIsNotNone(found)
                bootstrap = json.loads(found.group(1))
                headers = {"X-DEH-Token": bootstrap["token"]}
                request = urllib.request.Request(address + "/api/state", headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    state = json.load(response)
                self.assertEqual(len(state["faculty"]), 23)
                self.assertEqual(len(state["journals"]), 668)
                self.assertEqual(len({row["journal_key"] for row in state["journals"]}), 662)
                self.assertIsNone(state["snapshot"])
                self.assertIn("Refresh from WoS", html)
                self.assertNotIn("__DEH_BOOTSTRAP_JSON__", html)
                self.assertFalse(data_file.exists())
                request = urllib.request.Request(address + "/api/report", headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 409)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                reader.join(timeout=5)
                child.stdout.close()
                child.stderr.close()


if __name__ == "__main__":
    unittest.main()

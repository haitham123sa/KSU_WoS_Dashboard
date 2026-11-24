"""Guard the reference preview against claiming data or exposing a local cache."""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from scripts.build_dashboard import load_app
from scripts.build_preview import build


class PreviewTests(unittest.TestCase):
    def test_real_reference_inputs_without_network_or_credential(self):
        module = load_app()
        sentinel = "test-only-secret-must-not-be-in-preview"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "preview.html"
            with mock.patch.dict(os.environ, {"WOS_API_KEY": sentinel}), mock.patch.object(module, "StarterClient", side_effect=AssertionError("Preview must not create an API client")), mock.patch("urllib.request.urlopen", side_effect=AssertionError("Preview must not make a network request")):
                metadata = build(output, 2026, app_module=module)
            html = output.read_text(encoding="utf-8")
            match = re.search(r'<script id="deh-bootstrap" type="application/json">(.*?)</script>', html, re.S)
            bootstrap = json.loads(match.group(1))
            self.assertEqual(bootstrap["mode"], "offline")
            self.assertNotIn("token", bootstrap)
            self.assertIsNone(bootstrap["state"]["snapshot"])
            self.assertEqual(len(bootstrap["state"]["faculty"]), 23)
            self.assertEqual(len(bootstrap["state"]["journals"]), 668)
            self.assertEqual(metadata["distinct_journals"], 662)
            self.assertFalse(metadata["has_publication_data"])
            context = bootstrap["state"]["refresh_context"]
            self.assertTrue(context["preview"])
            self.assertEqual(context["default_academic_year"], 2026)
            self.assertTrue(context["url"].endswith("/actions/workflows/refresh.yml"))
            self.assertIn("connect-src 'none'", html)
            self.assertNotIn(sentinel, html)
            self.assertNotIn(str(output.parent), html)
            self.assertNotIn("__DEH_BOOTSTRAP_JSON__", html)

    def test_previous_collected_file_is_neither_loaded_nor_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cache = directory / "DEH_Research_Data.json"
            previous = '{"private_example":"do-not-copy-this-cache-into-the-preview"}'
            cache.write_text(previous, encoding="utf-8")
            output = directory / "DEH_Dashboard_Preview.html"
            build(output, 2025)
            html = output.read_text(encoding="utf-8")
            bootstrap = json.loads(re.search(r'<script id="deh-bootstrap" type="application/json">(.*?)</script>', html, re.S).group(1))
            self.assertIsNone(bootstrap["state"]["snapshot"])
            self.assertNotIn("do-not-copy-this-cache", html)
            self.assertEqual(cache.read_text(encoding="utf-8"), previous)


if __name__ == "__main__":
    unittest.main()

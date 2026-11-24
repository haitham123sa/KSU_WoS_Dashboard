"""Exercise workflow outcomes with synthetic replies and no network access."""
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

from scripts.build_dashboard import load_app
from scripts.refresh import run_refresh


KEY = "synthetic-key-for-refresh-tests"


def passed_probe(key, faculty=None):
    return {"ok": True, "status": "passed", "code": "ready", "message": "Synthetic connection passed.", "next_action": "", "requests_made": 2, "checks": []}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.module = load_app()
        self.module.FACULTY_DATA = self.module.FACULTY_DATA[:2]
        module = self.module

        class FakeClient:
            def __init__(self, api_key, **kwargs):
                self.api_key = api_key
                self.requests = 0

            def get(self, query, page):
                self.requests += 1
                record = {"uid": "WOS:SYNTHETIC-SHARED", "title": "Synthetic shared paper", "source": {"publishYear": 2026, "publishMonth": "AUG", "sourceTitle": "Synthetic journal"}, "citations": [{"db": "WOS", "count": 0}]}
                return {"metadata": {"total": 1, "page": page}, "hits": [record]}, module.now_utc(), {"page": page}

        self.fake_client = FakeClient
        self.module.StarterClient = FakeClient

    def tearDown(self):
        self.directory.cleanup()

    def run_case(self, **kwargs):
        options = {"api_key": KEY, "preflight_runner": passed_probe, "app_module": self.module}
        options.update(kwargs)
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout):
            code = run_refresh(self.output, 2026, **options)
        self.diagnostics = json.loads((self.output / "DEH_Run_Diagnostics.json").read_text())
        self.assertNotIn(KEY, self.stdout.getvalue())
        self.assertNotIn(KEY, json.dumps(self.diagnostics))
        return code

    def test_missing_secret_stops_before_network(self):
        def forbidden(*args, **kwargs):
            self.fail("A missing key must never make an API request")
        self.assertEqual(self.run_case(api_key="", preflight_runner=forbidden), 2)
        self.assertEqual(self.diagnostics["status"], "missing_or_invalid_secret")
        self.assertFalse((self.output / "DEH_Research_Report.html").exists())

    def test_previous_results_are_preserved_without_contacting_api(self):
        previous = self.output / "DEH_Research_Report.html"
        previous.write_text("previous report")
        def forbidden(*args, **kwargs):
            self.fail("An existing output directory must stop before an API request")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run_refresh(self.output, 2026, api_key=KEY, preflight_runner=forbidden, app_module=self.module)
        self.assertEqual(code, 2)
        self.assertEqual(previous.read_text(), "previous report")
        self.assertIn("existing_results_preserved", captured.getvalue())
        self.assertNotIn(KEY, captured.getvalue())
        self.assertFalse((self.output / "DEH_Run_Diagnostics.json").exists())

    def test_preflight_failure_is_visible_and_redacted(self):
        def denied(key, faculty=None):
            return {"ok": False, "code": "authentication_failed", "message": "Synthetic rejected " + key, "next_action": "Replace the rejected key.", "requests_made": 1}
        self.assertEqual(self.run_case(preflight_runner=denied), 3)
        self.assertEqual(self.diagnostics["status"], "preflight_failed")
        self.assertTrue(self.diagnostics["live_api_tested"])
        self.assertIn("[REDACTED]", self.diagnostics["message"])
        self.assertFalse((self.output / "DEH_Research_Data.json").exists())

    def test_diagnostics_only_creates_no_report(self):
        self.assertEqual(self.run_case(diagnostics_only=True), 0)
        self.assertEqual(self.diagnostics["status"], "preflight_passed")
        self.assertFalse((self.output / "DEH_Research_Report.html").exists())

    def test_complete_run_exports_union_and_requested_year(self):
        self.assertEqual(self.run_case(), 0)
        self.assertEqual(self.diagnostics["status"], "complete")
        self.assertEqual(self.diagnostics["requests_made"], 4)
        saved = json.loads((self.output / "DEH_Research_Data.json").read_text())
        snapshot = saved["snapshot"]
        self.assertEqual(len(snapshot["publications"]), 1)
        self.assertEqual(snapshot["requested_academic_year"], 2026)
        union = next(row for row in snapshot["summary_rows"] if row["level"] == "department_union" and row["period"] == "all")
        self.assertEqual(union["publication_total"], 1)
        self.assertEqual(union["current_citations_total"], 0)
        report = (self.output / "DEH_Research_Report.html").read_text()
        self.assertNotIn(KEY, report)
        self.assertNotIn(str(self.output), report)
        match = re.search(r'<script id="deh-bootstrap" type="application/json">(.*?)</script>', report, re.S)
        bootstrap = json.loads(match.group(1))
        self.assertEqual(bootstrap["state"]["refresh_context"]["method"], "github_actions")
        self.assertEqual(bootstrap["state"]["snapshot"]["requested_academic_year"], 2026)

    def test_incomplete_collection_exits_nonzero_with_unavailable_totals(self):
        module = self.module
        class DeniedClient(self.fake_client):
            def get(self, query, page):
                self.requests += 1
                raise module.CollectionError("Synthetic denied", stop_all=True)
        self.module.StarterClient = DeniedClient
        self.assertEqual(self.run_case(), 4)
        self.assertEqual(self.diagnostics["status"], "collection_incomplete")
        saved = json.loads((self.output / "DEH_Research_Data.json").read_text())
        union = next(row for row in saved["snapshot"]["summary_rows"] if row["level"] == "department_union" and row["period"] == "all")
        self.assertIsNone(union["publication_total"])
        self.assertIsNone(union["current_citations_total"])

    def test_valid_empty_search_requires_review(self):
        module = self.module
        class EmptyClient(self.fake_client):
            def get(self, query, page):
                self.requests += 1
                return {"metadata": {"total": 0, "page": page}, "hits": []}, module.now_utc(), {"page": page}
        self.module.StarterClient = EmptyClient
        self.assertEqual(self.run_case(), 4)
        self.assertEqual(self.diagnostics["status"], "all_faculty_no_matches")
        self.assertEqual(self.diagnostics["collection"]["publication_records"], 0)
        self.assertTrue(self.diagnostics["preflight"]["ok"])


if __name__ == "__main__":
    unittest.main()

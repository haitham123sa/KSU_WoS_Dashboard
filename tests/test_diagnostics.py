"""Offline checks: simulated responses only; no live API key or network access."""

import io
import json
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.collector import CollectionError, StarterClient
from src.diagnostics import run_preflight


TEST_KEY = "synthetic-secret-value-123"
FACULTY = {"faculty_key": "synthetic-faculty", "researcher_id": "AAA-1234-2020", "orcid": ""}


def response(total=1, hits=None):
    if hits is None:
        hits = [{"uid": "synthetic:test-record", "citations": [{"db": "WOS", "count": 0}]}] if total else []
    return io.BytesIO(json.dumps({"metadata": {"total": total, "page": 1, "limit": 1}, "hits": hits}).encode())


def http_error(status, key=TEST_KEY):
    body = json.dumps({"error": {"title": "Synthetic test error", "details": "Reflected " + key}}).encode()
    return urllib.error.HTTPError("https://api.clarivate.com/synthetic", status, "test", {}, io.BytesIO(body))


class DiagnosticTests(unittest.TestCase):
    @mock.patch("src.collector.time.sleep")
    def test_connection_and_faculty_queries_are_bounded(self, _sleep):
        with mock.patch("src.collector.urllib.request.urlopen", side_effect=[response(100), response(3)]) as open_url:
            report = run_preflight(TEST_KEY, FACULTY)
        self.assertTrue(report["ok"])
        self.assertEqual(report["requests_made"], 2)
        self.assertEqual([c["matching_records"] for c in report["checks"]], [100, 3])
        for args in open_url.call_args_list:
            request = args.args[0]
            params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            self.assertEqual(params["db"], ["WOS"])
            self.assertEqual(params["limit"], ["1"])
            self.assertEqual(params["page"], ["1"])
            self.assertEqual(args.kwargs["timeout"], 30)
            self.assertEqual(request.get_header("X-apikey"), TEST_KEY)
        self.assertEqual(report["checks"][0]["query"], "PY=2020")
        self.assertEqual(report["checks"][1]["query"], "AI=(AAA-1234-2020)")
        self.assertTrue(report["checks"][0]["current_wos_citation_count_present_in_sample"])
        self.assertNotIn(TEST_KEY, json.dumps(report))

    @mock.patch("src.collector.time.sleep")
    def test_valid_zero_faculty_matches_is_a_warning(self, _sleep):
        with mock.patch("src.collector.urllib.request.urlopen", side_effect=[response(), response(0)]):
            report = run_preflight(TEST_KEY, FACULTY)
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["code"], "faculty_no_matches")
        self.assertEqual(report["checks"][1]["matching_records"], 0)

    def test_http_failures_stop_preflight_and_preserve_safe_detail(self):
        for status, code in [(401, "authentication_rejected"), (403, "access_denied"), (429, "rate_or_quota_limited")]:
            with self.subTest(status=status):
                with mock.patch("src.collector.urllib.request.urlopen", side_effect=http_error(status)) as open_url:
                    report = run_preflight(TEST_KEY, FACULTY)
                self.assertFalse(report["ok"])
                self.assertEqual(report["code"], code)
                self.assertEqual(report["http_status"], status)
                self.assertEqual(open_url.call_count, 1)
                self.assertEqual(report["requests_made"], 1)
                self.assertIn("[REDACTED]", report["provider_detail"])
                self.assertNotIn(TEST_KEY, json.dumps(report))
                if status == 403:
                    self.assertIn("does not confirm", report["next_action"])

    def test_transport_errors_are_distinguished_without_echoing_exception(self):
        failures = [
            (ssl.SSLCertVerificationError(1, TEST_KEY), "tls_certificate_error"),
            (ssl.SSLError(1, TEST_KEY), "tls_connection_error"),
            (socket.gaierror(-2, TEST_KEY), "dns_error"),
            (TimeoutError(TEST_KEY), "network_timeout"),
            (ConnectionRefusedError(TEST_KEY), "network_error"),
        ]
        for error, code in failures:
            with self.subTest(code=code):
                with mock.patch("src.collector.urllib.request.urlopen", side_effect=urllib.error.URLError(error)) as open_url:
                    report = run_preflight(TEST_KEY)
                self.assertFalse(report["ok"])
                self.assertEqual(report["code"], code)
                self.assertEqual(open_url.call_count, 1)
                self.assertNotIn(TEST_KEY, json.dumps(report))

    def test_missing_total_or_records_cannot_be_zero_success(self):
        payloads = [
            {"metadata": {}, "hits": []},
            {"metadata": {"total": True}, "hits": []},
            {"metadata": {"total": 10}, "hits": []},
            {"metadata": {"total": 0}, "hits": [{"uid": "synthetic:test"}]},
            {"metadata": {"total": 1}, "hits": [{}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with mock.patch("src.collector.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                    report = run_preflight(TEST_KEY)
                self.assertFalse(report["ok"])
                self.assertEqual(report["code"], "unexpected_response_schema")

    def test_non_json_response_is_reported(self):
        with mock.patch("src.collector.urllib.request.urlopen", return_value=io.BytesIO(b"<html>sign in</html>")):
            report = run_preflight(TEST_KEY)
        self.assertFalse(report["ok"])
        self.assertEqual(report["code"], "invalid_response_json")

    def test_decoded_credential_reflection_is_redacted(self):
        key = 'synthetic-"quoted-key-123'
        with mock.patch("src.collector.urllib.request.urlopen", side_effect=http_error(400, key)):
            report = run_preflight(key)
        self.assertNotIn(key, json.dumps(report))
        self.assertNotIn("quoted-key", json.dumps(report))
        reflected = {"metadata": {"total": 1}, "hits": [{"uid": "synthetic:test", "title": key, key: [key]}]}
        with mock.patch("src.collector.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(reflected).encode())):
            payload, _, _ = StarterClient(key, interval=0).get("PY=2020", 1)
        self.assertNotIn("quoted-key", json.dumps(payload))
        self.assertEqual(payload["hits"][0]["title"], "[REDACTED]")

    @mock.patch("src.collector.time.sleep")
    def test_full_collector_retries_transient_errors_but_not_bad_credentials(self, _sleep):
        client = StarterClient(TEST_KEY, interval=0)
        with mock.patch("src.collector.urllib.request.urlopen", side_effect=[http_error(503) for _ in range(4)]) as open_url:
            with self.assertRaises(CollectionError) as caught:
                client.get("PY=2020", 1)
        self.assertEqual(open_url.call_count, 4)
        self.assertEqual(caught.exception.code, "api_service_error")
        with mock.patch("src.collector.urllib.request.urlopen", side_effect=http_error(401)) as open_url:
            with self.assertRaises(CollectionError):
                StarterClient(TEST_KEY, interval=0).get("PY=2020", 1)
        self.assertEqual(open_url.call_count, 1)

    def test_missing_secret_makes_no_request(self):
        factory = mock.Mock()
        report = run_preflight("", FACULTY, client_factory=factory)
        self.assertFalse(report["ok"])
        self.assertEqual(report["code"], "missing_api_key")
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

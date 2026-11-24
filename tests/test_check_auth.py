"""Offline authentication probes; no real API requests or credential values."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_auth import STATUS_MARKER, run_auth_check

KEY = 'synthetic-"secret\\value-123'


def result(status, payload, returncode=0, stderr=""):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, body + STATUS_MARKER + str(status).zfill(3) + "\n", stderr)


class AuthenticationCheckTests(unittest.TestCase):
    def run_check(self, *, key=KEY, trigger=None, replies=None, runner=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        if trigger is None:
            trigger = {"preflight": {"code": "authentication_rejected", "http_status": 401}}
        (folder / "DEH_Run_Diagnostics.json").write_text(json.dumps(trigger))
        runner = runner or mock.Mock(side_effect=replies)
        sleeper = mock.Mock()
        output = io.StringIO()
        environment_key = key if "\x00" not in key else "synthetic-environment-placeholder"
        injected = {"api_key": key} if "\x00" in key else {}
        with contextlib.redirect_stdout(output), mock.patch.dict("os.environ", {"WOS_API_KEY": environment_key}):
            code = run_auth_check(folder, runner=runner, sleeper=sleeper, **injected)
        report = json.loads((folder / "DEH_Authentication_Check.json").read_text())
        self.assertNotIn(KEY, json.dumps(report) + output.getvalue())
        return code, report, runner, sleeper, output.getvalue(), folder

    def test_bounded_requests_use_stdin_config_without_argument_or_file_leak(self):
        error = {"error_description": "Bad credentials " + KEY, "error": "Bad credentials"}
        code, report, runner, sleeper, printed, folder = self.run_check(replies=[result(401, error), result(401, error)])
        self.assertEqual(code, 0)
        self.assertEqual(report["requests_made"], 2)
        self.assertEqual([c["version"] for c in report["checks"]], ["v1", "v2"])
        self.assertTrue(all(c["http_status"] == 401 for c in report["checks"]))
        self.assertIn("[REDACTED]", report["checks"][0]["provider_detail"])
        sleeper.assert_called_once_with(1)
        for call in runner.call_args_list:
            args, kwargs = call.args[0], call.kwargs
            self.assertNotIn(KEY, " ".join(args))
            self.assertEqual(args[:2], ["curl", "--disable"])
            self.assertIn("--http1.1", args)
            self.assertNotIn("--location", args)
            self.assertNotIn("--insecure", args)
            self.assertEqual(args[args.index("--retry") + 1], "0")
            self.assertEqual(args[args.index("--max-redirs") + 1], "0")
            self.assertEqual(args[args.index("--connect-timeout") + 1], "10")
            self.assertEqual(args[args.index("--max-time") + 1], "30")
            self.assertEqual(args[args.index("--config") + 1], "-")
            self.assertNotIn("WOS_API_KEY", kwargs["env"])
            self.assertTrue(kwargs["capture_output"])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 35)
            config = kwargs["input"]
            self.assertIn('header = "X-ApiKey: synthetic-\\"secret\\\\value-123"', config)
            self.assertIn('header = "Accept: application/json"', config)
            self.assertIn('user-agent = "DEH-WoS-Collector/1.0"', config)
        self.assertEqual({p.name for p in folder.iterdir()}, {"DEH_Run_Diagnostics.json", "DEH_Authentication_Check.json"})
        self.assertNotIn("provider_detail", printed)

    def test_success_reports_counts_without_publication_or_secret_content(self):
        payload = {"metadata": {"total": 8, "page": 1, "limit": 1}, "hits": [{"uid": "synthetic:private", "title": KEY, "citations": [{"db": "WOS", "count": 0}]}]}
        code, report, *_ = self.run_check(replies=[result(200, payload), result(200, {"metadata": {"total": 0}, "hits": []})])
        self.assertEqual(code, 0)
        self.assertTrue(all(c["valid_response_schema"] for c in report["checks"]))
        self.assertEqual(report["checks"][0]["matching_records"], 8)
        self.assertTrue(report["checks"][0]["current_wos_citation_count_present_in_sample"])
        self.assertNotIn("synthetic:private", json.dumps(report))
        self.assertNotIn("title", json.dumps(report))

    def test_bad_200_is_not_success(self):
        code, report, *_ = self.run_check(replies=[result(200, "not json " + KEY), result(200, {"metadata": {"total": 1}, "hits": []})])
        self.assertEqual(code, 0)
        self.assertEqual([c["error_category"] for c in report["checks"]], ["invalid_response_json", "unexpected_response_schema"])
        self.assertFalse(any(c["valid_response_schema"] for c in report["checks"]))

    def test_missing_or_control_character_key_stops_without_requests(self):
        for key in ("", "   ", "synthetic\nkey", "synthetic\r", "synthetic\x00key"):
            with self.subTest(case=repr(key)):
                code, report, runner, sleeper, *_ = self.run_check(key=key)
                self.assertEqual(code, 0)
                self.assertEqual(report["status"], "input_issue")
                self.assertEqual(report["requests_made"], 0)
                runner.assert_not_called()
                sleeper.assert_not_called()

    def test_non_401_preflight_skips(self):
        for trigger in ({}, {"preflight": {"code": "authentication_rejected", "http_status": 403}}, {"preflight": {"code": "ready", "http_status": 401}}):
            code, report, runner, *_ = self.run_check(trigger=trigger)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "skipped")
            runner.assert_not_called()

    def test_format_flags_do_not_return_fragments_or_modify_key(self):
        for key, flag in [('"synthetic-value"', "surrounding_quotes"), ("********", "masking_characters"), ("your_api_key", "placeholder_value"), ("X-ApiKey: synthetic-value", "header_or_variable_assignment")]:
            code, report, runner, *_ = self.run_check(key=key, replies=[result(401, {}), result(401, {})])
            self.assertEqual(code, 0)
            self.assertTrue(report["format_flags"][flag])
            self.assertTrue(all(isinstance(value, bool) for value in report["format_flags"].values()))
            self.assertNotIn(key, json.dumps(report))
            self.assertIn(key.replace('"', '\\"'), runner.call_args_list[0].kwargs["input"])

    def test_transport_and_internal_errors_do_not_echo_captured_text(self):
        code, report, *_ = self.run_check(replies=[result(0, KEY, returncode=60, stderr=KEY), result(302, {"error": KEY})])
        self.assertEqual(code, 0)
        self.assertEqual(report["checks"][0]["error_category"], "tls_certificate_error")
        self.assertEqual(report["checks"][1]["error_category"], "redirect_not_followed")
        code, report, runner, *_ = self.run_check(runner=mock.Mock(side_effect=FileNotFoundError(KEY)))
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "internal_error")
        self.assertEqual(report["error_type"], "FileNotFoundError")
        self.assertEqual(report["requests_made"], 0)
        self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()

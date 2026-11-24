#!/usr/bin/env python3
"""Make two bounded, credential-safe curl probes after a confirmed HTTP 401.

Read WOS_API_KEY from the environment; never accept a credential argument.
The credential is supplied to curl through stdin configuration only. Responses
are held in memory, reduced to diagnostic fields, and discarded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
STATUS_MARKER = "\nDEH_HTTP_STATUS:"
VERSIONS = ("v1", "v2")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _format_flags(raw, key):
    quote_pairs = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))
    return {
        "missing": not bool(key),
        "control_characters": any(ord(char) < 32 or ord(char) == 127 for char in raw),
        "surrounding_quotes": any(key.startswith(a) and key.endswith(b) for a, b in quote_pairs) if key else False,
        "masking_characters": any(char in key for char in "*\u2022\u25cf\u25e6"),
        "placeholder_value": bool(re.fullmatch(r"(?:<[^>]+>|your[ _-]*(?:wos[ _-]*)?(?:api[ _-]*)?key|(?:paste|insert|replace)[ _-]*(?:your[ _-]*)?(?:api[ _-]*)?key(?:[ _-]*here)?|WOS_API_KEY|API_KEY|YOUR_KEY|CHANGE_ME|TODO)", key, re.I)),
        "header_or_variable_assignment": bool(re.match(r"^(?:export\s+)?(?:x[-_]api[-_]?key|wos_api_key|api[-_ ]?key)\s*[:=]", key, re.I)),
    }


def _config_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _provider_detail(payload, key):
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    values = [payload.get("error_description")]
    if isinstance(error, dict):
        values.extend(error.get(field) for field in ("title", "details", "message"))
    elif isinstance(error, str):
        values.append(error)
    cleaned = []
    for value in values:
        if isinstance(value, str):
            # Scrub after JSON decoding and before truncation.
            value = value.replace(key, "[REDACTED]") if key else value
            cleaned.append(" ".join(value.split())[:500])
    return "; ".join(cleaned)[:1000]


def _sample_summary(payload):
    if not isinstance(payload, dict):
        return None
    metadata, hits = payload.get("metadata"), payload.get("hits")
    if not isinstance(metadata, dict) or not isinstance(hits, list):
        return None
    total = metadata.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    if metadata.get("page", 1) != 1 or metadata.get("limit", 1) != 1 or len(hits) != min(total, 1):
        return None
    if any(not isinstance(row, dict) or not isinstance(row.get("uid"), str) or not row["uid"] for row in hits):
        return None
    citation_present = None
    if hits:
        entries = hits[0].get("citations")
        values = [entry.get("count") for entry in entries if isinstance(entry, dict) and str(entry.get("db", "")).upper() == "WOS"] if isinstance(entries, list) else []
        citation_present = bool(values) and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values) and len(set(values)) == 1
    return {"matching_records": total, "sampled_records": len(hits), "current_wos_citation_count_present_in_sample": citation_present}


def _probe(version, key, runner):
    endpoint = f"https://api.clarivate.com/apis/wos-starter/{version}/documents"
    params = {"db": "WOS", "q": "PY=2020", "limit": 1, "page": 1, "sortField": "LD+A"}
    args = ["curl", "--disable", "--silent", "--show-error", "--http1.1", "--config", "-",
            "--proto", "=https", "--connect-timeout", "10", "--max-time", "30", "--retry", "0",
            "--max-redirs", "0", "--request", "GET", "--url", endpoint + "?" + urllib.parse.urlencode(params),
            "--write-out", STATUS_MARKER + "%{http_code}\n"]
    config = "header = " + _config_quote("X-ApiKey: " + key) + "\nheader = \"Accept: application/json\"\nuser-agent = \"DEH-WoS-Collector/1.0\"\n"
    environment = {name: value for name, value in os.environ.items() if name != "WOS_API_KEY"}
    check = {"version": version, "client": "curl", "endpoint": endpoint, "http_status": None,
             "status": "transport_error", "error_category": "", "provider_detail": "", "valid_response_schema": False}
    try:
        completed = runner(args, input=config, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=35, check=False, shell=False, env=environment)
    except subprocess.TimeoutExpired:
        check["error_category"] = "network_timeout"
        return check
    finally:
        config = ""
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    body, separator, tail = stdout.rpartition(STATUS_MARKER)
    status_text = tail.strip() if separator else ""
    if re.fullmatch(r"\d{3}", status_text) and status_text != "000":
        check["http_status"] = int(status_text)
    if completed.returncode:
        check["error_category"] = {5: "proxy_resolution_error", 6: "dns_error", 7: "connection_error", 28: "network_timeout", 35: "tls_connection_error", 51: "tls_certificate_error", 60: "tls_certificate_error"}.get(completed.returncode, "curl_transport_error")
        return check
    if not separator or check["http_status"] is None:
        raise RuntimeError("Unexpected curl status framing")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = None
    status = check["http_status"]
    check["provider_detail"] = _provider_detail(payload, key)
    if status == 200:
        summary = _sample_summary(payload)
        if summary is None:
            check.update(status="invalid_response", error_category="invalid_response_json" if payload is None else "unexpected_response_schema")
        else:
            check.update(status="passed", error_category="", valid_response_schema=True, **summary)
    else:
        category = {401: "authentication_rejected", 403: "access_denied", 429: "rate_or_quota_limited"}.get(status)
        if category is None:
            category = "redirect_not_followed" if 300 <= status < 400 else ("api_service_error" if status >= 500 else "http_error")
        check.update(status="http_error", error_category=category)
    return check


def run_auth_check(output_dir, *, api_key=None, runner=None, sleeper=None):
    output_dir = Path(output_dir)
    diagnostic_path = output_dir / "DEH_Run_Diagnostics.json"
    result_path = output_dir / "DEH_Authentication_Check.json"
    report = {"status": "skipped", "message": "No confirmed HTTP 401 preflight requires an independent check.",
              "next_action": "Read the normal connection diagnostics.", "checked_at": _now(), "requests_made": 0,
              "format_flags": {}, "checks": []}
    key = ""
    exit_code = 0
    try:
        if diagnostic_path.exists():
            saved = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            preflight = saved.get("preflight", {}) if isinstance(saved, dict) else {}
            if isinstance(preflight, dict) and preflight.get("code") == "authentication_rejected" and preflight.get("http_status") == 401:
                raw = os.environ.get("WOS_API_KEY", "") if api_key is None else api_key
                raw = raw if isinstance(raw, str) else ""
                key = raw.strip()
                report["format_flags"] = _format_flags(raw, key)
                if report["format_flags"]["missing"] or report["format_flags"]["control_characters"]:
                    report.update(status="input_issue", message="The saved key is missing or contains control characters; no requests were sent.", next_action="Correct the saved secret, then repeat the normal connection test.")
                else:
                    for index, version in enumerate(VERSIONS):
                        if index:
                            (sleeper or time.sleep)(1)
                        report["checks"].append(_probe(version, key, runner or subprocess.run))
                        report["requests_made"] += 1
                    checks = report["checks"]
                    report.update(status="completed", message="The two independent curl checks completed.", next_action="Review the HTTP status and response validity for each endpoint before changing the collector.")
                    if all(check["http_status"] == 401 for check in checks):
                        report.update(message="Both documented Starter endpoints rejected the supplied key with HTTP 401 using curl.", next_action="Review any format flags and confirm the application's Starter subscription and key with Clarivate. This result does not establish why credentials were rejected.")
                    elif any(check["status"] == "passed" for check in checks):
                        report.update(message="Curl received a valid Starter response from at least one documented endpoint.", next_action="Compare the endpoint and client results with the original Python failure before changing collection code.")
    except Exception as exc:
        exit_code = 1
        report.update(status="internal_error", message="The independent authentication check could not finish.", next_action="Review the recorded exception type and the workflow setup.", error_type=type(exc).__name__)
    finally:
        # Fixed schema plus post-decode redaction prevent raw response or key output.
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if key:
            encoded = encoded.replace(key, "[REDACTED]")
        key = ""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        result_path.write_text(encoded + "\n", encoding="utf-8")
    except OSError:
        print("DEH authentication check: could not save the diagnostic report.", flush=True)
        return 1
    print("DEH authentication check: " + json.dumps({"status": report["status"], "requests_made": report["requests_made"], "diagnostics_file": result_path.name}), flush=True)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args(argv)
    return run_auth_check(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())

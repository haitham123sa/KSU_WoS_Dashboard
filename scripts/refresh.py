#!/usr/bin/env python3
"""Collect from WoS in GitHub Actions and export a single offline dashboard.

Use a fresh output directory for each run. Existing DEH results are preserved
and cause an early exit, so a failed attempt cannot export an older report as
new data. Every GitHub-hosted workflow run starts with a fresh results folder.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_dashboard import load_app


def current_academic_year():
    today = dt.datetime.now(ZoneInfo("Asia/Riyadh"))
    return today.year if today.month >= 8 else today.year - 1


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    return value


def save_diagnostics(path, value, secret):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(value, secret), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def github_refresh_context():
    return {
        "mode": "remote",
        "method": "cloudflare_worker",
        "workflow_name": "Refresh research data",
        "api_base": "https://deh-research-api.almansour-h.workers.dev"
    }


def run_refresh(output_dir, academic_year_start, diagnostics_only=False, *, api_key=None, preflight_runner=None, app_module=None):
    """Return a process exit code. Injectable clients are used only by tests."""
    output_dir = Path(output_dir).expanduser().resolve()
    existing = [name for name in ("DEH_Research_Data.json", "DEH_Research_Report.html", "DEH_Run_Diagnostics.json") if (output_dir / name).exists()]
    if existing:
        print("DEH result: " + json.dumps({"status": "existing_results_preserved", "message": "This output directory already contains DEH results. Choose a new --output-dir for this run; the previous files have been preserved."}), flush=True)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_path = output_dir / "DEH_Run_Diagnostics.json"
    started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    secret = os.environ.get("WOS_API_KEY", "") if api_key is None else api_key
    secret = secret.strip() if isinstance(secret, str) else ""
    result = {"started_at": started_at, "academic_year_start": academic_year_start, "mode": "diagnostics_only" if diagnostics_only else "collection", "live_api_tested": False}

    def finish(code, status, message, next_action=""):
        result.update(exit_code=code, status=status, message=message, next_action=next_action,
                      finished_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        save_diagnostics(diagnostics_path, result, secret)
        print("DEH result: " + json.dumps({"status": status, "message": redact(message, secret), "diagnostics_file": diagnostics_path.name}, ensure_ascii=False), flush=True)
        return code

    if not (8 <= len(secret) <= 512) or any(ord(char) < 32 or ord(char) == 127 for char in secret):
        return finish(2, "missing_or_invalid_secret", "A valid WOS_API_KEY secret is required before contacting Web of Science.", "In the repository, open Settings → Secrets and variables → Actions and add WOS_API_KEY using your Starter API key. Do not put the key in a file or workflow input.")
    if not isinstance(academic_year_start, int) or isinstance(academic_year_start, bool) or not 1000 <= academic_year_start <= 9998:
        return finish(2, "invalid_academic_year", "The academic-year starting year is invalid.", "Use a four-digit starting year, for example 2025 for 1 August 2025 through 31 July 2026.")

    try:
        module = app_module or load_app()
        if preflight_runner is None:
            from src.diagnostics import run_preflight
            preflight_runner = run_preflight
        print("Checking the WoS Starter connection before collecting the faculty list.", flush=True)
        preflight = preflight_runner(secret, faculty=module.FACULTY_DATA[0])
        result["preflight"] = redact(preflight, secret)
        result["live_api_tested"] = bool(preflight.get("requests_made", 0))
        if not preflight.get("ok"):
            return finish(3, "preflight_failed", preflight.get("message", "The connection check failed."), preflight.get("next_action", "Read the connection check details in DEH_Run_Diagnostics.json."))
        if diagnostics_only:
            return finish(0, "preflight_" + str(preflight.get("status", "passed")), preflight.get("message", "The API connection check completed."), preflight.get("next_action", "Run again with diagnostics_only disabled to collect the faculty list."))

        data_path = output_dir / "DEH_Research_Data.json"
        app = module.DehDashboardApp(data_path)
        app.refresh(secret, academic_year_start)
        last_progress = None
        while app.worker and app.worker.is_alive():
            app.worker.join(timeout=1)
            job = app.status()["job"]
            progress = (job.get("done"), job.get("total"), job.get("requests_made"))
            if progress != last_progress:
                print(f"Faculty searches finished: {progress[0]}/{progress[1]}; collection API requests: {progress[2]}.", flush=True)
                last_progress = progress

        state = app.public_state(for_export=True)
        snapshot = state.get("snapshot")
        job = app.status()["job"]
        result["collection"] = {"job": redact(job, secret), "storage_warning": app.storage_warning,
                                "publication_records": len(snapshot.get("publications", [])) if snapshot else None,
                                "faculty_results": snapshot.get("states", {}) if snapshot else {},
                                "run_status": snapshot.get("run_status") if snapshot else None}
        result["requests_made"] = int(preflight.get("requests_made", 0)) + int(job.get("requests_made", 0))
        if snapshot is not None:
            report_path = output_dir / "DEH_Research_Report.html"
            report_path.write_bytes(app.render_html(offline=True, refresh_context=github_refresh_context()))
            result["report_file"] = report_path.name
        if app.storage_warning:
            return finish(5, "storage_failed", app.storage_warning, "Review the report if one was produced. Use a writable output directory and preserve any existing data file before trying again.")
        if job.get("status") != "complete" or snapshot is None or snapshot.get("run_status") != "complete":
            return finish(4, "collection_incomplete", job.get("message", "The collection was incomplete."), "Open DEH_Run_Diagnostics.json to identify the failed faculty searches. Incomplete totals are unavailable in the report.")
        if not snapshot.get("publications"):
            return finish(4, "all_faculty_no_matches", "The API accepted the searches but returned no publication records for the supplied faculty identifiers.", "Verify that these identifiers are associated with publications in Web of Science Core Collection. This is an empty search result, not evidence that the API connection failed.")
        return finish(0, "complete", "The faculty collection completed and the dashboard report was generated.", "Download the run artifact, unzip it, and open DEH_Research_Report.html. To refresh again, run this workflow again.")
    except Exception as exc:
        # Never print arbitrary exception text: upstream clients may include credentials.
        result["internal_error_type"] = type(exc).__name__
        return finish(5, "internal_error", "The run stopped before it could finish preparing the results.", "Review the diagnostics file and the workflow step that failed. The exception type is recorded without credential-bearing text.")
    finally:
        secret = ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--academic-year-start", type=int, default=current_academic_year(), help="Starting year for an August–July reporting period; defaults to the current academic year in Riyadh")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results", help="Fresh output directory; prior DEH result files cause an early exit and are preserved")
    parser.add_argument("--diagnostics-only", action="store_true", help="Check API access and one faculty identifier, without a full collection")
    args = parser.parse_args(argv)
    return run_refresh(args.output_dir, args.academic_year_start, args.diagnostics_only)


if __name__ == "__main__":
    raise SystemExit(main())

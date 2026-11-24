#!/usr/bin/env python3
"""Build one offline HTML dashboard from reference inputs, without WoS calls.

The preview uses the same UI and offline renderer as a collected report. It
contains the supplied faculty and JCR references, but deliberately has no
publication snapshot. Building it never loads a user's collected data file,
reads an API credential, starts a server, or contacts Web of Science.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_dashboard import load_app
from scripts.refresh import current_academic_year


DEFAULT_OUTPUT = ROOT / "dist" / "DEH_Dashboard_Preview.html"
REPOSITORY_URL = "https://github.com/haitham123sa/deh-research-dashboard"


def build(output=DEFAULT_OUTPUT, academic_year_start=None, *, app_module=None):
    """Write a reference-only dashboard and return its verified file metadata."""
    year = current_academic_year() if academic_year_start is None else academic_year_start
    if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9998:
        raise ValueError("Use a four-digit academic-year starting year.")
    output = Path(output).expanduser().resolve()
    module = app_module or load_app()
    context = {
        "mode": "remote",
        "method": "cloudflare_worker",
        "workflow_name": "Refresh research data",
        "api_base": "https://deh-research-api.almansour-h.workers.dev",
        "preview": True,
        "default_academic_year": year,
        "preview_message": "No Web of Science publication data has been collected. Faculty and journal references are available to explore.",
    }

    # An isolated, empty directory prevents an old cache beside either the
    # launcher or the requested output from turning this preview into a report.
    with tempfile.TemporaryDirectory(prefix="deh-reference-preview-") as temporary:
        app = module.DehDashboardApp(Path(temporary) / "uncollected.json")
        if app.snapshot is not None:
            raise ValueError("A reference preview must not contain collected publications.")
        encoded = app.render_html(offline=True, refresh_context=context)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    return {
        "file": str(output),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview_only": True,
        "has_publication_data": False,
        "academic_year_start": year,
        "faculty_count": len(module.FACULTY_DATA),
        "journal_category_rows": len(module.JOURNAL_DATA),
        "distinct_journals": len({row["journal_key"] for row in module.JOURNAL_DATA}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--academic-year-start", type=int, default=None, help="Initial August–July starting year; defaults to the current year in Riyadh")
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output, args.academic_year_start), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the self-contained dashboard using only Python's standard library."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import pprint
import types

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "DEH_Dashboard.py"


def _collector_module():
    spec = importlib.util.spec_from_file_location("deh_build_collector", ROOT / "src" / "collector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_source():
    """Validate input data and return a standalone Python program as text."""
    source = (ROOT / "src" / "collector.py").read_text(encoding="utf-8")
    marker = "\ndef main(argv=None):"
    if source.count(marker) != 1:
        raise ValueError("Collector entry point changed; inspect the build boundary.")
    core = source.split(marker)[0]
    parsed = ast.parse(core)
    first = parsed.body[0]
    doc_end = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str) else 0
    core = "\n".join(core.splitlines()[doc_end:]).lstrip()
    collector = _collector_module()
    faculties, journals, warnings = collector.load_inputs(ROOT / "data" / "faculty.csv", ROOT / "data" / "journal_categories.csv")
    html = (ROOT / "src" / "ui.html").read_text(encoding="utf-8")
    if html.count("__DEH_BOOTSTRAP_JSON__") != 1:
        raise ValueError("The UI must have exactly one data placeholder.")
    provenance = json.loads((ROOT / "data" / "input_manifest.json").read_text(encoding="utf-8"))
    preface = '''#!/usr/bin/env python3
"""DEH Research Dashboard — self-contained build.

Run: python3 DEH_Dashboard.py
No third-party Python packages are needed. The app opens a local browser
dashboard, and collected data are saved beside this file. API keys are never
saved. GitHub users can run the Refresh research data workflow instead.
"""
'''
    constants = "\n\n# Validated data embedded at build time.\n"
    for name, value in (("FACULTY_DATA", faculties), ("JOURNAL_DATA", journals), ("INPUT_PROVENANCE", provenance), ("INPUT_WARNINGS", warnings)):
        constants += name + " = " + pprint.pformat(value, sort_dicts=False, width=110) + "\n\n"
    constants += "UI_HTML = " + repr(html) + "\n\n"
    result = preface + core + constants + (ROOT / "src" / "backend.py").read_text(encoding="utf-8") + "\n"
    ast.parse(result)
    return result


def load_app():
    """Load a fresh app module in memory; do not start a server or read a key."""
    module = types.ModuleType("deh_dashboard")
    module.__file__ = str(DEFAULT_OUTPUT)
    exec(compile(build_source(), str(DEFAULT_OUTPUT), "exec"), module.__dict__)
    return module


def build(output=DEFAULT_OUTPUT):
    output = Path(output).expanduser().resolve()
    encoded = build_source().encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    output.chmod(0o755)
    return {"file": str(output), "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

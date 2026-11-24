#!/usr/bin/env python3
"""Launch the local dashboard from this repository without a build step."""
from pathlib import Path
import sys

from scripts.build_dashboard import load_app


if __name__ == "__main__":
    module = load_app()
    # Keep the local cache in ignored results/ when running from the repository.
    args = sys.argv[1:]
    if "--data-file" not in args and not any(value.startswith("--data-file=") for value in args):
        args += ["--data-file", str(Path(__file__).resolve().parent / "results" / "DEH_Research_Data.json")]
    raise SystemExit(module.app_main(args))

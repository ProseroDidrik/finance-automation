#!/usr/bin/env python3
"""
run_all.py  –  Kör alla landsprocesser i sekvens.

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py run_all.py              # kör alla landsprocesser
    py run_all.py --dry-run    # dry-run alla
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = [
    "process_sweden.py",
    "process_norway.py",
    "process_finland.py",
    "process_denmark.py",
    "process_germany.py",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Kör alla landsprocesser i sekvens.")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Dry-run alla skript")
    args = parser.parse_args()

    extra = ["--dry-run"] if args.dry_run else []
    prefix = "[DRY-RUN] " if args.dry_run else ""
    base = Path(__file__).resolve().parent
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    errors = []

    for script in SCRIPTS:
        print(f"\n{'='*60}")
        print(f"{prefix}Kör {script}...")
        print("=" * 60)
        result = subprocess.run([sys.executable, str(base / script)] + extra, env=env)
        if result.returncode != 0:
            print(f"\nFEL: {script} avslutades med kod {result.returncode}")
            errors.append(script)

    print(f"\n{'='*60}")
    if errors:
        print(f"Klart med FEL i: {', '.join(errors)}")
    else:
        print("Alla landsprocesser klara.")


if __name__ == "__main__":
    main()

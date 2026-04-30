#!/usr/bin/env python3
"""
reset.py  –  Återställ alla processhanterade filer för ny körning.

Vad scriptet gör:
  1. Flyttar tillbaka filer från extracted/{Country}/Referens/ till extracted/{Country}/
  2. Raderar filer i extracted/{Country}/output/ (Finland, Denmark, Germany)

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py reset.py              # återställ alla länder
    py reset.py --dry-run    # preview utan ändringar
"""

import argparse
import shutil
from pathlib import Path

from shared import safe_dest

GET_TESTFILES = Path(
    r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister"
    r"\Phoenix Foundation\April alla filer\Get testfiles"
)

COUNTRIES = ["Sweden", "Norway", "Finland", "Denmark", "Germany"]


def reset_country(country: str, dry_run: bool) -> None:
    base = GET_TESTFILES / "extracted" / country
    referens = base / "Referens"
    output = base / "output"

    moved = 0
    deleted = 0

    if referens.exists():
        for src in sorted(f for f in referens.iterdir() if f.is_file()):
            dest = safe_dest(base / src.name)
            if dry_run:
                print(f"  [dry] FLYTTA  Referens/{src.name} ->{dest.name}")
            else:
                shutil.move(str(src), str(dest))
                print(f"  FLYTTA  Referens/{src.name} ->{dest.name}")
            moved += 1

    if output.exists():
        for f in sorted(f for f in output.iterdir() if f.is_file()):
            if dry_run:
                print(f"  [dry] RADERA  output/{f.name}")
            else:
                f.unlink()
                print(f"  RADERA  output/{f.name}")
            deleted += 1

    if moved == 0 and deleted == 0:
        print("  (inget att återställa)")
    else:
        print(f"  ->{moved} filer återställda, {deleted} output-filer raderade")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Återställ alla processhanterade filer för ny körning."
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="Visa utan att ändra")
    args = parser.parse_args()

    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(f"{prefix}Återställer filer för ny körning...\n")

    for country in COUNTRIES:
        print(f"=== {country} ===")
        reset_country(country, args.dry_run)
        print()


if __name__ == "__main__":
    main()

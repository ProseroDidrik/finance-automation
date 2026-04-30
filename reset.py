#!/usr/bin/env python3
"""
reset.py  –  Återställ alla processhanterade filer för ny körning.

Vad scriptet gör:
  1. Flyttar tillbaka filer från extracted/{period}/{Country}/Referens/ till extracted/{period}/{Country}/
  2. Raderar filer i extracted/{period}/{Country}/output/

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py reset.py --period 202604      # återställ en specifik period
    py reset.py                      # återställ alla perioder
    py reset.py --dry-run            # preview utan ändringar
"""

import argparse
import re
import shutil
from pathlib import Path

from shared import load_config, safe_dest

GET_TESTFILES = Path(load_config()["base_path"])
EXTRACTED = GET_TESTFILES / "extracted"

PERIOD_RE = re.compile(r"^\d{6}$")
COUNTRIES = ["Sweden", "Norway", "Finland", "Denmark", "Germany"]


def reset_country(country_dir: Path, dry_run: bool) -> tuple[int, int]:
    referens = country_dir / "Referens"
    output = country_dir / "output"
    moved = 0
    deleted = 0

    if referens.exists():
        for src in sorted(f for f in referens.iterdir() if f.is_file()):
            dest = safe_dest(country_dir / src.name)
            if dry_run:
                print(f"  [dry] FLYTTA  Referens/{src.name} -> {dest.name}")
            else:
                shutil.move(str(src), str(dest))
                print(f"  FLYTTA  Referens/{src.name} -> {dest.name}")
            moved += 1

    if output.exists():
        for f in sorted(f for f in output.iterdir() if f.is_file()):
            if dry_run:
                print(f"  [dry] RADERA  output/{f.name}")
            else:
                f.unlink()
                print(f"  RADERA  output/{f.name}")
            deleted += 1

    return moved, deleted


def reset_period(period: str, dry_run: bool) -> None:
    period_dir = EXTRACTED / period
    if not period_dir.exists():
        print(f"  (mapp saknas: {period_dir})")
        return

    total_moved = total_deleted = 0
    for country in COUNTRIES:
        country_dir = period_dir / country
        if not country_dir.exists():
            continue
        print(f"  --- {country} ---")
        moved, deleted = reset_country(country_dir, dry_run)
        if moved == 0 and deleted == 0:
            print("    (inget att återställa)")
        else:
            print(f"    {moved} filer återställda, {deleted} output-filer raderade")
        total_moved += moved
        total_deleted += deleted

    print(f"  Totalt: {total_moved} filer återställda, {total_deleted} output-filer raderade")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Återställ alla processhanterade filer för ny körning."
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="Visa utan att ändra")
    parser.add_argument(
        "--period", "-p", metavar="YYYYMM", default=None,
        help="Period att återställa (t.ex. 202604). Standard: alla perioder.",
    )
    args = parser.parse_args()

    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(f"{prefix}Återställer filer för ny körning...\n")

    if args.period:
        periods = [args.period]
    else:
        if not EXTRACTED.exists():
            print(f"Mappen saknas: {EXTRACTED}")
            return
        periods = sorted(
            d.name for d in EXTRACTED.iterdir()
            if d.is_dir() and PERIOD_RE.match(d.name)
        )
        if not periods:
            print(f"Inga period-mappar hittades i {EXTRACTED}")
            return

    for period in periods:
        print(f"=== Period {period} ===")
        reset_period(period, args.dry_run)
        print()


if __name__ == "__main__":
    main()

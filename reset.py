#!/usr/bin/env python3
"""
reset.py  –  Återställ alla processhanterade filer för ny körning.

Vad scriptet gör:
  1. Raderar filer i extracted/{period}/{Country}/Referens/
  2. Raderar filer direkt i extracted/{period}/{Country}/  (de extraherade filerna)
  3. Raderar filer i extracted/{period}/{Country}/output/

Undermappar som Facit/, TESTED/ och TMP/ lämnas orörda.

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py reset.py --period 202604      # återställ en specifik period
    py reset.py                      # återställ alla perioder
    py reset.py --dry-run            # preview utan ändringar
"""

import argparse
import re
from pathlib import Path

from shared import load_config

GET_TESTFILES = Path(load_config()["base_path"])
EXTRACTED = GET_TESTFILES / "extracted"

PERIOD_RE = re.compile(r"^\d{6}$")
COUNTRIES = ["Sweden", "Norway", "Finland", "Denmark", "Germany"]


def reset_country(country_dir: Path, dry_run: bool) -> int:
    deleted = 0

    for subdir, label in [
        (country_dir / "Referens", "Referens/"),
        (country_dir, ""),
        (country_dir / "output", "output/"),
    ]:
        if not subdir.exists():
            continue
        for f in sorted(f for f in subdir.iterdir() if f.is_file()):
            tag = f"{label}{f.name}"
            if dry_run:
                print(f"  [dry] RADERA  {tag}")
            else:
                f.unlink()
                print(f"  RADERA  {tag}")
            deleted += 1

    return deleted


def reset_period(period: str, dry_run: bool) -> None:
    period_dir = EXTRACTED / period
    if not period_dir.exists():
        print(f"  (mapp saknas: {period_dir})")
        return

    total_deleted = 0
    for country in COUNTRIES:
        country_dir = period_dir / country
        if not country_dir.exists():
            continue
        print(f"  --- {country} ---")
        deleted = reset_country(country_dir, dry_run)
        if deleted == 0:
            print("    (inget att återställa)")
        else:
            print(f"    {deleted} filer raderade")
        total_deleted += deleted

    print(f"  Totalt: {total_deleted} filer raderade")


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

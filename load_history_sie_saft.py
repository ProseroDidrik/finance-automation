"""Ladda historiska SIE- och SAF-T-filer från _history/{år}/ till DuckDB.

Hanterar dubbletter: om samma orgnr förekommer i flera filer i samma årsmapp
väljs filen med senast ändrat datum (LastWriteTime). SIE och SAF-T behandlas
som samma "lane" per orgnr — den senaste filen vinner oavsett format.

Mappar under base_path:
  _history/2022/   → period 202212 (fallback om ingen #PSALDO)
  _history/2023/   → period 202312
  _history/2024/   → period 202412
  _history/2025/   → period 202512

Körning:
  py load_history_sie_saft.py                   # alla år
  py load_history_sie_saft.py --years 2022 2023  # specifika år
  py load_history_sie_saft.py --dry-run
  py load_history_sie_saft.py --include-journal
  py load_history_sie_saft.py --format sie       # bara SIE-filer (hoppa över SAF-T)
  py load_history_sie_saft.py --override        # global override, skriver över allt
  py load_history_sie_saft.py --override 32 75  # bara dessa bolag
"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import db
import load_sie
import load_saft
from shared import begin_run, load_config, log

HISTORY_YEARS = [2022, 2023, 2024, 2025]
NS_SAFT = "urn:StandardAuditFile-Taxation-Financial:NO"

RE_ORGNR_SIE = re.compile(r"^#ORGNR\s+(\S+)", re.IGNORECASE | re.MULTILINE)


def normalize_orgnr(orgnr: str) -> str:
    return re.sub(r"[^0-9]", "", str(orgnr).strip())


def _quick_orgnr_sie(path: Path) -> str | None:
    """Läs de första 100 raderna i en SIE-fil för att hitta #ORGNR."""
    encodings = ("utf-8-sig", "cp437", "latin-1")
    for enc in encodings:
        try:
            text = ""
            with path.open(encoding=enc, errors="replace") as fh:
                for i, line in enumerate(fh):
                    text += line
                    if i >= 100:
                        break
            m = RE_ORGNR_SIE.search(text)
            if m:
                return normalize_orgnr(m.group(1).strip('"'))
        except Exception:
            continue
    return None


def _quick_orgnr_saft(path: Path) -> str | None:
    """Extrahera RegistrationNumber från SAF-T XML-header via iterparse."""
    try:
        for event, elem in ET.iterparse(str(path), events=("end",)):
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "RegistrationNumber" and elem.text:
                return normalize_orgnr(elem.text)
            # Stoppa när MasterFiles börjar — orgnr finns alltid i Header
            if local == "MasterFiles":
                break
    except Exception:
        pass
    return None


def discover_year(year_dir: Path,
                  allowed_formats: set[str]) -> dict[str, tuple[Path, str]]:
    """Returnerar {normalized_orgnr: (best_path, format)} för ett årsdir.

    format = 'sie' eller 'saft'
    allowed_formats: delmängd av {'sie', 'saft'} — filer av andra format hoppas
    över. Filtret appliceras FÖRE dedup så att en SIE-only-körning väljer den
    senaste SIE-filen även om en nyare SAF-T finns för samma orgnr.
    Senast ändrad fil per orgnr vinner inom de tillåtna formaten.
    """
    best: dict[str, tuple[Path, float, str]] = {}

    for f in year_dir.iterdir():
        if not f.is_file():
            continue
        suffix = f.suffix.upper()
        if suffix in (".SE", ".SI"):
            fmt = "sie"
        elif suffix == ".XML":
            fmt = "saft"
        else:
            continue
        if fmt not in allowed_formats:
            continue
        orgnr = _quick_orgnr_sie(f) if fmt == "sie" else _quick_orgnr_saft(f)

        if not orgnr:
            log("WARN", f.name, "Kunde inte läsa orgnr — filen skippas")
            continue

        mtime = f.stat().st_mtime
        prev = best.get(orgnr)
        if prev is None or mtime > prev[1]:
            best[orgnr] = (f, mtime, fmt)

    return {orgnr: (t[0], t[2]) for orgnr, t in best.items()}


def load_year(con: db.Conn, year: int, year_dir: Path,
              base_path: Path, orgnr_lookup_sie: dict, orgnr_lookup_saft: dict,
              *, dry_run: bool, include_journal: bool,
              allowed_formats: set[str],
              override: list[int] | None = None) -> dict[str, int]:
    """Ladda ett årsdir. Returnerar {status: count}."""
    period_fallback = f"{year}12"
    files = discover_year(year_dir, allowed_formats)
    if not files:
        log("WARN", str(year), f"Inga filer hittades i {year_dir}")
        return {"ok": 0, "warn": 0, "skip": 0, "error": 0}

    # Logga dedup-info — räkna bara filer av de format vi faktiskt laddar.
    def _counts_for(f: Path) -> bool:
        suffix = f.suffix.upper()
        return ((suffix in (".SE", ".SI") and "sie" in allowed_formats)
                or (suffix == ".XML" and "saft" in allowed_formats))

    all_files = sum(
        1 for f in year_dir.iterdir() if f.is_file() and _counts_for(f)
    )
    log("INFO", str(year),
        f"{len(files)} unika orgnr valda av {all_files} filer i {year_dir.name}")

    counts: dict[str, int] = {"ok": 0, "warn": 0, "skip": 0, "error": 0}

    for orgnr, (path, fmt) in sorted(files.items(), key=lambda x: x[1][0].name):
        if fmt == "sie":
            # Kolla om orgnr finns i SIE-lookup (svenska bolag)
            if orgnr not in orgnr_lookup_sie:
                log("SKIP", path.name,
                    f"OrgNr {orgnr} saknas i dim_company (SIE) — skippas")
                counts["skip"] += 1
                continue

            # Avgör om filen saknar #PSALDO → använd period_fallback
            text = load_sie.read_text_with_fallback(path)
            parsed = load_sie.parse_sie(text)
            has_psaldo = bool(parsed.get("psaldo"))
            period_override = None if has_psaldo else period_fallback

            status = load_sie.load_file(
                con, path, base_path, period_override, orgnr_lookup_sie,
                dry_run=dry_run, include_journal=include_journal,
                override=override,
            )
        else:
            # SAF-T
            if orgnr not in orgnr_lookup_saft:
                log("SKIP", path.name,
                    f"OrgNr {orgnr} saknas i dim_company (SAF-T) — skippas")
                counts["skip"] += 1
                continue

            status = load_saft.load_file(
                con, path, base_path, period_fallback,
                orgnr_lookup_saft,
                dry_run=dry_run,
                include_journal=include_journal,
                override=override,
            )

        counts[status] = counts.get(status, 0) + 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda historiska SIE/SAF-T-filer från _history/ till DuckDB."
    )
    parser.add_argument("--years", nargs="+", type=int, default=HISTORY_YEARS,
                        help=f"År att ladda (default: {HISTORY_YEARS})")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-journal", action="store_true",
                        help="Ladda även verifikat till fact_journal_sie/saft (tungt)")
    parser.add_argument("--format", choices=("sie", "saft", "both"), default="both",
                        help="Vilka filformat som ska laddas (default: both). "
                             "--format sie laddar bara SIE och hoppar över SAF-T.")
    parser.add_argument("--override", nargs="*", type=int, default=None, metavar="ID",
                        help="Skriv över befintlig SIE/SAFT-data. --override = global "
                             "(alla bolag); --override 32 75 = bara dessa bolag.")
    args = parser.parse_args()

    allowed_formats = {"sie", "saft"} if args.format == "both" else {args.format}

    begin_run("load_history_sie_saft.py", "HIST")
    ovr_desc = ("global" if args.override == [] else
                f"bolag={args.override}" if args.override else "off")
    log("START", "load_history_sie_saft.py",
        f"år={args.years}  dry_run={args.dry_run}  "
        f"journal={args.include_journal}  format={args.format}  override={ovr_desc}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])
    hist_root = base_path / "_history"

    if not hist_root.exists():
        log("ERROR", "load_history_sie_saft.py",
            f"_history saknas under base_path: {hist_root}")
        return

    con = db.connect()
    try:
        db.init_schema(con)
        orgnr_lookup_sie = load_sie.build_orgnr_lookup(con)
        orgnr_lookup_saft = load_saft.build_orgnr_lookup(con)

        if not orgnr_lookup_sie and not orgnr_lookup_saft:
            log("ERROR", "scan",
                "Inga bolag med orgnr i dim_company — kör 'py db.py' först")
            return

        totals: dict[str, int] = {"ok": 0, "warn": 0, "skip": 0, "error": 0}

        for year in sorted(args.years):
            year_dir = hist_root / str(year)
            if not year_dir.exists():
                log("WARN", str(year), f"Mapp saknas: {year_dir}")
                continue
            counts = load_year(
                con, year, year_dir, base_path,
                orgnr_lookup_sie, orgnr_lookup_saft,
                dry_run=args.dry_run, include_journal=args.include_journal,
                allowed_formats=allowed_formats,
                override=args.override,
            )
            log("INFO", str(year),
                f"OK={counts['ok']}  WARN={counts['warn']}  "
                f"SKIP={counts['skip']}  ERROR={counts['error']}")
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v

    finally:
        con.close()

    log("DONE", "load_history_sie_saft.py",
        f"{totals['ok']} OK  {totals['warn']} WARN  "
        f"{totals['skip']} SKIP  {totals['error']} ERROR")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
norway_saft.py — SAF-T renaming script for Norway subsidiaries
==============================================================
Läs in SAF-T XML-filer (zippade eller råa) från Norway-mappen,
extrahera nyckeldata ur XML:en och döp om filerna till:
  {BolagsID}_{Friendly name}_{SoftwareAbbr}_SAF-T_{PeriodStartYear}-{PeriodEnd}.xml

Flytta sedan alla icke-SAF-T-filer (xlsx, pdf, zip, etc.) till Referens/.
Kvar i Norway/ blir enbart de färdignamgivna SAF-T XML:erna.

Kör från: C:\\Users\\DidWac\\dev\\finance-automation\\
  py norway_saft.py              # kör allt
  py norway_saft.py --prefix 198 # kör bara ett bolag
  py norway_saft.py --dry-run    # visa vad som skulle hända utan att skriva
  py norway_saft.py --no-referens # hoppa över Referens-steget

Dependencies: pip install openpyxl
"""

import argparse
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shared import safe_dest, load_config, log, begin_run, prev_month_period

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent
GET_TESTFILES = Path(load_config()["base_path"])
NORWAY_DIR  = GET_TESTFILES / "extracted" / "Norway"
DOTTERBOLAG = _BASE / "_params" / "Dotterbolagslista.xlsx"

# ── SoftwareID → Förkortning ───────────────────────────────────────────────────
# Nyckel = lowercase substring i SoftwareID-fältet, värde = förkortning
SOFTWARE_MAP = [
    ("visma global",    "VG"),
    ("visma business",  "VB"),    # täcker "Visma Business" och "Visma Business NXT"
    ("visma net",       "VN"),
    ("24sevenoffice",   "247"),
    ("uni micro",       "Uni"),
    ("uni økonomi",     "Uni"),
    ("unimicro",        "Uni"),
    ("duett",           "Duett"),
    ("poweroffice",     "PO"),
    ("tripletex",       "TT"),
]

UNKNOWN_SOFTWARE_PREFIX = "UNK"  # fallback om ingen match


def software_abbr(software_id: str) -> str:
    """Mappa SoftwareID-sträng till förkortning."""
    s = software_id.lower().strip()
    for key, abbr in SOFTWARE_MAP:
        if key in s:
            return abbr
    short = re.sub(r"[^A-Za-z0-9]", "", software_id)[:8]
    return short if short else UNKNOWN_SOFTWARE_PREFIX


# ── Dotterbolagslista ──────────────────────────────────────────────────────────
def load_dotterbolag():
    """
    Returnerar:
      orgnr_lookup : dict  orgnr_digits (str) -> (bolagsid: int, friendly: str)
      id_lookup    : dict  bolagsid (int)     -> friendly (str)
    """
    try:
        import openpyxl
    except ImportError:
        print("ERROR: openpyxl saknas — kör: pip install openpyxl", file=sys.stderr)
        sys.exit(1)

    if not DOTTERBOLAG.exists():
        print(f"ERROR: Dotterbolagslista hittades inte: {DOTTERBOLAG}", file=sys.stderr)
        sys.exit(1)

    wb = openpyxl.load_workbook(DOTTERBOLAG, read_only=True, data_only=True)
    try:
        ws = wb["Data For Company Find"]
    except KeyError:
        print("ERROR: Fliken 'Data For Company Find' saknas i Dotterbolagslista.", file=sys.stderr)
        sys.exit(1)

    orgnr_lookup = {}
    id_lookup    = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 2:
            continue
        kind     = row[7] if len(row) > 7 else None
        if kind == "consolidated":
            continue
        bolagsid = row[1]
        friendly = row[4] or ""
        orgnr_raw = row[5]
        if bolagsid is None:
            continue
        bid = int(bolagsid)
        id_lookup[bid] = friendly.strip()
        if orgnr_raw:
            clean = re.sub(r"[^0-9]", "", str(orgnr_raw))
            if clean:
                orgnr_lookup[clean] = (bid, friendly.strip())

    wb.close()
    return orgnr_lookup, id_lookup


# ── XML-parsing ────────────────────────────────────────────────────────────────
def strip_ns(tag: str) -> str:
    """Ta bort XML-namespace från taggnamn."""
    return re.sub(r"\{[^}]+\}", "", tag)


def find_elem_text(root, local_name: str):
    """
    Sök igenom hela trädet och returnera textinnehållet för det första
    elementet vars lokala taggnamn matchar exakt (case-sensitive).
    """
    for elem in root.iter():
        if strip_ns(elem.tag) == local_name:
            if elem.text and elem.text.strip():
                return elem.text.strip()
    return None


def find_company_registration(root) -> str:
    """
    Hitta RegistrationNumber inuti Company-blocket (inte AuditFileSender).
    Returnerar bara siffror, utan 'NO'-prefix och 'MVA'-suffix.
    """
    for elem in root.iter():
        if strip_ns(elem.tag) == "Company":
            for child in elem:
                if strip_ns(child.tag) == "RegistrationNumber":
                    raw = child.text or ""
                    return re.sub(r"[^0-9]", "", raw)
            break
    return ""


def parse_saft_header(xml_bytes: bytes) -> dict:
    """
    Parsar SAF-T XML och returnerar dict med:
      software_id, registration_number, period_start_year, period_end
    """
    root = ET.fromstring(xml_bytes)

    result = {
        "software_id":         find_elem_text(root, "SoftwareID") or "",
        "registration_number": find_company_registration(root),
        "period_start_year":   None,
        "period_end":          None,
    }

    # Alt 1: PeriodStartYear + PeriodEnd (Duett, Uni Okonomi, PowerOffice m.fl.)
    psy = find_elem_text(root, "PeriodStartYear")
    pe  = find_elem_text(root, "PeriodEnd")
    if psy and pe:
        result["period_start_year"] = psy
        result["period_end"]        = str(int(pe))  # normalisera, ta bort ledande nolla

    # Alt 2: SelectionStartDate / SelectionEndDate (Tripletex, Visma Business NXT, 24SO)
    if not result["period_start_year"]:
        sd = find_elem_text(root, "SelectionStartDate")
        ed = find_elem_text(root, "SelectionEndDate")
        if sd and len(sd) >= 4:
            result["period_start_year"] = sd[:4]
        if ed and len(ed) >= 7:
            result["period_end"] = str(int(ed[5:7]))  # normalisera, ta bort ledande nolla

    # Fallback: innevarande ar, forra manaden
    today = date.today()
    if not result["period_start_year"]:
        result["period_start_year"] = str(today.year)
    if not result["period_end"]:
        m = today.month - 1
        result["period_end"] = str(m if m > 0 else 12)

    return result


# ── Las XML-bytes (zip eller rafil) ───────────────────────────────────────────
def read_xml_bytes(path: Path):
    """
    Returnerar (xml_bytes, inner_filename) eller (None, error_msg).
    Hanterar bade .zip och ra .xml-filer.
    """
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if not xmls:
                    return None, "ZIP innehaller ingen .xml-fil"
                with z.open(xmls[0]) as f:
                    return f.read(), xmls[0]
        except zipfile.BadZipFile as e:
            return None, f"Trasig ZIP: {e}"
    else:
        try:
            return path.read_bytes(), path.name
        except OSError as e:
            return None, str(e)


# ── Hitta SAF-T-filer i Norway-mappen ─────────────────────────────────────────
TARGET_PATTERN = re.compile(r"^\d{3}_.+_SAF-T_\d{4}-\d+\.xml$")


def collect_saft_files(prefix_filter=None) -> dict:
    """
    Returnerar dict: prefix_str -> [Path, ...]
    Inkluderar ALLA zip-filer och XML-filer som annu inte ar namngivna
    enligt malformatet NNN_..._SAF-T_YYYY-M.xml.
    """
    result = defaultdict(list)

    for f in sorted(NORWAY_DIR.iterdir()):
        if f.is_dir():
            continue
        m = re.match(r"^(\d+)_", f.name)
        if not m:
            continue
        prefix = m.group(1)
        if prefix_filter and prefix != prefix_filter:
            continue
        if f.suffix.lower() == ".zip":
            result[prefix].append(f)
        elif f.suffix.lower() == ".xml":
            if not TARGET_PATTERN.match(f.name):
                result[prefix].append(f)

    return result


# ── Bygg malfilnamn ────────────────────────────────────────────────────────────
def sanitize_name(s: str) -> str:
    """Ta bort tecken som ar ogiltiga i Windows-filnamn."""
    return re.sub(r'[\\/:*?"<>|]', "-", s).strip()


def make_target_name(
    bolagsid: int,
    friendly: str,
    sw_abbr: str,
    year: str,
    period: str,
    suffix: str = "",
) -> str:
    safe_friendly = sanitize_name(friendly)
    name = f"{bolagsid:03d}_{safe_friendly}_{sw_abbr}_SAF-T_{year}-{period}"
    if suffix:
        name += f"_{suffix}"
    return name + ".xml"


# ── Separator ──────────────────────────────────────────────────────────────────
def hr():
    print("-" * 70)


# ── SAF-T-bearbetning ──────────────────────────────────────────────────────────
def process_norway(prefix_filter, dry_run: bool):
    dry_label = "  [DRY RUN]" if dry_run else ""
    log("START", "process_norway.py", f"SAF-T namngivning{dry_label}")

    orgnr_lookup, id_lookup = load_dotterbolag()
    all_files = collect_saft_files(prefix_filter)

    if not all_files:
        print("Inga filer att bearbeta.")
        return

    issues  = []
    renames = []

    for prefix_str in sorted(all_files.keys(), key=int):
        prefix_int = int(prefix_str)
        files      = all_files[prefix_str]
        friendly_default = id_lookup.get(prefix_int, f"ID{prefix_int}")

        log("INFO", prefix_str, friendly_default)

        entries = []

        for src_path in files:
            xml_bytes, inner_or_err = read_xml_bytes(src_path)
            if xml_bytes is None:
                msg = f"{src_path.name}: {inner_or_err}"
                issues.append((prefix_str, msg))
                log("ERROR", prefix_str, msg)
                continue

            try:
                parsed = parse_saft_header(xml_bytes)
            except ET.ParseError as e:
                msg = f"{src_path.name}: XML-parse-fel: {e}"
                issues.append((prefix_str, msg))
                log("ERROR", prefix_str, msg)
                continue

            # Verifiera RegistrationNumber mot Dotterbolagslista
            reg_no = parsed["registration_number"]
            if reg_no:
                if reg_no in orgnr_lookup:
                    matched_id, matched_friendly = orgnr_lookup[reg_no]
                    if matched_id != prefix_int:
                        msg = (
                            f"{src_path.name}: OrgNr {reg_no} -> BolagsID {matched_id} "
                            f"({matched_friendly}) - stämmer INTE med filprefixet {prefix_str}!"
                        )
                        issues.append((prefix_str, msg))
                        log("WARN", prefix_str, msg)
                else:
                    msg = (
                        f"{src_path.name}: OrgNr {reg_no} saknas i "
                        f"Dotterbolagslista kol F - kan inte verifiera BolagsID"
                    )
                    issues.append((prefix_str, msg))
                    log("WARN", prefix_str, msg)
            else:
                msg = f"{src_path.name}: Inget RegistrationNumber hittades i XML:en"
                issues.append((prefix_str, msg))
                log("INFO", prefix_str, msg)

            entries.append((src_path, parsed))

        seen_names = set()
        for idx, (src_path, parsed) in enumerate(entries):
            sw_abbr  = software_abbr(parsed["software_id"])
            year     = parsed["period_start_year"]
            period   = parsed["period_end"]
            friendly = friendly_default

            target_name = make_target_name(prefix_int, friendly, sw_abbr, year, period)

            if target_name in seen_names:
                target_name = make_target_name(
                    prefix_int, friendly, sw_abbr, year, period, suffix=str(idx + 1)
                )
            seen_names.add(target_name)

            target_path = NORWAY_DIR / target_name

            sw_display = f"{parsed['software_id']!r} -> {sw_abbr}"
            print(f"       {src_path.name}")
            print(f"    →  {target_name}  [{year}-{period} | {sw_display}]")

            if dry_run:
                log("SKIP", prefix_str, f"[DRY] {src_path.name} → {target_name}")
                renames.append((src_path.name, target_name))
                continue

            if target_path.exists():
                log("SKIP", prefix_str, f"Mål finns redan: {target_name}")
                renames.append((src_path.name, target_name))
                continue

            if src_path.suffix.lower() == ".zip":
                xml_bytes, _ = read_xml_bytes(src_path)
                if xml_bytes is None:
                    log("ERROR", prefix_str, "Kunde inte läsa XML ur ZIP")
                    continue
                tmp_path = target_path.with_suffix(".tmp")
                if tmp_path.exists():
                    tmp_path.unlink()
                tmp_path.write_bytes(xml_bytes)
                try:
                    tmp_path.rename(target_path)
                except OSError:
                    os.rename(tmp_path, target_path)
                log("OK", prefix_str, f"Extraherad + omdöpt: {target_name}")
            else:
                try:
                    src_path.rename(target_path)
                except OSError:
                    os.rename(src_path, target_path)
                log("OK", prefix_str, f"Omdöpt: {target_name}")

            renames.append((src_path.name, target_name))

    n_ok   = sum(1 for _, tgt in renames if not dry_run or True)
    n_warn = len(issues)
    log("DONE", "process_norway.py", f"{len(renames)} fil(er)  {n_warn} varningar")


# ── Flytta icke-SAF-T-filer till Referens/ ────────────────────────────────────
SAFT_FINAL_PATTERN = re.compile(r"^\d{3}_.+_SAF-T_\d{4}-\d+\.xml$")


def move_to_referens(dry_run: bool):
    """
    Scannar Norway-mappen och flyttar allt som INTE ar en fardigbearbetad
    SAF-T XML (monster: NNN_..._SAF-T_YYYY-M.xml) till undermappen Referens/.
    Kors alltid pa hela mappen, oavsett --prefix.

    Kvar i Norway/ blir enbart de fardignamngivna SAF-T XML:erna.
    """
    referens_dir = NORWAY_DIR / "Referens"

    to_move = []
    for f in sorted(NORWAY_DIR.iterdir()):
        if f.is_dir():
            continue
        if SAFT_FINAL_PATTERN.match(f.name):
            continue  # SAF-T-fil som ska stanna kvar
        to_move.append(f)

    if not to_move:
        print("\nReferens: inga filer att flytta.")
        return

    print(f"\nFlytta {len(to_move)} filer till Referens/")
    hr()

    if not dry_run:
        referens_dir.mkdir(exist_ok=True)

    moved = 0
    moved = 0
    for f in to_move:
        dest = safe_dest(referens_dir / f.name)
        prefix = "(dry) " if dry_run else ""
        print(f"  {prefix}-> Referens/{dest.name}")

        if not dry_run:
            try:
                f.rename(dest)
            except OSError:
                os.rename(f, dest)
            moved += 1

    if dry_run:
        print(f"\n  (dry-run) {len(to_move)} filer skulle flyttas till Referens/")
    else:
        print(f"\nKlart. {moved} fil(er) flyttade till Referens/")


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="SAF-T namngivning och sortering for Norway-filer"
    )
    parser.add_argument(
        "--prefix", "-p",
        metavar="ID",
        help="Bearbeta bara ett specifikt BolagsID (t.ex. --prefix 198)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Visa vad som skulle handa utan att skriva nagra filer",
    )
    parser.add_argument(
        "--no-referens",
        action="store_true",
        help="Hoppa over steget att flytta icke-SAF-T-filer till Referens/",
    )
    parser.add_argument(
        "--period", metavar="YYYYMM", default=None,
        help="Period att köra (t.ex. 202604). Standard: extracted/Norway/",
    )
    args = parser.parse_args()

    if args.period:
        global NORWAY_DIR
        NORWAY_DIR = GET_TESTFILES / "extracted" / args.period / "Norway"

    begin_run("process_norway", args.period or prev_month_period())
    process_norway(prefix_filter=args.prefix, dry_run=args.dry_run)

    if not args.no_referens:
        move_to_referens(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

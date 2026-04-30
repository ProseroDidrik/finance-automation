#!/usr/bin/env python3
"""
process_sweden.py — Validerar och döper om SIE-filer i extracted/Sweden/

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py process_sweden.py            # kör på riktigt
    py process_sweden.py --dry-run  # visa vad som skulle hända

Vad scriptet gör (ett prefix/bolagsid i taget):
  1. Läser Dotterbolagslistan (col F=OrgNr → col B=BolagsID, col E=Friendly name)
  2. Parsar SIE-filerna FÖRST för att fastställa korrekt BolagsID via OrgNr-lookup
  3. Döper om SIE-filer:
       {ID:03d}_{FriendlyName}_SIE_{StartYear}-{EndYYYYMM}.EXT
       Obs: använder ID från Dotterbolagslistan, inte från filnamnets prefix
  4. Validerar #RAR 0: YTD (börjar 1 jan i år, slutar t.o.m. föregående månad)
  5. Flaggar prefix-mismatch, periodfelet, duplikat
  6. Flyttar icke-SIE-filer till Sweden/Referens/ — med korrigerat prefix om det behövs

Flaggtyper:
  ❌ PREFIX KORRIGERAT  — filprefixet stämde inte med OrgNr-lookup; alla filer döpts om
  ❌ Periodfel          — #RAR 0 täcker inte innevarande år YTD
  ❌ Duplikat           — 2+ SIE-filer med samma OrgNr under samma prefix
  ❌ OrgNr saknas i listan — col F i Dotterbolagslistan är tom för detta bolag
"""

import argparse
import re
import shutil
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import openpyxl
except ImportError:
    sys.exit("Saknar openpyxl — kör:  py -m pip install openpyxl")

from shared import safe_dest, load_config, log

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent  # = finance-automation\

GET_TESTFILES = Path(load_config()["base_path"])
DOTTERBOLAG  = _BASE / "_params" / "Dotterbolagslista.xlsx"
SWEDEN_DIR   = GET_TESTFILES / "extracted" / "Sweden"
REFERENS_DIR = SWEDEN_DIR / "Referens"

SIE_EXTENSIONS = {".se", ".sie"}


# ── SIE-läsning ────────────────────────────────────────────────────────────────

def read_sie_lines(path: Path) -> list[str]:
    """Läser SIE-fil med encoding-fallback: utf-8-sig → cp437 → latin-1.
    SIE-standarden anger PC8-format = IBM CP437. Filer från nyare program
    kan vara UTF-8. latin-1 är sista fallback för Windows-1252-varianter."""
    for enc in ("utf-8-sig", "cp437", "latin-1"):
        try:
            return path.read_text(encoding=enc).splitlines()
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError(f"Kan inte läsa {path.name} — okänd teckenkodning")


def parse_sie(path: Path) -> dict:
    """
    Returnerar dict med nycklarna:
      orgnr (str|None), rar_start (str|None), rar_end (str|None), fnamn (str|None)
    """
    result: dict = {"orgnr": None, "rar_start": None, "rar_end": None, "fnamn": None}
    try:
        lines = read_sie_lines(path)
    except ValueError as e:
        result["error"] = str(e)
        return result

    for line in lines:
        line = line.strip()
        # #ORGNR 556071-2340
        if m := re.match(r"^#ORGNR\s+(\S+)", line, re.IGNORECASE):
            result["orgnr"] = m.group(1).strip('"')
        # #RAR 0 20260101 20261231  (space eller tab som separator)
        elif m := re.match(r"^#RAR\s+0\s+(\d{8})\s+(\d{8})", line, re.IGNORECASE):
            result["rar_start"] = m.group(1)
            result["rar_end"]   = m.group(2)
        # #FNAMN "Axlås Solidlås AB"
        elif m := re.match(r'^#FNAMN\s+"?(.+?)"?\s*$', line, re.IGNORECASE):
            result["fnamn"] = m.group(1)
    return result


# ── Dotterbolagslistan ─────────────────────────────────────────────────────────

def normalize_orgnr(orgnr: str) -> str:
    """556071-2340 → 5560712340  (tar bort bindestreck och whitespace)"""
    return re.sub(r"[\s\-]", "", str(orgnr).strip())


def load_dotterbolag(path: Path) -> dict[str, dict]:
    """
    Returnerar dict:
      orgnr_normalized → {"id": int, "friendly": str, "raw_orgnr": str}

    Filtrerar bort Kind == 'consolidated'. Hoppar över rader där col F (OrgNr) är tom.
    Sheet: 'Data For Company Find'
    Kolumner: A=Namn, B=BolagsID, C=Market, D=AcqYear, E=Friendly name,
              F=OrgNr, G=Parent, H=Kind
    """
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    lookup: dict = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 8:
            continue
        bolag_id  = row[1]  # col B
        kind      = row[7]  # col H
        friendly  = row[4]  # col E
        orgnr_raw = row[5]  # col F

        if str(kind).strip().lower() == "consolidated":
            continue
        if not orgnr_raw:
            continue

        key = normalize_orgnr(str(orgnr_raw))
        if key:
            lookup[key] = {
                "id":        int(bolag_id) if bolag_id else None,
                "friendly":  str(friendly).strip() if friendly else "",
                "raw_orgnr": str(orgnr_raw).strip(),
            }

    wb.close()
    return lookup


# ── Period-validering ──────────────────────────────────────────────────────────

def validate_period(rar_start: str, rar_end: str) -> tuple[bool, str]:
    """
    Returnerar (ok: bool, meddelande: str).

    Regler (idag = date.today()):
      - rar_start ska vara YYYY0101 där YYYY = innevarande år
      - rar_end ska vara YYYY + månad >= (innevarande månad - 1), samma år

    Exempel (idag = 2026-04-28, föregående månad = 03):
      20260101 20261231  → OK  (täcker mer än föregående månad)
      20260101 20260331  → OK  (slutar exakt föregående månad)
      20250101 20251231  → EJ OK  (fel år)
      20260301 20260331  → EJ OK  (inte YTD, börjar inte 1 jan)
    """
    today        = date.today()
    current_year = today.year
    prev_month   = today.month - 1 if today.month > 1 else 12

    if len(rar_start) != 8 or len(rar_end) != 8:
        return False, f"Ogiltigt datumformat: start={rar_start} slut={rar_end}"

    start_year = int(rar_start[:4])
    start_mmdd = rar_start[4:]
    end_year   = int(rar_end[:4])
    end_month  = int(rar_end[4:6])

    if start_year != current_year:
        return False, (
            f"Fel startår {start_year} (förväntat {current_year}) — "
            f"period: {rar_start}–{rar_end}"
        )
    if start_mmdd != "0101":
        return False, (
            f"Inte year-to-date: startar {rar_start} "
            f"(ska börja {current_year}0101)"
        )
    if end_year != current_year:
        return False, (
            f"Fel slutår {end_year} (förväntat {current_year}) — "
            f"period: {rar_start}–{rar_end}"
        )
    if end_month < prev_month:
        return False, (
            f"Perioden täcker inte t.o.m. föregående månad: "
            f"{rar_start}–{rar_end} "
            f"(idag {today}, föregående månad = {current_year}{prev_month:02d})"
        )

    return True, f"{rar_start}–{rar_end} ✓"


# ── Filnamnsbyggare ────────────────────────────────────────────────────────────

def build_new_name(bolag_id: int, friendly: str, rar_start: str, rar_end: str, ext: str) -> str:
    """
    Format: {ID:03d}_{FriendlyName}_SIE_{StartYear}-{EndYYYYMM}.EXT
    Ex:     001_Axlås Solidlås AB_SIE_2026-202612.SE
            032_Axel Group AB_SIE_2026-202603.SE
    """
    start_year = rar_start[:4]   # "2026"
    end_yyyymm = rar_end[:6]     # "202612" eller "202603"
    safe = re.sub(r'[\\/:*?"<>|]', "", friendly).strip()
    return f"{bolag_id:03d}_{safe}_SIE_{start_year}-{end_yyyymm}{ext.upper()}"



def corrected_ref_filename(filename: str, old_prefix: int, new_prefix: int) -> str:
    """Byter ut det numeriska prefixet i ett filnamn.
    '102_Balansrapport.pdf' + old=102, new=999 → '999_Balansrapport.pdf'"""
    return re.sub(r"^\d+_", f"{new_prefix:03d}_", filename, count=1)


# ── Huvud ──────────────────────────────────────────────────────────────────────

def process_sweden(dry_run: bool = False) -> None:
    dry_label = "  [DRY RUN]" if dry_run else ""
    log("START", "process_sweden.py", f"{date.today()}{dry_label}")

    if not SWEDEN_DIR.exists():
        sys.exit(f"[ERROR]  Sweden-mappen saknas: {SWEDEN_DIR}")
    if not DOTTERBOLAG.exists():
        sys.exit(f"[ERROR]  Dotterbolagslistan saknas: {DOTTERBOLAG}")

    if not dry_run:
        REFERENS_DIR.mkdir(exist_ok=True)

    # Ladda lookup: orgnr_norm → {id, friendly, raw_orgnr}
    bolag_lookup = load_dotterbolag(DOTTERBOLAG)
    print(f"Dotterbolagslistan: {len(bolag_lookup)} bolag med OrgNr inlagda\n")

    # Samla filer direkt i Sweden/ (ej undermappar)
    all_files = [f for f in SWEDEN_DIR.iterdir() if f.is_file()]

    # Gruppera per numeriskt prefix
    by_prefix: dict[int, list[Path]] = defaultdict(list)
    no_prefix: list[Path] = []
    for f in all_files:
        m = re.match(r"^(\d+)_", f.name)
        if m:
            by_prefix[int(m.group(1))].append(f)
        else:
            no_prefix.append(f)

    ok_count    = 0
    flag_count  = 0
    moved_count = 0

    # ── Per bolag ─────────────────────────────────────────────────────────────
    for prefix in sorted(by_prefix):
        files = by_prefix[prefix]
        sie_files   = [f for f in files if f.suffix.lower() in SIE_EXTENSIONS]
        other_files = [f for f in files if f.suffix.lower() not in SIE_EXTENSIONS]

        log("INFO", f"{prefix:03d}", "")

        # ── STEG 1: Parsa SIE-filer för att bestämma korrekt BolagsID ────────
        # Måste göras INNAN referensfiler flyttas, så vi kan korrigera prefix.

        # Gruppera SIE-filer per OrgNr
        orgnr_to_files: dict[str, list[Path]] = defaultdict(list)
        parse_cache: dict[Path, dict] = {}
        for s in sie_files:
            p = parse_sie(s)
            parse_cache[s] = p
            key = normalize_orgnr(p["orgnr"]) if p.get("orgnr") else f"__unknown_{s.name}"
            orgnr_to_files[key].append(s)

        # Vilka unika korrekta ID:n hittar vi via OrgNr-lookup?
        ids_from_list: set[int] = set()
        for key, group in orgnr_to_files.items():
            p = parse_cache[group[0]]
            if p.get("orgnr"):
                hit = bolag_lookup.get(normalize_orgnr(p["orgnr"]))
                if hit and hit["id"]:
                    ids_from_list.add(hit["id"])

        # Fastställ effective_ref_id för referensfiler:
        # Om ALLA SIE-filer under prefixet pekar på SAMMA korrekta ID → använd det.
        # Om ambiguöst (flera olika ID:n) → behåll originalprefix för ref-filer.
        if len(ids_from_list) == 1:
            effective_ref_id = next(iter(ids_from_list))
        else:
            effective_ref_id = prefix  # ambiguöst eller okänt → rör ej prefix

        # ── STEG 2: Flytta referensfiler (med ev. prefix-korrigering) ─────────
        for f in other_files:
            if effective_ref_id != prefix:
                new_fname = corrected_ref_filename(f.name, prefix, effective_ref_id)
                dest = safe_dest(REFERENS_DIR / new_fname)
                print(f"  → Referens : {f.name}")
                print(f"    (prefix korrigerat → {new_fname})")
            else:
                dest = safe_dest(REFERENS_DIR / f.name)
                print(f"  → Referens : {f.name}")

            if not dry_run:
                shutil.move(str(f), str(dest))
            moved_count += 1

        if not sie_files:
            log("WARN", f"{prefix:03d}", "Ingen SIE-fil hittad")
            flag_count += 1
            continue

        # ── STEG 3: Bearbeta varje OrgNr-grupp ───────────────────────────────
        for orgnr_norm, group in orgnr_to_files.items():
            flags: list[str] = []

            # Duplikat-varning (samma OrgNr, flera filer)
            if len(group) > 1:
                flags.append(
                    f"Duplikat: {len(group)} SIE-filer med samma OrgNr "
                    f"— behåll den rätta manuellt: "
                    + ", ".join(f.name for f in group)
                )

            sie_path = group[0]
            parsed   = parse_cache[sie_path]

            if "error" in parsed:
                log("ERROR", f"{prefix:03d}", f"Läsfel i {sie_path.name}: {parsed['error']}")
                flag_count += 1
                continue

            orgnr_raw = parsed["orgnr"]
            rar_start = parsed["rar_start"]
            rar_end   = parsed["rar_end"]
            fnamn_sie = parsed["fnamn"] or ""

            # ── OrgNr-kontroll + fastställ effective_id ───────────────────────
            if not orgnr_raw:
                flags.append("Saknar #ORGNR i SIE-filen")
                friendly_name = fnamn_sie or f"Bolag{prefix:03d}"
                id_from_list  = None
                effective_id  = prefix

            else:
                hit = bolag_lookup.get(normalize_orgnr(orgnr_raw))

                if hit is None:
                    flags.append(
                        f"OrgNr {orgnr_raw} saknas i Dotterbolagslistan (col F) "
                        f"— FNAMN i SIE: \"{fnamn_sie}\""
                    )
                    friendly_name = fnamn_sie or f"Bolag{prefix:03d}"
                    id_from_list  = None
                    effective_id  = prefix

                else:
                    id_from_list  = hit["id"]
                    friendly_name = hit["friendly"] or fnamn_sie or f"Bolag{prefix:03d}"
                    effective_id  = id_from_list  # ← använd korrekt ID vid rename

                    if id_from_list != prefix:
                        flags.append(
                            f"PREFIX KORRIGERAT: filprefixet var {prefix:03d} "
                            f"men OrgNr {orgnr_raw} → BolagsID {id_from_list:03d} "
                            f"i Dotterbolagslistan — döpt om med korrekt prefix"
                        )

            # ── Periodkontroll ────────────────────────────────────────────────
            if not rar_start or not rar_end:
                flags.append("Saknar #RAR 0 i SIE-filen")
                period_msg = "—"
            else:
                period_ok, period_msg = validate_period(rar_start, rar_end)
                if not period_ok:
                    flags.append(f"Periodfel: {period_msg}")

            # ── Bygg nytt namn (med effective_id) ────────────────────────────
            can_rename = bool(rar_start and rar_end)
            new_name   = (
                build_new_name(effective_id, friendly_name, rar_start, rar_end, sie_path.suffix)
                if can_rename else None
            )

            # ── Utskrift ──────────────────────────────────────────────────────
            log("WARN" if flags else "OK", f"{effective_id:03d}", sie_path.name)
            print(f"    OrgNr   : {orgnr_raw or '—'}")
            if id_from_list is not None:
                prefix_note = f"  (var {prefix:03d} i filnamnet)" if id_from_list != prefix else ""
                print(f"    ListaID : {id_from_list:03d}{prefix_note}  Friendly: {friendly_name}")
            print(f"    RAR 0   : {period_msg}")
            if new_name:
                arrow = "→" if new_name != sie_path.name else "="
                print(f"    Namn    : {arrow} {new_name}")
            for flag in flags:
                print(f"    ! {flag}")

            # ── Rename ────────────────────────────────────────────────────────
            if new_name:
                dest = SWEDEN_DIR / new_name
                if dest != sie_path:
                    if not dry_run:
                        try:
                            sie_path.rename(dest)
                        except Exception as e:
                            print(f"    ❌ Rename misslyckades: {e}")
                            flag_count += 1
                            continue
                    print(f"    {'[DRY] ' if dry_run else ''}Döpt om")

            if flags:
                flag_count += 1
            else:
                ok_count += 1

    # ── Filer utan prefix ─────────────────────────────────────────────────────
    if no_prefix:
        print(f"\n── Filer utan ID-prefix (orörd) ──────────────────────")
        for f in no_prefix:
            print(f"  {f.name}")

    # ── Sammanfattning ────────────────────────────────────────────────────────
    log("DONE", "process_sweden.py",
        f"{ok_count} OK  {flag_count} WARN  {moved_count} → Referens")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validera och döp om SIE-filer i extracted/Sweden/"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Visa vad som skulle hända utan att ändra några filer",
    )
    args = parser.parse_args()
    process_sweden(dry_run=args.dry_run)

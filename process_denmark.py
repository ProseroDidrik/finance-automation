#!/usr/bin/env python3
"""
process_denmark.py  –  Danish Saldobalance XLSX → INL.xlsx  |  SAF-T rename for Actas (190)

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py process_denmark.py               # kör alla bolag
    py process_denmark.py 229 242       # kör specifika bolag
    py process_denmark.py --dry-run     # visa utan att skriva

Vad scriptet gör:
  1. Läser Saldobalance XLSX per bolag, delar konton i IS/BS per bolagskonfiguration
  2. Stoppar vid "Nulkontrol"-sektionen (IS-konton visas annars dubbelt)
  3. Hoppar över bold+underline summerings-rader (bolag 178)
  4. Skriver {kod}_{Namn}_{YYYYMM}_INL.xlsx till Denmark/output/
  5. Döper om SAF-T XML för Actas (190) till standardformat
  6. Flyttar källfiler till Denmark/Referens/

Bolagsspecifika IS/BS-gränser (4-siffrig kontoprefixnivå):
  178: IS = 0–4999,  BS = 5000+   (hoppa över bold+underline summary-rader)
  216: IS = 0–9999   (enbart resultaträkning, inga BS-konton)
  229: IS = 0–4999,  BS = 5000+
  242: IS = 0–799,   BS = 800+
  190: SAF-T XML — döps om, ingen INL.xlsx
"""

import argparse
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("Saknar openpyxl — kör:  py -m pip install openpyxl")

try:
    import pandas as pd
except ImportError:
    sys.exit("Saknar pandas — kör:  py -m pip install pandas openpyxl")

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent

GET_TESTFILES = Path(
    r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister"
    r"\Phoenix Foundation\April alla filer\Get testfiles"
)
DENMARK_DIR  = GET_TESTFILES / "extracted" / "Denmark"
OUTPUT_DIR   = DENMARK_DIR / "output"
REFERENS_DIR = DENMARK_DIR / "Referens"
DOTTERBOLAG  = _BASE / "_params" / "Dotterbolagslista.xlsx"

# ── SAF-T constants (Actas 190) ────────────────────────────────────────────────
ACTAS_CODE     = 190
ACTAS_XML      = "190_260408 SAF-T_export.xml"
ACTAS_REFERENS = ["190_03. Sikring Nord 0101-31032026.xlsx"]

# ── Company definitions ────────────────────────────────────────────────────────
# is_max : max 4-digit account prefix (inclusive) counted as IS/P&L
# bs_min : min 4-digit account prefix (inclusive) counted as BS; None = no BS
COMPANY_DEFS: dict[str, dict] = {
    "178": dict(
        is_max=4999, bs_min=5000, skip_formatting=True,
        file="178_03 Marts 2026.xlsx",
        extra=[],
    ),
    "216": dict(
        is_max=9999, bs_min=None, skip_formatting=False,
        file="216_Balance pr. 310326 SIKOM Danmark (2).xlsx",
        extra=["216_Balance pr. 310326 SIKOM Danmark.xlsx"],
    ),
    "229": dict(
        is_max=4999, bs_min=5000, skip_formatting=False,
        file="229_Saldobalance månedsopdelt - 01-03-2026 - 31-03-2026.xlsx",
        extra=["229_Saldobalance månedsopdelt - 01-01-2026 - 31-03-2026.xlsx"],
    ),
    "242": dict(
        is_max=799, bs_min=800, skip_formatting=False,
        file="242_Saldobalance - 01-03-2026 - 31-03-2026.xlsx",
        extra=["242_Saldobalance - 01-01-2026 - 31-03-2026.xlsx"],
    ),
}

SAFT_CODE = str(ACTAS_CODE)


# ── Period helper ──────────────────────────────────────────────────────────────
def prev_month_period() -> str:
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}12"
    return f"{today.year}{today.month - 1:02d}"


# ── Amount parsing (Danish: . thousands, , decimal) ───────────────────────────
def parse_amount(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return 0.0 if val != val else float(val)  # NaN → 0
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", "+"):
        return 0.0
    # "1.234,56" → remove . (thousands), , → .
    if re.match(r"^-?\d{1,3}(\.\d{3})+(,\d*)?$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── Account normalisation ──────────────────────────────────────────────────────
def normalize4(account: int) -> int:
    """4-digit prefix of any account number (4, 5, 6 digits)."""
    s = str(account)
    return int(s[:4]) if len(s) > 4 else account


# ── Dotterbolagslistan ─────────────────────────────────────────────────────────
def load_friendly_names() -> dict[int, str]:
    """bolagsid → friendly name from Dotterbolagslistan."""
    wb = openpyxl.load_workbook(str(DOTTERBOLAG), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    result: dict[int, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 5:
            continue
        bolag_id = row[1]
        friendly  = row[4]
        kind      = row[7] if len(row) > 7 else None
        if str(kind).strip().lower() == "consolidated":
            continue
        if bolag_id and friendly:
            result[int(bolag_id)] = str(friendly).strip()
    wb.close()
    return result


# ── Saldobalance column detection ─────────────────────────────────────────────
def find_columns(ws) -> tuple[int | None, dict]:
    """
    Scan the first 20 rows for the header row.
    Returns (header_row_index, cols_dict).
    cols_dict keys: acc, name, debet, kredit, saldo (any may be absent).
    """
    KEYWORDS = ("konto", "debet", "kredit", "saldo", "navn", "tekst", "beskrivelse")

    for row_idx, row in enumerate(ws.iter_rows(max_row=20)):
        texts = [
            str(c.value).strip().lower() if c.value is not None else ""
            for c in row
        ]
        if sum(1 for t in texts if any(kw in t for kw in KEYWORDS)) < 2:
            continue

        cols: dict = {}
        for j, t in enumerate(texts):
            if "acc"    not in cols and "konto" in t:
                cols["acc"] = j
            if "name"   not in cols and any(kw in t for kw in ("navn", "tekst", "beskrivelse")):
                cols["name"] = j
            if "debet"  not in cols and "debet" in t:
                cols["debet"] = j
            if "kredit" not in cols and "kredit" in t:
                cols["kredit"] = j
            if "saldo"  not in cols and "saldo" in t:
                cols["saldo"] = j

        if "acc" in cols or ("debet" in cols and "kredit" in cols):
            return row_idx, cols

    return None, {}


# ── Saldobalance reader ────────────────────────────────────────────────────────
def read_saldobalance(
    filepath: Path,
    is_max_4d: int,
    bs_min_4d: int | None,
    skip_formatting: bool = False,
) -> tuple[list, list]:
    """
    Returns (is_rows, bs_rows), each a list of (account_int, name_str, amount_float).

    Stops at the first row containing "nulkontrol" to avoid double-counting IS accounts.
    If skip_formatting=True, rows where any of the first 3 cells is bold+underline are skipped
    (handles 178's summary/total rows).
    Amount = Saldo column if present, otherwise Debet − Kredit.
    """
    wb = openpyxl.load_workbook(str(filepath), data_only=True)
    ws = wb.active

    header_row_idx, cols = find_columns(ws)
    if header_row_idx is None:
        wb.close()
        raise ValueError(f"Kunde inte hitta header-rad i {filepath.name}")

    col_acc    = cols.get("acc",   0)
    col_name   = cols.get("name",  1 if cols.get("acc", 0) == 0 else 0)
    col_debet  = cols.get("debet")
    col_kredit = cols.get("kredit")
    col_saldo  = cols.get("saldo")

    is_rows: list = []
    bs_rows: list = []

    for row in ws.iter_rows(min_row=header_row_idx + 2):
        # Stop at Nulkontrol section
        if any(
            "nulkontrol" in str(c.value).lower()
            for c in row if c.value is not None
        ):
            break

        if col_acc >= len(row):
            continue
        acc_cell = row[col_acc]
        if acc_cell.value is None:
            continue
        try:
            acc = int(float(str(acc_cell.value).strip()))
        except (ValueError, TypeError):
            continue

        # Skip bold+underline summary rows (178)
        if skip_formatting:
            for cell in row[:3]:
                f = cell.font
                if f and f.bold and f.underline:
                    acc = -1
                    break
            if acc == -1:
                continue

        # Name
        name = ""
        if col_name < len(row) and row[col_name].value is not None:
            name = str(row[col_name].value).strip()

        # Amount: Saldo preferred, else Debet − Kredit
        if col_saldo is not None and col_saldo < len(row):
            amt = parse_amount(row[col_saldo].value)
        elif col_debet is not None and col_kredit is not None:
            d = parse_amount(row[col_debet].value  if col_debet  < len(row) else None)
            k = parse_amount(row[col_kredit].value if col_kredit < len(row) else None)
            amt = d - k
        elif col_debet is not None and col_debet < len(row):
            amt = parse_amount(row[col_debet].value)
        else:
            continue

        amt = round(amt, 2)
        if amt == 0.0:
            continue

        acc4 = normalize4(acc)
        if acc4 <= is_max_4d:
            is_rows.append((acc, name, amt))
        elif bs_min_4d is not None and acc4 >= bs_min_4d:
            bs_rows.append((acc, name, amt))

    wb.close()
    return is_rows, bs_rows


# ── INL.xlsx output ────────────────────────────────────────────────────────────
def save_xlsx(is_rows: list, bs_rows: list, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"A": None, "B": None, "C": None}]
    for acc, name, amt in is_rows + bs_rows:
        records.append({"A": acc, "B": name, "C": amt})
    df = pd.DataFrame(records)
    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        df.to_excel(writer, index=False, header=False, sheet_name="Sheet1")


# ── Referens move ──────────────────────────────────────────────────────────────
def move_to_referens(filename: str, dry_run: bool) -> None:
    src = DENMARK_DIR / filename
    if not src.exists():
        return
    dst = REFERENS_DIR / filename
    if dst.exists():
        stem, ext = src.stem, src.suffix
        i = 1
        while dst.exists():
            dst = REFERENS_DIR / f"{stem}_{i}{ext}"
            i += 1
    if dry_run:
        print(f"    [dry] → Referens/{dst.name}")
    else:
        shutil.move(str(src), str(dst))
        print(f"    → Referens/{dst.name}")


# ── Process one saldobalance company ──────────────────────────────────────────
def process_company(
    code: str,
    friendly: str,
    period: str,
    filepath: Path,
    is_max_4d: int,
    bs_min_4d: int | None,
    skip_formatting: bool,
    extra_referens: list[str],
    dry_run: bool,
) -> None:
    print(f"\n── {code} {'─' * 45}")
    print(f"  {friendly}  ({period})")
    print(f"  Fil: {filepath.name}")

    if not filepath.exists():
        print(f"  ⚠  SKIP: Filen saknas (redan i Referens?)")
        return

    try:
        is_rows, bs_rows = read_saldobalance(filepath, is_max_4d, bs_min_4d, skip_formatting)
    except Exception as e:
        print(f"  ❌ Läsfel: {e}")
        return

    total = sum(r[2] for r in is_rows + bs_rows)
    check = "OK" if abs(total) < 1.0 else f"⚠ KONTROLLERA ({total:.2f})"
    print(f"  Rader IS={len(is_rows)}, BS={len(bs_rows)}   Summa={total:.4f}  {check}")

    out_name = f"{code}_{friendly}_{period}_INL.xlsx"
    out_path = OUTPUT_DIR / out_name

    if not dry_run:
        save_xlsx(is_rows, bs_rows, out_path)
    print(f"  {'[dry] ' if dry_run else ''}✔ Sparad: {out_name}")

    if not dry_run:
        REFERENS_DIR.mkdir(exist_ok=True)
    move_to_referens(filepath.name, dry_run)
    for fname in extra_referens:
        move_to_referens(fname, dry_run)


# ── SAF-T software map ─────────────────────────────────────────────────────────
_SW_MAP = [
    ("visma global",   "VG"),
    ("visma business", "VB"),
    ("visma net",      "VN"),
    ("24sevenoffice",  "247"),
    ("uni micro",      "Uni"),
    ("uni økonomi",    "Uni"),
    ("unimicro",       "Uni"),
    ("duett",          "Duett"),
    ("poweroffice",    "PO"),
    ("tripletex",      "TT"),
    ("e-conomic",      "EC"),
    ("economic",       "EC"),
    ("dinero",         "DN"),
    ("billy",          "BY"),
]


def _sw_abbr(software_id: str) -> str:
    s = software_id.lower().strip()
    for key, abbr in _SW_MAP:
        if key in s:
            return abbr
    short = re.sub(r"[^A-Za-z0-9]", "", software_id)[:8]
    return short or "UNK"


def _strip_ns(tag: str) -> str:
    return re.sub(r"\{[^}]+\}", "", tag)


def _find_elem_text(root, local_name: str) -> str | None:
    for elem in root.iter():
        if _strip_ns(elem.tag) == local_name and elem.text and elem.text.strip():
            return elem.text.strip()
    return None


def _find_registration(root) -> str:
    for elem in root.iter():
        if _strip_ns(elem.tag) == "Company":
            for child in elem:
                if _strip_ns(child.tag) == "RegistrationNumber":
                    return re.sub(r"[^0-9]", "", child.text or "")
            break
    return ""


def _parse_saft_header(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    result = {
        "software_id":         _find_elem_text(root, "SoftwareID") or "",
        "registration_number": _find_registration(root),
        "year":                None,
        "period":              None,
    }
    psy = _find_elem_text(root, "PeriodStartYear")
    pe  = _find_elem_text(root, "PeriodEnd")
    if psy and pe:
        result["year"]   = psy
        result["period"] = str(int(pe))
    if not result["year"]:
        sd = _find_elem_text(root, "SelectionStartDate")
        ed = _find_elem_text(root, "SelectionEndDate")
        if sd and len(sd) >= 4:
            result["year"] = sd[:4]
        if ed and len(ed) >= 7:
            result["period"] = str(int(ed[5:7]))
    today = date.today()
    if not result["year"]:
        result["year"] = str(today.year)
    if not result["period"]:
        m = today.month - 1
        result["period"] = str(m if m > 0 else 12)
    return result


# ── Process Actas (190) SAF-T ─────────────────────────────────────────────────
def process_actas(friendly: str, dry_run: bool) -> None:
    print(f"\n── {ACTAS_CODE} {'─' * 45}")
    print(f"  {friendly}  (SAF-T)")

    xml_path = DENMARK_DIR / ACTAS_XML
    if not xml_path.exists():
        print(f"  ⚠  SKIP: {ACTAS_XML} saknas (redan bearbetad?)")
    else:
        try:
            parsed = _parse_saft_header(xml_path.read_bytes())
        except ET.ParseError as e:
            print(f"  ❌ XML-parse-fel: {e}")
            parsed = None

        if parsed:
            sw       = _sw_abbr(parsed["software_id"])
            year     = parsed["year"]
            period   = parsed["period"]
            safe     = re.sub(r'[\\/:*?"<>|]', "-", friendly).strip()
            new_name = f"{ACTAS_CODE:03d}_{safe}_{sw}_SAF-T_{year}-{period}.xml"
            dest     = DENMARK_DIR / new_name
            print(f"  Software : {parsed['software_id']!r} → {sw}")
            print(f"  Period   : {year}-{period}")
            print(f"  Ny fil   : {new_name}")
            if dry_run:
                print(f"  [dry] Skulle döpa om")
            else:
                try:
                    xml_path.rename(dest)
                    print(f"  ✔ Omdöpt")
                except OSError as e:
                    print(f"  ❌ Rename misslyckades: {e}")

    if not dry_run:
        REFERENS_DIR.mkdir(exist_ok=True)
    for fname in ACTAS_REFERENS:
        move_to_referens(fname, dry_run)


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bearbeta danska Saldobalance-filer → INL.xlsx"
    )
    parser.add_argument(
        "codes", nargs="*",
        help="Bolagskoder att köra (standard: alla). Ex: py process_denmark.py 229 242",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Visa vad som skulle hända utan att skriva några filer",
    )
    args = parser.parse_args()

    period = prev_month_period()
    label  = "[DRY RUN] " if args.dry_run else ""
    print(f"{label}process_denmark.py — {date.today()}  Period: {period}")
    print(f"  Denmark-mapp : {DENMARK_DIR}")
    print(f"  Dotterbolag  : {DOTTERBOLAG}")

    if not DENMARK_DIR.exists():
        sys.exit(f"❌  Denmark-mappen saknas: {DENMARK_DIR}")
    if not DOTTERBOLAG.exists():
        sys.exit(f"❌  Dotterbolagslistan saknas: {DOTTERBOLAG}")

    friendlies = load_friendly_names()

    if not args.dry_run:
        OUTPUT_DIR.mkdir(exist_ok=True)
        REFERENS_DIR.mkdir(exist_ok=True)

    all_codes = sorted(COMPANY_DEFS.keys()) + [SAFT_CODE]
    codes_to_run = args.codes if args.codes else all_codes

    for code in codes_to_run:
        if code == SAFT_CODE:
            friendly = friendlies.get(ACTAS_CODE, "Actas")
            process_actas(friendly, args.dry_run)
            continue

        if code not in COMPANY_DEFS:
            print(f"\n⚠  Okänd bolagskod: {code}")
            continue

        cfg      = COMPANY_DEFS[code]
        friendly = friendlies.get(int(code), f"Bolag{code}")
        filepath = DENMARK_DIR / cfg["file"]

        process_company(
            code=code,
            friendly=friendly,
            period=period,
            filepath=filepath,
            is_max_4d=cfg["is_max"],
            bs_min_4d=cfg["bs_min"],
            skip_formatting=cfg["skip_formatting"],
            extra_referens=cfg["extra"],
            dry_run=args.dry_run,
        )

    print(f"\n{'═' * 55}")
    print("Klart!")
    if args.dry_run:
        print("(DRY RUN — inga filer ändrades)")


if __name__ == "__main__":
    main()

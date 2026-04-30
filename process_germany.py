#!/usr/bin/env python3
"""
process_germany.py  –  German SuSa / Monthly-Value XLSX → INL.xlsx

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py process_germany.py               # kör alla bolag
    py process_germany.py 231 245       # kör specifika bolag
    py process_germany.py --dry-run     # visa utan att skriva

Vad scriptet gör:
  1. Läser SuSa / Monthly-Value XLSX per bolag
  2. Extraherar månadsrörelser i kredit-debet-konvention (intäkter +, kostnader -)
  3. Skriver {kod}_{Namn}_{YYYYMM}_INL.xlsx till Germany/output/
  4. Flyttar källfiler till Germany/Referens/

Bolagsspecifika konfigurationer:
  188 (Bofferding):    Monthly-Value XLSX, konton 0–89999, negera alla belopp
  220 (Weckbacher):    SuSa CSV (cp1252, semikolon), amount=-(Soll+Haben), konton 0–8999
  231 (Mittermeier):   SuSa pro Monat, konton 0–8999, Haben−Soll per Mrz-kolumn
  245 (GOLDfunk):      SuSa pro Monat, konton 0–8999, Haben−Soll per Mrz-kolumn
  246 (HW Mechatronic): SuSa Jahresübersicht, Mrz-kolumn, konton 0–8999

Teckensättning SuSa (231/245/246):
  amount = Haben − Soll  →  intäkter +, kostnader −, tillgångsökning −, skuld­ökning +
  (summan av alla konton 0–8999 = 0 per dubbel bokföring)

Teckensättning Monthly-Value (188):
  filen lagrar debet-positivt (intäkter negativa, kostnader positiva)
  → negera alla belopp för att nå kredit-debet-konvention

IS-konton:
  188: 20 000–89 999  (engelskspråkigt DATEV-system)
  231/245/246: 4 000–8 999  (DATEV SKR03/04)
BS-konton:
  188: 0–19 999
  231/245/246: 0–3 999
"""

import argparse
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
GERMANY_DIR  = GET_TESTFILES / "extracted" / "Germany"
OUTPUT_DIR   = GERMANY_DIR / "output"
REFERENS_DIR = GERMANY_DIR / "Referens"
DOTTERBOLAG  = _BASE / "_params" / "Dotterbolagslista.xlsx"

# ── Company definitions ────────────────────────────────────────────────────────
# reader: "monthly_value" | "susa_pro_monat" | "susa_jahresuebersicht" | "skip"
COMPANY_DEFS: dict[str, dict] = {
    "188": dict(
        reader="monthly_value",
        file="188_Bofferding GmbH monthly value 2026_03.xlsx",
        extra=[
            "188_Bofferding GmbH balance sheet 2026_03.xlsx",
            "188_Bofferding GmbH balance sheet 2026_03 (2).xlsx",
            "188_Bofferding GmbH monthly value 2026_03 (2).xlsx",
            "188_Bofferding GmbH profit loss 2026_03.xlsx",
            "188_Bofferding GmbH profit loss 2026_03 (2).xlsx",
            "188_Bofferding GmbH reporting 2026_03.xlsx",
            "188_Bofferding GmbH reporting 2026_03 (2).xlsx",
        ],
    ),
    "220": dict(
        reader="susa_csv",
        file="220_Susa_03_2026.csv",
        extra=[
            "220_BAB aktueller Monat KST-Übersicht Umlage 3_2026.pdf",
            "220_BAB aktueller Monat KST-Übersicht Umlage 2_2026.pdf",
            "220_Kurzfristige Erfolgsrechnung der Fa. Weckbacher GmbH_03_2026.pdf",
        ],
    ),
    "231": dict(
        reader="susa_pro_monat",
        file="231_Susa 03.2026.xlsx",
        extra=[
            "231_BWA 03.2026.xlsx",
            "231_BWA 03.2026 (2).xlsx",
            "231_Susa 03.2026 (2).xlsx",
        ],
    ),
    "245": dict(
        reader="susa_pro_monat",
        file="245_GF Sich - SuSa 3.2026.xlsx",
        extra=[
            "245_GF Sich - BWA 3.2026.xlsx",
            "245_GF Sich - BWA 3.2026 (2).xlsx",
            "245_GF Sich - BWA JÜ 3.2026.xlsx",
            "245_GF Sich - BWA JÜ 3.2026 (2).xlsx",
            "245_GF Sich - SuSa 3.2026 (2).xlsx",
            "245_GF Sich.- BWA 3.2026.pdf",
            "245_GF Sich.- BWA 3.2026 (2).pdf",
        ],
    ),
    "246": dict(
        reader="susa_jahresuebersicht",
        file="246_SUSA_20260331.xlsx",
        extra=[
            "246_Kurzfristige BWA_20260331.xlsx",
            "246_Kurzfristige BWA_20260331 (2).xlsx",
            "246_SUSA_20260228.xlsx",
            "246_SUSA_20260228 (2).xlsx",
            "246_SUSA_20260331 (2).xlsx",
        ],
    ),
}

# ── Period helpers ─────────────────────────────────────────────────────────────
_MONTH_MAP = {
    "jan": 1,  "feb": 2,  "mar": 3,  "mrz": 3,  "apr": 4,
    "mai": 5,  "may": 5,  "jun": 6,  "jul": 7,  "aug": 8,
    "sep": 9,  "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12,
}


def prev_month_period() -> str:
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}12"
    return f"{today.year}{today.month - 1:02d}"


def _match_period(cell_val, period: str) -> bool:
    """True if a header cell value matches YYYYMM period."""
    if cell_val is None:
        return False
    year, month = period[:4], int(period[4:])
    val = str(cell_val).strip().lower()
    m = re.match(r"^([a-zäöü]+)[/\s\-](\d{4})$", val)
    if m:
        abbr = m.group(1)[:3]
        yr = m.group(2)
        return yr == year and _MONTH_MAP.get(abbr) == month
    return False


# ── Amount parsing ─────────────────────────────────────────────────────────────
def parse_de(val) -> float:
    """Parse German number format: '1.234,56' → 1234.56."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return 0.0 if val != val else float(val)
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", "+"):
        return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_amount(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return 0.0 if val != val else float(val)
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", "+"):
        return 0.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_account(val) -> int | None:
    """Parse account number; strips spaces (e.g. '84 000' → 84000)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return int(float(val))
        except (ValueError, OverflowError):
            return None
    s = str(val).strip().replace(" ", "")
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# ── Dotterbolagslistan ─────────────────────────────────────────────────────────
def load_friendly_names() -> dict[int, str]:
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


# ── BS/IS classification ───────────────────────────────────────────────────────
def classify_188(acc: int) -> str | None:
    """'IS' | 'BS' | None (=exclude) for Bofferding English-DATEV numbering."""
    if acc >= 90000:
        return None
    if 20000 <= acc <= 89999:
        return "IS"
    return "BS"


def classify_susa(acc: int) -> str | None:
    """'IS' | 'BS' | None for standard DATEV SKR03/04 SuSa accounts."""
    if acc >= 9000:
        return None          # sub-ledger, contra, or statistical accounts
    if 4000 <= acc <= 8999:
        return "IS"
    return "BS"


# ── Reader: Monthly-Value XLSX (188) ──────────────────────────────────────────
def read_monthly_value(
    filepath: Path, period: str
) -> tuple[list, list]:
    """
    Reads 188-style 'monthly value' file.

    Finds the column header matching *period* (e.g. 'Mar/2026' for 202603).
    Negates amounts to convert from debit-positive to kredit-debet convention.
    Returns (is_rows, bs_rows).
    """
    wb = openpyxl.load_workbook(str(filepath), data_only=True)
    ws = wb.active

    amount_col: int | None = None
    header_row: int | None = None

    for row_idx, row in enumerate(ws.iter_rows(max_row=5, values_only=True)):
        for col_idx, cell in enumerate(row):
            if _match_period(cell, period):
                amount_col = col_idx
                header_row = row_idx
                break
        if amount_col is not None:
            break

    if amount_col is None:
        wb.close()
        raise ValueError(
            f"Kunde inte hitta månadskolumn för period {period} i {filepath.name}"
        )

    # Account in col 0, name in col 1
    is_rows: list = []
    bs_rows: list = []

    for row in ws.iter_rows(min_row=header_row + 2, values_only=True):
        acc = parse_account(row[0] if row else None)
        if acc is None:
            continue
        kind = classify_188(acc)
        if kind is None:
            continue
        name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        raw = row[amount_col] if len(row) > amount_col else None
        amt = round(-parse_amount(raw), 2)   # negate: debit-positive → kredit-debet
        if amt == 0.0:
            continue
        if kind == "IS":
            is_rows.append((acc, name, amt))
        else:
            bs_rows.append((acc, name, amt))

    wb.close()
    return is_rows, bs_rows


# ── Reader: SuSa pro Monat (231, 245) ─────────────────────────────────────────
def read_susa_pro_monat(filepath: Path) -> tuple[list, list]:
    """
    Reads 'Summen- und Saldenliste pro Monat' format.

    Expected header (row 2):
      Konto | Beschriftung | EB-Wert | S | H | Saldo | S | H |
      Mrz 2026 Soll | Haben | kum. Werte Soll | Haben

    Amount = col9 (Haben) − col8 (Soll) = kredit-debet sign.
    Excludes accounts >= 9000 (sub-ledger, contra).
    """
    wb = openpyxl.load_workbook(str(filepath), data_only=True)
    ws = wb.active

    is_rows: list = []
    bs_rows: list = []

    for row in ws.iter_rows(min_row=3, values_only=True):
        acc = parse_account(row[0] if row else None)
        if acc is None:
            continue
        kind = classify_susa(acc)
        if kind is None:
            continue
        name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        soll  = parse_amount(row[8] if len(row) > 8 else None)
        haben = parse_amount(row[9] if len(row) > 9 else None)
        amt = round(haben - soll, 2)
        if amt == 0.0:
            continue
        if kind == "IS":
            is_rows.append((acc, name, amt))
        else:
            bs_rows.append((acc, name, amt))

    wb.close()
    return is_rows, bs_rows


# ── Reader: SuSa Jahresübersicht (246) ────────────────────────────────────────
def read_susa_jahresuebersicht(
    filepath: Path, period: str
) -> tuple[list, list]:
    """
    Reads 'SUSA Jahresübersicht' with one column group per month.

    Header layout per month group: amount | S | H
    Finds the column group matching *period* via header text (e.g. 'Mrz/2026').
    Amount sign: +amount if H-indicator, −amount if S-indicator.
    Excludes accounts >= 9000.
    """
    wb = openpyxl.load_workbook(str(filepath), data_only=True)
    ws = wb.active

    amount_col: int | None = None
    header_row: int | None = None

    for row_idx, row in enumerate(ws.iter_rows(max_row=5, values_only=True)):
        for col_idx, cell in enumerate(row):
            if _match_period(cell, period):
                amount_col = col_idx
                header_row = row_idx
                break
        if amount_col is not None:
            break

    if amount_col is None:
        wb.close()
        raise ValueError(
            f"Kunde inte hitta månadskolumn för period {period} i {filepath.name}"
        )

    s_col = amount_col + 1   # S (Soll/debit) indicator column
    h_col = amount_col + 2   # H (Haben/credit) indicator column

    is_rows: list = []
    bs_rows: list = []

    for row in ws.iter_rows(min_row=header_row + 2, values_only=True):
        acc = parse_account(row[0] if row else None)
        if acc is None:
            continue
        kind = classify_susa(acc)
        if kind is None:
            continue
        if len(row) <= amount_col or row[amount_col] is None:
            continue
        name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        raw = parse_amount(row[amount_col])
        s_ind = row[s_col] if len(row) > s_col else None
        h_ind = row[h_col] if len(row) > h_col else None
        if h_ind == "H":
            amt = round(raw, 2)
        elif s_ind == "S":
            amt = round(-raw, 2)
        else:
            continue
        if amt == 0.0:
            continue
        if kind == "IS":
            is_rows.append((acc, name, amt))
        else:
            bs_rows.append((acc, name, amt))

    wb.close()
    return is_rows, bs_rows


# ── Reader: SuSa CSV (220) ────────────────────────────────────────────────────
def read_susa_csv(filepath: Path) -> tuple[list, list]:
    """
    Reads DATEV SuSa CSV (cp1252, semicolon-delimited).

    Column layout:
      0 Kontonummer | 1 Kontobezeichnung | 2 Anfangsbestand |
      3 Monatsumsatz Soll | 4 Monatsumsatz Haben | ...

    Haben is stored as a negative value.
    amount = -(Soll + Haben)  →  kredit-debet convention (revenues +, costs −).
    Skips rows with no account number (summary/header rows).
    Excludes accounts >= 9000.
    """
    import csv

    is_rows: list = []
    bs_rows: list = []

    with open(filepath, encoding="cp1252", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        for row in reader:
            if not row:
                continue
            acc = parse_account(row[0])
            if acc is None:
                continue
            kind = classify_susa(acc)
            if kind is None:
                continue
            name = row[1].strip() if len(row) > 1 else ""
            soll  = parse_de(row[3] if len(row) > 3 else None)
            haben = parse_de(row[4] if len(row) > 4 else None)
            amt = round(-(soll + haben), 2)
            if amt == 0.0:
                continue
            if kind == "IS":
                is_rows.append((acc, name, amt))
            else:
                bs_rows.append((acc, name, amt))

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
    src = GERMANY_DIR / filename
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


# ── Process one company ────────────────────────────────────────────────────────
def process_company(
    code: str,
    friendly: str,
    period: str,
    cfg: dict,
    dry_run: bool,
) -> None:
    print(f"\n── {code} {'─' * 45}")
    print(f"  {friendly}  ({period})")

    if cfg["reader"] == "skip":
        print("  ⚠  SKIP: Krypterade filer (.p7m) kan inte bearbetas automatiskt")
        if not dry_run:
            REFERENS_DIR.mkdir(exist_ok=True)
        for fname in cfg.get("extra", []):
            move_to_referens(fname, dry_run)
        return

    filepath = GERMANY_DIR / cfg["file"]
    print(f"  Fil: {filepath.name}")

    if not filepath.exists():
        print("  ⚠  SKIP: Filen saknas (redan i Referens?)")
        return

    try:
        reader = cfg["reader"]
        if reader == "monthly_value":
            is_rows, bs_rows = read_monthly_value(filepath, period)
        elif reader == "susa_pro_monat":
            is_rows, bs_rows = read_susa_pro_monat(filepath)
        elif reader == "susa_jahresuebersicht":
            is_rows, bs_rows = read_susa_jahresuebersicht(filepath, period)
        elif reader == "susa_csv":
            is_rows, bs_rows = read_susa_csv(filepath)
        else:
            print(f"  ❌ Okänd reader: {reader}")
            return
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
    move_to_referens(cfg["file"], dry_run)
    for fname in cfg.get("extra", []):
        move_to_referens(fname, dry_run)


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bearbeta tyska SuSa/Monthly-Value-filer → INL.xlsx"
    )
    parser.add_argument(
        "codes", nargs="*",
        help="Bolagskoder att köra (standard: alla). Ex: py process_germany.py 231 245",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Visa vad som skulle hända utan att skriva några filer",
    )
    args = parser.parse_args()

    period = prev_month_period()
    label  = "[DRY RUN] " if args.dry_run else ""
    print(f"{label}process_germany.py — {date.today()}  Period: {period}")
    print(f"  Germany-mapp : {GERMANY_DIR}")
    print(f"  Dotterbolag  : {DOTTERBOLAG}")

    if not GERMANY_DIR.exists():
        sys.exit(f"❌  Germany-mappen saknas: {GERMANY_DIR}")
    if not DOTTERBOLAG.exists():
        sys.exit(f"❌  Dotterbolagslistan saknas: {DOTTERBOLAG}")

    friendlies = load_friendly_names()

    if not args.dry_run:
        OUTPUT_DIR.mkdir(exist_ok=True)
        REFERENS_DIR.mkdir(exist_ok=True)

    all_codes  = sorted(COMPANY_DEFS.keys())
    codes_to_run = args.codes if args.codes else all_codes

    for code in codes_to_run:
        if code not in COMPANY_DEFS:
            print(f"\n⚠  Okänd bolagskod: {code}")
            continue
        cfg      = COMPANY_DEFS[code]
        friendly = friendlies.get(int(code), f"Bolag{code}")
        process_company(code, friendly, period, cfg, args.dry_run)

    print(f"\n{'═' * 55}")
    print("Klart!")
    if args.dry_run:
        print("(DRY RUN — inga filer ändrades)")


if __name__ == "__main__":
    main()

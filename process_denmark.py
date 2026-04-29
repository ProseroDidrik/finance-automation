#!/usr/bin/env python3
"""
process_denmark.py  –  Danish Saldobalance XLSX → INL.xlsx

Kör från C:\\Users\\DidWac\\dev\\finance-automation\\ :
    py process_denmark.py               # kör alla bolag
    py process_denmark.py 229 242       # kör specifika bolag
    py process_denmark.py --dry-run     # visa utan att skriva

Vad scriptet gör:
  1. Läser Saldobalance XLSX per bolag, delar konton i IS/BS per bolagskonfiguration
  2. Stoppar vid "Nulkontrol"-sektionen (IS-konton visas annars dubbelt)
  3. Hoppar över bold+underline summerings-rader (bolag 178)
  4. Skriver {kod}_{Namn}_{YYYYMM}_INL.xlsx till Denmark/output/
  5. Flyttar källfiler till Denmark/Referens/

Bolagsspecifika IS/BS-gränser (4-siffrig kontoprefixnivå):
  178: IS = 0–4999,  BS = 5000+   (hoppa över bold+underline summary-rader)
  190: IS = 0–4999,  BS = 5000+
  216: IS = 0–9999   (enbart resultaträkning, inga BS-konton)
  229: IS = 0–4999,  BS = 5000+   (BS från YTD-fil: FEB-saldo − MAR-saldo)
  242: IS = 0–799,   BS = 800+
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
DENMARK_DIR  = GET_TESTFILES / "extracted" / "Denmark"
OUTPUT_DIR   = DENMARK_DIR / "output"
REFERENS_DIR = DENMARK_DIR / "Referens"
DOTTERBOLAG  = _BASE / "_params" / "Dotterbolagslista.xlsx"

# ── Company definitions ────────────────────────────────────────────────────────
# is_max : max 4-digit account prefix (inclusive) counted as IS/P&L
# bs_min : min 4-digit account prefix (inclusive) counted as BS; None = no BS
COMPANY_DEFS: dict[str, dict] = {
    "178": dict(
        is_max=4999, bs_min=5000, skip_formatting=True,
        file="178_03 Marts 2026.xlsx",
        exclude=[1999, 6140],
        extra=[],
    ),
    "190": dict(
        is_max=4999, bs_min=5000, skip_formatting=False,
        file="190_03. Sikring Nord 0101-31032026.xlsx",
        extra=[],
    ),
    "216": dict(
        is_max=9999, bs_min=None, skip_formatting=False,
        file="216_Balance pr. 310326 SIKOM Danmark.xlsx",
        extra=[],
    ),
    "229": dict(
        is_max=4999, bs_min=5000, skip_formatting=False,
        file="229_Saldobalance månedsopdelt - 01-03-2026 - 31-03-2026.xlsx",
        ytd_file="229_Saldobalance månedsopdelt - 01-01-2026 - 31-03-2026.xlsx",
        extra=["229_Saldobalance månedsopdelt - 01-01-2026 - 31-03-2026.xlsx"],
    ),
    "242": dict(
        is_max=799, bs_min=800, skip_formatting=False,
        file="242_Saldobalance - 01-03-2026 - 31-03-2026.xlsx",
        extra=["242_Saldobalance - 01-01-2026 - 31-03-2026.xlsx"],
    ),
}

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
    KEYWORDS = ("konto", "debet", "kredit", "saldo", "navn", "tekst", "beskrivelse", "periode")

    for row_idx, row in enumerate(ws.iter_rows(max_row=20)):
        texts = [
            str(c.value).strip().lower() if c.value is not None else ""
            for c in row
        ]
        if sum(1 for t in texts if any(kw in t for kw in KEYWORDS)) < 1:
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
            if "saldo"  not in cols and ("saldo" in t or "periode" in t):
                cols["saldo"] = j

        # Require saldo > 0 when it's the only useful column found, to avoid
        # matching title rows like "Periode: 01-03-2026" at col 0 (which would
        # clash with the default acc=0).
        has_accs  = "acc" in cols or ("debet" in cols and "kredit" in cols)
        has_saldo = "saldo" in cols and cols["saldo"] > 0
        if has_accs or has_saldo:
            return row_idx, cols

    return None, {}


# ── Saldobalance reader ────────────────────────────────────────────────────────
def read_saldobalance(
    filepath: Path,
    is_max_4d: int,
    bs_min_4d: int | None,
    skip_formatting: bool = False,
    exclude: list[int] | None = None,
) -> tuple[list, list]:
    """
    Returns (is_rows, bs_rows), each a list of (account_int, name_str, amount_float).

    Amounts use kredit-debet sign convention (income positive, costs negative).
    Stops at a Nulkontrol ACCOUNT ROW (account present + "nulkontrol" in any cell).
    Section-header rows (empty account) named "Nulkontrol" are skipped, not treated
    as stop signals — this handles månedsopdelt files (e.g. 229) where the file has
    a second IS+BS section under a "Nulkontrol" heading.
    Duplicate account numbers are skipped to avoid double-counting from that second
    section.
    If skip_formatting=True, rows where any of the first 3 cells is bold+underline
    are skipped (handles 178's summary/total rows).
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
    seen_accs: set[int] = set()
    in_nulkontrol = False  # True once we pass the Nulkontrol section header

    for row in ws.iter_rows(min_row=header_row_idx + 2):
        if col_acc >= len(row):
            continue
        acc_cell = row[col_acc]

        # Detect the Nulkontrol section-header row (empty account containing
        # "Nulkontrol"): e.g. row 132 in månedsopdelt files.  After this point
        # IS accounts have YTD-cumulative values, not monthly movements.
        if acc_cell.value is None or str(acc_cell.value).strip() == "":
            if any("nulkontrol" in str(c.value).lower() for c in row if c.value is not None):
                in_nulkontrol = True
            continue

        try:
            acc = int(float(str(acc_cell.value).strip()))
        except (ValueError, TypeError):
            continue

        # Stop on a numeric "Nulkontrol" account row (e.g. acc=9990 in 178).
        if any("nulkontrol" in str(c.value).lower() for c in row if c.value is not None):
            break

        # Skip accounts already processed (månedsopdelt double-section files)
        if acc in seen_accs:
            continue
        seen_accs.add(acc)

        if exclude and acc in exclude:
            continue

        # In the Nulkontrol section, IS accounts carry YTD values — skip them.
        # BS accounts (after the YTD-IS section) are still valid to collect.
        acc4 = normalize4(acc)
        if in_nulkontrol and acc4 <= is_max_4d:
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

        # Amount: sign convention = kredit − debet (income positive, costs negative).
        # Saldo/Periode columns already store debet−kredit, so negate them.
        if col_saldo is not None and col_saldo < len(row):
            amt = -parse_amount(row[col_saldo].value)
        elif col_debet is not None and col_kredit is not None:
            d = parse_amount(row[col_debet].value  if col_debet  < len(row) else None)
            k = parse_amount(row[col_kredit].value if col_kredit < len(row) else None)
            amt = k - d
        elif col_debet is not None and col_debet < len(row):
            amt = -parse_amount(row[col_debet].value)
        else:
            continue

        amt = round(amt, 2)
        if amt == 0.0:
            continue

        if acc4 <= is_max_4d:
            is_rows.append((acc, name, amt))
        elif bs_min_4d is not None and acc4 >= bs_min_4d:
            bs_rows.append((acc, name, amt))

    wb.close()
    return is_rows, bs_rows


# ── YTD BS reader (månedsopdelt) ───────────────────────────────────────────────
def read_bs_from_ytd(filepath: Path, bs_min_4d: int) -> list:
    """
    Reads BS accounts from a YTD månedsopdelt file.
    Finds the last two month columns (e.g. FEB 2026, MAR 2026) and returns
    BS rows as (acc, name, prev_val − curr_val) in kredit-debet convention.
    """
    wb = openpyxl.load_workbook(str(filepath), data_only=True)
    ws = wb.active

    prev_col: int | None = None
    curr_col: int | None = None
    header_row_idx: int | None = None

    for row_idx, row in enumerate(ws.iter_rows(max_row=20)):
        month_cols = []
        for j, cell in enumerate(row):
            if cell.value is None:
                continue
            t = str(cell.value).strip().lower()
            # Match "jan 2026", "feb2026", "mar 2026" etc.
            if re.match(r"^[a-zæøå]{3}\s*\d{4}$", t):
                month_cols.append(j)
        if len(month_cols) >= 2:
            prev_col = month_cols[-2]
            curr_col = month_cols[-1]
            header_row_idx = row_idx
            break

    if header_row_idx is None or prev_col is None or curr_col is None:
        wb.close()
        raise ValueError(f"Fann inga månadskolumner i {filepath.name}")

    # Detect acc/name columns from same or neighbouring rows
    col_acc, col_name = 0, 2
    for row_idx2, row in enumerate(ws.iter_rows(max_row=header_row_idx + 1)):
        texts = [str(c.value).strip().lower() if c.value is not None else "" for c in row]
        for j, t in enumerate(texts):
            if "konto" in t:
                col_acc = j
            if any(kw in t for kw in ("navn", "tekst", "beskrivelse")):
                col_name = j

    bs_rows: list = []
    seen_accs: set[int] = set()

    for row in ws.iter_rows(min_row=header_row_idx + 2):
        if col_acc >= len(row):
            continue
        acc_cell = row[col_acc]
        if acc_cell.value is None:
            continue
        try:
            acc = int(float(str(acc_cell.value).strip()))
        except (ValueError, TypeError):
            continue

        if any("nulkontrol" in str(c.value).lower() for c in row if c.value is not None):
            break

        if acc in seen_accs:
            continue
        seen_accs.add(acc)

        acc4 = normalize4(acc)
        if acc4 < bs_min_4d:
            continue

        name = ""
        if col_name < len(row) and row[col_name].value is not None:
            name = str(row[col_name].value).strip()

        prev_val = parse_amount(row[prev_col].value if prev_col < len(row) else None)
        curr_val = parse_amount(row[curr_col].value if curr_col < len(row) else None)
        amt = round(prev_val - curr_val, 2)
        if amt == 0.0:
            continue

        bs_rows.append((acc, name, amt))

    wb.close()
    return bs_rows


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
    exclude: list[int] | None = None,
    ytd_filepath: Path | None = None,
) -> None:
    print(f"\n── {code} {'─' * 45}")
    print(f"  {friendly}  ({period})")
    print(f"  Fil: {filepath.name}")

    if not filepath.exists():
        print(f"  ⚠  SKIP: Filen saknas (redan i Referens?)")
        return

    try:
        is_rows, bs_rows = read_saldobalance(filepath, is_max_4d, bs_min_4d, skip_formatting, exclude)
        if ytd_filepath is not None and bs_min_4d is not None:
            if ytd_filepath.exists():
                bs_rows = read_bs_from_ytd(ytd_filepath, bs_min_4d)
            else:
                print(f"  ⚠ YTD-fil saknas: {ytd_filepath.name}")
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

    all_codes = sorted(COMPANY_DEFS.keys())
    codes_to_run = args.codes if args.codes else all_codes

    for code in codes_to_run:
        if code not in COMPANY_DEFS:
            print(f"\n⚠  Okänd bolagskod: {code}")
            continue

        cfg         = COMPANY_DEFS[code]
        friendly    = friendlies.get(int(code), f"Bolag{code}")
        filepath    = DENMARK_DIR / cfg["file"]
        ytd_file    = cfg.get("ytd_file")
        ytd_filepath = DENMARK_DIR / ytd_file if ytd_file else None

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
            exclude=cfg.get("exclude"),
            ytd_filepath=ytd_filepath,
        )

    print(f"\n{'═' * 55}")
    print("Klart!")
    if args.dry_run:
        print("(DRY RUN — inga filer ändrades)")


if __name__ == "__main__":
    main()

"""
process_finland.py  –  Finnish Balansräkning + Resultaträkning → INL.xlsx

Output:  {code}_{FriendlyName}_{YYYYMM}_INL.xlsx
Layout:  empty row 1, then IS rows, then BS rows
         A = account number,  B = account name,  C = amount
Signs:   BS accounts 1-1999 (or 100-199999 for 6-digit) are multiplied by -1
Skip:    237X accounts (årets resultat), zero amounts, summary rows, bold rows (134)
Verify:  column C must sum to 0
"""

import csv
import os
import re
import shutil

import pandas as pd
import xlrd


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FINLAND_DIR = (
    r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation"
    r"\April alla filer\Get testfiles\extracted\Finland"
)
OUTPUT_DIR = os.path.join(FINLAND_DIR, "output")
REFERENS_DIR = os.path.join(FINLAND_DIR, "Referens")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def parse_amount(val) -> float:
    if val is None or (isinstance(val, float) and val != val):
        return 0.0
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    s = s.replace(" ", "").replace(",", ".")          # Finnish thousands/decimal
    if s in ("", "nan", "0.00", "0"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


_ACC_RE = re.compile(r"^(\d+)[\s,]+(.+)$")

def split_account(text: str):
    """Return (int_acc, name) or None for non-account rows."""
    text = text.strip()
    m = _ACC_RE.match(text)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None


def _normalize4(account: int) -> int:
    """Return the 4-digit prefix of any account number (works for 4, 5, 6 digits)."""
    s = str(account)
    return int(s[:4]) if len(s) > 4 else account


def is_237x(account: int) -> bool:
    """True for årets-resultat accounts (237X family, any digit count)."""
    return 2370 <= _normalize4(account) <= 2379


def should_flip(account: int) -> bool:
    """BS asset accounts (1–1999 in 4-digit space) need sign flip."""
    return 1 <= _normalize4(account) <= 1999


def apply_flip(amount: float, account: int) -> float:
    return -amount if should_flip(account) else amount


def extract_company_name(raw: str) -> str:
    name = str(raw).strip()
    for sfx in [" Oy", " Ab", " AS", " A/S", " Ltd", " OY", " AB", " ry"]:
        if name.endswith(sfx):
            name = name[: -len(sfx)].strip()
            break
    return name


_MONTH_NAMES = {
    "january": 1,  "february": 2,  "march": 3,    "april": 4,
    "may": 5,      "june": 6,      "july": 7,     "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "tammikuu": 1, "helmikuu": 2,  "maaliskuu": 3, "huhtikuu": 4,
    "toukokuu": 5, "kesäkuu": 6,   "heinäkuu": 7,  "elokuu": 8,
    "syyskuu": 9,  "lokakuu": 10,  "marraskuu": 11, "joulukuu": 12,
}


def extract_period(text: str) -> str:
    """Extract YYYYMM from any period string (dates, period codes, month names)."""
    m = re.search(r"(\d{1,2})/(\d{4})", text)
    if m:
        return f"{m.group(2)}{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}{int(m.group(2)):02d}"
    m = re.search(r"(20\d{4})", text)
    if m:
        return m.group(1)
    # "March 2026" / "Maaliskuu 2026"
    m = re.search(r"(\w+)\s+(20\d{2})", text)
    if m:
        month_num = _MONTH_NAMES.get(m.group(1).lower())
        if month_num:
            return f"{m.group(2)}{month_num:02d}"
    return ""


def _find_period_col(rows: list, target_period: str) -> int | None:
    """Scan the first rows for a cell whose extract_period() matches target_period.
    Works for both list-of-lists (CSV) and list-of-tuples (XLSX)."""
    for row in rows[:10]:
        for i, cell in enumerate(row):
            if extract_period(str(cell).strip() if cell is not None else "") == target_period:
                return i
    return None


def save_xlsx(is_rows: list, bs_rows: list, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    records = [{"A": None, "B": None, "C": None}]
    for acc, name, amt in is_rows + bs_rows:
        records.append({"A": acc, "B": name, "C": amt})
    df = pd.DataFrame(records)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, header=False, sheet_name="Sheet1")


def verify_sum(is_rows, bs_rows) -> float:
    return sum(r[2] for r in is_rows + bs_rows)


def move_to_referens(filename: str):
    src = os.path.join(FINLAND_DIR, filename)
    dst = os.path.join(REFERENS_DIR, filename)
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.move(src, dst)
        print(f"  Moved to Referens: {filename}")


def move_pdfs_to_referens():
    for f in os.listdir(FINLAND_DIR):
        if f.lower().endswith(".pdf"):
            move_to_referens(f)


# ---------------------------------------------------------------------------
# Format A  –  "Fennoa CSV" (146)
#   col 0 = "XXXX Name",  col 1 = latest month amount
#   encoding latin-1, separator ;
# ---------------------------------------------------------------------------

def read_fennoa_csv(filepath: str) -> list:
    df = pd.read_csv(filepath, header=None, encoding="latin-1", sep=";", dtype=str)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[1])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format B  –  "Muutos CSV/XLSX"  (161, 181, 173-BS)
#   col 0 = "XXXX Name",  col 2 = Muutos (change)
#   encoding latin-1, separator ; (CSV) or default (XLSX)
# ---------------------------------------------------------------------------

def _collect_muutos(df: pd.DataFrame) -> list:
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[2])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def read_muutos_csv(filepath: str) -> list:
    df = pd.read_csv(filepath, header=None, encoding="latin-1", sep=";", dtype=str)
    return _collect_muutos(df)


def read_muutos_xlsx(filepath: str, sheet_name=0) -> list:
    df = pd.read_excel(filepath, sheet_name=sheet_name, header=None, dtype=str)
    return _collect_muutos(df)


# ---------------------------------------------------------------------------
# Format C  –  "Income-only XLSX"  (173-IS)
#   col 0 = "XXXX Name",  col 1 = amount
# ---------------------------------------------------------------------------

def read_income_only_xlsx(filepath: str, sheet_name=0) -> list:
    df = pd.read_excel(filepath, sheet_name=sheet_name, header=None, dtype=str)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[1])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format D  –  "Period XLS"  (166 IS)
#   row 0 = period labels (202601, 202602, 202603)
#   col 0 = "XXXX  Name",  find col with 202603 → monthly IS amount
#
# Format D2 – "Period XLS diff" (166 BS)
#   Same layout but col values are CUMULATIVE balances → change = col_target - col_prev
# ---------------------------------------------------------------------------

def _find_period_col(df: pd.DataFrame, target_period: str, filepath: str) -> int:
    for j, val in enumerate(df.iloc[0]):
        if pd.notna(val) and extract_period(str(val)) == target_period:
            return j
    raise ValueError(f"Period column {target_period} not found in {filepath}")


def read_period_xls(filepath: str, target_period: str = "202603") -> list:
    df = pd.read_excel(filepath, header=None, dtype=str)
    period_col = _find_period_col(df, target_period, filepath)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[period_col])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def read_period_xls_diff(filepath: str, target_period: str = "202603") -> list:
    """166-style BS: values are period-end balances → compute col_target - col_prev."""
    df = pd.read_excel(filepath, header=None, dtype=str)
    period_col = _find_period_col(df, target_period, filepath)
    prev_col = period_col - 1   # previous month column
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        curr = parse_amount(row.iloc[period_col])
        prev = parse_amount(row.iloc[prev_col]) if prev_col >= 0 else 0.0
        amt = curr - prev
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format E  –  "Turvatalo XLSX"  (153)
#   row 5 = period headers ("3/2026", ...)
#   col 0 = "XXXX, Name",  col 1 = 3/2026 amount
# ---------------------------------------------------------------------------

def read_turvatalo_xlsx(filepath: str, target_period: str = "202603") -> list:
    df = pd.read_excel(filepath, header=None, dtype=str)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[1])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format F  –  "Ajan Lukko XLSX"  (179)
#   Wide format, account in col 3 ("XXXX   Name"),  period col found via "3 (3)" header
# ---------------------------------------------------------------------------

def _find_col_by_pattern(df: pd.DataFrame, header_row: int, pattern: str) -> int:
    for j, val in enumerate(df.iloc[header_row]):
        if pd.notna(val) and re.search(pattern, str(val), re.IGNORECASE):
            return j
    raise ValueError(f"Column matching '{pattern}' not found in row {header_row}")


def read_ajan_lukko_xlsx(filepath: str) -> list:
    df = pd.read_excel(filepath, header=None, dtype=str)
    period_col = _find_col_by_pattern(df, header_row=5, pattern=r"3\s*\(3\)")
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[3]) if pd.notna(row.iloc[3]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[period_col])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format G  –  "Kausi XLS/XLSM"  (134, 196)
#   col 0 = account number (numeric),  col 1 = name,  col 2 = Kausi (period)
#   For 134: skip bold rows (xlrd)
# ---------------------------------------------------------------------------

def _is_bold_xlrd(wb, ws, row_idx: int) -> bool:
    row = ws.row(row_idx)
    if len(row) < 2:
        return False
    xf = wb.xf_list[row[1].xf_index]
    return bool(wb.font_list[xf.font_index].bold)


def read_kausi_xls(filepath: str, skip_bold: bool = False) -> list:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".xls" and skip_bold:
        return _read_kausi_xls_xlrd(filepath)
    if ext in (".xlsx", ".xlsm") and skip_bold:
        return _read_kausi_xlsx_openpyxl(filepath)
    df = pd.read_excel(filepath, header=None, dtype=str)
    return _collect_kausi_df(df)


def _read_kausi_xls_xlrd(filepath: str) -> list:
    wb = xlrd.open_workbook(filepath, formatting_info=True)
    ws = wb.sheets()[0]
    rows = []
    for i in range(ws.nrows):
        if _is_bold_xlrd(wb, ws, i):
            continue
        row = ws.row(i)
        val0 = row[0].value if row else None
        if not val0:
            continue
        try:
            acc = int(float(val0))
        except (ValueError, TypeError):
            continue
        name = str(row[1].value).strip() if len(row) > 1 else ""
        amt_raw = row[2].value if len(row) > 2 else None
        amt = parse_amount(amt_raw)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def _read_kausi_xlsx_openpyxl(filepath: str) -> list:
    from openpyxl import load_workbook
    wb = load_workbook(filepath, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows():
        if len(row) < 3:
            continue
        cell1 = row[1]
        if cell1.font and cell1.font.bold:
            continue
        cell0 = row[0]
        if cell0.value is None:
            continue
        try:
            acc = int(float(str(cell0.value)))
        except (ValueError, TypeError):
            continue
        name = str(cell1.value).strip() if cell1.value else ""
        amt = parse_amount(row[2].value)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def _collect_kausi_df(df: pd.DataFrame) -> list:
    rows = []
    for _, row in df.iterrows():
        val0 = row.iloc[0]
        if pd.isna(val0) or str(val0).strip() == "":
            continue
        try:
            acc = int(float(str(val0).strip()))
        except (ValueError, TypeError):
            continue
        name = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        amt = parse_amount(row.iloc[2])
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format H  –  "Avaava CSV UTF-8"  (199)
#   Semicolon separated, UTF-8 BOM, amounts may have spaces (thousands sep)
#   BS: col 2 = Jakson muutos,  IS: col 1 = period amount
# ---------------------------------------------------------------------------

def read_avaava_csv(filepath: str, amount_col: int) -> list:
    rows = []
    with open(filepath, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        for line in reader:
            if not line:
                continue
            cell = line[0].strip()
            parsed = split_account(cell)
            if parsed is None:
                continue
            acc, name = parsed
            if len(line) <= amount_col:
                continue
            amt = parse_amount(line[amount_col])
            if amt == 0.0:
                continue
            rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format I  –  Generic CSV with configurable column and encoding
#   Used for: UTF-16-LE muutos (193/195 BS), UTF-16-LE fennoa (195 IS),
#             UTF-16-LE monthly col 3 (193 IS)
# ---------------------------------------------------------------------------

def _read_csv_rows(filepath: str, encoding: str) -> list[list[str]]:
    """Read a semicolon-separated CSV as a list of string lists.
    Uses csv.reader via StringIO so UTF-16-LE quoted files parse correctly."""
    import io as _io, csv as _csv
    with open(filepath, encoding=encoding) as fh:
        content = fh.read()
    return list(_csv.reader(_io.StringIO(content), delimiter=";", quotechar='"'))


def read_csv_col_enc(filepath: str, col: int, encoding: str = "latin-1",
                     target_period: str = None) -> list:
    """col 0 = 'NNNN Name', col <col> = amount. Handles any encoding.
    If target_period given, scans header rows to find the right column dynamically."""
    all_rows = _read_csv_rows(filepath, encoding)
    if target_period:
        found = _find_period_col(all_rows, target_period)
        if found is not None:
            col = found
    rows = []
    for row in all_rows:
        cell = row[0].strip() if row else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row[col] if len(row) > col else None)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def read_muutos_csv_enc(filepath: str, encoding: str = "latin-1") -> list:
    """Muutos-style (col 2 = change) with configurable encoding."""
    rows = []
    for row in _read_csv_rows(filepath, encoding):
        cell = row[0].strip() if row else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row[2] if len(row) > 2 else None)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format J  –  Emsec XLSX  (215)
#   BS: col 1 = 'NNNN Name', col 3 = change (numeric or '\xa0'-formatted string)
#   PL: col 1 = 'NNNN Name', col 2 = monthly amount
# ---------------------------------------------------------------------------

def read_emsec_bs_xlsx(filepath: str) -> list:
    df = pd.read_excel(filepath, header=None, dtype=object)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[1]) if len(row) > 1 and row.iloc[1] is not None else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[3] if len(row) > 3 else None)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def read_emsec_pl_xlsx(filepath: str) -> list:
    df = pd.read_excel(filepath, header=None, dtype=object)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[1]) if len(row) > 1 and row.iloc[1] is not None else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        amt = parse_amount(row.iloc[2] if len(row) > 2 else None)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format K  –  JM Lukko combined BS+IS XLSX  (221)
#   Single sheet; col 0 = 'NNNN Name', col 1 = YTD, col 2 = monthly.
#   Filter by 4-digit account prefix: 1000-2999 → BS, 3000-9999 → IS.
# ---------------------------------------------------------------------------

def read_combined_xlsx_col2(filepath: str, acc_min_4d: int = 0, acc_max_4d: int = 9999) -> list:
    df = pd.read_excel(filepath, header=None, dtype=str)
    rows = []
    for _, row in df.iterrows():
        cell = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
        parsed = split_account(cell)
        if parsed is None:
            continue
        acc, name = parsed
        if not (acc_min_4d <= _normalize4(acc) <= acc_max_4d):
            continue
        amt = parse_amount(row.iloc[2] if len(row) > 2 else None)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Format L  –  ANV period-column CSV  (238)
#   Header row: ;202601;202602;202603;
#   BS: col3 - col2 (period-end balances → monthly change)
#   IS: col3 directly (monthly amounts)
#   Encoding cp1252; amounts may use space as thousands sep.
# ---------------------------------------------------------------------------

def _parse_period_csv_lines(filepath: str):
    with open(filepath, encoding="cp1252", newline="") as fh:
        for line in fh:
            cols = line.rstrip("\n").split(";")
            cell = cols[0].strip() if cols else ""
            parsed = split_account(cell)
            if parsed is None:
                continue
            yield parsed, cols


def _period_csv_target_col(filepath: str, target_period: str) -> int:
    """Find the column index matching target_period in the header of a period CSV."""
    with open(filepath, encoding="cp1252") as fh:
        header = fh.readline()
    cols = header.rstrip("\n").split(";")
    for i, c in enumerate(cols):
        if extract_period(c.strip()) == target_period:
            return i
    # Fallback: last YYYYMM-looking column
    last = None
    for i, c in enumerate(cols):
        if re.match(r"^\s*20\d{4}\s*$", c):
            last = i
    return last if last is not None else 3


def read_period_csv_diff(filepath: str, target_period: str) -> list:
    """238 BS: period-column CSV. Computes change = col[target] - col[target-1]."""
    target_col = _period_csv_target_col(filepath, target_period)
    rows = []
    for (acc, name), cols in _parse_period_csv_lines(filepath):
        curr = parse_amount(cols[target_col]) if len(cols) > target_col else 0.0
        prev = parse_amount(cols[target_col - 1]) if len(cols) > target_col - 1 else 0.0
        amt = round(curr - prev, 2)
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


def read_period_csv_col(filepath: str, target_period: str) -> list:
    """238 IS: period-column CSV. Uses the column matching target_period."""
    target_col = _period_csv_target_col(filepath, target_period)
    rows = []
    for (acc, name), cols in _parse_period_csv_lines(filepath):
        amt = parse_amount(cols[target_col]) if len(cols) > target_col else 0.0
        if amt == 0.0:
            continue
        rows.append((acc, name, amt))
    return rows


# ---------------------------------------------------------------------------
# Common post-processing (flip signs, exclude 237X)
# ---------------------------------------------------------------------------

def post_process(raw_rows: list, flip: bool = True) -> list:
    result = []
    for acc, name, amt in raw_rows:
        if is_237x(acc):
            continue
        if flip:
            amt = apply_flip(amt, acc)
        result.append((acc, name, round(amt, 2)))
    return result


# ---------------------------------------------------------------------------
# Company processor
# ---------------------------------------------------------------------------

def process_company(
    code: str,
    friendly_name: str,
    period: str,
    bs_rows_raw: list,
    is_rows_raw: list,
    extra_files_to_referens: list = None,
):
    print(f"\n{'='*60}")
    print(f"Processing {code}  –  {friendly_name}  ({period})")

    bs_rows = post_process(bs_rows_raw, flip=True)
    is_rows = post_process(is_rows_raw, flip=False)

    total = verify_sum(is_rows, bs_rows)
    check = "OK (~0)" if abs(total) < 0.11 else f"WARNING: {total:.2f}"
    print(f"  Rows IS={len(is_rows)}, BS={len(bs_rows)}   Sum={total:.4f}  {check}")

    filename = f"{code}_{friendly_name}_{period}_INL.xlsx"
    save_xlsx(is_rows, bs_rows, os.path.join(OUTPUT_DIR, filename))
    print(f"  Saved: {filename}")

    if extra_files_to_referens:
        for f in extra_files_to_referens:
            move_to_referens(f)

    return filename


# ---------------------------------------------------------------------------
# Individual company definitions
# ---------------------------------------------------------------------------

def p(name):
    """Full path in Finland dir."""
    return os.path.join(FINLAND_DIR, name)


def run_146():
    return process_company(
        code="146", friendly_name="Avain-Asema", period="202603",
        bs_rows_raw=read_fennoa_csv(p("146_Balansräkning 31032026.csv")),
        is_rows_raw=read_fennoa_csv(p("146_Resultaträkning 32026.csv")),
        extra_files_to_referens=[
            "146_Balansräkning 31032026.csv",
            "146_Resultaträkning 32026.csv",
        ],
    )


def run_134():
    return process_company(
        code="134", friendly_name="Arvolukko", period="202603",
        bs_rows_raw=read_kausi_xls(p("134_Arvolukko Oy 032026 tase.xls"), skip_bold=True),
        is_rows_raw=read_kausi_xls(p("134_Arvolukko Oy 032026 tuloslaskelma.xls"), skip_bold=True),
        extra_files_to_referens=[
            "134_Arvolukko Oy 032026 tase.xls",
            "134_Arvolukko Oy 032026 tuloslaskelma.xls",
            "134_Arvolukko Oy 032026 konsernianalyysi .xls",
        ],
    )


def run_153():
    return process_company(
        code="153", friendly_name="Turvatalo", period="202603",
        bs_rows_raw=read_turvatalo_xlsx(p("153_Balans 03-2026.xlsx")),
        is_rows_raw=read_turvatalo_xlsx(p("153_Income statement 03-2026.xlsx")),
        extra_files_to_referens=[
            "153_Balans 03-2026.xlsx",
            "153_Income statement 03-2026.xlsx",
        ],
    )


def run_161():
    return process_company(
        code="161", friendly_name="Lukitustekniikka-STY", period="202603",
        bs_rows_raw=read_muutos_csv(p("161_Tase_Lukitustekniikka-STY_Oy_(01.03.2026-31.03.2026) (2).csv")),
        is_rows_raw=read_fennoa_csv(p("161_Tuloslaskelma_Lukitustekniikka-STY_Oy_(01.03.2026-31.03.2026).csv")),
        extra_files_to_referens=[
            "161_Tase_Lukitustekniikka-STY_Oy_(01.03.2026-31.03.2026) (2).csv",
            "161_Tuloslaskelma_Lukitustekniikka-STY_Oy_(01.03.2026-31.03.2026).csv",
        ],
    )


def run_166():
    return process_company(
        code="166", friendly_name="Lukkoassa", period="202603",
        bs_rows_raw=read_period_xls_diff(p("166_Tase 31032026 kk.xls")),
        is_rows_raw=read_period_xls(p("166_Tuloslaskelma 31032026 kk sve.xls")),
        extra_files_to_referens=[
            "166_Tase 31032026 kk.xls",
            "166_Tuloslaskelma 31032026 kk sve.xls",
        ],
    )


def run_173():
    return process_company(
        code="173", friendly_name="Avainahjo", period="202603",
        bs_rows_raw=read_muutos_xlsx(p("173_AvainahjoBalance0326.xlsx")),
        is_rows_raw=read_income_only_xlsx(p("173_AvainahjoIncome0326.xlsx")),
        extra_files_to_referens=[
            "173_AvainahjoBalance0326.xlsx",
            "173_AvainahjoIncome0326.xlsx",
        ],
    )


def run_179():
    return process_company(
        code="179", friendly_name="Ajan-Lukko", period="202603",
        bs_rows_raw=read_ajan_lukko_xlsx(p("179_3.2026 balance sheet.xlsx")),
        is_rows_raw=read_ajan_lukko_xlsx(p("179_3.2026 income statement.xlsx")),
        extra_files_to_referens=[
            "179_3.2026 balance sheet.xlsx",
            "179_3.2026 income statement.xlsx",
        ],
    )


def run_181():
    return process_company(
        code="181", friendly_name="Tele-Projekti", period="202603",
        bs_rows_raw=read_muutos_csv(p("181_Tase_Tele-Projekti_Oy_(01.03.2026-31.03.2026).csv")),
        is_rows_raw=read_fennoa_csv(p("181_Tuloslaskelma_Tele-Projekti_Oy_(01.03.2026-31.03.2026).csv")),
        extra_files_to_referens=[
            "181_Tase_Tele-Projekti_Oy_(01.03.2026-31.03.2026).csv",
            "181_Tuloslaskelma_Tele-Projekti_Oy_(01.03.2026-31.03.2026).csv",
        ],
    )


def run_196():
    return process_company(
        code="196", friendly_name="ST-Halytys", period="202603",
        bs_rows_raw=read_kausi_xls(p("196_ST Hälytys Oy, tase 31.3.2026.pdf.xlsm"), skip_bold=True),
        is_rows_raw=read_kausi_xls(p("196_ST Hälytys Oy, profit & loss statement 3.2026.xlsm"), skip_bold=True),
        extra_files_to_referens=[
            "196_ST Hälytys Oy, tase 31.3.2026.pdf.xlsm",
            "196_ST Hälytys Oy, profit & loss statement 3.2026.xlsm",
        ],
    )


def run_199():
    return process_company(
        code="199", friendly_name="Etela-Halytintekniikka", period="202603",
        bs_rows_raw=read_avaava_csv(p("199_Tase 3_26.csv"), amount_col=2),
        is_rows_raw=read_avaava_csv(p("199_Tulos 3_26.csv"), amount_col=1),
        extra_files_to_referens=[
            "199_Tase 3_26.csv",
            "199_Tulos 3_26.csv",
        ],
    )


def run_170():
    return process_company(
        code="170", friendly_name="PAP", period="202603",
        bs_rows_raw=read_fennoa_csv(p("170_tase 3.2026.csv")),
        is_rows_raw=read_fennoa_csv(p("170_tulos 3.2026.csv")),
        extra_files_to_referens=[
            "170_tase 3.2026.csv",
            "170_tulos 3.2026.csv",
            "170_tase 3.2026.pdf",
            "170_tulos 3.2026.pdf",
            "170_tase-erittelyt 3.2026.pdf",
        ],
    )


def run_177():
    f = p("177_LL Balance and income statement MARCH 2026.xlsx")
    return process_company(
        code="177", friendly_name="Lukkoluket", period="202603",
        bs_rows_raw=read_muutos_xlsx(f, sheet_name="LL Balance Sheet MAR 2026"),
        is_rows_raw=read_income_only_xlsx(f, sheet_name="LL Income Statement MAR 2026"),
        extra_files_to_referens=["177_LL Balance and income statement MARCH 2026.xlsx"],
    )


def run_182():
    return process_company(
        code="182", friendly_name="THV", period="202603",
        bs_rows_raw=read_muutos_csv(p("182_Tase_THV_Tele-_ja_Hälytysvalvonta_Oy_(01.03.2026-31.03.2026).csv")),
        is_rows_raw=read_fennoa_csv(p("182_Tuloslaskelma_THV_Tele-_ja_Hälytysvalvonta_Oy_(01.03.2026-31.03.2026).csv")),
        extra_files_to_referens=[
            "182_Tase_THV_Tele-_ja_Hälytysvalvonta_Oy_(01.03.2026-31.03.2026).csv",
            "182_Tuloslaskelma_THV_Tele-_ja_Hälytysvalvonta_Oy_(01.03.2026-31.03.2026).csv",
            "182_Alv 03 2026.pdf",
            "182_Tase 03 2026.pdf",
            "182_Tulos 03 2026.pdf",
        ],
    )


def run_185():
    return process_company(
        code="185", friendly_name="Suomen-Turvalukko", period="202603",
        bs_rows_raw=read_muutos_csv(p("185_Balance_sheet_Suomen_Turvalukko_Oy_(01.03.2026-31.03.2026) (1).csv")),
        is_rows_raw=read_fennoa_csv(p("185_Income_statement_Suomen_Turvalukko_Oy_(01.03.2026-31.03.2026).csv")),
        extra_files_to_referens=[
            "185_Balance_sheet_Suomen_Turvalukko_Oy_(01.03.2026-31.03.2026) (1).csv",
            "185_Income_statement_Suomen_Turvalukko_Oy_(01.03.2026-31.03.2026).csv",
        ],
    )


def run_193():
    return process_company(
        code="193", friendly_name="Suomen-Turvakonsultit", period="202603",
        bs_rows_raw=read_muutos_csv_enc(
            p("193_Balance_sheet_Suomen_Turvakonsultit_Oy_(01.03.2026-31.03.2026).csv"),
            encoding="utf-16-le",
        ),
        is_rows_raw=read_csv_col_enc(
            p("193_Profit_and_loss_statement,_monthly_Suomen_Turvakonsultit_Oy_(01.01.2026-31.03.2026).csv"),
            col=3,
            encoding="utf-16-le",
            target_period="202603",
        ),
        extra_files_to_referens=[
            "193_Balance_sheet_Suomen_Turvakonsultit_Oy_(01.03.2026-31.03.2026).csv",
            "193_Profit_and_loss_statement,_monthly_Suomen_Turvakonsultit_Oy_(01.01.2026-31.03.2026).csv",
        ],
    )


def run_195():
    return process_company(
        code="195", friendly_name="Meri-Lapin", period="202603",
        bs_rows_raw=read_muutos_csv_enc(
            p("195_Tase_Meri-Lapin_Lukituspalvelu_Oy_(01.03.2026-31.03.2026).csv"),
            encoding="utf-16-le",
        ),
        is_rows_raw=read_csv_col_enc(
            p("195_Tuloslaskelma_Meri-Lapin_Lukituspalvelu_Oy_(01.03.2026-31.03.2026).csv"),
            col=1,
            encoding="utf-16-le",
        ),
        extra_files_to_referens=[
            "195_Tase_Meri-Lapin_Lukituspalvelu_Oy_(01.03.2026-31.03.2026).csv",
            "195_Tuloslaskelma_Meri-Lapin_Lukituspalvelu_Oy_(01.03.2026-31.03.2026).csv",
            "195_Tuloslaskelma,_kuukausittain_Meri-Lapin_Lukituspalvelu_Oy_(01.01.2026-31.03.2026).csv",
        ],
    )


def run_215():
    return process_company(
        code="215", friendly_name="Emsec", period="202603",
        bs_rows_raw=read_emsec_bs_xlsx(p("215_Emsec Oy BS 01.03.-31.3.2026.xlsx")),
        is_rows_raw=read_emsec_pl_xlsx(p("215_Emsec Oy PL 01.03.-31.3.2026.xlsx")),
        extra_files_to_referens=[
            "215_Emsec Oy BS 01.03.-31.3.2026.xlsx",
            "215_Emsec Oy PL 01.03.-31.3.2026.xlsx",
        ],
    )


def run_221():
    f = p("221_maaliskuu 2026.xlsx")
    return process_company(
        code="221", friendly_name="JM-Lukko", period="202603",
        bs_rows_raw=read_combined_xlsx_col2(f, acc_min_4d=1000, acc_max_4d=2999),
        is_rows_raw=read_combined_xlsx_col2(f, acc_min_4d=3000, acc_max_4d=9999),
        extra_files_to_referens=["221_maaliskuu 2026.xlsx"],
    )


def run_238():
    return process_company(
        code="238", friendly_name="ANV", period="202603",
        bs_rows_raw=read_period_csv_diff(p("238_tase-kk_1004090426.csv"), target_period="202603"),
        is_rows_raw=read_period_csv_col(p("238_tuloslaskelma-kk_1004090426.csv"), target_period="202603"),
        extra_files_to_referens=[
            "238_tase-kk_1004090426.csv",
            "238_tuloslaskelma-kk_1004090426.csv",
        ],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

RUNNERS = {
    "134": run_134,
    "146": run_146,
    "153": run_153,
    "161": run_161,
    "166": run_166,
    "170": run_170,
    "173": run_173,
    "177": run_177,
    "179": run_179,
    "181": run_181,
    "182": run_182,
    "185": run_185,
    "193": run_193,
    "195": run_195,
    "196": run_196,
    "199": run_199,
    "215": run_215,
    "221": run_221,
    "238": run_238,
}

if __name__ == "__main__":
    import sys

    move_pdfs_to_referens()

    codes = sys.argv[1:] if len(sys.argv) > 1 else sorted(RUNNERS)
    skipped = []
    for code in codes:
        if code not in RUNNERS:
            print(f"Unknown company code: {code}")
            continue
        try:
            RUNNERS[code]()
        except FileNotFoundError as e:
            skipped.append(code)
            print(f"  SKIP {code}: source file not found — {e.filename}")

    if skipped:
        print(f"\nSkipped (already processed / files in Referens): {', '.join(skipped)}")

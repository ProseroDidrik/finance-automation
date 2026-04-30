"""Shared utilities for finance-automation country processing scripts."""
from pathlib import Path
import shutil
import sys

try:
    import openpyxl
except ImportError:
    sys.exit("Saknar openpyxl — kör:  py -m pip install openpyxl")

try:
    import pandas as pd
except ImportError:
    sys.exit("Saknar pandas — kör:  py -m pip install pandas openpyxl")


def load_dotterbolag(path: Path) -> dict[int, str]:
    """bolagsid → friendly name from Dotterbolagslistan, skips 'consolidated' rows."""
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    result: dict[int, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 5:
            continue
        bolag_id = row[1]
        friendly = row[4]
        kind = row[7] if len(row) > 7 else None
        if str(kind).strip().lower() == "consolidated":
            continue
        if bolag_id and friendly:
            result[int(bolag_id)] = str(friendly).strip()
    wb.close()
    return result


def safe_dest(dest: Path) -> Path:
    """Return a unique path: append _2, _3, ... if dest already exists."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    i = 2
    while True:
        candidate = dest.parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def move_to_referens_safe(src: Path, referens_dir: Path, dry_run: bool) -> Path:
    """Move src into referens_dir, avoiding filename collisions. Returns actual destination."""
    dst = safe_dest(referens_dir / src.name)
    if dry_run:
        print(f"    [dry] → Referens/{dst.name}")
    else:
        referens_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"    → Referens/{dst.name}")
    return dst


def save_inl_xlsx(is_rows: list, bs_rows: list, output_path: Path) -> None:
    """Write IS+BS rows to INL.xlsx (empty row 1, then data rows with cols A/B/C)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"A": None, "B": None, "C": None}]
    for acc, name, amt in is_rows + bs_rows:
        records.append({"A": acc, "B": name, "C": amt})
    df = pd.DataFrame(records)
    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        df.to_excel(writer, index=False, header=False, sheet_name="Sheet1")

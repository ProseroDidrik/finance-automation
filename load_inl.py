"""Ladda INL.xlsx-filer (FI/DK/DE) till fact_balances i DuckDB.

INL.xlsx layout (skrivs av shared.save_inl_xlsx):
  rad 1: tom
  rad 2..: A=konto, B=namn, C=belopp, D='IS'|'BS'  (D saknas i äldre filer)

Default-källa: extracted/{period}/{Country}/output/*_INL.xlsx under base_path.
Använd --source-dir för att peka mot annan mapp (t.ex. _inbox/Facit för backfill).

Idempotens: rader för (company_id, period, source_kind, source_file) tas bort innan
nya skrivs. Du kan ladda samma fil om igen utan dubbletter.

Filnamnsformat: {ID:03d}_{FriendlyName}_{YYYYMM}_INL.xlsx
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import openpyxl

import db
from shared import begin_run, load_config, log, prev_month_period

INL_FILENAME_RE = re.compile(r"^(\d{3})_(.+)_(\d{6})_INL\.xlsx$", re.IGNORECASE)
# INL genereras av process_denmark/finland/germany.py. CENTR = Prosero-koncernbolag
# som processas av en av dessa pipelines (Oy via FI, GmbH via DE).
COUNTRIES = ("Denmark", "Finland", "Germany", "CENTR")
SOURCE_KIND = "INL"
PERIOD_TYPE = "monthly"  # INL = månadsvis saldobalans


def parse_inl_filename(name: str) -> tuple[int, str, str] | None:
    """`229_Zipp Systems_202602_INL.xlsx` → (229, 'Zipp Systems', '202602')."""
    m = INL_FILENAME_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


def read_inl_rows(path: Path) -> tuple[list[tuple], bool]:
    """Returnera (rows, has_statement_type).

    rows: list of (account_code, account_name, amount, statement_type, row_index)
    where statement_type is 'IS'/'BS'/None. row_index follows file order (1-based,
    skipping the empty header row).
    """
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    rows: list[tuple] = []
    has_st = False
    idx = 0
    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if r_idx == 0:
            continue  # header (tom rad)
        if not row or row[0] is None:
            continue
        acc = str(row[0]).strip() if row[0] is not None else ""
        if not acc:
            continue
        name = str(row[1]).strip() if len(row) > 1 and row[1] is not None else None
        amt = row[2] if len(row) > 2 else None
        if amt is None:
            continue
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            continue
        st = None
        if len(row) > 3 and row[3] is not None:
            s = str(row[3]).strip().upper()
            if s in ("IS", "BS"):
                st = s
                has_st = True
        idx += 1
        rows.append((acc, name, amt, st, idx))
    wb.close()
    return rows, has_st


def load_file(con, path: Path, base_path: Path, *, dry_run: bool) -> str:
    """Load one INL file into fact_balances. Returns status: ok|warn|skip|error."""
    parsed = parse_inl_filename(path.name)
    if parsed is None:
        log("SKIP", path.name, "Filnamn matchar inte INL-mönstret")
        return "skip"
    company_id, _friendly_from_name, period = parsed

    # Verifiera att bolaget finns i dim_company
    co = con.execute(
        "SELECT name, country, currency FROM dim_company WHERE company_id = ?",
        [company_id],
    ).fetchone()
    if co is None:
        log("ERROR", company_id, f"Saknas i dim_company: {path.name}")
        return "error"
    _name, country, currency = co
    if country not in COUNTRIES:
        log("SKIP", company_id, f"INL gäller endast FI/DK/DE — bolagets land är {country}")
        return "skip"

    try:
        rows, has_st = read_inl_rows(path)
    except Exception as e:
        log("ERROR", company_id, f"Läsfel {path.name}: {e}")
        return "error"

    if not rows:
        log("WARN", company_id, f"Inga rader i {path.name}")
        return "warn"

    total = sum(r[2] for r in rows)
    is_warn = abs(total) >= 1.0
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    if dry_run:
        log("INFO", company_id,
            f"[DRY] {path.name}  rows={len(rows)} sum={total:.4f} "
            f"statement_type={'JA' if has_st else 'nej'}")
        return "warn" if is_warn else "ok"

    db.sync_dim_period(con, [period])

    con.execute("BEGIN")
    try:
        con.execute(
            """DELETE FROM fact_balances
               WHERE company_id = ? AND period = ?
                 AND source_kind = ? AND source_file = ?""",
            [company_id, period, SOURCE_KIND, rel_src],
        )
        con.executemany(
            """INSERT INTO fact_balances
               (company_id, period, period_type, account_code, account_name,
                amount, currency, statement_type, source_kind, source_file,
                row_index, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(company_id, period, PERIOD_TYPE, r[0], r[1], r[2], currency,
              r[3], SOURCE_KIND, rel_src, r[4], now) for r in rows],
        )
        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [company_id, period, SOURCE_KIND, rel_src, len(rows), total, has_st,
             "warn" if is_warn else "ok",
             f"sum={total:.4f}" + ("" if has_st else " (utan IS/BS-flagga)"),
             now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"DB-fel {path.name}: {e}")
        return "error"

    flag = "" if has_st else "  (utan IS/BS-flagga — kör om process_*.py för full klassning)"
    status = "WARN" if is_warn else "OK"
    log(status, company_id, f"{path.name}  rader={len(rows)} sum={total:.4f}{flag}")
    return "warn" if is_warn else "ok"


def discover_files(source_dir: Path, period: str | None) -> list[Path]:
    """Hitta INL-filer i source_dir; filtrera på period om angiven."""
    files = sorted(source_dir.rglob("*_INL.xlsx"))
    if period:
        files = [f for f in files if f"_{period}_INL" in f.name]
    return files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda INL.xlsx-filer till DuckDB (fact_balances)."
    )
    parser.add_argument("--period", default=None,
                        help="Filtrera på YYYYMM (default: alla träffar)")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att söka i (default: extracted/{period}/*/output under base_path)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Läs och rapportera, skriv inte till DB")
    args = parser.parse_args()

    period_for_log = args.period or "all"
    begin_run("load_inl.py", period_for_log)
    log("START", "load_inl.py", f"period={period_for_log} dry_run={args.dry_run}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])

    if args.source_dir:
        source_dir = Path(args.source_dir)
    else:
        period = args.period or prev_month_period()
        source_dir = base_path / "extracted" / period
        # Sök i alla {Country}/output-undermappar
        log("INFO", "scan", f"Söker i {source_dir}/*/output/*_INL.xlsx")

    if not source_dir.exists():
        log("ERROR", "scan", f"Källmapp saknas: {source_dir}")
        return

    files = discover_files(source_dir, args.period)
    if not files:
        log("WARN", "scan", f"Inga INL-filer hittades i {source_dir}")
        log("DONE", "load_inl.py", "0 OK  0 WARN  0 SKIP  0 ERROR")
        return

    con = db.connect()
    try:
        db.init_schema(con)
        counts = {"ok": 0, "warn": 0, "skip": 0, "error": 0}
        for f in files:
            status = load_file(con, f, base_path, dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1
    finally:
        con.close()

    log("DONE", "load_inl.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  {counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()

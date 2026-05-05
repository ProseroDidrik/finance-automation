"""Ladda historisk MAN/IMP/IMP_ADJ-data till fact_balances.

Hanterar tre typer av källfiler (samma kolumnstruktur):
  - "2022 2025 MAN IMP IMP_adj A B.xlsx"  (Excel, med header-rad)
  - "2026 MAN IMP_Adj.xlsx"               (Excel, med header-rad)
  - "SE Backup 2022 to 2026 march.txt"    (semikolon-CSV, ingen header)
  - "NO DE FI DK Other Backup 2022 to 2026 March.txt"  (semikolon-CSV)

Kolumnstruktur (0-indexerat):
  0  (A) Konto      — "{bolag}_{konto}" eller "P_{konto}" (dim i Mercur)
  2  (C) Källa      — "MAN" | "IMP" | "IMP_ADJ"
  3  (D) Scenario   — "A" (Utfall) | "B" (Budget)
  5  (F) Månad      — YYYYMM
  9  (J) Bolag      — company_id (heltal)
  11 (L) Val        — valutakod
  12 (M) Värde      — belopp (komma som decimaltecken i txt-filer)

Konto-parsning:
  "{digits}_{rest}" → account_code = "{rest}"   (sifferprefixet är Resultatenhet)
  "P_{rest}"        → account_code = "P_{rest}" (manuellt justerings-konto)

Idempotens: senaste körning vinner per (company_id, period, source_kind, scenario).

Körning:
  py load_history_excel.py                  # alla filer
  py load_history_excel.py --dry-run
  py load_history_excel.py --file "2026 MAN IMP_Adj.xlsx"  # specifik fil
  py load_history_excel.py --scenario A     # filtrera scenario
  py load_history_excel.py --skip-backup    # hoppa över backup-txt
"""
from __future__ import annotations

import argparse
import csv
import io
from datetime import datetime
from pathlib import Path

import openpyxl

import db
from shared import begin_run, load_config, log

SOURCE_KIND_VALID = {"MAN", "IMP", "IMP_ADJ"}
PERIOD_TYPE = "monthly"

INSERT_SQL = """
    INSERT INTO fact_balances
        (company_id, period, period_type, account_code, account_name,
         amount, currency, statement_type, source_kind, source_file,
         row_index, scenario, loaded_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def parse_account_code(konto: str) -> str:
    """Returnerar account_code-delen från ett Mercur-konto-ID.

    "{digits}_{rest}" → "{rest}"  (sifferprefixet = Resultatenhet/dimension)
    "P_{rest}"        → "P_{rest}"
    annat             → konto (as-is)
    """
    parts = konto.split("_", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]
    return konto


def parse_amount(val) -> float | None:
    """Konverterar str/float/int till float. Hanterar komma-decimal."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def iter_excel_rows(path: Path):
    """Yield tuples (konto, kalla, scenario, period, bolag_str, currency, amount_raw)
    för varje datarad i en Excel-fil (hoppar över header-raden).
    """
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    first = True
    for row in ws.iter_rows(values_only=True):
        if first:
            first = False
            continue  # header-rad
        if not row or row[0] is None:
            continue
        yield (
            str(row[0]).strip() if row[0] else "",   # Konto
            str(row[2]).strip() if row[2] else "",   # Källa
            str(row[3]).strip() if row[3] else "",   # Scenario
            str(row[5]).strip() if row[5] else "",   # Månad
            str(row[9]).strip() if row[9] else "",   # Bolag
            str(row[11]).strip() if row[11] else "", # Val
            row[12],                                  # Värde (raw)
        )
    wb.close()


def iter_txt_rows(path: Path):
    """Yield samma tuple-format från semikolon-CSV utan header."""
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=";", quotechar='"')
    for row in reader:
        if len(row) < 13:
            continue
        yield (
            row[0].strip(),   # Konto
            row[2].strip(),   # Källa
            row[3].strip(),   # Scenario
            row[5].strip(),   # Månad
            row[9].strip(),   # Bolag
            row[11].strip(),  # Val
            row[12].strip(),  # Värde (sträng, komma-decimal)
        )


def load_file(con, path: Path, base_path: Path,
              *, dry_run: bool,
              filter_scenario: str | None,
              valid_companies: set[int]) -> dict[str, int]:
    """Ladda en fil. Returnerar {status: count}."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        rows_iter = iter_excel_rows(path)
    elif suffix == ".txt":
        rows_iter = iter_txt_rows(path)
    else:
        log("SKIP", path.name, f"Okänt filformat: {suffix}")
        return {"skip": 1}

    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    # Samla rader per (company_id, period, source_kind, scenario)
    # så att vi kan DELETE en gång per kombination.
    from collections import defaultdict
    lane_rows: dict[tuple, list[tuple]] = defaultdict(list)
    skipped = 0
    row_idx = 0

    for konto, kalla, scenario, period, bolag_str, currency, amount_raw in rows_iter:
        row_idx += 1

        # Filtrera scenario
        if filter_scenario and scenario != filter_scenario:
            continue

        # Validera källa
        kalla_upper = kalla.upper().replace("-", "_").replace(" ", "_")
        if kalla_upper not in SOURCE_KIND_VALID:
            skipped += 1
            continue

        # Validera period
        if not period or len(period) != 6 or not period.isdigit():
            skipped += 1
            continue

        # Validera bolag
        try:
            company_id = int(bolag_str)
        except ValueError:
            skipped += 1
            continue

        if company_id not in valid_companies:
            skipped += 1
            continue

        # Belopp
        amount = parse_amount(amount_raw)
        if amount is None:
            continue  # tomma belopp är OK att skippa (blank row i Mercur)

        # Konto → account_code
        account_code = parse_account_code(konto)
        if not account_code:
            skipped += 1
            continue

        currency = (currency or "").upper().strip()
        lane_key = (company_id, period, kalla_upper, scenario)
        lane_rows[lane_key].append((
            company_id, period, PERIOD_TYPE, account_code, None,
            amount, currency, None, kalla_upper, rel_src, row_idx, scenario, now,
        ))

    if not lane_rows:
        log("WARN", path.name, f"Inga giltiga rader (skippade={skipped})")
        return {"warn": 1}

    total_rows = sum(len(v) for v in lane_rows.values())
    log("INFO", path.name,
        f"{total_rows} rader  {len(lane_rows)} lanes  skippade={skipped}")

    if dry_run:
        for (cid, period, sk, scen), rows in sorted(lane_rows.items())[:5]:
            log("INFO", cid, f"[DRY] period={period} source={sk} scenario={scen} rader={len(rows)}")
        log("OK", path.name, f"[DRY] {total_rows} rader  {len(lane_rows)} lanes")
        return {"ok": 1}

    # Synka perioder
    all_periods = list({key[1] for key in lane_rows})
    db.sync_dim_period(con, all_periods)

    con.execute("BEGIN")
    try:
        for (company_id, period, source_kind, scenario), rows in lane_rows.items():
            con.execute(
                """DELETE FROM fact_balances
                   WHERE company_id = ? AND period = ?
                     AND source_kind = ? AND scenario = ?""",
                [company_id, period, source_kind, scenario],
            )
            con.executemany(INSERT_SQL, rows)
        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [None, "HIST", "MAN/IMP", rel_src, total_rows,
             sum(r[5] for rows in lane_rows.values() for r in rows),
             False, "ok", f"lanes={len(lane_rows)} skipped={skipped}", now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", path.name, f"DB-fel: {e}")
        return {"error": 1}

    log("OK", path.name, f"{total_rows} rader laddade  {len(lane_rows)} lanes")
    return {"ok": 1}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda historisk MAN/IMP/IMP_ADJ-data till DuckDB."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--file", default=None,
                        help="Ladda bara en specifik fil (filnamn under _history/)")
    parser.add_argument("--scenario", choices=["A", "B"], default=None,
                        help="Filtrera: ladda bara detta scenario")
    parser.add_argument("--skip-backup", action="store_true",
                        help="Hoppa över backup-txt-filerna")
    args = parser.parse_args()

    begin_run("load_history_excel.py", "HIST")
    log("START", "load_history_excel.py",
        f"dry_run={args.dry_run}  scenario={args.scenario or 'alla'}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])
    hist_root = base_path / "_history"

    if not hist_root.exists():
        log("ERROR", "load_history_excel.py",
            f"_history saknas: {hist_root}")
        return

    EXCEL_FILES = [
        "2022 2025 MAN IMP IMP_adj A B.xlsx",
        "2026 MAN IMP_Adj.xlsx",
    ]
    TXT_FILES = [
        "SE Backup 2022 to 2026 march.txt",
        "NO DE FI DK Other Backup 2022 to 2026 March.txt",
    ]

    if args.file:
        # Begränsa till en fil
        all_files = [hist_root / args.file]
    else:
        all_files = [hist_root / f for f in EXCEL_FILES]
        if not args.skip_backup:
            all_files += [hist_root / f for f in TXT_FILES]

    con = db.connect()
    try:
        db.init_schema(con)

        # Hämta giltiga company_ids
        valid_companies: set[int] = {
            row[0]
            for row in con.execute("SELECT company_id FROM dim_company").fetchall()
        }
        if not valid_companies:
            log("ERROR", "load_history_excel.py",
                "Inga bolag i dim_company — kör 'py db.py' först")
            return

        totals: dict[str, int] = {"ok": 0, "warn": 0, "skip": 0, "error": 0}

        for fpath in all_files:
            if not fpath.exists():
                log("WARN", fpath.name, f"Filen saknas: {fpath}")
                totals["skip"] += 1
                continue
            counts = load_file(
                con, fpath, base_path,
                dry_run=args.dry_run,
                filter_scenario=args.scenario,
                valid_companies=valid_companies,
            )
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v

    finally:
        con.close()

    log("DONE", "load_history_excel.py",
        f"{totals['ok']} OK  {totals['warn']} WARN  "
        f"{totals['skip']} SKIP  {totals['error']} ERROR")


if __name__ == "__main__":
    main()

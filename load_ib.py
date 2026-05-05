"""Ladda ingående balanser (IB) för dec 2021 → period 202112.

Källfiler under _history/ (Mercur-rapportexporter):
  FI IB 2022.xlsx  — Finland, EUR
  DK IB 2022.xlsx  — Danmark, DKK
  DE IB 2022.xlsx  — Tyskland, EUR
  NO IB 2022.xlsx  — Norge, NOK
  SE IB 2022.xlsx  — Sverige, SEK

Filformat (python-calamine för att undvika openpyxl fontId-bug):
  Rad 1–7: metadata-header
  Rad 7, kolumn B: "UB202112" (bekräftar period)
  Rad 8+: "  {company_id}_{account_code} {account_name}", belopp i kol B

source_kind = 'IB', period_type = 'monthly', period = '202112'

Konflikthantering (Alt A): rader för company_id hoppas över om det redan
finns data för (company_id, '202112') med source_kind IN ('SIE', 'SAFT', 'INL')
i fact_balances.

Körning:
  py load_ib.py
  py load_ib.py --dry-run
  py load_ib.py --file "FI IB 2022.xlsx"  # specifik fil
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import python_calamine

import db
from shared import begin_run, load_config, log

IB_PERIOD = "202112"
SOURCE_KIND = "IB"
PERIOD_TYPE = "monthly"

IB_FILES = [
    "FI IB 2022.xlsx",
    "DK IB 2022.xlsx",
    "DE IB 2022.xlsx",
    "NO IB 2022.xlsx",
    "SE IB 2022.xlsx",
]

# Matchning: "  12_1400 Kontonamn" eller "  242_831 Kontonamn"
RE_ACCOUNT_ROW = re.compile(r"^\s+(\d+)_(\S+)\s*(.*)?$")

INSERT_SQL = """
    INSERT INTO fact_balances
        (company_id, period, period_type, account_code, account_name,
         amount, currency, statement_type, source_kind, source_file,
         row_index, scenario, loaded_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def parse_ib_file(path: Path) -> tuple[str | None, str | None, list[tuple]]:
    """Läs en IB-fil med python-calamine.

    Returnerar (period, currency, rows) där rows = lista av
    (company_id, account_code, account_name, amount).
    Returnerar (None, None, []) vid fel.
    """
    try:
        wb = python_calamine.load_workbook(str(path))
    except Exception as e:
        log("ERROR", path.name, f"Kunde inte öppna filen: {e}")
        return None, None, []

    sheet_name = wb.sheet_names[0]
    ws = wb.get_sheet_by_name(sheet_name)
    all_rows = list(ws.to_python())

    # Header-analys: rad 5 = valuta ("Valuta: EUR Euro"), rad 7 = period ("", "UB202112", ...)
    currency = None
    period_str = None

    for i, row in enumerate(all_rows[:8]):
        if not row:
            continue
        cell0 = str(row[0]).strip() if row[0] else ""
        if cell0.startswith("Valuta:"):
            # "Valuta: EUR Euro" → "EUR"
            parts = cell0.split()
            if len(parts) >= 2:
                currency = parts[1].upper()
        elif not cell0 and len(row) >= 2 and row[1]:
            # Rad 7: kolumn B innehåller t.ex. "UB202112"
            col_b = str(row[1]).strip()
            m = re.search(r"(\d{6})", col_b)
            if m:
                period_str = m.group(1)

    if not currency or not period_str:
        log("ERROR", path.name,
            f"Kunde inte hitta valuta ({currency}) eller period ({period_str}) i header")
        return None, None, []

    if period_str != IB_PERIOD:
        log("WARN", path.name,
            f"Period i filen är {period_str}, förväntat {IB_PERIOD} — laddar ändå")

    # Data-rader
    rows_out: list[tuple] = []
    for row in all_rows[7:]:  # hoppa över header-rader 1–7
        if not row or row[0] is None:
            continue
        cell0 = str(row[0])
        m = RE_ACCOUNT_ROW.match(cell0)
        if not m:
            continue  # grupprads-header som "Alla balanskonto..."

        company_id = int(m.group(1))
        account_code = m.group(2).strip()
        account_name = m.group(3).strip() if m.group(3) else None

        # Belopp i kolumn B
        amount_raw = row[1] if len(row) > 1 else None
        if amount_raw is None or str(amount_raw).strip() == "":
            continue  # tomt belopp = 0-rad, skippa
        try:
            amount = float(str(amount_raw).replace(",", "."))
        except (ValueError, TypeError):
            continue

        rows_out.append((company_id, account_code, account_name, amount))

    return period_str, currency, rows_out


def load_file(con, path: Path, base_path: Path,
              existing_companies: set[int],
              *, dry_run: bool) -> dict[str, int]:
    """Ladda en IB-fil. Returnerar {status: count}."""
    period, currency, raw_rows = parse_ib_file(path)
    if not raw_rows:
        log("WARN", path.name, "Inga rader att ladda")
        return {"warn": 1}

    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    # Filtrera bort bolag som redan har SIE/SAFT/INL data för 202112
    company_ids_in_file = {r[0] for r in raw_rows}
    skip_companies = company_ids_in_file & existing_companies
    if skip_companies:
        log("INFO", path.name,
            f"Hoppar över {len(skip_companies)} bolag med befintlig SIE/SAFT/INL-data "
            f"för {IB_PERIOD}: {sorted(skip_companies)[:10]}")

    filtered = [r for r in raw_rows if r[0] not in skip_companies]
    if not filtered:
        log("SKIP", path.name,
            f"Alla {len(raw_rows)} rader skippade (SIE/SAFT/INL finns redan)")
        return {"skip": 1}

    # Gruppera per company_id
    from collections import defaultdict
    by_company: dict[int, list] = defaultdict(list)
    for cid, acc, name, amt in filtered:
        by_company[cid].append((cid, acc, name, amt))

    total = len(filtered)
    log("INFO", path.name,
        f"{total} rader  {len(by_company)} bolag  "
        f"skippade={len(skip_companies)}  valuta={currency}  period={period}")

    if dry_run:
        log("OK", path.name, f"[DRY] {total} rader")
        return {"ok": 1}

    db.sync_dim_period(con, [IB_PERIOD])

    con.execute("BEGIN")
    try:
        for company_id, rows in by_company.items():
            con.execute(
                "DELETE FROM fact_balances WHERE company_id = ? AND period = ? AND source_kind = ?",
                [company_id, IB_PERIOD, SOURCE_KIND],
            )
            idx = 0
            for cid, acc, name, amt in rows:
                idx += 1
                # statement_type: BS-konton (prefix 1/2), IS annars
                first_digit = acc[0] if acc and acc[0].isdigit() else ""
                st = "BS" if first_digit in ("1", "2") else ("IS" if first_digit else None)
                con.executemany(INSERT_SQL, [(
                    cid, IB_PERIOD, PERIOD_TYPE, acc, name,
                    amt, currency, st, SOURCE_KIND, rel_src, idx, "A", now,
                )])

        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [None, IB_PERIOD, SOURCE_KIND, rel_src, total,
             sum(r[3] for r in filtered), True, "ok",
             f"bolag={len(by_company)} skippade={len(skip_companies)}", now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", path.name, f"DB-fel: {e}")
        return {"error": 1}

    log("OK", path.name, f"{total} rader laddade  {len(by_company)} bolag")
    return {"ok": 1}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Ladda ingående balanser (period {IB_PERIOD}) till DuckDB."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--file", default=None,
                        help="Ladda bara en specifik fil (filnamn under _history/)")
    args = parser.parse_args()

    begin_run("load_ib.py", IB_PERIOD)
    log("START", "load_ib.py",
        f"period={IB_PERIOD}  dry_run={args.dry_run}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])
    hist_root = base_path / "_history"

    if not hist_root.exists():
        log("ERROR", "load_ib.py", f"_history saknas: {hist_root}")
        return

    files = [hist_root / (args.file or f) for f in (
        [args.file] if args.file else IB_FILES
    )]

    con = db.connect()
    try:
        db.init_schema(con)

        # Hitta bolag som redan har data för 202112 (SIE/SAFT/INL) → skip
        existing_companies: set[int] = {
            row[0]
            for row in con.execute(
                """SELECT DISTINCT company_id FROM fact_balances
                   WHERE period = ? AND source_kind IN ('SIE', 'SAFT', 'INL')""",
                [IB_PERIOD],
            ).fetchall()
        }
        log("INFO", "load_ib.py",
            f"{len(existing_companies)} bolag har redan SIE/SAFT/INL för {IB_PERIOD}")

        totals: dict[str, int] = {"ok": 0, "warn": 0, "skip": 0, "error": 0}

        for fpath in files:
            if not fpath.exists():
                log("WARN", fpath.name if args.file else fpath.name,
                    f"Filen saknas: {fpath}")
                totals["skip"] += 1
                continue
            counts = load_file(con, fpath, base_path, existing_companies,
                               dry_run=args.dry_run)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v

    finally:
        con.close()

    log("DONE", "load_ib.py",
        f"{totals['ok']} OK  {totals['warn']} WARN  "
        f"{totals['skip']} SKIP  {totals['error']} ERROR")


if __name__ == "__main__":
    main()

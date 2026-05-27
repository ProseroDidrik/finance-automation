"""Engångsskript: bygg om 145s 202604-INL.xlsx från Postgres med IS/BS-flagga.

Bakgrund: process_finland.read_income_only_xlsx läser col B (april-månad) ur 145s
IS-fil för 202604, men för YTD-balans mot BS-Closing-balance måste vi använda col C
(YTD jan-april). Existerande fact_balances-rader (37 st, sum=0) är dock korrekt;
bara `statement_type`-kolumnen är NULL. Detta skript:

  1. Läser de 37 raderna ur Postgres
  2. Klassificerar IS/BS via första-siffra-regeln (1-2 → BS, 3+ → IS)
  3. Skriver en korrekt INL.xlsx till output/

Sedan: kör `py load_inl.py --period 202604 --override 145` för att ladda om
med statement_type satt.

Inte återanvändbart för andra bolag/månader — fixar specifikt 202604/145-gapet.
"""
from __future__ import annotations

import os
from pathlib import Path

import openpyxl
import psycopg

OUT = Path(
    r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation"
    r"\April alla filer\Get testfiles\extracted\202604\Finland\output"
    r"\145_Prosero Security Oy_202604_INL.xlsx"
)


def st_for(code: str) -> str | None:
    c = str(code).strip()
    if not c or not c[0].isdigit():
        return None
    return "BS" if c[0] in ("1", "2") else "IS"


def main() -> None:
    url = os.environ["DATABASE_URL_ETL"]
    with psycopg.connect(url) as con:
        rows = con.execute(
            """SELECT account_code, account_name, amount
               FROM fact_balances
               WHERE company_id = 145 AND period = '202604'
                 AND source_kind = 'IMP' AND scenario = 'A'
               ORDER BY row_index""",
        ).fetchall()

    is_rows = [r for r in rows if st_for(r[0]) == "IS"]
    bs_rows = [r for r in rows if st_for(r[0]) == "BS"]
    other = [r for r in rows if st_for(r[0]) not in ("IS", "BS")]

    print(f"Postgres -> IS={len(is_rows)} BS={len(bs_rows)} other={len(other)} (total {len(rows)})")
    print(f"  sum_is = {sum(float(r[2]) for r in is_rows):.4f}")
    print(f"  sum_bs = {sum(float(r[2]) for r in bs_rows):.4f}")
    print(f"  total  = {sum(float(r[2]) for r in rows):.4f}")
    if other:
        print(f"  WARN unclassified: {other}")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None, None, None, None])  # rad 1 är tom enligt INL-konvention
    for code, name, amt in is_rows:
        ws.append([code, name, float(amt), "IS"])
    for code, name, amt in bs_rows:
        ws.append([code, name, float(amt), "BS"])
    wb.save(str(OUT))
    print(f"\nSkrev: {OUT.name} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

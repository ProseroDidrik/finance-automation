"""verify_inl.py — sanity-test för INL-banan i report_pnl (DK/FI/DE).

Kör report_pnl för ett DK-bolag och jämför summan av amount_month
mot summan i INL.xlsx-källfilen (kolumn C ska summera till ~0 per file-design).

Kör:  py webapp/scripts/verify_inl.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
SQL_PATH = REPO / "webapp" / "backend" / "sql" / "report_pnl.sql"
DB_PATH = REPO / "data" / "finance.duckdb"


def run_for(con, company_id: int, period: str, prev_period: str):
    sql = SQL_PATH.read_text(encoding="utf-8")
    year_start = period[:4] + "01"
    df = con.execute(sql, [company_id, year_start, period, prev_period, period]).df()
    return df


def main() -> int:
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # --- DK 229 Zipp Systems, period 202603 (INL = monthly direkt) ---
    company_id = 229
    period = "202603"
    prev = "202602"

    info = con.execute(
        "SELECT company_id, name, country, currency FROM dim_company WHERE company_id = ?",
        [company_id],
    ).fetchone()
    print(f"Bolag: {info[0]} {info[1]} ({info[2]}, {info[3]})")

    # Hämta råa INL-balanser för IS (P&L) — det vi förväntar oss att se i report_pnl
    raw_is = con.execute(
        """SELECT account_code, amount, statement_type
           FROM fact_balances
           WHERE company_id = ? AND period = ? AND source_kind = 'INL'
             AND statement_type = 'IS'
           ORDER BY account_code""",
        [company_id, period],
    ).df()
    raw_sum = raw_is["amount"].sum()
    print(f"\nRå INL data, period {period}, IS-rader:")
    print(f"  {len(raw_is)} rader, summa = {raw_sum:,.2f}")

    # Kör report_pnl
    df = run_for(con, company_id, period, prev)
    print(f"\nreport_pnl: {len(df)} rader totalt")

    leaves = df[df["is_aggregated"] == False]
    print(f"  Bolagskonto-leaves under P&L: {len(leaves)}")

    leaf_sum_month = leaves["amount_month"].sum()
    leaf_sum_ytd = leaves["amount_ytd"].sum()
    print(f"  Summa amount_month (leaves): {leaf_sum_month:,.2f}")
    print(f"  Summa amount_ytd (leaves):   {leaf_sum_ytd:,.2f}")

    # P&L-rotnoden i pnl_tree heter inte 'P&L' utan storgrupperna under den.
    # Summan av alla storgrupper (depth=1) ska vara samma som leaf-sum.
    storgrupp = df[(df["is_aggregated"] == True) & (df["depth"] == 1)]
    storgrupp_sum_month = storgrupp["amount_month"].sum()
    storgrupp_sum_ytd = storgrupp["amount_ytd"].sum()
    print(f"\n  Storgrupp-summa month: {storgrupp_sum_month:,.2f}")
    print(f"  Storgrupp-summa YTD:   {storgrupp_sum_ytd:,.2f}")

    # Sanity-check: leaf-sum == storgrupp-sum (rollup ska bevara totalen)
    print()
    if abs(leaf_sum_month - storgrupp_sum_month) < 0.01:
        print("  OK: leaf-sum == storgrupp-sum (month) — rollup bevarar total")
    else:
        print(f"  FEL: leaf-sum != storgrupp-sum (month). Diff: "
              f"{leaf_sum_month - storgrupp_sum_month:,.2f}")
        return 1
    if abs(leaf_sum_ytd - storgrupp_sum_ytd) < 0.01:
        print("  OK: leaf-sum == storgrupp-sum (YTD)   — rollup bevarar total")
    else:
        print(f"  FEL: leaf-sum != storgrupp-sum (YTD). Diff: "
              f"{leaf_sum_ytd - storgrupp_sum_ytd:,.2f}")
        return 1

    # För INL: amount_month ska vara EXAKT lika med fact_balances.amount för IS-rader,
    # eftersom INL är monthly från start (period_type='monthly').
    print()
    print("Sanity: amount_month per leaf jämförs mot råa fact_balances")
    raw_by_acc = dict(zip(raw_is["account_code"], raw_is["amount"]))
    diffs = 0
    matched = 0
    for _, row in leaves.iterrows():
        acc = row["account_code"]
        if acc in raw_by_acc:
            raw_v = raw_by_acc[acc]
            our_v = row["amount_month"]
            if abs((our_v or 0) - raw_v) > 0.01:
                print(f"  MISMATCH konto {acc}: våra {our_v:,.2f}, råa {raw_v:,.2f}")
                diffs += 1
            else:
                matched += 1

    print(f"  Matched: {matched}, Mismatched: {diffs}")
    if diffs == 0 and matched > 0:
        print("\nOK: INL-banan fungerar korrekt.")
        return 0
    print("\nFEL: avvikelser i INL-banan.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

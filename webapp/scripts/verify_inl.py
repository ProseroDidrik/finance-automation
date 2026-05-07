"""verify_inl.py — sanity-test för INL-banan i report_pnl (DK/FI/DE).

Kör report_pnl för ett DK-bolag och jämför summan av amount_month
mot summan i INL.xlsx-källfilen (kolumn C ska summera till ~0 per file-design).

Kör:  py webapp/scripts/verify_inl.py
Förutsätter att DATABASE_URL är satt och Postgres-warehouset är ifyllt.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import db  # noqa: E402

SQL_PATH = REPO / "webapp" / "backend" / "sql" / "report_pnl.sql"


def run_for(con: db.Conn, company_id: int, period: str, prev_period: str) -> list[dict]:
    sql = SQL_PATH.read_text(encoding="utf-8")
    year_start = period[:4] + "01"
    params = [
        None, company_id, year_start, period,         # best_source (4)
        company_id, year_start, period, "A",          # raw_balances (4)
        prev_period, period,                           # balances (2)
    ]
    return con.fetch_dicts(sql, params)


def main() -> int:
    # --- DK 229 Zipp Systems, period 202603 (INL = monthly direkt) ---
    company_id = 229
    period = "202603"
    prev = "202602"

    with db.connect(read_only=True) as con:
        info = con.execute(
            "SELECT company_id, name, country, currency FROM dim_company WHERE company_id = %s",
            [company_id],
        ).fetchone()
        if info is None:
            print(f"FEL: bolag {company_id} hittades inte i dim_company")
            return 1
        print(f"Bolag: {info[0]} {info[1]} ({info[2]}, {info[3]})")

        # Hämta råa INL-balanser för IS (P&L) — det vi förväntar oss att se i report_pnl
        raw_is = con.fetch_dicts(
            """SELECT account_code, amount, statement_type
               FROM fact_balances
               WHERE company_id = %s AND period = %s AND source_kind = 'IMP'
                 AND statement_type = 'IS'
               ORDER BY account_code""",
            [company_id, period],
        )
        rows = run_for(con, company_id, period, prev)

    raw_sum = sum((r.get("amount") or 0.0) for r in raw_is)
    print(f"\nRå INL data, period {period}, IS-rader:")
    print(f"  {len(raw_is)} rader, summa = {raw_sum:,.2f}")

    print(f"\nreport_pnl: {len(rows)} rader totalt")

    leaves = [r for r in rows if not r.get("is_aggregated")]
    print(f"  Bolagskonto-leaves under P&L: {len(leaves)}")

    leaf_sum_month = sum((r.get("amount_month") or 0.0) for r in leaves)
    leaf_sum_ytd = sum((r.get("amount_ytd") or 0.0) for r in leaves)
    print(f"  Summa amount_month (leaves): {leaf_sum_month:,.2f}")
    print(f"  Summa amount_ytd (leaves):   {leaf_sum_ytd:,.2f}")

    # P&L-rotnoden i pnl_tree heter inte 'P&L' utan storgrupperna under den.
    # Summan av alla storgrupper (depth=1) ska vara samma som leaf-sum.
    storgrupp = [r for r in rows if r.get("is_aggregated") and r.get("depth") == 1]
    storgrupp_sum_month = sum((r.get("amount_month") or 0.0) for r in storgrupp)
    storgrupp_sum_ytd = sum((r.get("amount_ytd") or 0.0) for r in storgrupp)
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
    raw_by_acc = {r["account_code"]: r["amount"] for r in raw_is}
    diffs = 0
    matched = 0
    for row in leaves:
        acc = row.get("account_code")
        if acc in raw_by_acc:
            raw_v = raw_by_acc[acc]
            our_v = row.get("amount_month")
            if abs((our_v or 0) - (raw_v or 0)) > 0.01:
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

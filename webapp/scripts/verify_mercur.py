"""verify_mercur.py — kör report_pnl för bolag 76 / period 202603 och jämför mot Mercur-exemplet.
Verifierar både råa P&L-noder OCH KPI-formler från pnl_kpis.yaml.

Kör:  py webapp/scripts/verify_mercur.py
Förutsätter att DATABASE_URL är satt och att Postgres-warehouset är ifyllt
(via scripts/migrate_duckdb_to_postgres.py eller vanlig load_*-pipeline).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import db  # noqa: E402
from webapp.backend.kpi import compute_kpis  # noqa: E402

SQL_PATH = REPO / "webapp" / "backend" / "sql" / "report_pnl.sql"

COMPANY_ID = 76
PERIOD = "202603"
PREV_PERIOD = "202602"

# === Förväntade Mercur-värden (post-flip, dvs som de visas i Mercur) ===

NODE_EXPECTED = {
    # account_id: (month, ytd) — alla i SEK
    "Net Sales":                   (6_061_618.76,  24_142_109.80),
    "Other operational sales":     (   139_471.00,     357_619.00),
    "Total Sales":                 (6_201_089.76,  24_499_728.80),
    "Materialkost":                (-2_481_639.34, -11_893_336.88),
    "Subcontractor total":         (   -69_977.95,    -185_757.85),
    "Total Direct Cost":           (-2_551_617.29, -12_079_094.73),
    "Premises":                    (  -247_172.30,    -883_303.57),
    "Transportation":              (  -202_621.62,    -678_075.16),
    "Consultants":                 (  -159_041.38,    -413_250.46),
    "Other External Costs":        (  -305_715.20,  -1_030_303.63),
    "Personnel":                   (-2_116_825.88,  -6_417_887.66),
    "Operating Expenses":          (-3_031_376.38,  -9_422_820.48),
    "Depreciation TANG":           (   -34_606.04,    -103_818.17),
    "Depreciation INTANG":         (   -34_597.58,    -103_792.75),
    "Financial income and expense":(   -53_452.68,     -68_392.62),
}

KPI_EXPECTED = {
    "gross_profit":  ( 3_649_472.47,  12_420_634.07),
    "ebitda_adj":    (   618_096.09,   2_997_813.59),
    "ebita_adj":     (   583_490.05,   2_893_995.42),
    "ebit":          (   548_892.47,   2_790_202.67),
    "profit_period": (   495_439.79,   2_721_810.05),
    "local_profit":  (   495_439.79,   2_721_810.05),
}


def run_report(con: db.Conn, company_id: int, period: str, prev_period: str) -> list[dict]:
    """Samma parameter-ordning som webapp/backend/main.py:_params(src=None, scenario='A')."""
    sql = SQL_PATH.read_text(encoding="utf-8")
    year_start = period[:4] + "01"
    params = [
        None, company_id, year_start, period,         # best_source (4)
        company_id, year_start, period, "A",          # raw_balances (4)
        prev_period, period,                           # balances (2)
    ]
    return con.fetch_dicts(sql, params)


def cmp_block(title: str, expected: dict, lookup):
    print(f"\n=== {title} ===")
    print(f"{'Key':<32} {'Var man':>14} {'Mercur man':>14} {'Diff':>10}    "
          f"{'Var YTD':>14} {'Mercur YTD':>14} {'Diff':>10}")
    print("-" * 122)
    matches = mismatches = 0
    for key, (exp_m, exp_y) in expected.items():
        our_m, our_y = lookup(key)

        def fmt(v):
            return f"{v:>14,.2f}" if v is not None else f"{'—':>14}"

        def diff(ours, exp):
            if ours is None or exp is None:
                return f"{'—':>10}"
            d = ours - exp
            return f"{d:>10,.2f}"

        print(f"{key:<32} {fmt(our_m)} {fmt(exp_m)} {diff(our_m, exp_m)}    "
              f"{fmt(our_y)} {fmt(exp_y)} {diff(our_y, exp_y)}")
        for ours, exp in [(our_m, exp_m), (our_y, exp_y)]:
            if ours is None and exp is None:
                matches += 1
            elif ours is None or exp is None:
                mismatches += 1
            elif abs(ours - exp) < 0.5:
                matches += 1
            else:
                mismatches += 1
    print(f"\n  Match: {matches}  Mismatch: {mismatches}")
    return matches, mismatches


def main() -> int:
    with db.connect(read_only=True) as con:
        rows = run_report(con, COMPANY_ID, PERIOD, PREV_PERIOD)
    print(f"report_pnl(company={COMPANY_ID}, period={PERIOD}): {len(rows)} rader")

    # Slå upp aggregerade noder (post-flip för jämförelse)
    by_acc = {r["account_id"]: r for r in rows if r.get("is_aggregated")}

    def node_lookup(acc):
        r = by_acc.get(acc)
        if r is None:
            return (None, None)
        m = r.get("amount_month")
        y = r.get("amount_ytd")
        return (None if m is None else -m, None if y is None else -y)

    n_match, n_mis = cmp_block("Råa P&L-noder (post-flip)", NODE_EXPECTED, node_lookup)

    # KPI:er via formel-evaluator
    kpis = compute_kpis(rows)

    def kpi_lookup(kid):
        k = kpis.get(kid, {})
        return (k.get("amount_month"), k.get("amount_ytd"))

    k_match, k_mis = cmp_block("KPI-formler (Bruttovinst, EBITDA, EBIT, …)",
                                KPI_EXPECTED, kpi_lookup)

    print()
    print(f"TOTAL: {n_match + k_match} match  /  {n_mis + k_mis} mismatch")

    if (n_mis + k_mis) > 0:
        print("\nVarning: någon datapunkt matchar inte. Granska ovan.")
        return 1
    print("\nOK: Alla varden matchar Mercur-exemplet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

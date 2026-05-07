"""Diagnostik för report_pnl row-explosion (Postgres)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import db  # noqa: E402

# Hårdkodade testkonstanter — interpoleras direkt i SQL eftersom CREATE TEMP VIEW
# inte stöder bind-parametrar i Postgres (view-kropparna lagras som literal SQL).
COMPANY = 76
PERIOD = "202603"
PREV = "202602"


def main() -> int:
    with db.connect(read_only=True) as con:
        con.execute(
            """
            CREATE TEMP VIEW pnl_tree AS
            WITH RECURSIVE walk(account_id, parent_id, depth, sort_path) AS (
              SELECT account_id, parent_id, 0, account_id
              FROM dim_account_map WHERE account_id = 'P&L'
              UNION ALL
              SELECT m.account_id, m.parent_id, t.depth + 1,
                     t.sort_path || '/' || m.account_id
              FROM dim_account_map m JOIN walk t ON m.parent_id = t.account_id
            ) SELECT * FROM walk
            """
        )
        n_tree = con.execute("SELECT COUNT(*) FROM pnl_tree").fetchone()[0]
        n_dist = con.execute("SELECT COUNT(DISTINCT account_id) FROM pnl_tree").fetchone()[0]
        print(f"pnl_tree rows:         {n_tree}")
        print(f"  distinct account_id: {n_dist}")
        for d, n in con.execute(
            "SELECT depth, COUNT(*) AS n FROM pnl_tree GROUP BY depth ORDER BY depth"
        ).fetchall():
            print(f"  depth={d:>2} n={n}")

        con.execute(f"""
            CREATE TEMP VIEW raw_balances AS
            SELECT fb.company_id, fb.period, fb.account_code, fb.amount, fb.period_type
            FROM fact_balances fb JOIN dim_company c ON c.company_id = fb.company_id
            WHERE fb.company_id = {COMPANY}
              AND fb.period IN ('{PERIOD}', '{PREV}')
              AND fb.source_kind = CASE c.country
                   WHEN 'Sweden' THEN 'SIE' WHEN 'Norway' THEN 'SAFT' ELSE 'IMP' END
        """)
        print()
        print("raw_balances rows:", con.execute("SELECT COUNT(*) FROM raw_balances").fetchone()[0])
        for period, n in con.execute(
            "SELECT period, COUNT(*) AS n FROM raw_balances GROUP BY period ORDER BY period"
        ).fetchall():
            print(f"  period={period} n={n}")

        # Hur ofta finns dubbletter (company, account_code) inom samma period?
        print()
        print("Duplicates in raw_balances per (period, account_code):")
        dups = con.execute(
            """
            SELECT period, account_code, COUNT(*) AS n
            FROM raw_balances GROUP BY period, account_code HAVING COUNT(*) > 1
            LIMIT 20
            """
        ).fetchall()
        if not dups:
            print("  (inga)")
        for period, acc, n in dups:
            print(f"  {period}  {acc}  n={n}")

        con.execute(f"""
            CREATE TEMP VIEW leaf_amounts AS
            SELECT m.account_id AS leaf_node_id, m.parent_id AS group_node_id,
                   cur.amount AS amount_ytd,
                   cur.amount - COALESCE(prev.amount, 0) AS amount_month
            FROM raw_balances cur
            JOIN dim_account_map m
              ON m.company_id = cur.company_id AND m.account_code = cur.account_code
            JOIN pnl_tree t ON t.account_id = m.parent_id
            LEFT JOIN raw_balances prev
              ON prev.company_id=cur.company_id AND prev.account_code=cur.account_code
             AND prev.period='{PREV}'
            WHERE cur.period='{PERIOD}'
        """)
        print()
        print("leaf_amounts rows:        ",
              con.execute("SELECT COUNT(*) FROM leaf_amounts").fetchone()[0])
        print("  distinct leaf_node_id:  ",
              con.execute("SELECT COUNT(DISTINCT leaf_node_id) FROM leaf_amounts").fetchone()[0])
        print()
        print("Top duplicates in leaf_amounts:")
        for leaf, n in con.execute(
            """SELECT leaf_node_id, COUNT(*) AS n FROM leaf_amounts
               GROUP BY leaf_node_id HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 5"""
        ).fetchall():
            print(f"  {leaf}  n={n}")

        # Hur kommer dubbletter till? Kolla dim_account_map
        print()
        n_dam = con.execute(
            "SELECT COUNT(*) FROM dim_account_map WHERE company_id = %s", [COMPANY],
        ).fetchone()[0]
        print(f"dim_account_map rows for company {COMPANY}: {n_dam}")
        print("  distinct account_code:",
              con.execute(
                  "SELECT COUNT(DISTINCT account_code) FROM dim_account_map WHERE company_id = %s",
                  [COMPANY],
              ).fetchone()[0])
        print("  distinct (account_code) with multiple rows:")
        for acc, n in con.execute(
            """SELECT account_code, COUNT(*) AS n
               FROM dim_account_map WHERE company_id = %s
               GROUP BY account_code HAVING COUNT(*) > 1 LIMIT 10""",
            [COMPANY],
        ).fetchall():
            print(f"  {acc}  n={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

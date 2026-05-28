"""Verifiera: har SAFT-posterna genuina bokföringsdatum, och sätter ETL
period från transaction_date eller filheadern?

Tittar på bolag 16/6010 (avskrivning, jan-koncentrerad i bucket-analysen) +
36/5095 (löner, ojämn) och jämför period-kolumn mot transaction_date.
"""
import os
import psycopg
from psycopg.rows import dict_row

URL = os.environ["DATABASE_URL_ETL"]

SQL = """
SELECT period,
       transaction_date,
       account_code,
       amount,
       transaction_description,
       source_file
FROM fact_journal_saft
WHERE company_id = %s
  AND account_code = %s
  AND period IN ('202601','202602','202603','202604')
ORDER BY transaction_date, line_no
"""

for bolag, konto in [(16, '6010'), (16, '6010'), (158, '6010'), (36, '5095')]:
    with psycopg.connect(URL, row_factory=dict_row) as c:
        rows = c.execute(SQL, (bolag, konto)).fetchall()
    print(f"\n=== bolag {bolag} konto {konto} — {len(rows)} rader ===")
    print(f"{'period':>7} {'txn_date':>12} {'amount':>14}  desc")
    for r in rows[:20]:
        td = str(r['transaction_date'])
        desc = (r['transaction_description'] or '')[:40]
        print(f"{r['period']:>7} {td:>12} {float(r['amount']):>14,.2f}  {desc}")
    if len(rows) > 20:
        print(f"  ... {len(rows)-20} fler rader")
    if rows:
        print(f"  source_file: {rows[0]['source_file']}")

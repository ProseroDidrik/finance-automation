"""Bekräfta att 104-laddningen BARA la till 2022 (2023-2026 oförändrade).
Pre-load (investigate_104, före laddning): journal total = 58846 rader / 40
perioder över 2023-2026 (inget 2022). Post-load: 2022=16600 nytt, 2023-2026
ska vara EXAKT 58846 fortfarande.
"""
import os, psycopg
c = psycopg.connect(os.environ["DATABASE_URL_RO"], connect_timeout=30)
cur = c.cursor(); cur.execute("SET statement_timeout='60s'")
cur.execute("""
  SELECT left(period,4) yr, count(*) journal
  FROM fact_journal_saft WHERE company_id=104 GROUP BY 1 ORDER BY 1""")
print("104 journal per år:")
total_post2022 = 0
for yr, n in cur.fetchall():
    print(f"  {yr}: {n}")
    if yr != "2022":
        total_post2022 += n
print(f"2023-2026 summa = {total_post2022}  (pre-load total var 58846 → {'OFÖRÄNDRAT' if total_post2022==58846 else 'ÄNDRAT!'})")

cur.execute("""
  SELECT period, count(*) FROM fact_balances
  WHERE company_id=104 AND source_kind='SAFT' GROUP BY 1 ORDER BY 1""")
print("\n104 SAFT-balans per period (202212 ska vara nytt, resten oförändrat):")
for p, n in cur.fetchall():
    print(f"  {p}: {n}")

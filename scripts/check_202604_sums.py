import os, psycopg, sys
sys.stdout.reconfigure(encoding="utf-8")
con = psycopg.connect(os.environ["DATABASE_URL"])
cur = con.cursor()

print("=== IMP (FI/DK/DE Excel-INL): ska summera till 0 ===")
cur.execute("""
  SELECT dc.country, dc.company_id, dc.name,
         COUNT(*) AS rader,
         ROUND(SUM(fb.amount)::numeric, 2) AS summa
  FROM fact_balances fb
  JOIN dim_company dc USING (company_id)
  WHERE period='202604' AND source_kind='IMP'
  GROUP BY 1,2,3
  HAVING ABS(SUM(fb.amount)) > 0.5
  ORDER BY ABS(SUM(fb.amount)) DESC
""")
for r in cur.fetchall(): print(" ", r)

print()
print("=== SIE (SE): per-bolag sum (by design ofta != 0; >1M = möjligt fel) ===")
cur.execute("""
  SELECT dc.country, dc.company_id, dc.name,
         COUNT(*) AS rader,
         ROUND(SUM(fb.amount)::numeric, 2) AS summa
  FROM fact_balances fb
  JOIN dim_company dc USING (company_id)
  WHERE period='202604' AND source_kind='SIE'
  GROUP BY 1,2,3
  HAVING ABS(SUM(fb.amount)) > 1000000
  ORDER BY ABS(SUM(fb.amount)) DESC
""")
for r in cur.fetchall(): print(" ", r)

print()
print("=== SAFT (NO/DK): per-bolag sum (by design ofta != 0; >1M = möjligt fel) ===")
cur.execute("""
  SELECT dc.country, dc.company_id, dc.name,
         COUNT(*) AS rader,
         ROUND(SUM(fb.amount)::numeric, 2) AS summa
  FROM fact_balances fb
  JOIN dim_company dc USING (company_id)
  WHERE period='202604' AND source_kind='SAFT'
  GROUP BY 1,2,3
  HAVING ABS(SUM(fb.amount)) > 1000000
  ORDER BY ABS(SUM(fb.amount)) DESC
""")
for r in cur.fetchall(): print(" ", r)

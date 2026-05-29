import os, psycopg
c = psycopg.connect(os.environ["DATABASE_URL_RO"], connect_timeout=30)
cur = c.cursor(); cur.execute("SET statement_timeout='60s'")
cur.execute("""
  SELECT
    (SELECT count(*) FROM fact_journal_saft  WHERE company_id=104 AND period LIKE '2022%') journal_2022,
    (SELECT count(*) FROM fact_saft_analysis WHERE company_id=104 AND period LIKE '2022%') analys_2022,
    (SELECT count(*) FROM fact_balances WHERE company_id=104 AND period='202212' AND source_kind='SAFT') balans_202212
""")
print("104 2022 — journal / analys / balans(202212):", cur.fetchone())
# GRIND 1 igen, bara 104:
cur.execute("""
  WITH a AS (SELECT DISTINCT period FROM fact_saft_analysis WHERE company_id=104),
       j AS (SELECT DISTINCT period FROM fact_journal_saft WHERE company_id=104)
  SELECT a.period FROM a LEFT JOIN j USING(period) WHERE j.period IS NULL ORDER BY 1
""")
orphans = [r[0] for r in cur.fetchall()]
print("104 analysperioder utan journal (ska vara []):", orphans)

"""Kvantifiera clobber: (bolag, period) dar analys << journal (analys saknas
trots att journal finns). Signatur for per-period-DELETE-overskrivning vid
korsfilsleakande ValueDate. Heuristik: journal>=200 rader men analys<journal*0.2.
"""
import os, psycopg
DSN = os.environ["DATABASE_URL_RO"]
SQL = """
WITH j AS (
  SELECT company_id, period, count(*) jn FROM fact_journal_saft GROUP BY 1,2
), a AS (
  SELECT company_id, period, count(*) an FROM fact_saft_analysis GROUP BY 1,2
)
SELECT j.company_id, j.period, j.jn AS journal, COALESCE(a.an,0) AS analysis
FROM j LEFT JOIN a USING (company_id, period)
WHERE j.jn >= 200 AND COALESCE(a.an,0) < j.jn * 0.2
ORDER BY j.company_id, j.period
LIMIT 200;
"""
with psycopg.connect(DSN, connect_timeout=30) as con:
    with con.cursor() as cur:
        cur.execute("SET statement_timeout='150s'")
        cur.execute(SQL)
        rows = cur.fetchall()
        print("company_id | period | journal | analysis  (analys << journal = misstänkt clobber)")
        from collections import Counter
        byco = Counter()
        for r in rows:
            print(" | ".join(str(x) for x in r)); byco[r[0]] += 1
        print(f"\nTotalt misstänkta (bolag,period): {len(rows)}")
        print("Per bolag:", dict(byco))

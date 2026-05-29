import os, psycopg
DSN = os.environ["DATABASE_URL_RO"]
# Full periodbild for 16 och 200: clobber (erratiskt hog/lag) vs export-tappad
# Analysis (konstant lag/noll fran nagon punkt). Visa bade analys och journal.
for co in (16, 200):
    print(f"\n========== BOLAG {co} ==========")
    with psycopg.connect(DSN, connect_timeout=30) as con:
        with con.cursor() as cur:
            cur.execute("SET statement_timeout='120s'")
            cur.execute("""
              WITH j AS (SELECT period, count(*) jn, min(source_file) jf FROM fact_journal_saft WHERE company_id=%s GROUP BY 1),
                   a AS (SELECT period, count(*) an, min(source_file) af FROM fact_saft_analysis WHERE company_id=%s GROUP BY 1)
              SELECT COALESCE(j.period,a.period) period, COALESCE(jn,0) journal, COALESCE(an,0) analysis,
                     COALESCE(af,jf) src
              FROM j FULL JOIN a ON j.period=a.period
              ORDER BY 1""", (co, co))
            for r in cur.fetchall():
                print(f"{r[0]} | J={r[1]:>6} | A={r[2]:>6} | {r[3]}")

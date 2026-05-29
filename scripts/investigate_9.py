import os, psycopg
DSN = os.environ["DATABASE_URL_RO"]
# Foretag 9: out-of-FY ValueDate. Fragan: har per-period DELETE i backfillen
# (filordning 2022->2025) skrivit over gammal-ars analys med strayrader fran
# 202512-filen? Jamfor analys vs journal per period + vilken fil som ater finns.
Q = {
 "9 journal per period (fil)": """
   SELECT period, count(*) n, count(DISTINCT source_file) files, min(source_file) ex
   FROM fact_journal_saft WHERE company_id=9 GROUP BY 1 ORDER BY 1""",
 "9 analysis per period (fil)": """
   SELECT period, count(*) n, count(DISTINCT source_file) files, min(source_file) ex
   FROM fact_saft_analysis WHERE company_id=9 GROUP BY 1 ORDER BY 1""",
}
with psycopg.connect(DSN, connect_timeout=30) as con:
    with con.cursor() as cur:
        cur.execute("SET statement_timeout='120s'")
        for title, sql in Q.items():
            cur.execute(sql); rows=cur.fetchall(); cols=[d.name for d in cur.description]
            print(f"\n=== {title} ===")
            print(" | ".join(cols))
            for r in rows: print(" | ".join(str(x) for x in r))

import os, psycopg
DSN = os.environ["DATABASE_URL_RO"]

Q = {
 "104 journal per period": """
   SELECT period, count(*) n, count(DISTINCT source_file) files, min(source_file) ex
   FROM fact_journal_saft WHERE company_id=104 GROUP BY 1 ORDER BY 1""",
 "104 analysis per period": """
   SELECT period, count(*) n, min(source_file) ex
   FROM fact_saft_analysis WHERE company_id=104 GROUP BY 1 ORDER BY 1""",
 "104 balances SAFT per period": """
   SELECT period, source_kind, count(*) n
   FROM fact_balances WHERE company_id=104 AND source_kind='SAFT'
   GROUP BY 1,2 ORDER BY 1""",
 "104 journal total + filer": """
   SELECT count(*) rows, count(DISTINCT period) periods, count(DISTINCT source_file) files
   FROM fact_journal_saft WHERE company_id=104""",
 "104 journal distinct source_file": """
   SELECT DISTINCT source_file FROM fact_journal_saft WHERE company_id=104""",
}

with psycopg.connect(DSN, connect_timeout=30) as con:
    with con.cursor() as cur:
        cur.execute("SET statement_timeout='120s'")
        for title, sql in Q.items():
            cur.execute(sql)
            rows = cur.fetchall()
            cols=[d.name for d in cur.description]
            print(f"\n=== {title} ===")
            print(" | ".join(cols))
            for r in rows[:60]:
                print(" | ".join(str(x) for x in r))

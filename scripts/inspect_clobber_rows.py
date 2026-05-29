"""Designfraga for clobber-fixen: ar strö-ValueDate-raderna i en sen fil
DUBBLETTER av historikfilens rader (→ source_file-DELETE ger dubbelrakning)
eller AKTA distinkta korrigeringar (→ source_file-DELETE = ratt union)?

Test: titta pa de 8 journalraderna i (9, 202203) som kom fran 202604-filen.
Om deras transaction_date ligger 2026 (postat 2026, valuerat 2022) = akta
korrigering. Om transaction_date 2022 = potentiell re-export/dubblett.
"""
import os, psycopg
DSN = os.environ["DATABASE_URL_RO"]
with psycopg.connect(DSN, connect_timeout=30) as con:
    with con.cursor() as cur:
        cur.execute("SET statement_timeout='60s'")
        cur.execute("""
          SELECT column_name FROM information_schema.columns
          WHERE table_name='fact_journal_saft' ORDER BY ordinal_position""")
        print("fact_journal_saft kolumner:", [r[0] for r in cur.fetchall()])

        c = "transaction_id, transaction_date, account_code, amount, currency, source_file"
        for label, sql in {
          "9/202203 (clobbrad, 202604-fil)":
            f"SELECT {c} FROM fact_journal_saft WHERE company_id=9 AND period='202203' ORDER BY transaction_date LIMIT 20",
          "9/202204 (intakt, 2022-fil) — 5 ex":
            f"SELECT {c} FROM fact_journal_saft WHERE company_id=9 AND period='202204' ORDER BY transaction_date LIMIT 5",
        }.items():
            cur.execute(sql)
            cols=[d.name for d in cur.description]; rows=cur.fetchall()
            print(f"\n=== {label} ({len(rows)} rader) ===")
            print(" | ".join(cols))
            for r in rows: print(" | ".join(str(x) for x in r))

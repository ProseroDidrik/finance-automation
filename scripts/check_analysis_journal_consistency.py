"""Hard konsistenskoll: fact_saft_analysis mot fact_journal_saft.

Bevisar att dimensionslagret är periodiserat på samma ValueDate-grund som
journalen (se reference_saft_valuedate_bug). Read-only, kör mot prod via
DATABASE_URL_RO (mcp_readonly-rollen).

Tre grindar:
  1. PERIOD-BINDNING: varje (company, period) i analysen MÅSTE finnas i
     journalen. Analyseperioder ⊆ journalperioder. Träffar här = b711832
     återinförd i dim-lagret.
  2. SOURCE_FILE-BINDNING: varje (company, period, source_file) i analysen
     finns i journalen. Strängare — bevisar samma fil.
  3. ÖVERSIKT: per år, antal analys-/journalrader + distinkt (bolag, period).
"""
import os
import psycopg

DSN = os.environ["DATABASE_URL_RO"]

# Distinkta (bolag, period)-mängder är små (~32 bolag x ~60 perioder) — anti-join
# på dem är billig och index-vänlig, till skillnad fran korrelerad NOT EXISTS
# over 9.2M rader.
GATE1 = """
WITH a AS (SELECT DISTINCT company_id, period FROM fact_saft_analysis),
     j AS (SELECT DISTINCT company_id, period FROM fact_journal_saft)
SELECT a.company_id, a.period
FROM a LEFT JOIN j USING (company_id, period)
WHERE j.company_id IS NULL
ORDER BY a.company_id, a.period
LIMIT 100;
"""

GATE2 = """
WITH a AS (SELECT DISTINCT company_id, period, source_file FROM fact_saft_analysis),
     j AS (SELECT DISTINCT company_id, period, source_file FROM fact_journal_saft)
SELECT a.company_id, a.period, a.source_file
FROM a LEFT JOIN j USING (company_id, period, source_file)
WHERE j.source_file IS NULL
ORDER BY a.company_id, a.period
LIMIT 100;
"""

OVERVIEW = """
SELECT left(period, 4) AS yr,
       count(*) AS analysis_rows,
       count(DISTINCT (company_id, period)) AS analysis_company_periods
FROM fact_saft_analysis
GROUP BY 1
ORDER BY 1;
"""


def run(cur, sql, title):
    cur.execute(sql)
    rows = cur.fetchall()
    cols = [d.name for d in cur.description]
    print(f"\n=== {title} ===")
    print(" | ".join(cols))
    if not rows:
        print("(0 rader)")
    for r in rows:
        print(" | ".join(str(x) for x in r))
    return rows


def main():
    with psycopg.connect(DSN, connect_timeout=30) as con:
        with con.cursor() as cur:
            try:
                cur.execute("SET statement_timeout = '180s'")
            except Exception as e:
                print(f"(kunde inte hoja statement_timeout: {e})")
            g1 = run(cur, GATE1, "GRIND 1: analysperiod utan journalperiod (MASTE vara 0)")
            g2 = run(cur, GATE2, "GRIND 2: analys (bolag,period,fil) utan journalmotsvarighet")
            run(cur, OVERVIEW, "OVERSIKT per ar")
    print("\n--- DOM ---")
    print(f"GRIND 1 (period-bindning): {'PASS' if not g1 else 'FAIL — ' + str(len(g1)) + ' avvikelser'}")
    print(f"GRIND 2 (source_file):     {'PASS' if not g2 else 'avvikelser: ' + str(len(g2)) + ' (utred — kan vara olika filsträng mellan laddningar)'}")


if __name__ == "__main__":
    main()

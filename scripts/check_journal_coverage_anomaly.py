"""Återkommande grind 2: journal-täcknings-anomali (clobber-detektor).

Fångar den blinda fläcken som check_analysis_journal_consistency.py missar: när
BÅDE journal och analys raderats av samma strö-fil stämmer analys ⊆ journal och
passerar tyst. Här letar vi i stället efter (bolag, period) där journalradantalet
KOLLAPSAR mot bolagets typiska FY-volym = signaturen för per-period-DELETE-clobber.

Heuristik (kandidater för mänsklig granskning, inte auto-åtgärd):
  per (bolag, FY-år): median journalrader över månader med >0;
  flagga månad där count < FACTOR * median OCH median >= MIN_MEDIAN OCH
  FY-året har >= MIN_MONTHS aktiva månader (så medianen är meningsfull).

Read-only. Mot prod via DATABASE_URL_RO (mcp_readonly).
"""
import os
import statistics
from collections import defaultdict
import psycopg

DSN = os.environ["DATABASE_URL_RO"]
FACTOR = 0.10        # < 10% av FY-medianen = kollaps
MIN_MEDIAN = 100     # bara FY-år med substantiell volym
MIN_MONTHS = 6       # median meningsfull först vid halvår+

SQL = """
SELECT company_id, period, count(*) AS n
FROM fact_journal_saft
GROUP BY 1, 2
"""


def main():
    with psycopg.connect(DSN, connect_timeout=30) as con:
        with con.cursor() as cur:
            cur.execute("SET statement_timeout='150s'")
            cur.execute(SQL)
            rows = cur.fetchall()

    # gruppera per (bolag, år)
    by_cy: dict[tuple, list] = defaultdict(list)
    for cid, period, n in rows:
        by_cy[(cid, period[:4])].append((period, n))

    flagged = []
    for (cid, yr), months in by_cy.items():
        counts = [n for _, n in months]
        if len(counts) < MIN_MONTHS:
            continue
        med = statistics.median(counts)
        if med < MIN_MEDIAN:
            continue
        for period, n in sorted(months):
            if n < FACTOR * med:
                flagged.append((cid, period, n, int(med)))

    flagged.sort()
    print(f"company | period | journal | FY-median  (count < {int(FACTOR*100)}% av median)")
    by_co = defaultdict(int)
    for cid, period, n, med in flagged:
        print(f"{cid} | {period} | {n} | {med}")
        by_co[cid] += 1
    print(f"\nFlaggade (bolag,period): {len(flagged)}")
    print("Per bolag:", dict(sorted(by_co.items())))
    print("\nOBS: flaggar BEFINTLIG clobbrad historik (väntar B1ms-säker reload).")
    print("FY-fixen (denna gren) hindrar NYA clobbers — guarden ska gå mot 0 efter reload.")


if __name__ == "__main__":
    main()

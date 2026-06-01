"""Återkommande grind 2: journal-täcknings-anomali (clobber-detektor).

Fångar den blinda fläcken som check_analysis_journal_consistency.py missar: när
BÅDE journal och analys raderats av samma strö-fil stämmer analys ⊆ journal och
passerar tyst. Här letar vi i stället efter (bolag, period) där journalradantalet
KOLLAPSAR mot bolagets typiska FY-volym = signaturen för per-period-DELETE-clobber.

Heuristik (kandidater för mänsklig granskning, inte auto-åtgärd):
  per (bolag, FY-år): baslinje = MAX journalrader över månaderna;
  flagga månad där count < FACTOR * baslinje OCH baslinje >= MIN_BASELINE OCH
  FY-året har >= MIN_MONTHS månader. INNEVARANDE + FRAMTIDA månader (>= innevarande
  period) exkluderas helt — de har ~0 rader (laddas efter månadsskiftet / har inte
  inträffat), inte för att de clobbats.

OBS baslinjen är MAX, inte median (bug_003): medianen räknas över SAMMA månader
vi letar anomalier i, så när >hälften av en högvolym-FY är clobbrad kollapsar
medianen under tröskeln och hela FY:t hoppas över → de VÄRST clobbrade FY:n gav
noll träffar. MAX (övre kvantil-idé) är robust mot hur många månader som faller.

KÄND RESIDUAL: bolag med EN volym-tung månad och låg jämn rörelse i övriga
(t.ex. Buysec cid 176: en månad ~3300, resten ~130) flaggas på MAX-baslinjen
trots komplett källa. Radantal ensamt kan inte skilja en sån lumpy-men-hel
fördelning från en äkta clobb (remnant 2–120 överlappar lumpy 110–145) utan att
riskera missa riktiga clobbar — lämnas till mänsklig granskning (kolla källfilens
radantal, jfr Elverum-flödet).

Read-only. Mot prod via DATABASE_URL_RO (mcp_readonly).
"""
import datetime
import os
from collections import defaultdict
import psycopg

FACTOR = 0.10        # < 10% av FY-baslinjen = kollaps
MIN_BASELINE = 500   # bara FY-år med substantiell volym (max-månad). Höjt 100→500:
                     # pyttebolag (max-månad < 500) ger meningslösa kollaps-flaggor
                     # (en lugn månad är brus, inte clobb).
MIN_MONTHS = 6       # baslinje meningsfull först vid halvår+


def current_yyyymm() -> str:
    """Innevarande period (YYYYMM) — gräns för att exkludera framtida månader."""
    t = datetime.date.today()
    return f"{t.year:04d}{t.month:02d}"

SQL = """
SELECT company_id, period, count(*) AS n
FROM fact_journal_saft
GROUP BY 1, 2
"""


def flag_anomalies(rows, factor=FACTOR, min_baseline=MIN_BASELINE,
                   min_months=MIN_MONTHS, current_period=None):
    """Ren detektor (ingen DB, testbar). rows = [(company_id, period, n), ...].
    Returnerar sorterad [(company_id, period, n, baslinje), ...] för månader vars
    journalradantal kollapsar < factor * FY-baslinje. Baslinje = MAX (inte median)
    så detektorn INTE blir blind när >hälften av en FY är clobbrad (bug_003).

    current_period (YYYYMM): om satt exkluderas INNEVARANDE + FRAMTIDA månader
    (period >= current_period) helt — innevarande månad är ännu mid-load (SAF-T
    laddas efter månadsskiftet) och delar framtidens ~0-rader-egenskap. De räknas
    varken mot baslinje/MIN_MONTHS eller flaggas. None = ingen filtrering
    (bakåtkompatibelt för historik-only-rader/tester)."""
    if current_period is not None:
        rows = [(cid, period, n) for cid, period, n in rows
                if period < current_period]
    by_cy: dict[tuple, list] = defaultdict(list)
    for cid, period, n in rows:
        by_cy[(cid, period[:4])].append((period, n))

    flagged = []
    for (cid, _yr), months in by_cy.items():
        counts = [n for _, n in months]
        if len(counts) < min_months:
            continue
        base = max(counts)          # robust mot hur många månader som clobbrats
        if base < min_baseline:
            continue
        for period, n in sorted(months):
            if n < factor * base:
                flagged.append((cid, period, n, base))
    return sorted(flagged)


def main():
    dsn = os.environ["DATABASE_URL_RO"]
    with psycopg.connect(dsn, connect_timeout=30) as con:
        with con.cursor() as cur:
            cur.execute("SET statement_timeout='280s'")
            cur.execute(SQL)
            rows = cur.fetchall()

    cur_period = current_yyyymm()
    flagged = flag_anomalies(rows, current_period=cur_period)
    print(f"company | period | journal | FY-baslinje(max)  (count < {int(FACTOR*100)}% av max, "
          f"baslinje>={MIN_BASELINE}, exkl. innevarande+framtida >= {cur_period})")
    by_co = defaultdict(int)
    for cid, period, n, base in flagged:
        print(f"{cid} | {period} | {n} | {base}")
        by_co[cid] += 1
    print(f"\nFlaggade (bolag,period): {len(flagged)}")
    print("Per bolag:", dict(sorted(by_co.items())))
    print("\nOBS: flaggar BEFINTLIG clobbrad historik (väntar B1ms-säker reload).")
    print("FY-fixen (denna gren) hindrar NYA clobbers — guarden ska gå mot 0 efter reload.")


if __name__ == "__main__":
    main()

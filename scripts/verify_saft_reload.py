"""Verifiera en SAF-T-reload mot Mercur-facit.

EN databasanslutning (gentle på B1ms-servern — per-bolag-loop på samma
connection, inte en connection per bolag). Default NO+DK.

Tre kontroller:
  [1] Jan-koncentration vs Mercur (ALLA kontoklasser): för varje (bolag, konto)
      vars journal bara har rörelse i jan (feb=mar=apr≈0), kolla om Mercur
      också bokar det i jan (=korrekt, t.ex. ingående balans / årlig post) eller
      sprider det (=kvarvarande fel-periodisering, t.ex. SAF-T saknar ValueDate).
  [2] FY-filer (158/189, 2026-12): YTD-match SAFT vs -Mercur jan-apr — sanity
      att ValueDate-cutoff:en inte tappade fel rader.

Körs: DATABASE_URL_ETL satt + PYTHONIOENCODING=utf-8.
"""
import os
import sys
import psycopg
from psycopg.rows import dict_row
from collections import defaultdict

URL = os.environ["DATABASE_URL_ETL"]
COUNTRIES = sys.argv[1:] or ["Norway", "Denmark"]
FY_FILES = [158, 189]  # bolag med FY-fil (2026-12) → ValueDate-cutoff påverkar YTD


def jan_concentrated(c, b):
    """(konto, jan) för konton med rörelse bara i jan inom 202601-202604."""
    return c.execute("""
        WITH pm AS (SELECT account_code, period, SUM(amount) amt
          FROM fact_journal_saft WHERE company_id=%s AND period BETWEEN '202601' AND '202604'
          GROUP BY account_code, period),
        piv AS (SELECT account_code,
            MAX(amt) FILTER (WHERE period='202601') jan,
            MAX(amt) FILTER (WHERE period='202602') feb,
            MAX(amt) FILTER (WHERE period='202603') mar,
            MAX(amt) FILTER (WHERE period='202604') apr
          FROM pm GROUP BY account_code)
        SELECT account_code, jan FROM piv
        WHERE jan IS NOT NULL AND abs(jan)>100
          AND coalesce(abs(feb),0)<0.01 AND coalesce(abs(mar),0)<0.01
          AND coalesce(abs(apr),0)<0.01""", (b,)).fetchall()


def mercur_months(c, b, k):
    return {r['period']: float(r['amt']) for r in c.execute("""
        SELECT period, SUM(amount) amt FROM backup_from_mercur
        WHERE company_id=%s AND account_code=%s AND period BETWEEN '202601' AND '202604'
          AND scenario='A' GROUP BY period""", (b, k)).fetchall()}


with psycopg.connect(URL, row_factory=dict_row, connect_timeout=15) as c:
    placeholders = ",".join(["%s"] * len(COUNTRIES))
    bolag = [r['company_id'] for r in c.execute(f"""
        SELECT DISTINCT j.company_id FROM fact_journal_saft j
        JOIN dim_company d ON d.company_id=j.company_id
        WHERE d.country IN ({placeholders}) AND j.period BETWEEN '202601' AND '202604'
        ORDER BY j.company_id""", COUNTRIES).fetchall()]
    print(f"Bolag med SAFT-data FY2026 ({'+'.join(COUNTRIES)}): {len(bolag)}")

    # [1] Jan-koncentration vs Mercur, alla kontoklasser
    residual, genuine, no_merc = [], 0, 0
    for b in bolag:
        for r in jan_concentrated(c, b):
            k, jan = r['account_code'], float(r['jan'])
            m = mercur_months(c, b, k)
            if not m:
                no_merc += 1
                continue
            nonjan = sum(abs(v) for p, v in m.items() if p != '202601')
            janm = abs(m.get('202601', 0.0))
            if nonjan > max(1.0, 0.10 * janm):
                residual.append((b, k, jan, m))
            else:
                genuine += 1

    total = len(residual) + genuine + no_merc
    print(f"\n[1] Jan-koncentrerade (bolag,konto)-par: {total}")
    print(f"      korrekt (Mercur också jan):        {genuine}")
    print(f"      ingen Mercur-data:                 {no_merc}")
    print(f"  >>> KVARVARANDE FEL (Mercur sprider):  {len(residual)}")
    if residual:
        by_b = defaultdict(int)
        for b, k, jan, m in residual:
            by_b[b] += 1
        print(f"      per bolag: {dict(sorted(by_b.items(), key=lambda x:-x[1]))}")
        print(f"      {'bolag':>5} {'konto':>6} {'saft_jan':>14}   mercur jan-apr")
        for b, k, jan, m in sorted(residual, key=lambda x: -abs(x[2]))[:25]:
            ms = " ".join(f"{p[-2:]}:{v:,.0f}" for p, v in sorted(m.items()))
            print(f"      {b:>5} {k:>6} {jan:>14,.2f}   {ms}")

    # [2] FY-fil YTD-sanity
    print(f"\n[2] FY-fil YTD-match SAFT vs -Mercur (jan-apr):")
    for b in FY_FILES:
        saft = {r['account_code']: float(r['ytd']) for r in c.execute(
            "SELECT account_code, SUM(amount) ytd FROM fact_journal_saft WHERE company_id=%s AND period BETWEEN '202601' AND '202604' GROUP BY account_code", (b,)).fetchall()}
        merc = {r['account_code']: float(r['ytd']) for r in c.execute(
            "SELECT account_code, SUM(amount) ytd FROM backup_from_mercur WHERE company_id=%s AND period BETWEEN '202601' AND '202604' AND scenario='A' GROUP BY account_code", (b,)).fetchall()}
        konton = set(saft) | set(merc)
        m = sum(1 for k in konton if abs(saft.get(k, 0.0) - (-merc.get(k, 0.0))) <= max(1.0, 0.005 * max(abs(saft.get(k, 0.0)), abs(merc.get(k, 0.0)))))
        pct = 100 * m / len(konton) if konton else 100
        print(f"      bolag {b}: {m}/{len(konton)} = {pct:.1f}%")

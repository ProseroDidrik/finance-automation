"""Verifiera hypotes 1: Mercur sprider SAFT jan-värdet jämnt över N månader.

Bolag 16/36/158, konton 6010/5095/5096, jan-apr 2026.
Om Mercurs månadsvärden = SAFT_jan / 4 (för apr-period) stämmer hypotesen.
"""
import os
import psycopg
from psycopg.rows import dict_row

URL = os.environ["DATABASE_URL_ETL"]

SQL = """
WITH saft AS (
  SELECT company_id AS bolag, account_code AS konto, period,
         SUM(amount) AS saft_amount
  FROM fact_journal_saft
  WHERE company_id IN (16, 36, 158)
    AND period IN ('202601','202602','202603','202604')
    AND account_code IN ('6010','5095','5096')
  GROUP BY company_id, account_code, period
),
merc AS (
  SELECT company_id AS bolag, account_code AS konto, period,
         SUM(amount) AS merc_amount
  FROM backup_from_mercur
  WHERE company_id IN (16, 36, 158)
    AND period IN ('202601','202602','202603','202604')
    AND account_code IN ('6010','5095','5096')
    AND scenario = 'A'
  GROUP BY company_id, account_code, period
)
SELECT
  COALESCE(s.bolag, m.bolag)   AS bolag,
  COALESCE(s.konto, m.konto)   AS konto,
  COALESCE(s.period, m.period) AS period,
  s.saft_amount,
  m.merc_amount
FROM saft s
FULL OUTER JOIN merc m
  ON s.bolag = m.bolag AND s.konto = m.konto AND s.period = m.period
ORDER BY bolag, konto, period
"""

with psycopg.connect(URL, row_factory=dict_row) as c:
    rows = c.execute(SQL).fetchall()

print(f"{'bolag':>5} {'konto':>6} {'period':>7} {'saft':>16} {'mercur':>16}")
print("-" * 56)
for r in rows:
    saft = f"{float(r['saft_amount']):16,.2f}" if r['saft_amount'] is not None else " " * 16
    merc = f"{float(r['merc_amount']):16,.2f}" if r['merc_amount'] is not None else " " * 16
    print(f"{r['bolag']:>5} {r['konto']:>6} {r['period']:>7} {saft} {merc}")

# Hypotes-check
print("\n=== Hypotes 1: SAFT_total / 4 == Mercur per månad? ===")
agg = {}
for r in rows:
    k = (r['bolag'], r['konto'])
    if k not in agg:
        agg[k] = {'saft': 0.0, 'merc_months': {}}
    if r['saft_amount'] is not None:
        agg[k]['saft'] += float(r['saft_amount'])
    if r['merc_amount'] is not None:
        agg[k]['merc_months'][r['period']] = float(r['merc_amount'])

for (b, k), v in sorted(agg.items()):
    saft_total = v['saft']
    months = v['merc_months']
    n = len(months)
    if n == 0:
        print(f"bolag={b} konto={k}  saft={saft_total:>12,.2f}  (ingen Mercur-data)")
        continue
    expected = saft_total / n
    avg = sum(months.values()) / n
    spread = (max(months.values()) - min(months.values())) if months else 0
    jamn = "JA " if abs(spread) < 1.0 else "NEJ"
    matchar = "JA " if abs(avg - expected) < 1.0 else "NEJ"
    print(f"bolag={b} konto={k}  saft_total={saft_total:>11,.2f}  N={n}  expected/N={expected:>10,.2f}  "
          f"mercur_avg={avg:>10,.2f}  jämn={jamn}  matchar={matchar}")
    print(f"      mercur per månad: {months}")

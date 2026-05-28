"""Buckea alla NO-bolag med jan-koncentration på avskrivning/årliga konton.

Per-bolag loop för att undvika statement timeout på fact_journal_saft.
"""
import os
import psycopg
from psycopg.rows import dict_row
from collections import defaultdict

URL = os.environ["DATABASE_URL_ETL"]

# Hämta NO-bolag som har SAFT-data för 202604-FY
SQL_BOLAG = """
SELECT DISTINCT j.company_id
FROM fact_journal_saft j
JOIN dim_company d ON d.company_id = j.company_id
WHERE d.country = 'Norway'
  AND j.period IN ('202601','202602','202603','202604')
ORDER BY j.company_id
"""

SQL_PER_BOLAG = """
WITH per_month AS (
  SELECT account_code, period, SUM(amount) AS amt
  FROM fact_journal_saft
  WHERE company_id = %s
    AND period IN ('202601','202602','202603','202604')
  GROUP BY account_code, period
),
pivot AS (
  SELECT account_code,
         MAX(CASE WHEN period='202601' THEN amt END) AS jan,
         MAX(CASE WHEN period='202602' THEN amt END) AS feb,
         MAX(CASE WHEN period='202603' THEN amt END) AS mar,
         MAX(CASE WHEN period='202604' THEN amt END) AS apr
  FROM per_month
  GROUP BY account_code
)
SELECT account_code, jan, feb, mar, apr
FROM pivot
WHERE jan IS NOT NULL
  AND ABS(COALESCE(jan,0)) > 100
  AND COALESCE(ABS(feb),0) < 0.01
  AND COALESCE(ABS(mar),0) < 0.01
  AND COALESCE(ABS(apr),0) < 0.01
"""

SQL_MERCUR_PER_BOLAG = """
SELECT account_code, period, SUM(amount) AS amt
FROM backup_from_mercur
WHERE company_id = %s
  AND period IN ('202601','202602','202603','202604')
  AND scenario='A'
GROUP BY account_code, period
"""

# Steg 1: hämta NO-bolag
print("Hämtar NO-bolag ...")
with psycopg.connect(URL, row_factory=dict_row) as c:
    bolag = [r['company_id'] for r in c.execute(SQL_BOLAG).fetchall()]
print(f"  {len(bolag)} NO-bolag har SAFT-data jan-apr 2026")

# Steg 2: loopa per bolag, samla kandidater + Mercur-data
all_cands = []   # list of dict
mercur_data = {} # (bolag, konto) -> {period: amt}

for b in bolag:
    with psycopg.connect(URL, row_factory=dict_row) as c:
        rows = c.execute(SQL_PER_BOLAG, (b,)).fetchall()
        for r in rows:
            all_cands.append({'bolag': b, 'konto': r['account_code'], 'jan': float(r['jan'])})
        merc_rows = c.execute(SQL_MERCUR_PER_BOLAG, (b,)).fetchall()
        for mr in merc_rows:
            mercur_data.setdefault((b, mr['account_code']), {})[mr['period']] = float(mr['amt'])

print(f"  {len(all_cands)} (bolag,konto)-par med jan-koncentration")

# Steg 3: klassificera
buckets = defaultdict(list)
for c in all_cands:
    key = (c['bolag'], c['konto'])
    saft_jan = c['jan']
    m = mercur_data.get(key, {})
    if not m:
        buckets['INGEN_MERCUR'].append((key, saft_jan, None, None))
        continue
    vals = list(m.values())
    n_obs = len(vals)
    avg = sum(vals) / n_obs
    spread = max(vals) - min(vals) if vals else 0
    jamn = abs(spread) < max(abs(avg) * 0.05, 1.0)
    # Test både flippad och ej, för N=4 och N=12
    expected = {
      'flip_4':  -saft_jan / 4,
      'flip_12': -saft_jan / 12,
      'plain_4':  saft_jan / 4,
      'plain_12': saft_jan / 12,
    }
    match = None
    for name, exp in expected.items():
        if abs(avg - exp) < max(abs(exp) * 0.02, 1.0):
            match = name
            break
    if not jamn:
        buckets['ANNAN_KALLA'].append((key, saft_jan, avg, m))
    elif match:
        buckets['AUTO_FIXABLE'].append((key, saft_jan, avg, match))
    else:
        buckets['INKOMPLETT_SAFT'].append((key, saft_jan, avg, m))

# Rapport
print(f"\n=== Bucket-summering ===")
for bucket, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
    bolag_unique = len(set(k[0] for k, *_ in items))
    print(f"  {bucket:20s}  {len(items):>3} (bolag,konto)-par  ({bolag_unique} unika bolag)")

print(f"\n=== AUTO_FIXABLE (spridning skulle lösa) ===")
print(f"{'bolag':>5} {'konto':>6} {'saft_jan':>14} {'mercur_avg':>14} {'mönster':>10}")
for key, saft_jan, avg, m in sorted(buckets['AUTO_FIXABLE']):
    print(f"{key[0]:>5} {key[1]:>6} {saft_jan:>14,.2f} {avg:>14,.2f} {m:>10s}")

print(f"\n=== INKOMPLETT_SAFT (jämn Mercur men ej spridningsmatch — extern_action) ===")
print(f"{'bolag':>5} {'konto':>6} {'saft_jan':>14} {'mercur_avg':>14}")
for key, saft_jan, avg, m in sorted(buckets['INKOMPLETT_SAFT']):
    print(f"{key[0]:>5} {key[1]:>6} {saft_jan:>14,.2f} {avg:>14,.2f}")

print(f"\n=== ANNAN_KALLA (ojämn Mercur — löner/manuella — extern_action) ===")
print(f"{'bolag':>5} {'konto':>6} {'saft_jan':>14} {'mercur_avg':>14}")
for key, saft_jan, avg, m in sorted(buckets['ANNAN_KALLA'])[:25]:
    print(f"{key[0]:>5} {key[1]:>6} {saft_jan:>14,.2f} {avg:>14,.2f}")
if len(buckets['ANNAN_KALLA']) > 25:
    print(f"  ... och {len(buckets['ANNAN_KALLA']) - 25} till")

if buckets['INGEN_MERCUR']:
    print(f"\n=== INGEN_MERCUR (kandidat finns i SAFT men ej i backup_from_mercur) ===")
    print(f"  {len(buckets['INGEN_MERCUR'])} par — listas ej, troligen bolag som inte finns i Mercur backup")

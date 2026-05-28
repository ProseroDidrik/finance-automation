"""YTD-jämförelse NO: stämmer SAFT mot Mercur när vi summerar jan-apr per konto?

Hypotes: periodiseringsskillnader (SAFT jan-koncentrerad vs Mercur spridd)
suddas ut på YTD-nivå. Om så → ~100% beloppsöverensstämmelse på YTD.

backup_from_mercur = månadsrörelse (Mercur-konv, intäkt+).
fact_journal_saft  = vouchers/rörelse (SIE-konv, intäkt-).
Sign-flip: Mercur = -SAFT. Jämför SAFT_ytd vs -Mercur_ytd.
"""
import os
import psycopg
from psycopg.rows import dict_row
from collections import defaultdict

URL = os.environ["DATABASE_URL_ETL"]

SQL_BOLAG = """
SELECT DISTINCT j.company_id
FROM fact_journal_saft j
JOIN dim_company d ON d.company_id = j.company_id
WHERE d.country = 'Norway'
  AND j.period IN ('202601','202602','202603','202604')
ORDER BY j.company_id
"""

SQL_SAFT = """
SELECT account_code, SUM(amount) AS ytd
FROM fact_journal_saft
WHERE company_id = %s AND period IN ('202601','202602','202603','202604')
GROUP BY account_code
"""

SQL_MERC = """
SELECT account_code, SUM(amount) AS ytd
FROM backup_from_mercur
WHERE company_id = %s AND period IN ('202601','202602','202603','202604')
  AND scenario='A'
GROUP BY account_code
"""

with psycopg.connect(URL, row_factory=dict_row) as c:
    bolag = [r['company_id'] for r in c.execute(SQL_BOLAG).fetchall()]

print(f"{len(bolag)} NO-bolag\n")

# Per bolag: räkna konton som stämmer på YTD (med flip)
TOL = 1.0  # 1 krona
tot_match = tot_konton = 0
per_bolag_stats = []
big_diffs = []  # (bolag, konto, saft_ytd, merc_flip, diff)

for b in bolag:
    with psycopg.connect(URL, row_factory=dict_row) as c:
        saft = {r['account_code']: float(r['ytd']) for r in c.execute(SQL_SAFT, (b,)).fetchall()}
        merc = {r['account_code']: float(r['ytd']) for r in c.execute(SQL_MERC, (b,)).fetchall()}
    konton = set(saft) | set(merc)
    match = 0
    for k in konton:
        s = saft.get(k, 0.0)
        m = merc.get(k, 0.0)
        # sign-flip: SAFT ≈ -Mercur
        diff = abs(s - (-m))
        # tolerans: 1 kr eller 0.5% av max(|s|,|m|)
        tol = max(TOL, 0.005 * max(abs(s), abs(m)))
        if diff <= tol:
            match += 1
        else:
            big_diffs.append((b, k, s, -m, s - (-m)))
    tot_match += match
    tot_konton += len(konton)
    pct = 100.0 * match / len(konton) if konton else 0
    per_bolag_stats.append((b, match, len(konton), pct))

print(f"=== YTD-överensstämmelse per bolag (SAFT vs -Mercur, jan-apr summa) ===")
print(f"{'bolag':>5} {'match':>6} {'konton':>7} {'pct':>7}")
for b, m, n, pct in sorted(per_bolag_stats, key=lambda x: x[3]):
    flag = "  <-- låg" if pct < 90 else ""
    print(f"{b:>5} {m:>6} {n:>7} {pct:>6.1f}%{flag}")

print(f"\n=== TOTALT YTD (rått) ===")
print(f"  {tot_match} / {tot_konton} konton stämmer på YTD = {100.0*tot_match/tot_konton:.1f}%")

# ---- Kategorisering av de 349 avvik ----
# big_diffs: (bolag, konto, saft_ytd, merc_flip, diff)  där diff = saft - (-merc)
def is_mercur_internal(k):
    return k.startswith('P_') or k in ('9997', '9998', '9999')

scope_merc_only = []   # saft≈0, merc≠0
scope_saft_only = []   # merc≈0, saft≠0
amount_diff = []       # båda≠0
mercur_internal = []

for row in big_diffs:
    b, k, s, mf, d = row
    if is_mercur_internal(k):
        mercur_internal.append(row)
    elif abs(s) < 1.0:
        scope_merc_only.append(row)
    elif abs(mf) < 1.0:
        scope_saft_only.append(row)
    else:
        amount_diff.append(row)

# ---- Offsetting-par: inom samma bolag, två konton vars diff tar ut varandra ----
from collections import defaultdict
by_bolag = defaultdict(list)
for row in amount_diff:
    by_bolag[row[0]].append(row)

offsetting = []   # (bolag, kontoA, kontoB, diffA, diffB)
matched_idx = set()
for b, rows in by_bolag.items():
    for i in range(len(rows)):
        if id(rows[i]) in matched_idx:
            continue
        for j in range(i+1, len(rows)):
            if id(rows[j]) in matched_idx:
                continue
            dA, dB = rows[i][4], rows[j][4]
            if abs(dA + dB) < max(1.0, 0.01*abs(dA)):
                offsetting.append((b, rows[i][1], rows[j][1], dA, dB))
                matched_idx.add(id(rows[i]))
                matched_idx.add(id(rows[j]))
                break

real_amount_diff = [r for r in amount_diff if id(r) not in matched_idx]

print(f"\n=== Nedbrytning av {len(big_diffs)} YTD-avvik ===")
print(f"  Mercur-interna konton (P_*, 9997):           {len(mercur_internal):>4}  (ej SAFT-konton)")
print(f"  Konto BARA i Mercur (saft=0):                {len(scope_merc_only):>4}  (konsolidering/scope)")
print(f"  Konto BARA i SAFT (merc=0):                  {len(scope_saft_only):>4}  (Mercur saknar)")
print(f"  Motpostande par (kontomappning, netto 0):    {len(offsetting)*2:>4}  ({len(offsetting)} par)")
print(f"  >>> GENUIN belopps-diff (kvar):              {len(real_amount_diff):>4}")

# Justerade procent
adj1 = tot_match + len(mercur_internal)
adj2 = adj1 + len(scope_merc_only)
adj3 = adj2 + len(offsetting)*2
print(f"\n=== Justerad YTD-överensstämmelse ===")
print(f"  Rått:                                         {100.0*tot_match/tot_konton:.1f}%")
print(f"  + Mercur-interna bortstrippade:               {100.0*adj1/tot_konton:.1f}%")
print(f"  + Mercur-only konsolideringskonton:           {100.0*adj2/tot_konton:.1f}%")
print(f"  + motpostande kontomappningar nettade:        {100.0*adj3/tot_konton:.1f}%")
print(f"  Återstår {len(real_amount_diff)} konton = {100.0*len(real_amount_diff)/tot_konton:.1f}% genuin diff")

print(f"\n=== Motpostande par (samma pengar, annat kontonr) ===")
print(f"{'bolag':>5} {'kontoA':>7} {'kontoB':>7} {'diffA':>16} {'diffB':>16}")
for b, ka, kb, dA, dB in sorted(offsetting, key=lambda x: -abs(x[3]))[:15]:
    print(f"{b:>5} {ka:>7} {kb:>7} {dA:>16,.2f} {dB:>16,.2f}")

print(f"\n=== GENUIN belopps-diff (topp 25 — den riktiga listan) ===")
print(f"{'bolag':>5} {'konto':>6} {'saft_ytd':>16} {'merc_flip':>16} {'diff':>16}")
for b, k, s, mf, d in sorted(real_amount_diff, key=lambda x: -abs(x[4]))[:25]:
    print(f"{b:>5} {k:>6} {s:>16,.2f} {mf:>16,.2f} {d:>16,.2f}")

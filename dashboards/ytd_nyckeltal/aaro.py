"""aaro.py — AARO-konto-klassificering: warehouse vs Mercur per AARO-grupp-konto.

Bygger AARO_DATA-payloaden (flik "Aaro-klassificering" i HTML + Excel). Per
AARO-konto (account_id, t.ex. 'Sales'/'COGS', + 4-siffrig aaro_code) jämförs
Mercur-facit mot warehouse-summan, för båda år (YTD apr 2026 vs 2025).

Warehouse-aggregering = samma dim_account_map-walk som topgroup-queryn, men vi
fångar `account_id`-NIVÅN (den närmaste AARO-grupp-noden ovanför lövet) i stället
för top_group-nivån. account_id är join-nyckeln (4-siffer-koden finns INTE som
egen nod). best_source per (bolag, period), FX→SEK, abs() per (bolag, aaro_id).

Kontrakt (läses av render_html.js renderAaro + render_xlsx flik 5), per post:
  top_group, account_id, aaro_code, desc,
  facit_utfall, warehouse_total, diff, diff_pct,            (2026)
  facit_utfall_25, warehouse_total_25, diff_25, diff_pct_25 (2025)
Alla belopp i RÅ SEK. `diff`/`diff_25` behövs (JS-sorten läser Math.abs(diff)).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db_io  # noqa: E402
from config import derive_periods, fx_for  # noqa: E402

BASE_SOURCES = ("SIE_PSALDO", "SIE_VER", "SIE", "SAFT", "SAFT_VER", "IMP")
ALL_SOURCES = BASE_SOURCES + ("MAN", "IMP_ADJ")


def _aaro_query(account_ids, targets_sql, start_period, end_period):
    """Bygg AARO-warehouse-query. account_ids quotas in (UTF-8, åäö-säkert)."""
    id_list = ",".join("'" + a.replace("'", "''") + "'" for a in account_ids)
    src_base = ",".join(f"'{s}'" for s in BASE_SOURCES)
    src_all = ",".join(f"'{s}'" for s in ALL_SOURCES)
    return f"""
WITH RECURSIVE walk AS (
  SELECT m.company_id, m.account_code, m.account_id AS cur_id, m.parent_id, 0 AS depth
  FROM dim_account_map m WHERE m.is_aggregated = FALSE AND m.company_id IS NOT NULL
  UNION ALL
  SELECT w.company_id, w.account_code, p.account_id, p.parent_id, w.depth + 1
  FROM walk w JOIN dim_account_map p ON w.parent_id = p.account_id WHERE w.depth < 10
),
acc_aaro AS (  -- NÄRMASTE aaro-grupp-nod ovanför lövet (depth ASC)
  SELECT DISTINCT ON (company_id, account_code) company_id, account_code, cur_id AS aaro_id
  FROM walk WHERE cur_id IN ({id_list})
  ORDER BY company_id, account_code, depth ASC
),
fb_signed AS (
  SELECT fb.company_id, fb.period, fb.account_code, fb.source_kind, c.currency,
         fb.amount * CASE WHEN fb.account_code LIKE 'P_%' THEN -1 ELSE 1 END AS amount
  FROM fact_balances fb JOIN dim_company c ON c.company_id = fb.company_id
  WHERE fb.scenario = 'A' AND fb.source_kind IN ({src_all})
    AND fb.period BETWEEN '{start_period}' AND '{end_period}'
),
base_pick AS (
  SELECT company_id, period,
    CASE WHEN bool_or(source_kind='SIE_PSALDO') THEN 'SIE_PSALDO'
         WHEN bool_or(source_kind='SIE_VER') THEN 'SIE_VER'
         WHEN bool_or(source_kind='SIE') THEN 'SIE'
         WHEN bool_or(source_kind='SAFT') THEN 'SAFT'
         WHEN bool_or(source_kind='SAFT_VER') THEN 'SAFT_VER'
         WHEN bool_or(source_kind='IMP') THEN 'IMP' END AS base_src
  FROM fb_signed WHERE source_kind IN ({src_base})
  GROUP BY company_id, period
),
targets AS (SELECT * FROM (VALUES {targets_sql}) AS t(target_period)),
base_ytd AS (
  SELECT t.target_period, bp.company_id, fb.currency, a.aaro_id, SUM(fb.amount) AS amount
  FROM targets t
  JOIN base_pick bp ON bp.period = t.target_period
  JOIN fb_signed fb ON fb.company_id = bp.company_id AND fb.source_kind = bp.base_src
  JOIN acc_aaro a ON a.company_id = fb.company_id AND a.account_code = fb.account_code
  WHERE (bp.base_src IN ('SIE','SIE_VER','SAFT','SAFT_VER') AND fb.period = t.target_period)
     OR (bp.base_src IN ('SIE_PSALDO','IMP')
         AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period)
  GROUP BY t.target_period, bp.company_id, fb.currency, a.aaro_id
),
adj_ytd AS (
  SELECT t.target_period, fb.company_id, fb.currency, a.aaro_id, SUM(fb.amount) AS amount
  FROM targets t
  JOIN fb_signed fb ON fb.source_kind IN ('MAN','IMP_ADJ')
    AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period
  JOIN acc_aaro a ON a.company_id = fb.company_id AND a.account_code = fb.account_code
  GROUP BY t.target_period, fb.company_id, fb.currency, a.aaro_id
)
SELECT json_agg(row_to_json(u))::text AS payload FROM (
  SELECT target_period, company_id, currency, aaro_id, SUM(amount) AS amount_local
  FROM (SELECT * FROM base_ytd UNION ALL SELECT * FROM adj_ytd) x
  GROUP BY target_period, company_id, currency, aaro_id
) u;
"""


def _wh_totals(rows):
    """warehouse_total per (aaro_id, period) i SEK: abs(amount_local)*FX, summa över bolag."""
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        per = r["target_period"]
        rate = fx_for(per).get(r["currency"], 1.0)
        sek = abs(r["amount_local"] or 0) * rate
        out[(r["aaro_id"], per)] = out.get((r["aaro_id"], per), 0.0) + sek
    return out


def _diff_pct(facit, wh):
    return (facit - wh) / facit if abs(facit) > 1000 else None


def build_aaro_classification(aaro_2026, aaro_2025, wh_totals, cur, prev):
    """Bygg AARO_DATA-listan. aaro_2026/_2025 = parse_aaro_facit-output (per rad)."""
    records = []
    for r26, r25 in zip(aaro_2026, aaro_2025):
        aid = r26["account_id"]
        f26 = abs(r26["utfall"] or 0)
        f25 = abs(r25["utfall"] or 0)
        w26 = wh_totals.get((aid, cur), 0.0)
        w25 = wh_totals.get((aid, prev), 0.0)
        records.append({
            "top_group": r26["top_group"], "account_id": aid,
            "aaro_code": r26["aaro_code"], "desc": r26["desc"],
            "facit_utfall": round(f26), "warehouse_total": round(w26),
            "diff": round(f26 - w26), "diff_pct": _diff_pct(f26, w26),
            "facit_utfall_25": round(f25), "warehouse_total_25": round(w25),
            "diff_25": round(f25 - w25), "diff_pct_25": _diff_pct(f25, w25),
        })
    return records


def run(facit_dir, mercur, period="202604"):
    """Parsa Mercur (21) + kör warehouse-query → AARO_DATA-lista."""
    per = derive_periods(period)
    fp = Path(facit_dir) / "Resultaträkning (21).xlsx"
    aaro_2026, aaro_2025 = mercur.parse_aaro_facit(fp)
    account_ids = sorted({r["account_id"] for r in aaro_2026})

    targets_sql = f"('{per['prev']}'),('{per['cur']}')"
    sql = _aaro_query(account_ids, targets_sql,
                      start_period=f"{per['prev'][:4]}01", end_period=per["cur"])
    con = db_io.connect()
    try:
        rows = db_io.run_payload(con, sql)
    finally:
        con.close()
    wh = _wh_totals(rows)
    return build_aaro_classification(aaro_2026, aaro_2025, wh, per["cur"], per["prev"])

"""aaro.py — AARO-konto-klassificering: warehouse vs Mercur per AARO-grupp-konto.

Bygger AARO_DATA-payloaden (flik "Aaro-klassificering" i HTML + Excel). Per
AARO-konto (account_id, t.ex. 'Sales'/'COGS', + 4-siffrig aaro_code) jämförs
Mercur-facit mot warehouse-summan, för båda år (YTD apr 2026 vs 2025).

Warehouse-aggregering = samma dim_account_map-walk som topgroup-queryn, men vi
fångar `account_id`-NIVÅN (den närmaste AARO-grupp-noden ovanför lövet) i stället
för top_group-nivån. account_id är join-nyckeln (4-siffer-koden finns INTE som
egen nod).

v1.8b: per-MÅNADS-FX (speglar YTD_TOPGROUP_QUERY). Queryn emitterar
månadsrörelser (LAG-differencing av ytd-källor, partitionerat per år) och
_wh_totals FX-konverterar varje månad mot sin egen snittkurs via `rate_of`.
Tidigare kollapsades till YTD och multiplicerades med en enda periodkurs.
Inkluderar samma at-target-grind + villkorade P-kods-flip (co_conv) som topgroup.

Kontrakt (läses av render_html.js renderAaro + render_xlsx flik 5), per post:
  top_group, account_id, aaro_code, desc,
  facit_utfall, warehouse_total, diff, diff_pct,            (2026)
  facit_utfall_25, warehouse_total_25, diff_25, diff_pct_25 (2025)
Alla belopp i RÅ SEK. `diff`/`diff_25` behövs (JS-sorten läser Math.abs(diff)).
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db_io  # noqa: E402
from aggregate import _make_fx_resolver  # noqa: E402
from config import derive_periods  # noqa: E402

BASE_SOURCES = ("SIE_PSALDO", "SIE_VER", "SIE", "SAFT", "SAFT_VER", "IMP")
ALL_SOURCES = BASE_SOURCES + ("MAN", "IMP_ADJ")


def _aaro_query(account_ids, targets_sql, start_period, end_period):
    """Bygg AARO-warehouse-query (månadsrörelse-grain). account_ids quotas in (UTF-8)."""
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
-- Delade (bolagsagnostiska) trädnoder: account_id satt, account_code/company_id =
-- NULL. Omfattar P-koder (P_30 …) men även '_'/'BUDG'. `walk` seedar bara company_id
-- IS NOT NULL → MAN/IMP_ADJ bokade på delade koder droppas annars tyst. Walka upp
-- dem separat (speglar report_pnl.sql:177).
pwalk AS (
  SELECT m.account_id AS pcode, m.account_id AS cur_id, m.parent_id, 0 AS depth
  FROM dim_account_map m
  WHERE m.account_code IS NULL AND m.company_id IS NULL AND m.is_aggregated = FALSE
  UNION ALL
  SELECT pw.pcode, p.account_id, p.parent_id, pw.depth + 1
  FROM pwalk pw JOIN dim_account_map p ON pw.parent_id = p.account_id WHERE pw.depth < 12
),
acc_aaro AS (  -- NÄRMASTE aaro-grupp-nod ovanför lövet (depth ASC)
  SELECT company_id, account_code, aaro_id FROM (
    SELECT DISTINCT ON (company_id, account_code) company_id, account_code, cur_id AS aaro_id
    FROM walk WHERE cur_id IN ({id_list})
    ORDER BY company_id, account_code, depth ASC
  ) per_company
  UNION ALL
  -- P-koder: company_id = NULL → matchas på account_code oavsett bolag.
  SELECT NULL::int AS company_id, pcode AS account_code, aaro_id FROM (
    SELECT DISTINCT ON (pcode) pcode, cur_id AS aaro_id
    FROM pwalk WHERE cur_id IN ({id_list})
    ORDER BY pcode, depth ASC
  ) pcode_aaro
),
fb_raw AS (
  -- Rå belopp utan teckenflip (P-kods-flippen görs villkorat i adj_mvmt nedan).
  SELECT fb.company_id, fb.period, fb.account_code, fb.source_kind, c.currency, fb.amount
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
  FROM fb_raw WHERE source_kind IN ({src_base})
  GROUP BY company_id, period
),
co_conv AS (  -- är bas-källan SIE-konvention? styr villkorlig P-kods-flip
  SELECT company_id,
         bool_or(base_src IN ('SIE','SIE_VER','SIE_PSALDO','SAFT','SAFT_VER')) AS is_sie
  FROM base_pick GROUP BY company_id
),
picked AS (  -- vald källas värde per (bolag,period,konto); SUM krävs (dim-splittar)
  SELECT bp.company_id, bp.period, bp.base_src, fb.account_code, fb.currency,
         SUM(fb.amount) AS val
  FROM base_pick bp
  JOIN fb_raw fb ON fb.company_id = bp.company_id
    AND fb.source_kind = bp.base_src AND fb.period = bp.period
  GROUP BY bp.company_id, bp.period, bp.base_src, fb.account_code, fb.currency
),
base_mvmt AS (  -- ytd-källor → bal[m]-LAG per (bolag,konto,ÅR); monthly → val rakt av
  SELECT company_id, account_code, currency, period,
    CASE WHEN base_src IN ('SIE','SIE_VER','SAFT','SAFT_VER')
         THEN val - COALESCE(
            LAG(val) OVER (PARTITION BY company_id, account_code, substring(period,1,4)
                           ORDER BY period), 0)
         ELSE val END AS movement
  FROM picked
),
adj_mvmt AS (  -- MAN/IMP_ADJ redan rörelser; villkorad P-kods-flip (Mercur→SIE) på SIE-bas
  SELECT fb.company_id, fb.account_code, fb.currency, fb.period,
         SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%' AND COALESCE(cc.is_sie, false)
                              THEN -1 ELSE 1 END) AS movement
  FROM fb_raw fb
  LEFT JOIN co_conv cc ON cc.company_id = fb.company_id
  WHERE fb.source_kind IN ('MAN','IMP_ADJ')
  GROUP BY fb.company_id, fb.account_code, fb.currency, fb.period
),
all_mvmt AS (
  SELECT company_id, account_code, currency, period, movement, true AS is_base FROM base_mvmt
  UNION ALL
  SELECT company_id, account_code, currency, period, movement, false AS is_base FROM adj_mvmt
),
targets AS (SELECT * FROM (VALUES {targets_sql}) AS t(target_period)),
base_at_target AS (  -- bas-bidraget kräver bas-rad VID target-månaden (annars inaktuellt YTD)
  SELECT t.target_period, bp.company_id
  FROM targets t JOIN base_pick bp ON bp.period = t.target_period
),
ytd_month AS (
  SELECT t.target_period, m.company_id, m.account_code, m.currency, m.period AS month, m.movement
  FROM targets t
  JOIN all_mvmt m ON m.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period
  WHERE m.is_base = false
     OR EXISTS (SELECT 1 FROM base_at_target b
                WHERE b.target_period = t.target_period AND b.company_id = m.company_id)
)
SELECT json_agg(row_to_json(u))::text AS payload FROM (
  SELECT y.target_period, y.company_id, y.currency, a.aaro_id, y.month,
         SUM(y.movement)::float AS movement_local
  FROM ytd_month y
  JOIN acc_aaro a ON a.account_code = y.account_code
    AND (a.company_id = y.company_id OR a.company_id IS NULL)
  GROUP BY y.target_period, y.company_id, y.currency, a.aaro_id, y.month
) u;
"""


def _wh_totals(rows, rate_of):
    """warehouse_total per (aaro_id, period) i SEK med PER-MÅNADS-FX.

    Summerar movement_local × månadskurs PER bolag, abs() per (bolag, aaro_id,
    period), summerar sedan över bolag — teckenrobust, samma semantik som tidigare
    men med rätt månadskurs i st f en periodkurs.
    """
    per_co: dict[tuple, float] = defaultdict(float)
    for r in rows:
        key = (r["company_id"], r["target_period"], r["aaro_id"])
        per_co[key] += (r["movement_local"] or 0) * rate_of(r["currency"], r["month"])

    out: dict[tuple[str, str], float] = {}
    for (_cid, per, aid), sek in per_co.items():
        out[(aid, per)] = out.get((aid, per), 0.0) + abs(sek)
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


def run(facit_dir, mercur, fx_rates, period="202604"):
    """Parsa Mercur (21) + kör warehouse-query → AARO_DATA-lista.

    fx_rates = dim_exchange_rate-månadskurser (db_io.fetch_all); FX per månad.
    """
    rate_of, _ = _make_fx_resolver(fx_rates)
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
    wh = _wh_totals(rows, rate_of)
    return build_aaro_classification(aaro_2026, aaro_2025, wh, per["cur"], per["prev"])

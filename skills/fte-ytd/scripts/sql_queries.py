"""SQL-templates för fte-ytd-skill. Använd via mcp__finance-warehouse__query_sql.

KRITISKT: Anropa describe_schema FÖRST i varje session — semantiken kan ha ändrats.
"""

# Stora YTD-query: per (bolag, period, top_group) för flera target_periods
YTD_TOPGROUP_QUERY = """
WITH RECURSIVE walk AS (
  SELECT m.company_id, m.account_code, m.account_id AS cur_id, m.parent_id, 0 AS depth
  FROM dim_account_map m
  WHERE m.is_aggregated = FALSE AND m.company_id IS NOT NULL
  UNION ALL
  SELECT w.company_id, w.account_code, p.account_id, p.parent_id, w.depth + 1
  FROM walk w JOIN dim_account_map p ON w.parent_id = p.account_id
  WHERE w.depth < 10
),
acc_topgroup AS (
  SELECT DISTINCT ON (company_id, account_code) company_id, account_code, cur_id AS top_group
  FROM walk
  WHERE cur_id IN ('Total Sales','Total Direct Cost','Personnel','Consultants',
                   'Other External Costs','Premises','Transportation','Depreciation')
  ORDER BY company_id, account_code, depth DESC
),
fb_signed AS (
  SELECT fb.company_id, fb.period, fb.account_code, fb.source_kind,
         fb.amount * CASE WHEN fb.account_code LIKE 'P_%' THEN -1 ELSE 1 END AS amount
  FROM fact_balances fb
  WHERE fb.scenario='A'
    AND fb.source_kind IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','IMP','MAN','IMP_ADJ')
    AND fb.period BETWEEN '{start_period}' AND '{end_period}'
),
base_pick AS (
  SELECT company_id, period,
    CASE
      WHEN bool_or(source_kind='SIE_PSALDO') THEN 'SIE_PSALDO'
      WHEN bool_or(source_kind='SIE_VER') THEN 'SIE_VER'
      WHEN bool_or(source_kind='SIE') THEN 'SIE'
      WHEN bool_or(source_kind='SAFT') THEN 'SAFT'
      WHEN bool_or(source_kind='IMP') THEN 'IMP'
    END AS base_src
  FROM fb_signed
  WHERE source_kind IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','IMP')
  GROUP BY company_id, period
),
targets AS (SELECT * FROM (VALUES {targets}) AS t(target_period)),
base_ytd AS (
  -- KRITISKT: SIE_PSALDO + IMP är monthly (summera), SIE + SIE_VER + SAFT är ytd (ta direkt)
  SELECT t.target_period, bp.company_id, fb.account_code, SUM(fb.amount) AS amount
  FROM targets t
  JOIN base_pick bp ON bp.period = t.target_period
  JOIN fb_signed fb ON fb.company_id = bp.company_id AND fb.source_kind = bp.base_src
  WHERE (bp.base_src IN ('SIE','SIE_VER','SAFT') AND fb.period = t.target_period)
     OR (bp.base_src IN ('SIE_PSALDO','IMP') AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period)
  GROUP BY t.target_period, bp.company_id, fb.account_code
),
adj_ytd AS (
  SELECT t.target_period, fb.company_id, fb.account_code, SUM(fb.amount) AS amount
  FROM targets t
  JOIN fb_signed fb ON fb.source_kind IN ('MAN','IMP_ADJ')
    AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period
  GROUP BY t.target_period, fb.company_id, fb.account_code
),
ytd_combined AS (
  SELECT target_period, company_id, account_code, SUM(amount) AS amount FROM (
    SELECT * FROM base_ytd UNION ALL SELECT * FROM adj_ytd
  ) u GROUP BY target_period, company_id, account_code
)
SELECT json_agg(row_to_json(t))::text AS payload FROM (
  SELECT y.target_period, c.company_id, c.name, c.country, c.currency, c.kind, c.parent_id,
    ag.top_group, ROUND(SUM(y.amount)::numeric, 0)::float AS amount_local
  FROM ytd_combined y
  JOIN acc_topgroup ag ON ag.company_id = y.company_id AND ag.account_code = y.account_code
  JOIN dim_company c ON c.company_id = y.company_id
  GROUP BY y.target_period, c.company_id, c.name, c.country, c.currency, c.kind, c.parent_id, ag.top_group
) t;
"""

PERSONNEL_QUERY = """
WITH snapshots AS (
  SELECT * FROM (VALUES
    (DATE '2025-04-30', 'apr_2025'),
    (DATE '2025-12-31', 'dec_2025'),
    (DATE '2026-04-30', 'apr_2026')
  ) AS s(snap_date, snap_label)
),
fte_at AS (
  SELECT s.snap_label, p.company_id,
    SUM(COALESCE(p.employment_pct, 1.0)) AS fte,
    COUNT(*) AS headcount
  FROM reporting.personnel p
  CROSS JOIN snapshots s
  WHERE p.employed_from <= s.snap_date
    AND (p.employed_to IS NULL OR p.employed_to > s.snap_date)
  GROUP BY s.snap_label, p.company_id
),
hires_2026 AS (SELECT company_id, COUNT(*) AS hires_2026 FROM reporting.personnel
  WHERE employed_from BETWEEN DATE '2026-01-01' AND DATE '2026-04-30' GROUP BY company_id),
leavers_2026 AS (SELECT company_id, COUNT(*) AS leavers_2026 FROM reporting.personnel
  WHERE employed_to BETWEEN DATE '2026-01-01' AND DATE '2026-04-30' GROUP BY company_id),
hires_2025 AS (SELECT company_id, COUNT(*) AS hires_2025_ytd FROM reporting.personnel
  WHERE employed_from BETWEEN DATE '2025-01-01' AND DATE '2025-04-30' GROUP BY company_id),
leavers_2025 AS (SELECT company_id, COUNT(*) AS leavers_2025_ytd FROM reporting.personnel
  WHERE employed_to BETWEEN DATE '2025-01-01' AND DATE '2025-04-30' GROUP BY company_id),
companies AS (SELECT DISTINCT company_id FROM reporting.personnel)
SELECT json_agg(row_to_json(t))::text AS payload FROM (
  SELECT c.company_id, dc.name, dc.country,
    MAX(CASE WHEN f.snap_label='apr_2025' THEN f.fte END) AS fte_apr_2025,
    MAX(CASE WHEN f.snap_label='dec_2025' THEN f.fte END) AS fte_dec_2025,
    MAX(CASE WHEN f.snap_label='apr_2026' THEN f.fte END) AS fte_apr_2026,
    MAX(CASE WHEN f.snap_label='apr_2025' THEN f.headcount END) AS hc_apr_2025,
    MAX(CASE WHEN f.snap_label='dec_2025' THEN f.headcount END) AS hc_dec_2025,
    MAX(CASE WHEN f.snap_label='apr_2026' THEN f.headcount END) AS hc_apr_2026,
    h26.hires_2026, l26.leavers_2026, h25.hires_2025_ytd, l25.leavers_2025_ytd
  FROM companies c
  JOIN dim_company dc USING (company_id)
  LEFT JOIN fte_at f USING (company_id)
  LEFT JOIN hires_2026 h26 USING (company_id)
  LEFT JOIN leavers_2026 l26 USING (company_id)
  LEFT JOIN hires_2025 h25 USING (company_id)
  LEFT JOIN leavers_2025 l25 USING (company_id)
  GROUP BY c.company_id, dc.name, dc.country, h26.hires_2026, l26.leavers_2026, h25.hires_2025_ytd, l25.leavers_2025_ytd
) t;
"""

# NO_YTD_2025_SYNTH_QUERY borttagen i v1.4: fact_journal_saft är bara ~6% inläst
# för 2025 → syntes fabricerade siffror (~1% av facit). Se pitfall #11. Bolag utan
# månadsvis SAFT 2025 hanteras via helårsproxy (deras 202512 finns redan i
# YTD_TOPGROUP_QUERY) och flaggas FULL_YEAR_PROXY_2025.

# v1.5: detektera full_year_only-mängden DYNAMISKT (ersätter v1.4:s hårdkodade
# lista). Returnerar JSON-array av company_ids; skicka in som `full_year_only_cids`
# till build_dashboard_data. Avviker resultatet mot tidigare → någon SAFT har
# laddats om; ingen kodändring behövs (poängen med dynamisk detektion).
FULL_YEAR_ONLY_DETECT_QUERY = """
WITH saft_periods_2025 AS (
  SELECT company_id,
         COUNT(DISTINCT period) AS n_periods,
         BOOL_OR(period = '202512') AS has_yearend
  FROM fact_balances
  WHERE source_kind = 'SAFT' AND scenario = 'A'
    AND period BETWEEN '202501' AND '202512'
  GROUP BY company_id
)
SELECT json_agg(company_id ORDER BY company_id)::text AS payload
FROM saft_periods_2025
WHERE n_periods = 1 AND has_yearend;
"""

DIM_COMPANY_QUERY = """
SELECT json_agg(json_build_object(
  'company_id', company_id, 'name', name, 'country', country,
  'kind', kind, 'currency', currency, 'parent_id', parent_id
))::text AS payload FROM dim_company;
"""


def render_query(template, **kwargs):
    """Hjälpfunktion för att fylla i mallar med target_periods etc.
    
    Exempel:
        sql = render_query(YTD_TOPGROUP_QUERY,
                            start_period='202501', end_period='202612',
                            targets="('202504'),('202604'),('202512')")
    """
    return template.format(**kwargs)

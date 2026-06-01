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
-- Bolagsagnostiska (DELADE) trädnoder: account_id satt, account_code/company_id =
-- NULL. Omfattar Mercurs P-koder (P_30 …) men även '_' och 'BUDG' (→ Other External
-- Costs). Den vanliga `walk` seedar bara company_id IS NOT NULL och matchar
-- account_code, så delade noder mappas aldrig → varje MAN/IMP_ADJ bokad på en delad
-- kod droppas tyst av slutjoinen. Walka upp dem separat. Speglar report_pnl.sql:177
-- (account_id = account_code, account_code/company_id NULL).
pwalk AS (
  SELECT m.account_id AS pcode, m.account_id AS cur_id, m.parent_id, 0 AS depth
  FROM dim_account_map m
  WHERE m.account_code IS NULL AND m.company_id IS NULL AND m.is_aggregated = FALSE
  UNION ALL
  SELECT pw.pcode, p.account_id, p.parent_id, pw.depth + 1
  FROM pwalk pw JOIN dim_account_map p ON pw.parent_id = p.account_id
  WHERE pw.depth < 12
),
acc_topgroup AS (
  SELECT company_id, account_code, top_group FROM (
    SELECT DISTINCT ON (company_id, account_code) company_id, account_code, cur_id AS top_group
    FROM walk
    WHERE cur_id IN ('Total Sales','Total Direct Cost','Personnel','Consultants',
                     'Other External Costs','Premises','Transportation','Depreciation')
    ORDER BY company_id, account_code, depth DESC
  ) per_company
  UNION ALL
  -- P-koder: company_id = NULL → matchas på account_code oavsett bolag i slutjoinen.
  SELECT NULL::int AS company_id, pcode AS account_code, top_group FROM (
    SELECT DISTINCT ON (pcode) pcode, cur_id AS top_group
    FROM pwalk
    WHERE cur_id IN ('Total Sales','Total Direct Cost','Personnel','Consultants',
                     'Other External Costs','Premises','Transportation','Depreciation')
    ORDER BY pcode, depth DESC
  ) pcode_tg
),
fb_raw AS (
  -- Rå belopp utan teckenflip. P-kods-flippen görs i adj_ytd, villkorad på
  -- bas-källans konvention (se kommentar där) — INTE ovillkorligt här, eftersom
  -- IMP-bas (FI/DK/DE) lagrar intäkt positiv (Mercur-konv) medan SIE/SAFT lagrar
  -- intäkt negativ (SIE-konv). P-koder finns bara på MAN (verifierat), så base_ytd
  -- påverkas inte av att flippen flyttats.
  SELECT fb.company_id, fb.period, fb.account_code, fb.source_kind, fb.amount
  FROM fact_balances fb
  WHERE fb.scenario='A'
    AND fb.source_kind IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','SAFT_VER','IMP','MAN','IMP_ADJ')
    AND fb.period BETWEEN '{start_period}' AND '{end_period}'
),
base_pick AS (
  SELECT company_id, period,
    CASE
      WHEN bool_or(source_kind='SIE_PSALDO') THEN 'SIE_PSALDO'
      WHEN bool_or(source_kind='SIE_VER') THEN 'SIE_VER'
      WHEN bool_or(source_kind='SIE') THEN 'SIE'
      WHEN bool_or(source_kind='SAFT') THEN 'SAFT'
      WHEN bool_or(source_kind='SAFT_VER') THEN 'SAFT_VER'
      WHEN bool_or(source_kind='IMP') THEN 'IMP'
    END AS base_src
  FROM fb_raw
  WHERE source_kind IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','SAFT_VER','IMP')
  GROUP BY company_id, period
),
targets AS (SELECT * FROM (VALUES {targets}) AS t(target_period)),
base_ytd AS (
  -- KRITISKT: SIE_PSALDO + IMP är monthly (summera), SIE + SIE_VER + SAFT är ytd (ta direkt)
  SELECT t.target_period, bp.company_id, fb.account_code, SUM(fb.amount) AS amount
  FROM targets t
  JOIN base_pick bp ON bp.period = t.target_period
  JOIN fb_raw fb ON fb.company_id = bp.company_id AND fb.source_kind = bp.base_src
  WHERE (bp.base_src IN ('SIE','SIE_VER','SAFT','SAFT_VER') AND fb.period = t.target_period)
     OR (bp.base_src IN ('SIE_PSALDO','IMP') AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period)
  GROUP BY t.target_period, bp.company_id, fb.account_code
),
adj_ytd AS (
  -- P-koder lagras i Mercur-konvention (intäkt positiv). Flippa till SIE-konvention
  -- BARA när bolagets bas-källa är SIE/SAFT (SE/NO/CA) — då matchar P-koden den
  -- SIE-konv basen. För IMP-bas (FI/DK/DE) är basen redan Mercur-konv → ingen flip,
  -- annars adderas en säljSÄNKANDE P-kods-MAN med fel tecken och dubblar felet
  -- (Arvolukko 134 2025: 28,7M i st f rätt 17,8M). base_src=NULL → ingen flip.
  -- Speglar report_pnl.sql:s villkorade flip (där via best_source.is_sie).
  SELECT t.target_period, fb.company_id, fb.account_code,
         SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%'
                               AND bp.base_src IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','SAFT_VER')
                              THEN -1 ELSE 1 END) AS amount
  FROM targets t
  JOIN fb_raw fb ON fb.source_kind IN ('MAN','IMP_ADJ')
    AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period
  LEFT JOIN base_pick bp ON bp.company_id = fb.company_id AND bp.period = t.target_period
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
  -- Vanliga konton matchas på (company_id, account_code); delade koder (company_id =
  -- NULL i acc_topgroup) matchas på account_code oavsett bolag. Delade topgroup-koder
  -- (P_*, '_', BUDG) är icke-numeriska och kolliderar aldrig med per-bolags numeriska
  -- kontonummer → ingen dubbelmatch.
  JOIN acc_topgroup ag ON ag.account_code = y.account_code
    AND (ag.company_id = y.company_id OR ag.company_id IS NULL)
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
# v1.6: exkludera bolag som har SAFT_VER-syntes (synthesize_saft_ver.py) — de har
# en riktig interim-YTD-baslinje (jan..nov ur journalen) och ska INTE behandlas
# som full_year_only/proxy. De får i stället normal YoY mot 202504. SAFT_VER är
# redan inkopplat i base_pick (under SAFT). Bolag utan SAFT_VER förblir proxy.
FULL_YEAR_ONLY_DETECT_QUERY = """
WITH saft_periods_2025 AS (
  SELECT company_id,
         COUNT(DISTINCT period) AS n_periods,
         BOOL_OR(period = '202512') AS has_yearend
  FROM fact_balances
  WHERE source_kind = 'SAFT' AND scenario = 'A'
    AND period BETWEEN '202501' AND '202512'
  GROUP BY company_id
),
has_saft_ver AS (
  SELECT DISTINCT company_id
  FROM fact_balances
  WHERE source_kind = 'SAFT_VER' AND scenario = 'A'
    AND period BETWEEN '202501' AND '202511'
)
SELECT json_agg(company_id ORDER BY company_id)::text AS payload
FROM saft_periods_2025 s
WHERE s.n_periods = 1 AND s.has_yearend
  AND s.company_id NOT IN (SELECT company_id FROM has_saft_ver);
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

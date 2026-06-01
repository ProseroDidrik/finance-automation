-- detect_revenue_ytd.sql
-- KORREKT YTD-omsättning per bolag — ersätter detektions-queryn som gav 1469.6.
--
-- BAKGRUND / FÄLLAN
--   SIE_PSALDO är MÅNADSRÖRELSE (period_type='monthly'), inte YTD.
--   Att summera SIE_PSALDO.amount på EN period ger en månads rörelse (~1/N av YTD)
--   och får varje SE-bolag att se ~65 % för lågt ut. Den buggen producerade
--   "1469.6 MSEK / 119 MSEK saknad omsättning". PSALDO ska KUMULERAS jan→period.
--
-- KORREKT NORMALISERING (samma som report_pnl.sql):
--   period_type='monthly'  → SUM(amount) över jan..period   (kumulera)
--   period_type='ytd'      → amount vid den valda perioden   (ta sista)
--   best_source per land + scenario='A' + additiva lager (MAN/IMP_ADJ).
--
-- Sätt period/year_start i params-CTE:n. year_start = kalenderårets januari
-- (YYYY01) — ALDRIG = period. Kör read-only (mcp_readonly funkar).

WITH params AS (
  SELECT '202604'::text AS period,     -- vald YTD-period
         '202601'::text AS ystart      -- kalenderårets start (YYYY01)
),

-- Bästa bas-källa per (bolag, period), prioritet per land.
best_source AS (
  SELECT fb.company_id, fb.period,
    CASE c.country
      WHEN 'Sweden' THEN CASE
        WHEN bool_or(fb.source_kind = 'SIE_PSALDO') THEN 'SIE_PSALDO'
        WHEN bool_or(fb.source_kind = 'SIE_VER')    THEN 'SIE_VER'
        WHEN bool_or(fb.source_kind = 'SIE')        THEN 'SIE'
        WHEN bool_or(fb.source_kind = 'IMP')        THEN 'IMP' END
      WHEN 'Norway' THEN CASE
        WHEN bool_or(fb.source_kind = 'SAFT')       THEN 'SAFT'
        WHEN bool_or(fb.source_kind = 'SIE_PSALDO') THEN 'SIE_PSALDO'
        WHEN bool_or(fb.source_kind = 'SIE')        THEN 'SIE'
        WHEN bool_or(fb.source_kind = 'IMP')        THEN 'IMP' END
      WHEN 'CA' THEN CASE
        WHEN bool_or(fb.source_kind = 'SIE_PSALDO') THEN 'SIE_PSALDO'
        WHEN bool_or(fb.source_kind = 'SIE_VER')    THEN 'SIE_VER'
        WHEN bool_or(fb.source_kind = 'SIE')        THEN 'SIE'
        WHEN bool_or(fb.source_kind = 'IMP')        THEN 'IMP' END
      ELSE  -- Finland, Denmark, Germany, CENTR
        CASE WHEN bool_or(fb.source_kind = 'IMP')   THEN 'IMP' END
    END AS src
  FROM fact_balances fb
  JOIN dim_company c ON c.company_id = fb.company_id
  CROSS JOIN params p
  WHERE fb.period BETWEEN p.ystart AND p.period
    AND fb.scenario = 'A'
  GROUP BY fb.company_id, fb.period, c.country
),

-- Rader som ingår: vald bas-källa ELLER additiva lager (MAN/IMP_ADJ).
-- Filter på intäktskonton (klass 3 = nettoomsättning + övriga rörelseintäkter).
-- OBS: detta är en omsättnings-PROXY på BAS-klass 3. Den exakta KPI:n
-- "Total Sales" mappas via webapp/backend/pnl_kpis.yaml — använd den för
-- den officiella koncerntotalen.
sel AS (
  SELECT fb.company_id, fb.period, fb.period_type, fb.account_code, fb.amount
  FROM fact_balances fb
  JOIN best_source bs
    ON bs.company_id = fb.company_id
   AND bs.period     = fb.period
   AND (fb.source_kind = bs.src OR fb.source_kind IN ('MAN', 'IMP_ADJ'))
  CROSS JOIN params p
  WHERE fb.scenario = 'A'
    AND fb.period BETWEEN p.ystart AND p.period
    AND fb.account_code LIKE '3%'
),

-- YTD per bolag: kumulera monthly, ta ytd vid vald period.
rev AS (
  SELECT company_id,
    COALESCE(SUM(amount) FILTER (WHERE period_type = 'monthly'), 0)
  + COALESCE(SUM(amount) FILTER (WHERE period_type = 'ytd'
             AND period = (SELECT period FROM params)), 0) AS rev_signed
  FROM sel
  GROUP BY company_id
),

-- Oberoende referens: SIE #RES-YTD vid perioden (för drift-detektion).
sie_ref AS (
  SELECT fb.company_id, SUM(fb.amount) AS sie_ytd_signed
  FROM fact_balances fb
  CROSS JOIN params p
  WHERE fb.source_kind = 'SIE' AND fb.period_type = 'ytd'
    AND fb.period = p.period AND fb.scenario = 'A'
    AND fb.account_code LIKE '3%'
  GROUP BY fb.company_id
)

SELECT
  r.company_id,
  c.name,
  c.country,
  round((-r.rev_signed)::numeric / 1e6, 2)            AS rev_ytd_msek,        -- korrekt YTD (presenterat +)
  round((-s.sie_ytd_signed)::numeric / 1e6, 2)        AS sie_ytd_ref_msek,    -- oberoende referens (SE)
  CASE WHEN s.sie_ytd_signed IS NULL OR s.sie_ytd_signed = 0 THEN NULL
       ELSE round((r.rev_signed / s.sie_ytd_signed)::numeric, 3) END AS ratio,
  CASE WHEN s.sie_ytd_signed IS NOT NULL AND s.sie_ytd_signed <> 0
        AND abs(r.rev_signed / s.sie_ytd_signed - 1) > 0.03
       THEN 'DRIFT >3% — utred (Σ#PSALDO≠#RES e.dyl.)' ELSE 'ok' END AS flag
FROM rev r
JOIN dim_company c ON c.company_id = r.company_id
LEFT JOIN sie_ref s ON s.company_id = r.company_id
ORDER BY rev_ytd_msek DESC NULLS LAST;

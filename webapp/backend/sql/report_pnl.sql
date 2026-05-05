-- report_pnl.sql
-- Returnerar P&L-trädet för (company_id, period) som en flat tabell.
-- En rad per nod i P&L-hierarkin: aggregerade noder (storgrupp/grupp/gruppkonto)
-- + bolagskonto-leaves (bara för det valda bolaget).
--
-- Amounts:
--   amount_month — månadens belopp (SE/NO: YTD - prev_month_YTD; INL: rådata)
--   amount_ytd   — YTD-belopp t.o.m. perioden
-- Sign-konvention: SIE-rå (revenue negativ, expense positiv).
-- Presentation/sign-flip görs i frontend via pnl_kpis.yaml.
--
-- KÄND BEGRÄNSNING: YTD-konvertering antar kalenderår (jan-dec). Bolag med
-- räkenskapsår som inte börjar i januari (t.ex. sept-aug) ger fel YTD-värden.
-- Alla nuvarande data följer kalenderår; dokumenterat för framtida laddningar.
--
-- Parametrar (?-bind i ordning):
--   1: company_id (INT)
--   2: period (TEXT)
--   3: year_start (TEXT, 'YYYY01')
--   4: prev_period (TEXT)  — för LEFT JOIN (SE/NO YTD-subtraktion)
--   5: period (TEXT)        — för WHERE i balances

WITH RECURSIVE
-- 1. Aggregerade noder i P&L-trädet.
pnl_tree(account_id, parent_id, label_sv, label_en, depth, sort_path) AS (
  SELECT account_id, parent_id, description, description_en, 0, account_id
  FROM dim_account_map WHERE account_id = 'P&L'
  UNION ALL
  SELECT m.account_id, m.parent_id, m.description, m.description_en,
         t.depth + 1, t.sort_path || '/' || m.account_id
  FROM dim_account_map m
  JOIN pnl_tree t ON m.parent_id = t.account_id
  WHERE m.is_aggregated = TRUE
),

-- 2. Hämta perioder från årsstart t.o.m. valt period (för INL-kumulering)
--    + minst förra månaden (för SE/NO YTD-diff).
raw_balances AS (
  SELECT fb.company_id, fb.period, fb.account_code, fb.account_name,
         fb.period_type, fb.amount
  FROM fact_balances fb
  JOIN dim_company c ON c.company_id = fb.company_id
  WHERE fb.company_id = ?
    AND fb.period BETWEEN ? AND ?      -- year_start..period
    AND fb.source_kind = CASE c.country
        WHEN 'Sweden'  THEN 'SIE'
        WHEN 'Norway'  THEN 'SAFT'
        WHEN 'Finland' THEN 'INL'
        WHEN 'Denmark' THEN 'INL'
        WHEN 'Germany' THEN 'INL'
        WHEN 'CENTR'   THEN 'INL'
        WHEN 'CA'      THEN 'SIE'
      END
),

-- 3. För INL-bolag: kumulativ YTD = SUM över alla månader sedan årsstart.
--    För SE/NO (YTD-format): YTD = nuvarande periodens amount direkt.
balances AS (
  SELECT
    cur.company_id, cur.account_code, cur.account_name,
    -- amount_month
    CASE
      WHEN cur.period_type = 'monthly' THEN cur.amount
      WHEN cur.period_type = 'ytd'     THEN cur.amount - COALESCE(prev.amount, 0)
    END AS amount_month,
    -- amount_ytd
    CASE
      WHEN cur.period_type = 'monthly' THEN inl_ytd.s
      WHEN cur.period_type = 'ytd'     THEN cur.amount
    END AS amount_ytd
  FROM raw_balances cur
  -- Föregående månads YTD (för SE/NO)
  LEFT JOIN raw_balances prev
    ON prev.company_id   = cur.company_id
   AND prev.account_code = cur.account_code
   AND prev.period       = ?           -- prev_period
   AND prev.period_type  = 'ytd'
  -- Kumulativ summa jan..valt period (för INL)
  LEFT JOIN (
    SELECT company_id, account_code,
           SUM(amount) AS s
    FROM raw_balances
    WHERE period_type = 'monthly'
    GROUP BY company_id, account_code
  ) inl_ytd
    ON inl_ytd.company_id   = cur.company_id
   AND inl_ytd.account_code = cur.account_code
  WHERE cur.period = ?                  -- bara valt period, inte årets alla månader
),

-- 4. Bolagskonton som mappar in i P&L-trädet via parent_id.
leaf_amounts AS (
  SELECT
    m.account_id   AS leaf_node_id,
    m.parent_id    AS group_node_id,
    m.account_code,
    b.account_name AS leaf_label,
    b.amount_month, b.amount_ytd
  FROM balances b
  JOIN dim_account_map m
    ON m.company_id = b.company_id AND m.account_code = b.account_code
  JOIN pnl_tree t ON t.account_id = m.parent_id
),

-- 5. Walka uppåt från varje leaf, en rad per (leaf, ancestor) — för rollup.
ancestor_walk(leaf_node_id, ancestor_id, amount_month, amount_ytd) AS (
  SELECT leaf_node_id, group_node_id, amount_month, amount_ytd
  FROM leaf_amounts
  UNION ALL
  SELECT a.leaf_node_id, m.parent_id, a.amount_month, a.amount_ytd
  FROM ancestor_walk a
  JOIN dim_account_map m ON m.account_id = a.ancestor_id
  WHERE m.parent_id IS NOT NULL
),

-- 6. Summa per ancestor → amount för varje aggregerad nod.
agg_sums AS (
  SELECT ancestor_id,
         SUM(amount_month) AS amount_month,
         SUM(amount_ytd)   AS amount_ytd
  FROM ancestor_walk
  GROUP BY ancestor_id
)

-- 7a. Aggregerade rader (storgrupp / grupp / gruppkonto)
SELECT
  t.account_id,
  t.parent_id,
  t.label_sv,
  t.label_en,
  TRUE AS is_aggregated,
  t.depth,
  CAST(NULL AS VARCHAR) AS account_code,
  CAST(NULL AS VARCHAR) AS leaf_label,
  agg.amount_month,
  agg.amount_ytd,
  t.sort_path
FROM pnl_tree t
LEFT JOIN agg_sums agg ON agg.ancestor_id = t.account_id
WHERE t.account_id != 'P&L'

UNION ALL

-- 7b. Bolagskonto-leaves (för valt bolag)
SELECT
  m.account_id,
  m.parent_id,
  m.description AS label_sv,
  m.description_en AS label_en,
  FALSE AS is_aggregated,
  t.depth + 1 AS depth,
  m.account_code,
  l.leaf_label,
  l.amount_month,
  l.amount_ytd,
  t.sort_path || '/' || m.account_id AS sort_path
FROM leaf_amounts l
JOIN dim_account_map m ON m.account_id = l.leaf_node_id
JOIN pnl_tree t ON t.account_id = m.parent_id

ORDER BY sort_path

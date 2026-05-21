-- report_pivot.sql
-- Returnerar P&L-trädet för (många bolag × många bucket-perioder) i ett anrop.
-- Avsedd för pivot-vy: rader = konto-tree, kolumner = period-buckets, en cell-summa
-- per (bolag, account_id, bucket_key).
--
-- Tokenen <bucket-values> i bucket_spec-CTE:n ersätts av Python till en
-- VALUES-lista (3 placeholder per bucket) innan SQL:en skickas till psycopg.
--
-- Bind-parametrar (i ordning, EFTER token-substitutionen):
--   - alla bucket-värden (3 per bucket: key, start_period, end_period)
--   - company_ids                  : INTEGER[]
--   - scenario                     : TEXT ('A' eller 'B')
--   - report_currency              : TEXT ('SEK' eller 'LOCAL') — andra → LOCAL
--   - source_kind override         : TEXT eller NULL (auto via prio per land)

WITH RECURSIVE
-- Bucket-spec injiceras av Python (VALUES per bucket):
bucket_spec(bucket_key, start_period, end_period) AS ({bucket_values}),

-- Lista över bolag att rapportera:
company_filter AS (SELECT UNNEST(%s::INTEGER[]) AS company_id),

-- Alla månader vi behöver hämta (täckning av alla bucket-intervall):
months_needed AS (
    SELECT DISTINCT p.period
    FROM dim_period p
    JOIN bucket_spec b
      ON p.period BETWEEN b.start_period AND b.end_period
),

-- Föregående månad för varje "needed" — för YTD-diff (SE/NO).
months_with_prev AS (
    SELECT period FROM months_needed
    UNION
    SELECT
        CASE
            WHEN CAST(SUBSTRING(period, 5, 2) AS INTEGER) = 1
                THEN CAST(CAST(SUBSTRING(period, 1, 4) AS INTEGER) - 1 AS VARCHAR) || '12'
            ELSE
                SUBSTRING(period, 1, 4)
                || LPAD(CAST(CAST(SUBSTRING(period, 5, 2) AS INTEGER) - 1 AS VARCHAR), 2, '0')
        END
    FROM months_needed
),

-- 1. P&L-trädet (samma rekursion som report_pnl.sql).
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

-- 2. Bästa source_kind per (bolag, period) — landsspecifik prioritet.
best_source AS (
    SELECT
        fb.company_id,
        fb.period,
        COALESCE(%s, CASE c.country
            WHEN 'Sweden' THEN
                CASE
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_VER'    THEN 1 ELSE 0 END) = 1 THEN 'SIE_VER'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
                    ELSE NULL
                END
            WHEN 'Norway' THEN
                CASE
                    WHEN MAX(CASE WHEN fb.source_kind = 'SAFT'    THEN 1 ELSE 0 END) = 1 THEN 'SAFT'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'     THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ' THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
                    ELSE NULL
                END
            WHEN 'CA' THEN
                CASE
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_VER'    THEN 1 ELSE 0 END) = 1 THEN 'SIE_VER'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
                    ELSE NULL
                END
            ELSE  -- Finland, Denmark, Germany, CENTR
                CASE
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'     THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ' THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
                    ELSE NULL
                END
        END) AS source_kind
    FROM fact_balances fb
    JOIN dim_company c ON c.company_id = fb.company_id
    JOIN company_filter cf ON cf.company_id = fb.company_id
    JOIN months_with_prev mw ON mw.period = fb.period
    GROUP BY fb.company_id, fb.period, c.country
),

-- 3. Råa balances för valt scenario, summerade per (bolag, period, konto).
--    P-koder normaliseras till SIE-konvention (negat).
raw_balances AS (
    SELECT
        fb.company_id, fb.period, fb.account_code,
        MAX(fb.account_name) AS account_name,
        MAX(fb.period_type)  AS period_type,
        SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%%' THEN -1 ELSE 1 END) AS amount
    FROM fact_balances fb
    JOIN best_source bs
        ON bs.company_id  = fb.company_id
       AND bs.period      = fb.period
       AND bs.source_kind = fb.source_kind
    JOIN months_with_prev mw ON mw.period = fb.period
    WHERE fb.scenario = %s
    GROUP BY fb.company_id, fb.period, fb.account_code
),

-- 4. Månadsbelopp: monthly→direkt; ytd→ diff mot föregående månad inom samma år.
--    Vid årsskifte (januari) sätts prev=NULL → amount_month = ytd(januari) direkt,
--    eftersom YTD-data antas återställas vid kalenderårsstart (samma antagande
--    som befintlig report_pnl.sql).
month_amounts AS (
    SELECT
        cur.company_id, cur.period, cur.account_code, cur.account_name,
        CASE
            WHEN cur.period_type = 'monthly' THEN cur.amount
            WHEN cur.period_type = 'ytd'     THEN cur.amount - COALESCE(prev.amount, 0)
        END AS amount_month
    FROM raw_balances cur
    LEFT JOIN raw_balances prev
        ON prev.company_id   = cur.company_id
       AND prev.account_code = cur.account_code
       AND prev.period_type  = 'ytd'
       AND SUBSTRING(prev.period, 1, 4) = SUBSTRING(cur.period, 1, 4)  -- samma kalenderår
       AND prev.period       = (
            CASE
                WHEN CAST(SUBSTRING(cur.period, 5, 2) AS INTEGER) = 1
                    THEN CAST(CAST(SUBSTRING(cur.period, 1, 4) AS INTEGER) - 1 AS VARCHAR) || '12'
                ELSE
                    SUBSTRING(cur.period, 1, 4)
                    || LPAD(CAST(CAST(SUBSTRING(cur.period, 5, 2) AS INTEGER) - 1 AS VARCHAR), 2, '0')
            END
       )
    JOIN months_needed mn ON mn.period = cur.period
),

-- 5. Valutakonvertering. report_currency='SEK' → multiplicera med FX-rate (avg);
--    'LOCAL' (eller okänt) → behåll lokalt belopp.
month_amounts_fx AS (
    SELECT
        m.company_id, m.period, m.account_code, m.account_name,
        m.amount_month * (
            CASE
                WHEN %s = 'SEK' AND c.currency != 'SEK' THEN COALESCE(fx.rate, NULL)
                ELSE 1.0
            END
        ) AS amount_reported
    FROM month_amounts m
    JOIN dim_company c ON c.company_id = m.company_id
    LEFT JOIN dim_exchange_rate fx
        ON fx.period    = m.period
       AND fx.currency  = c.currency
       AND fx.rate_type = 'avg'
),

-- 6. Summa till bucket-nivå.
bucket_amounts AS (
    SELECT
        m.company_id,
        m.account_code,
        MAX(m.account_name) AS account_name,
        b.bucket_key,
        SUM(m.amount_reported) AS amount
    FROM month_amounts_fx m
    JOIN bucket_spec b ON m.period BETWEEN b.start_period AND b.end_period
    GROUP BY m.company_id, m.account_code, b.bucket_key
),

-- 7. Mappa bolagskonton till leaf-noder i pnl_tree.
leaf_amounts AS (
    SELECT
        m.account_id   AS leaf_node_id,
        m.parent_id    AS group_node_id,
        m.account_code,
        ba.account_name AS leaf_label,
        ba.company_id,
        ba.bucket_key,
        ba.amount
    FROM bucket_amounts ba
    JOIN dim_account_map m
        ON  (m.company_id = ba.company_id AND m.account_code = ba.account_code)
         OR (m.account_id = ba.account_code AND m.account_code IS NULL AND m.company_id IS NULL)
    JOIN pnl_tree t ON t.account_id = m.parent_id
),

-- 8. Walk uppåt från varje leaf till alla ancestors.
ancestor_walk(company_id, bucket_key, leaf_node_id, ancestor_id, amount) AS (
    SELECT company_id, bucket_key, leaf_node_id, group_node_id, amount
    FROM leaf_amounts
    UNION ALL
    SELECT a.company_id, a.bucket_key, a.leaf_node_id, m.parent_id, a.amount
    FROM ancestor_walk a
    JOIN dim_account_map m ON m.account_id = a.ancestor_id
    WHERE m.parent_id IS NOT NULL
),

-- 9. Aggregat per (bolag, ancestor, bucket).
agg_sums AS (
    SELECT company_id, ancestor_id AS account_id, bucket_key,
           SUM(amount) AS amount
    FROM ancestor_walk
    GROUP BY company_id, ancestor_id, bucket_key
)

-- 10a. Aggregerade noder.
SELECT
    agg.company_id,
    t.account_id,
    t.parent_id,
    t.label_sv,
    t.label_en,
    TRUE AS is_aggregated,
    t.depth,
    CAST(NULL AS VARCHAR) AS account_code,
    CAST(NULL AS VARCHAR) AS leaf_label,
    agg.bucket_key,
    agg.amount,
    t.sort_path
FROM pnl_tree t
JOIN agg_sums agg ON agg.account_id = t.account_id
WHERE t.account_id != 'P&L'

UNION ALL

-- 10b. Bolagskonto-leaves.
SELECT
    l.company_id,
    m.account_id,
    m.parent_id,
    m.description AS label_sv,
    m.description_en AS label_en,
    FALSE AS is_aggregated,
    t.depth + 1 AS depth,
    m.account_code,
    l.leaf_label,
    l.bucket_key,
    l.amount,
    t.sort_path || '/' || m.account_id AS sort_path
FROM leaf_amounts l
JOIN dim_account_map m ON m.account_id = l.leaf_node_id
JOIN pnl_tree t ON t.account_id = m.parent_id

ORDER BY company_id, sort_path, bucket_key;

-- Jämförelse backup_from_mercur vs fact_balances per (bolag, period, källa, scenario).
-- Status: 'missing' = finns i backup, saknas i fact_balances
--         'mismatch' = finns i båda men summor avviker >1%
--         'ok'       = finns i båda och summor matchar
--
-- Källa-normalisering: backup_from_mercur använder Mercurs benämning (IMP = utfall),
-- medan fact_balances märker utfallsdata efter filformat (INL/SIE/SAFT/SIE_PSALDO/IMP).
-- För jämförelse väljer vi en kanonisk utfalls-källa per (bolag, period, scenario) i
-- prio-ordning IMP > INL > SIE > SAFT > SIE_PSALDO och taggar den 'IMP' så att den
-- joinar mot backup. Detta undviker dubbelräkning när både gamla historik-IMP och
-- ny INL/SIE/SAFT finns för samma period.
WITH backup_agg AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)    AS rows,
           SUM(amount) AS total
    FROM backup_from_mercur
    GROUP BY 1, 2, 3, 4
),
fact_actual_grouped AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)    AS rows,
           SUM(amount) AS total,
           CASE source_kind
               WHEN 'IMP'        THEN 1
               WHEN 'INL'        THEN 2
               WHEN 'SIE'        THEN 3
               WHEN 'SAFT'       THEN 4
               WHEN 'SIE_PSALDO' THEN 5
           END AS prio
    FROM fact_balances
    WHERE source_kind IN ('IMP','INL','SIE','SAFT','SIE_PSALDO')
    GROUP BY company_id, period, source_kind, scenario
),
fact_actual_picked AS (
    SELECT company_id, period, scenario, rows, total,
           ROW_NUMBER() OVER (PARTITION BY company_id, period, scenario ORDER BY prio) AS rn
    FROM fact_actual_grouped
),
fact_agg AS (
    SELECT company_id, period, 'IMP' AS source_kind, scenario, rows, total
    FROM fact_actual_picked
    WHERE rn = 1
    UNION ALL
    SELECT company_id, period, source_kind, scenario,
           COUNT(*) AS rows, SUM(amount) AS total
    FROM fact_balances
    WHERE source_kind IN ('MAN', 'IMP_ADJ')
    GROUP BY company_id, period, source_kind, scenario
)
SELECT
    COALESCE(b.company_id,   f.company_id)   AS company_id,
    c.name                                    AS company_name,
    c.country,
    COALESCE(b.period,       f.period)        AS period,
    COALESCE(b.source_kind,  f.source_kind)   AS source_kind,
    COALESCE(b.scenario,     f.scenario)      AS scenario,
    b.rows   AS backup_rows,
    f.rows   AS fact_rows,
    b.total  AS backup_sum,
    f.total  AS fact_sum,
    CASE
        WHEN f.company_id IS NULL THEN 'missing'
        WHEN ABS(COALESCE(b.total, 0) - COALESCE(f.total, 0))
             > 0.01 * NULLIF(ABS(COALESCE(b.total, 0)), 0) THEN 'mismatch'
        ELSE 'ok'
    END AS status
FROM backup_agg b
FULL OUTER JOIN fact_agg f
    ON  b.company_id  = f.company_id
    AND b.period      = f.period
    AND b.source_kind = f.source_kind
    AND b.scenario    = f.scenario
LEFT JOIN dim_company c
    ON COALESCE(b.company_id, f.company_id) = c.company_id
ORDER BY
    COALESCE(b.period, f.period),
    c.country,
    COALESCE(b.company_id, f.company_id)

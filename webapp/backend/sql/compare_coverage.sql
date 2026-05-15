-- Jämförelse Mercur-facit (`backup_from_mercur`) vs `fact_balances` per
-- (bolag, period, källa, scenario).
--
-- Status:
--   'missing'      — facit har rader, fact_balances har inga (riktigt saknad data)
--   'missing_zero' — facit har rader men summan ≈ 0 för SIE/SAFT (Mercur har
--                    pre-allokerat tomma noll-rader för bolag utan månadsbevegelse;
--                    ingen riktig data saknas, bara harmlös pre-allokering)
--   'extra'        — fact_balances har rader, facit har inga (utanför facit-scope)
--   'mismatch'     — båda har rader men beloppen avviker
--                    (>1 procent OCH >1 enhet absolut — det andra villkoret tar bort FP-brus)
--   'ok'           — båda har rader, beloppen matchar
--
-- OBS: för SE-SIE / NO-SAFT är beloppen *inte* jämförbara rakt av eftersom
-- fact_balances lagrar YTD-saldon medan backup_from_mercur (M-rader) lagrar
-- månadsbevegelser. Mismatch-statusen är därför oftast meningslös för SIE/SAFT.
-- För IMP / MAN / IMP_ADJ stämmer båda sidor som monthly och då är beloppen
-- direkt jämförbara.
--
-- Normalisering: fact_balances har SE-data som både SIE (transaktioner) och
-- SIE_PSALDO (periodsaldon från samma fil). Vi väljer SIE om den finns,
-- annars SIE_PSALDO. På så vis matchar backup.SIE direkt mot fact-SIE.
-- Begränsa till utfall (scenario='A') — budget (B) ligger utanför facit.
WITH backup_agg AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)    AS rows,
           SUM(amount) AS total
    FROM backup_from_mercur
    WHERE scenario = 'A'
    GROUP BY 1, 2, 3, 4
),
sie_pick AS (
    SELECT DISTINCT company_id, period, scenario,
           FIRST_VALUE(source_kind) OVER (
               PARTITION BY company_id, period, scenario
               ORDER BY CASE source_kind WHEN 'SIE' THEN 1
                                          WHEN 'SIE_PSALDO' THEN 2 END
           ) AS picked_kind
    FROM fact_balances
    WHERE source_kind IN ('SIE', 'SIE_PSALDO') AND scenario = 'A'
),
fact_sie AS (
    SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
           COUNT(*)::int  AS rows,
           SUM(fb.amount) AS total
    FROM sie_pick p
    JOIN fact_balances fb
      ON fb.company_id  = p.company_id
     AND fb.period      = p.period
     AND fb.scenario    = p.scenario
     AND fb.source_kind = p.picked_kind
    GROUP BY fb.company_id, fb.period, fb.scenario
),
fact_other AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)::int AS rows,
           SUM(amount)   AS total
    FROM fact_balances
    WHERE source_kind IN ('IMP', 'SAFT', 'MAN', 'IMP_ADJ') AND scenario = 'A'
    GROUP BY 1, 2, 3, 4
),
fact_agg AS (
    SELECT * FROM fact_sie
    UNION ALL
    SELECT * FROM fact_other
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
        -- Pre-allokerade noll-rader i Mercur-backup för SIE/SAFT — harmlöst.
        -- IMP behåller "missing" trots bk_sum≈0 (IMP balanserar till 0 per design,
        -- så summan särskiljer inte tomma rader från riktigt saknade).
        WHEN f.company_id IS NULL
             AND b.source_kind IN ('SIE', 'SAFT')
             AND ABS(COALESCE(b.total, 0)) < 1
        THEN 'missing_zero'
        WHEN f.company_id IS NULL THEN 'missing'
        WHEN b.company_id IS NULL THEN 'extra'
        -- Belopp-mismatch flaggas bara när monthly↔monthly är jämförbart
        -- (IMP/MAN/IMP_ADJ). SIE/SAFT skiljer per definition (YTD vs M).
        WHEN COALESCE(b.source_kind, f.source_kind) IN ('IMP', 'MAN', 'IMP_ADJ')
             AND ABS(COALESCE(b.total, 0) - COALESCE(f.total, 0)) > 1
             AND ABS(COALESCE(b.total, 0) - COALESCE(f.total, 0))
                 > 0.01 * NULLIF(ABS(COALESCE(b.total, 0)), 0)
        THEN 'mismatch'
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

-- Pivot: per supplier_name × år, summa amount.
-- Parametrar (i ordning):
--   1: country (TEXT)
--   2: company_ids (INTEGER[]) — NULL = alla
--   3: segments (TEXT[]) — NULL = alla, även NULL-segment
--   4: include_uncategorized (BOOLEAN) — TRUE = inkludera rader utan supplier_name
WITH filtered AS (
    SELECT *
    FROM fact_supplier_spend
    WHERE country = %s
      AND period_kind = 'FULL'
      AND (CAST(%s AS INTEGER[]) IS NULL OR company_id IN (SELECT UNNEST(CAST(%s AS INTEGER[]))))
      AND (CAST(%s AS TEXT[])    IS NULL OR segment    IN (SELECT UNNEST(CAST(%s AS TEXT[]))))
      AND (CAST(%s AS BOOLEAN)   = TRUE  OR supplier_name IS NOT NULL)
)
SELECT
    COALESCE(supplier_name, '(okänd)') AS supplier_name,
    year,
    SUM(amount)                          AS amount
FROM filtered
GROUP BY 1, 2
ORDER BY 1, 2;

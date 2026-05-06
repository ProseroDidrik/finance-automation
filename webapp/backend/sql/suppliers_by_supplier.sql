-- Pivot: per supplier_name × år, summa amount.
-- Parametrar (i ordning):
--   ?  country (TEXT)
--   ?  company_ids (INTEGER[]) — NULL = alla
--   ?  segments (TEXT[]) — NULL = alla, även NULL-segment
--   ?  include_uncategorized (BOOLEAN) — TRUE = inkludera rader utan supplier_name
WITH filtered AS (
    SELECT *
    FROM fact_supplier_spend
    WHERE country = ?
      AND period_kind = 'FULL'
      AND (CAST(? AS INTEGER[]) IS NULL OR company_id IN (SELECT UNNEST(CAST(? AS INTEGER[]))))
      AND (CAST(? AS TEXT[])    IS NULL OR segment    IN (SELECT UNNEST(CAST(? AS TEXT[]))))
      AND (CAST(? AS BOOLEAN)   = TRUE  OR supplier_name IS NOT NULL)
)
SELECT
    COALESCE(supplier_name, '(okänd)') AS supplier_name,
    year,
    SUM(amount)                          AS amount
FROM filtered
GROUP BY 1, 2
ORDER BY 1, 2;

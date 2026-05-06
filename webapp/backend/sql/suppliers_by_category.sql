-- Pivot: per kategori × segment × år, summa amount.
-- Samma parameter-signatur som suppliers_by_supplier.sql.
WITH filtered AS (
    SELECT *
    FROM fact_supplier_spend
    WHERE country = ?
      AND period_kind = 'FULL'
      AND (CAST(? AS INTEGER[]) IS NULL OR company_id IN (SELECT UNNEST(CAST(? AS INTEGER[]))))
      AND (CAST(? AS TEXT[])    IS NULL OR segment    IN (SELECT UNNEST(CAST(? AS TEXT[]))))
      AND (CAST(? AS BOOLEAN)   = TRUE  OR kategori IS NOT NULL)
)
SELECT
    COALESCE(kategori, '(okategoriserat)') AS kategori,
    segment,
    year,
    SUM(amount)                              AS amount
FROM filtered
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

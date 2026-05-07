-- Aggregat per (bolag, år) för personnel-fliken.
-- Inputs:
--   $1 = country (e.g. 'Sweden')
--   $2 = years   (LIST of integers, e.g. [2023, 2024, 2025, 2026])
--
-- Output: en rad per (company × year) med UB/Began/Slutat. Frontend pivotar.

WITH years(y) AS (SELECT unnest(%s::INTEGER[]) AS y),
     base AS (
         SELECT *
         FROM fact_personnel
         WHERE country = %s
     )
SELECT
    b.company_id,
    c.name                                                     AS company_name,
    y.y                                                        AS year,
    COUNT(*) FILTER (
        WHERE b.employed_from <= MAKE_DATE(y.y, 12, 31)
          AND (b.employed_to IS NULL OR b.employed_to > MAKE_DATE(y.y, 12, 31))
    )                                                          AS ub,
    COUNT(*) FILTER (WHERE EXTRACT(year FROM b.employed_from) = y.y)  AS began,
    COUNT(*) FILTER (WHERE EXTRACT(year FROM b.employed_to)   = y.y)  AS slutat
FROM base b
CROSS JOIN years y
JOIN dim_company c ON c.company_id = b.company_id
GROUP BY b.company_id, c.name, y.y
ORDER BY c.name, y.y;
